# =============================================================================
# competitor_heatmap.py — Suivi statistique des concurrents inter-runs
# =============================================================================
#
# Au-dela de "Aixtron a depose 1 brevet ce run", repond a :
#   - "Activite chez Aixtron est-elle ANORMALEMENT haute / basse vs ses standards ?"
#   - "Quels concurrents accelerent leur depot de brevets ce trimestre ?"
#
# Architecture :
#   1. A chaque run, compte mentions par company dans les articles retenus
#      (matching case-insensitive dans title+summary, comme _force_company_scores)
#   2. Stocke un snapshot dans data/competitor_history.json (rotation 20 runs)
#   3. Compare au moyen historique : ratio current/avg → alerte si > 2.0
#   4. Retourne un dict consommable par mailer.py pour une section dédiée
#
# Tout local, 0 cout, 0 API. Wire en fin de ai_filter.
# =============================================================================

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("competitor_heatmap")


# =============================================================================
# Configuration
# =============================================================================

HEATMAP_ENABLED: bool = os.environ.get("HEATMAP_ENABLED", "true").lower() in ("true", "1", "yes")
HEATMAP_LOOKBACK_RUNS: int = int(os.environ.get("HEATMAP_LOOKBACK_RUNS", "8"))
HEATMAP_HISTORY_KEEP: int = int(os.environ.get("HEATMAP_HISTORY_KEEP", "20"))
# Seuil ratio pour declencher une alerte "anomalie d'activite"
HEATMAP_ANOMALY_RATIO: float = float(os.environ.get("HEATMAP_ANOMALY_RATIO", "2.0"))
# Mentions absolues minimales pour qu'une anomalie soit reportee (evite bruit)
HEATMAP_MIN_MENTIONS: int = int(os.environ.get("HEATMAP_MIN_MENTIONS", "2"))

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_HISTORY_PATH = os.path.join(_DATA_DIR, "competitor_history.json")


# =============================================================================
# Comptage des mentions dans le run courant
# =============================================================================

def count_mentions_in_articles(
    articles: list[dict[str, Any]],
    companies: list[str],
) -> dict[str, int]:
    """Pour chaque entreprise dans `companies`, compte les articles ou son nom
    apparait (case-insensitive) dans title+summary+source.

    Inclut une logique de comptage des brevets specifiquement : on incremente +1
    si le nom apparait dans `patent_assignee` (signal industriel direct).
    """
    if not articles or not companies:
        return {}
    companies_norm = [(c, c.lower()) for c in companies if c]
    counts: dict[str, int] = {c: 0 for c, _ in companies_norm}

    for art in articles:
        blob = " ".join([
            str(art.get("title") or ""),
            str(art.get("summary") or ""),
            str(art.get("source") or ""),
            str(art.get("patent_assignee") or ""),
        ]).lower()
        if not blob.strip():
            continue
        for name, name_lower in companies_norm:
            if name_lower in blob:
                counts[name] += 1
    return counts


