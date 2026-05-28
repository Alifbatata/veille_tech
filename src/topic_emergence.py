# =============================================================================
# topic_emergence.py — Detection automatique de thematiques emergentes
# =============================================================================
#
# Plutot que de demander a l'utilisateur de manuellement maintenir
# `cross_domain_topics`, ce module detecte les n-grams (1-3 mots) qui :
#   - Apparaissent frequemment dans les articles retenus haut score (>=4)
#   - Sont absents des targets.json actuels (companies/keywords/cross_domain/...)
#   - Sont techniques (pas des stop-words ou termes vagues)
#
# Strategie :
#   1. TF-IDF sur les articles top-scored avec n-grams (1, 2, 3)
#   2. Filtre stop-words techniques + termes deja listes
#   3. Top N termes ranked par score TF-IDF agrege
#   4. Suggestion CLI : "Ajouter <terme> a cross_domain_topics ? [y/n]"
#
# Tout local, 0 cout. Utilise sklearn TF-IDF (deja installe pour scoring_v2).
# =============================================================================

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("topic_emergence")


# =============================================================================
# Configuration
# =============================================================================

EMERGENCE_ENABLED: bool = os.environ.get("EMERGENCE_ENABLED", "true").lower() in ("true", "1", "yes")
EMERGENCE_MIN_SCORE: int = int(os.environ.get("EMERGENCE_MIN_SCORE", "4"))
EMERGENCE_TOP_N: int = int(os.environ.get("EMERGENCE_TOP_N", "10"))
EMERGENCE_MIN_DOC_FREQ: int = int(os.environ.get("EMERGENCE_MIN_DOC_FREQ", "3"))
EMERGENCE_MAX_DOC_FREQ_PCT: float = float(os.environ.get("EMERGENCE_MAX_DOC_FREQ_PCT", "0.6"))

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_EMERGENCE_PATH = os.path.join(_DATA_DIR, "emerging_topics.json")

# Stop-words techniques : termes trop generiques pour etre actionnables
_STOP_TERMS = frozenset({
    "thin film", "thin films", "physical vapor", "chemical vapor", "atomic layer",
    "deposition method", "deposition process", "coating method", "coating process",
    "applied physics", "materials science", "high performance", "low cost",
    "et al", "case study", "review article", "research article", "open access",
    "experimental study", "experimental results", "numerical simulation",
    "future work", "first principles", "ab initio", "machine learning",
    "deep learning", "neural network",
})


# =============================================================================
# Detection des n-grams emergents
# =============================================================================

def _known_terms_set(
    companies: list[str],
    keywords: list[str],
    solo_keywords: list[str],
    research_orgs: list[str],
    cross_domain_topics: list[str],
) -> set[str]:
    """Construit l'ensemble des termes deja listes (normalises lowercase)."""
    known: set[str] = set()
    for lst in (companies, keywords, solo_keywords, research_orgs, cross_domain_topics):
        for item in lst or []:
            normalized = re.sub(r"\s+", " ", item.lower().strip())
            known.add(normalized)
            # Egalement chaque mot pris isolement (pour matcher "PVD" vs "PVD coating")
            for word in normalized.split():
                if len(word) >= 3:
                    known.add(word)
    return known


