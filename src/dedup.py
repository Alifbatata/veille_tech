# =============================================================================
# dedup.py — Deduplication semantique d'articles
# =============================================================================
#
# Sur 1500+ articles bruts par run, on a souvent :
#   - Un preprint arXiv + sa version publiee dans un journal
#   - Un communique de presse repris par plusieurs media (GNews)
#   - Le meme paper indexe par OpenAlex et Crossref simultanement
#
# La dedup URL existante (seen_urls) ne capture PAS ces cas (URLs differentes,
# meme contenu). Cette dedup semantique compare TF-IDF + cosine et regroupe
# les articles dont la similarite >= threshold (0.85 par defaut, conservateur).
#
# Strategie de selection (qui garder dans un groupe de doublons) :
#   1. Si l'un est categorise "patent" : priorise les patents (signal industriel +)
#   2. Sinon : priorise le resume le plus long (proxy de qualite/completude)
#   3. Sinon : priorise le plus recent (collected_at)
#
# Complexite : O(n²) en theorie, mais avec sklearn TfidfVectorizer + sparse
# matrices, en pratique < 2s pour 2000 articles. RAM negligeable (~30 MB).
# =============================================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("dedup")


# =============================================================================
# Configuration
# =============================================================================

DEDUP_ENABLED: bool = os.environ.get("DEDUP_ENABLED", "true").lower() in ("true", "1", "yes")
DEDUP_THRESHOLD: float = float(os.environ.get("DEDUP_THRESHOLD", "0.85"))
# Au-dela de N articles, on skip (tres improbable mais protection RAM)
DEDUP_MAX_ARTICLES: int = int(os.environ.get("DEDUP_MAX_ARTICLES", "5000"))


# =============================================================================
# Selection du meilleur article dans un groupe de doublons
# =============================================================================

def _pick_best_in_group(articles: list[dict[str, Any]]) -> int:
    """Choisit l'index du meilleur article dans une liste de doublons semantiques.

    Critères (ordre de priorite) :
      1. Categorie 'patent' (signal industriel direct, plus rare et precieux)
      2. Resume le plus long (proxy de qualite/completude)
      3. Plus recent (collected_at decroissant)
      4. Index 0 (fallback)
    """
    if not articles:
        return 0
    if len(articles) == 1:
        return 0

    def score_tuple(a: dict[str, Any]) -> tuple:
        is_patent = 1 if a.get("category", "").lower() == "patent" else 0
        summary_len = len(a.get("summary", "") or "")
        collected = a.get("collected_at", "")
        return (is_patent, summary_len, collected)

    indexed = list(enumerate(articles))
    indexed.sort(key=lambda iv: score_tuple(iv[1]), reverse=True)
    return indexed[0][0]


# =============================================================================
# Deduplication par TF-IDF cosine
# =============================================================================

def deduplicate_semantically(
    articles: list[dict[str, Any]],
    threshold: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Regroupe les articles semantiquement identiques et n'en garde qu'un par groupe.

    Args:
        articles: liste de dicts (title + summary requis).
        threshold: similarite cosine min pour considerer dupliques (default 0.85).

    Returns:
        (kept_articles, n_removed_duplicates)
    """
    if not DEDUP_ENABLED or len(articles) < 2:
        return articles, 0
    thr = threshold if threshold is not None else DEDUP_THRESHOLD

    if len(articles) > DEDUP_MAX_ARTICLES:
        logger.warning(
            f"⚠️ Dedup semantique : {len(articles)} articles depasse DEDUP_MAX_ARTICLES "
            f"({DEDUP_MAX_ARTICLES}), skip pour proteger la RAM."
        )
        return articles, 0

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        import numpy as np
    except ImportError:
        logger.info("ℹ️ Dedup semantique : sklearn indispo, skip.")
        return articles, 0

    texts = [
        (a.get("title", "") + ". " + a.get("summary", "")).strip()
        for a in articles
    ]
    # Articles sans contenu : on les passe a travers sans dedup
    nonempty_idx = [i for i, t in enumerate(texts) if len(t) > 20]
    if len(nonempty_idx) < 2:
        return articles, 0

    nonempty_texts = [texts[i] for i in nonempty_idx]
    try:
        vec = TfidfVectorizer(
            stop_words="english",
            max_features=8000,
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
        )
        matrix = vec.fit_transform(nonempty_texts)
        # Matrice de similarite (n x n, sparse-compatible)
        sim = cosine_similarity(matrix)
    except (ValueError, MemoryError) as e:
        logger.warning(f"⚠️ Dedup semantique : echec calcul TF-IDF ({e}). Skip.")
        return articles, 0

    # Union-find pour grouper les paires similaires
    n = len(nonempty_idx)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        # Triangle superieur seulement
        for j in range(i + 1, n):
            if sim[i, j] >= thr:
                union(i, j)

    # Construit les groupes
    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(nonempty_idx[i])

    kept_idx: set[int] = set()
    # Toujours garder les articles vides (sans contenu) tels quels
    for i, t in enumerate(texts):
        if len(t) <= 20:
            kept_idx.add(i)

    # Pour chaque groupe, garder le meilleur
    for group_indices in groups.values():
        if len(group_indices) == 1:
            kept_idx.add(group_indices[0])
        else:
            sub = [articles[i] for i in group_indices]
            best = _pick_best_in_group(sub)
            kept_idx.add(group_indices[best])

    kept = [articles[i] for i in range(len(articles)) if i in kept_idx]
    removed = len(articles) - len(kept)

    if removed > 0:
        logger.info(
            f"♻️ Dedup semantique : {removed}/{len(articles)} doublon(s) elimine(s) "
            f"(seuil cosine {thr:.2f}, {len(kept)} articles uniques restants)."
        )
    return kept, removed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    sample = [
        {"title": "ALD coating for batteries", "summary": "atomic layer deposition improves cycle life", "category": "science"},
        {"title": "Atomic layer deposition improves battery cycle life",
         "summary": "ALD coating of cathode materials extends Li-ion cycle life",
         "category": "science"},
        {"title": "Football match result", "summary": "Team A beat Team B 2-1", "category": "news"},
        {"title": "New patent: TiN sputtering target", "summary": "Patent filed by Aixtron AG", "category": "patent"},
    ]
    kept, removed = deduplicate_semantically(sample)
    print(f"Kept {len(kept)}, removed {removed}")
    for a in kept:
        print(f"  - {a['title']}")
