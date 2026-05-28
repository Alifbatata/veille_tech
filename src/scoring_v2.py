# =============================================================================
# scoring_v2.py — Pipeline scoring industriel (pre-ranking + MMR + calibration)
# =============================================================================
#
# Inspire des architectures AlphaSense / Feedly Leo et de la recherche
# LLM-as-a-judge 2025 (Multi-judge, G-Eval, verbalized confidence, MMR).
#
# Composants (TOUS gratuits, locaux, 0 coût API additionnel) :
#
#   1. pre_rank_articles : pre-tri AVANT scoring LLM via BM25.
#      BM25 = standard industriel de la recherche (Lucene, Elasticsearch). Excelle
#      sur vocab technique specialise (PVD, ALD, HiPIMS, magnetron) ou les
#      embeddings neuronaux generiques peinent. 0 dependance lourde (~20 KB).
#      → Fallback TF-IDF (sklearn) si rank_bm25 indispo
#      → Fallback no-op si ni l'un ni l'autre
#      → economise 15-30% des tokens Gemini en filtrant la queue
#
#   2. compute_mmr_ranking : reranking diversifie post-scoring.
#      → Maximal Marginal Relevance (Carbonell & Goldstein 1998, encore SOTA)
#      → utilise TF-IDF (sklearn) pour la similarite inter-articles
#      → evite que 5 articles "metasurfaces" saturent le top de l'email
#
#   3. track_score_distribution : calibration drift inter-runs.
#      → persistance dans data/score_calibration.json
#      → warning si dérive (>50% en 5★ ou <5% en 4-5★)
#
# Aucune dependance dure : si rank_bm25 ou sklearn manquent, le module se
# dégradé proprement (no-op + log) et le pipeline continue.
#
# Note : sentence-transformers est utilisable en option (set EMBEDDINGS_OPTIONAL=true)
# si on veut un boost qualite pour MMR sur sujets generaux. PAS utile pour
# pre-ranking technique (BM25 est meilleur sur vocab specialise).
# =============================================================================

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("scoring_v2")


# =============================================================================
# Configuration via variables d'environnement
# =============================================================================

# Pre-ranking
PRERANK_ENABLED: bool = os.environ.get("PRERANK_ENABLED", "true").lower() in ("true", "1", "yes")
# Fraction minimale du flux a conserver, peu importe les scores. Protection
# contre les seuils trop stricts qui dégommeraient tout sur un batch atypique.
PRERANK_KEEP_TOP_FRACTION: float = float(os.environ.get("PRERANK_KEEP_TOP_FRACTION", "0.80"))
# Seuil BM25 normalise pour conserver un article hors du top-fraction
# (1.0 = score median, 0.0 = aucun match). Conservateur a 0.05.
PRERANK_MIN_BM25_NORMALIZED: float = float(os.environ.get("PRERANK_MIN_BM25_NORMALIZED", "0.05"))

# MMR diversification
MMR_ENABLED: bool = os.environ.get("MMR_ENABLED", "true").lower() in ("true", "1", "yes")
# Lambda MMR : 1.0 = pure relevance (pas de diversite), 0.0 = pure diversite.
MMR_LAMBDA: float = float(os.environ.get("MMR_LAMBDA", "0.7"))
MMR_TOP_K: int = int(os.environ.get("MMR_TOP_K", "60"))

# Calibration drift
CALIBRATION_TRACK_ENABLED: bool = os.environ.get("CALIBRATION_TRACK_ENABLED", "true").lower() in ("true", "1", "yes")
CALIBRATION_DRIFT_HIGH_THRESHOLD: float = float(os.environ.get("CALIBRATION_DRIFT_HIGH_THRESHOLD", "0.50"))
CALIBRATION_DRIFT_LOW_THRESHOLD: float = float(os.environ.get("CALIBRATION_DRIFT_LOW_THRESHOLD", "0.05"))
CALIBRATION_HISTORY_KEEP: int = int(os.environ.get("CALIBRATION_HISTORY_KEEP", "20"))

# Optionnel : utiliser sentence-transformers pour MMR (qualite +, mais 22 MB + torch).
# Par défaut OFF : TF-IDF est largement suffisant pour notre cas.
ST_OPTIONAL_ENABLED: bool = os.environ.get("ST_OPTIONAL_ENABLED", "false").lower() in ("true", "1", "yes")
ST_MODEL_NAME: str = os.environ.get("ST_MODEL_NAME", "all-MiniLM-L6-v2")

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_SCORE_CALIBRATION_PATH = os.path.join(_DATA_DIR, "score_calibration.json")