def _split_patents_vs_general(
    articles: list[dict[str, Any]],
    companies: list[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Compte separe pour brevets vs articles generaux par company."""
    patents = [a for a in articles if a.get("category") == "patent"]
    others = [a for a in articles if a.get("category") != "patent"]
    return (
        count_mentions_in_articles(patents, companies),
        count_mentions_in_articles(others, companies),
    )


# =============================================================================
# Historique + detection d'anomalies
# =============================================================================

def _load_history() -> dict[str, Any]:
    data = safe_read_json(_HISTORY_PATH, default={"history": []})
    if not isinstance(data, dict):
        return {"history": []}
    data.setdefault("history", [])
    return data


def _save_history(data: dict[str, Any]) -> None:
    data["history"] = data.get("history", [])[-HEATMAP_HISTORY_KEEP:]
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write_json(_HISTORY_PATH, data)
    except OSError as e:
        logger.warning(f"⚠️ Persistance competitor_history.json impossible : {e}")


def compute_heatmap(
    articles: list[dict[str, Any]],
    companies: list[str],
) -> dict[str, Any]:
    """Compute et persiste les stats concurrentielles du run.

    Args:
        articles: articles retenus (avec score >= seuil).
        companies: liste des concurrents trackes (TARGET_COMPANIES).

    Returns:
        Dict pour consommation par mailer.py / dashboard.py :
        {
          "current_run": {"Aixtron": {"total": 3, "patents": 1, "others": 2}, ...},
          "anomalies":   [{"name": "Aixtron", "current": 3, "avg": 0.5, "ratio": 6.0, "direction": "up"}],
          "top_active":  [("Aixtron", 3), ("Veeco", 2)],
        }
    """
    empty = {"current_run": {}, "anomalies": [], "top_active": []}
    if not HEATMAP_ENABLED or not articles or not companies:
        return empty

    patent_counts, other_counts = _split_patents_vs_general(articles, companies)
    current_breakdown: dict[str, dict[str, int]] = {}
    for c in companies:
        p = patent_counts.get(c, 0)
        o = other_counts.get(c, 0)
        if p + o > 0:
            current_breakdown[c] = {"total": p + o, "patents": p, "others": o}

    history_data = _load_history()
    history: list[dict[str, Any]] = history_data.get("history", [])

    # Calcul de la moyenne historique (lookback runs)
    lookback = history[-HEATMAP_LOOKBACK_RUNS:] if history else []

    anomalies: list[dict[str, Any]] = []
    for c, breakdown in current_breakdown.items():
        cur = breakdown["total"]
        if cur < HEATMAP_MIN_MENTIONS:
            continue
        # Moyenne historique (zero compté pour les runs sans mention)
        hist_values = []
        for entry in lookback:
            past = entry.get("counts", {}).get(c)
            if isinstance(past, dict):
                hist_values.append(past.get("total", 0))
            else:
                hist_values.append(int(past or 0))
        avg = sum(hist_values) / len(hist_values) if hist_values else 0.0
        # Ratio (avec lissage : avg=0 traite comme 0.5 pour eviter division par 0)
        safe_avg = max(avg, 0.5)
        ratio = cur / safe_avg
        if ratio >= HEATMAP_ANOMALY_RATIO:
            anomalies.append({
                "name":     c,
                "current":  cur,
                "avg":      round(avg, 2),
                "ratio":    round(ratio, 2),
                "patents":  breakdown["patents"],
                "others":   breakdown["others"],
                "direction": "up",
            })
        elif avg >= 2 and cur < avg * 0.3:  # Activite anormalement basse
            anomalies.append({
                "name":     c,
                "current":  cur,
                "avg":      round(avg, 2),
                "ratio":    round(cur / safe_avg, 2),
                "patents":  breakdown["patents"],
                "others":   breakdown["others"],
                "direction": "down",
            })

    # Persistance du snapshot courant
    history.append({
        "date":   datetime.now(timezone.utc).isoformat(),
        "counts": current_breakdown,
    })
    history_data["history"] = history
    _save_history(history_data)

    top_active = sorted(
        ((c, b["total"]) for c, b in current_breakdown.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )[:10]

    if anomalies:
        for a in anomalies:
            arrow = "📈" if a["direction"] == "up" else "📉"
            logger.info(
                f"{arrow} Heatmap : {a['name']} = {a['current']} mention(s) "
                f"(moyenne {a['avg']}, ratio x{a['ratio']})"
            )

    return {
        "current_run": current_breakdown,
        "anomalies":   anomalies,
        "top_active":  top_active,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    sample = [
        {"title": "Aixtron files new ALD patent", "summary": "Aixtron AG patent", "category": "patent",
         "patent_assignee": "Aixtron AG"},
        {"title": "Veeco quarterly report", "summary": "Veeco Inc", "category": "news"},
    ]
    result = compute_heatmap(sample, ["Aixtron", "Veeco", "Picosun"])
    print(f"Current run : {result['current_run']}")
    print(f"Top active  : {result['top_active']}")