def detect_emerging_topics(
    retained_articles: list[dict[str, Any]],
    companies: list[str],
    keywords: list[str],
    solo_keywords: list[str],
    research_orgs: list[str],
    cross_domain_topics: list[str],
) -> list[dict[str, Any]]:
    """Detecte les n-grams emergents dans les articles top-scored.

    Returns:
        Liste de {term, score, freq, examples_titles} triee par score desc.
        Vide si module desactive ou si sklearn manque.
    """
    if not EMERGENCE_ENABLED:
        return []
    # Filtre articles haute pertinence (signal fort = pas du bruit)
    high_score = [a for a in retained_articles if int(a.get("score", 0)) >= EMERGENCE_MIN_SCORE]
    if len(high_score) < EMERGENCE_MIN_DOC_FREQ:
        return []

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        logger.info("ℹ️ Topic emergence : sklearn indispo, skip.")
        return []

    texts = [
        (a.get("title", "") + ". " + a.get("summary", "")).strip()
        for a in high_score
    ]

    try:
        vec = TfidfVectorizer(
            stop_words="english",
            max_features=2000,
            ngram_range=(1, 3),
            min_df=EMERGENCE_MIN_DOC_FREQ,
            max_df=EMERGENCE_MAX_DOC_FREQ_PCT,
            sublinear_tf=True,
            token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]{2,}\b",  # mots >= 3 chars
        )
        matrix = vec.fit_transform(texts)
        # Score agrege par terme (somme des TF-IDF sur tous les docs)
        scores = matrix.sum(axis=0).A1
        feature_names = vec.get_feature_names_out()
    except (ValueError, MemoryError) as e:
        logger.warning(f"⚠️ Topic emergence : echec TF-IDF ({e})")
        return []

    known = _known_terms_set(companies, keywords, solo_keywords, research_orgs, cross_domain_topics)

    # Filtre + ranking
    candidates: list[tuple[str, float]] = []
    for term, score in zip(feature_names, scores):
        term_norm = re.sub(r"\s+", " ", term.lower().strip())
        if term_norm in known or term_norm in _STOP_TERMS:
            continue
        # Filtre les termes trop courts ou tres communs
        if len(term_norm) < 4:
            continue
        # Compte la frequence documentaire reelle
        col_idx = list(feature_names).index(term)
        doc_freq = (matrix[:, col_idx] > 0).sum()
        if doc_freq < EMERGENCE_MIN_DOC_FREQ:
            continue
        candidates.append((term_norm, float(score)))

    candidates.sort(key=lambda kv: kv[1], reverse=True)
    top = candidates[:EMERGENCE_TOP_N]

    # Build with examples titles
    results: list[dict[str, Any]] = []
    for term, score in top:
        examples = []
        for a in high_score:
            blob = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if term in blob:
                examples.append((a.get("title") or "")[:120])
                if len(examples) >= 2:
                    break
        results.append({
            "term":      term,
            "score":     round(score, 3),
            "examples":  examples,
        })

    if results:
        logger.info(f"🌱 Topics emergents detectes : {len(results)} suggestion(s).")
        for r in results[:3]:
            logger.info(f"   • « {r['term']} » (score {r['score']})")
    return results


def persist_emerging_topics(topics: list[dict[str, Any]]) -> None:
    """Stocke les topics emergents dans data/emerging_topics.json (rolling history)."""
    if not topics:
        return
    data = safe_read_json(_EMERGENCE_PATH, default={"history": []})
    if not isinstance(data, dict):
        data = {"history": []}
    history: list[dict[str, Any]] = data.get("history", [])
    history.append({
        "date":   datetime.now(timezone.utc).isoformat(),
        "topics": topics,
    })
    data["history"] = history[-10:]  # garde 10 runs
    data["last_updated"] = history[-1]["date"]
    try:
        atomic_write_json(_EMERGENCE_PATH, data)
    except OSError as e:
        logger.warning(f"⚠️ Persistance emerging_topics.json impossible : {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = [
        {"title": "Topological insulator thin film for spintronics",
         "summary": "Bismuth selenide topological insulator deposited by PVD enables...",
         "score": 5},
        {"title": "Topological superconductor heterostructure",
         "summary": "Topological insulator integration with superconductors...",
         "score": 4},
        {"title": "Topological photonic crystal",
         "summary": "Topological insulator concepts applied to photonic crystals...",
         "score": 4},
    ]
    topics = detect_emerging_topics(sample, [], ["PVD"], [], [], ["spintronics"])
    print(f"Detected {len(topics)} emerging topics")
    for t in topics:
        print(f"  {t['score']:.2f} - {t['term']}")