# =============================================================================
# Tokenization simple (suffisante pour BM25/TF-IDF sur vocab technique)
# =============================================================================

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    """Tokenization lowercase, garde mots alphanumeriques. Preserve les sigles
    techniques courts (PVD, ALD, CVD, MEMS) qui font tout le signal du domaine."""
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# =============================================================================
# Pre-ranker lazy-loadable : BM25 -> TF-IDF -> no-op
# =============================================================================

_ranker_kind: str | None = None  # "bm25" | "tfidf" | "none"


def _detect_ranker_kind() -> str:
    """Detecte la meilleure lib disponible pour le pre-ranking."""
    global _ranker_kind
    if _ranker_kind is not None:
        return _ranker_kind
    try:
        from rank_bm25 import BM25Okapi  # noqa: F401
        _ranker_kind = "bm25"
        logger.info("📊 Pre-ranker pret : BM25 (standard industriel, vocab technique).")
        return _ranker_kind
    except ImportError:
        pass
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: F401
        _ranker_kind = "tfidf"
        logger.info("📊 Pre-ranker pret : TF-IDF (sklearn fallback).")
        return _ranker_kind
    except ImportError:
        logger.warning(
            "⚠️ Ni rank_bm25 ni sklearn dispo. Pre-ranking + MMR desactives. "
            "Pour les activer : pip install rank_bm25 scikit-learn"
        )
        _ranker_kind = "none"
        return _ranker_kind


# =============================================================================
# Profil cible
# =============================================================================

def build_target_profile(
    companies: list[str],
    keywords: list[str],
    solo_keywords: list[str],
    research_orgs: list[str],
    cross_domain_topics: list[str],
) -> str:
    """Concatene les 5 listes en un texte representatif.

    Ordre / pondération implicite via repetition : termes techniques cles repetes
    plusieurs fois pour augmenter leur poids dans TF-IDF/BM25.
    """
    parts: list[str] = [
        # Base thematique repetee pour ancrer le BM25 sur le vocab cle
        "thin film deposition PVD CVD ALD sputtering magnetron HiPIMS coating",
        "physical vapor deposition chemical vapor deposition atomic layer deposition",
        "thin film coating hard coating DLC tribological surface treatment",
    ]
    if keywords:
        parts.append(" ".join(keywords))
    if solo_keywords:
        parts.append(" ".join(solo_keywords))
    if cross_domain_topics:
        parts.append(" ".join(cross_domain_topics))
    if companies:
        parts.append(" ".join(companies))
    if research_orgs:
        parts.append(" ".join(research_orgs))
    return " ".join(parts)


# =============================================================================
# Pre-ranking par BM25 (priorite) ou TF-IDF (fallback)
# =============================================================================

def pre_rank_articles(
    articles: list[dict[str, Any]],
    target_profile_text: str,
) -> tuple[list[dict[str, Any]], int]:
    """Filtre les articles par BM25 (priorite) ou TF-IDF (fallback) vs profil cible.

    Strategie conservatrice : garde TOUJOURS au moins PRERANK_KEEP_TOP_FRACTION
    du flux (80%) — la queue n'est coupee qu'en-dessous du seuil normalise.

    Attache `prerank_similarity` ∈ [0, 1] a chaque article (score normalise
    par le max du batch, reutilise par d'autres modules).

    Returns:
        (kept_articles, n_rejected)
    """
    if not PRERANK_ENABLED or not articles:
        return articles, 0

    kind = _detect_ranker_kind()
    if kind == "none":
        return articles, 0

    article_texts = [
        f"{a.get('title', '')}. {a.get('summary', '')}".strip()
        for a in articles
    ]

    try:
        if kind == "bm25":
            from rank_bm25 import BM25Okapi
            corpus_tokens = [_tokenize(t) for t in article_texts]
            bm25 = BM25Okapi(corpus_tokens)
            query_tokens = _tokenize(target_profile_text)
            raw_scores = bm25.get_scores(query_tokens)
            # BM25Okapi.get_scores retourne un np.ndarray ; on convertit en list
            # de float Python pour homogeneite avec le branch TF-IDF.
            scores = [float(s) for s in raw_scores]
        else:  # tfidf
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            vec = TfidfVectorizer(
                stop_words="english",
                max_features=5000,
                ngram_range=(1, 2),
                sublinear_tf=True,
            )
            all_texts = [target_profile_text] + article_texts
            matrix = vec.fit_transform(all_texts)
            scores = [float(s) for s in cosine_similarity(matrix[0], matrix[1:]).flatten()]
    except (RuntimeError, ValueError, OSError, ImportError) as e:
        logger.warning(f"⚠️ Pre-ranking : echec calcul ({e}). Skip pre-rank.")
        return articles, 0

    # Normaliser pour avoir [0, 1] (utile pour comparer + threshold lisible)
    max_score = max(scores) if scores else 1.0
    norm = max_score if max_score > 0 else 1.0
    normalized = [s / norm for s in scores]

    for art, sim in zip(articles, normalized):
        art["prerank_similarity"] = float(sim)

    # Strategie de filtrage : garde min top_fraction, et seuil sur la queue
    n = len(articles)
    min_keep = max(1, int(n * PRERANK_KEEP_TOP_FRACTION))

    indexed = sorted(enumerate(normalized), key=lambda kv: kv[1], reverse=True)
    kept_indices: set[int] = set(idx for idx, _ in indexed[:min_keep])
    for idx, sim in indexed[min_keep:]:
        if sim >= PRERANK_MIN_BM25_NORMALIZED:
            kept_indices.add(idx)

    kept = [articles[i] for i in range(n) if i in kept_indices]
    rejected = n - len(kept)

    if rejected > 0:
        avg_sim_kept = sum(articles[i].get("prerank_similarity", 0) for i in kept_indices) / max(1, len(kept_indices))
        logger.info(
            f"📊 Pre-rank {kind} : {rejected}/{n} rejete(s) ({100 * rejected / n:.1f}%) "
            f"sous similarite normalisee {PRERANK_MIN_BM25_NORMALIZED:.2f}. "
            f"Moyenne similarite gardée : {avg_sim_kept:.3f}."
        )
    return kept, rejected


# =============================================================================
# MMR — Maximal Marginal Relevance via TF-IDF (ou sentence-transformers optionnel)
# =============================================================================

def compute_mmr_ranking(
    articles: list[dict[str, Any]],
    lambda_param: float | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Re-ordonne les articles par MMR : relevance × diversite.

    Pour la similarite inter-articles :
      - Par defaut : TF-IDF (sklearn). Gratuit, leger, qualite suffisante.
      - Si ST_OPTIONAL_ENABLED=true ET sentence-transformers installe :
        utilise sentence-transformers pour une similarite semantique.

    Strategie : applique MMR aux top-K articles (ceux que l'utilisateur verra).
    Au-dela : ordre par score brut conserve.
    """
    if not MMR_ENABLED or len(articles) <= 1:
        return articles

    lambda_val = lambda_param if lambda_param is not None else MMR_LAMBDA
    k = min(top_k if top_k is not None else MMR_TOP_K, len(articles))

    head = articles[:k]
    tail = articles[k:]
    head_texts = [
        f"{a.get('title', '')}. {a.get('summary', '')}".strip()
        for a in head
    ]

    sim_matrix = _compute_inter_article_similarity(head_texts)
    if sim_matrix is None:
        return articles  # No way to compute similarities → skip MMR

    relevance = [int(a.get("score", 1)) / 5.0 for a in head]

    selected_indices: list[int] = []
    remaining: set[int] = set(range(len(head)))
    while remaining:
        best_idx = -1
        best_mmr = -float("inf")
        for idx in remaining:
            rel = relevance[idx]
            if not selected_indices:
                mmr_score = lambda_val * rel
            else:
                max_sim_selected = max(float(sim_matrix[idx][s]) for s in selected_indices)
                mmr_score = lambda_val * rel - (1 - lambda_val) * max_sim_selected
            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = idx
        if best_idx == -1:
            break
        selected_indices.append(best_idx)
        remaining.remove(best_idx)

    reordered_head = [head[i] for i in selected_indices]
    moved = sum(1 for i, art in enumerate(reordered_head) if head[i] is not art)
    if moved > 0:
        logger.info(
            f"♻️ MMR diversification (lambda={lambda_val:.2f}, top {k}) : "
            f"{moved} article(s) reordonne(s) pour diversite."
        )

    return reordered_head + tail


def _compute_inter_article_similarity(texts: list[str]) -> Any | None:
    """Calcule la matrice de similarite cosine entre les articles.

    Priorite :
      1. sentence-transformers SI ST_OPTIONAL_ENABLED ET install OK
      2. TF-IDF (sklearn)
      3. None (no-op MMR)
    """
    if ST_OPTIONAL_ENABLED:
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
            model = SentenceTransformer(ST_MODEL_NAME)
            embs = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
            normed = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-12)
            return normed @ normed.T
        except ImportError:
            logger.debug("sentence-transformers indispo malgre ST_OPTIONAL_ENABLED, fallback TF-IDF.")
        except (RuntimeError, OSError) as e:
            logger.debug(f"sentence-transformers : {e}, fallback TF-IDF.")

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        vec = TfidfVectorizer(
            stop_words="english",
            max_features=5000,
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        matrix = vec.fit_transform(texts)
        return cosine_similarity(matrix, matrix)
    except (ImportError, ValueError):
        return None


# =============================================================================
# Tracking calibration drift
# =============================================================================

def _compute_distribution(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcule la distribution des scores pour ce run."""
    if not articles:
        return {"n": 0, "by_score": {}, "high_pct": 0.0, "five_pct": 0.0}
    n = len(articles)
    by_score = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for a in articles:
        s = int(a.get("score", 0))
        if 1 <= s <= 5:
            by_score[s] += 1
    high = by_score[4] + by_score[5]
    return {
        "n":        n,
        "by_score": {str(k): v for k, v in by_score.items()},
        "high_pct": high / n if n else 0.0,
        "five_pct": by_score[5] / n if n else 0.0,
    }


def track_score_distribution(retained_articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Persiste la distribution des scores et detecte la dérive inter-runs."""
    if not CALIBRATION_TRACK_ENABLED:
        return {"distribution": {}, "drift_warning": None}

    distribution = _compute_distribution(retained_articles)

    historical = safe_read_json(_SCORE_CALIBRATION_PATH, default={"history": []})
    if not isinstance(historical, dict):
        historical = {"history": []}
    history: list[dict[str, Any]] = historical.get("history", [])

    entry = {"date": datetime.now(timezone.utc).isoformat(), **distribution}
    history.append(entry)
    history = history[-CALIBRATION_HISTORY_KEEP:]
    historical["history"] = history
    historical["last_updated"] = entry["date"]

    drift_warning: str | None = None
    if len(history) >= 3:
        last3 = history[-3:]
        avg_5 = sum(e.get("five_pct", 0) for e in last3) / 3
        avg_high = sum(e.get("high_pct", 0) for e in last3) / 3
        if avg_5 > CALIBRATION_DRIFT_HIGH_THRESHOLD:
            drift_warning = (
                f"⚠️ Drift detecte : moyenne 5★ = {avg_5:.0%} sur 3 derniers runs "
                f"(seuil {CALIBRATION_DRIFT_HIGH_THRESHOLD:.0%}). "
                "Possible over-scoring Gemini."
            )
        elif avg_high < CALIBRATION_DRIFT_LOW_THRESHOLD:
            drift_warning = (
                f"⚠️ Drift detecte : moyenne 4-5★ = {avg_high:.0%} sur 3 derniers runs "
                f"(seuil {CALIBRATION_DRIFT_LOW_THRESHOLD:.0%}). "
                "Possible under-scoring ou pre-filtre trop strict."
            )

    if drift_warning:
        historical["last_drift_warning"] = drift_warning
        logger.warning(drift_warning)
    try:
        atomic_write_json(_SCORE_CALIBRATION_PATH, historical)
    except OSError as e:
        logger.warning(f"⚠️ Persistance score_calibration.json impossible : {e}")

    return {"distribution": distribution, "drift_warning": drift_warning}


# =============================================================================
# Entrypoint principal applique a un resultat de filter_articles_with_ai
# =============================================================================

def apply_v2_pipeline(
    retained_articles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Applique MMR diversification + tracking calibration au resultat IA.

    Le pre-ranking est fait AVANT l'appel Gemini (voir ai_filter.py).
    Cette fonction s'applique APRES le scoring, sur le tri final.

    Returns:
        (reordered_articles, meta_v2)
    """
    reordered = compute_mmr_ranking(retained_articles)
    calibration = track_score_distribution(reordered)
    meta_v2 = {
        "mmr_applied":     MMR_ENABLED,
        "mmr_lambda":      MMR_LAMBDA if MMR_ENABLED else None,
        "ranker_kind":     _ranker_kind or _detect_ranker_kind(),
        "distribution":    calibration.get("distribution", {}),
        "drift_warning":   calibration.get("drift_warning"),
    }
    return reordered, meta_v2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    kind = _detect_ranker_kind()
    print(f"Ranker kind : {kind}")
    if kind != "none":
        profile = build_target_profile(
            ["Aixtron", "Veeco"],
            ["PVD", "ALD"],
            ["HiPIMS magnetron sputtering"],
            ["EPFL"],
            ["metasurfaces"],
        )
        sample = [
            {"title": "Atomic layer deposition for batteries", "summary": "ALD coating cycle life", "score": 5},
            {"title": "Stock market update", "summary": "Tech sector valuation", "score": 1},
            {"title": "Metasurfaces enable structural color", "summary": "Photonic crystals deposit", "score": 4},
            {"title": "Football match", "summary": "Team A beat Team B", "score": 1},
        ]
        kept, rejected = pre_rank_articles(sample, profile)
        print(f"Pre-rank : {len(kept)} kept / {rejected} rejected")
        for a in sample:
            print(f"  sim={a.get('prerank_similarity', 0):.3f} | {a['title']}")
