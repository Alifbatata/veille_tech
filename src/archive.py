# =============================================================================
# archive.py — Archive cumulative des articles filtrés (pour rattrapage)
# =============================================================================
# Permet d'envoyer un récapitulatif "tout ce qu'on a collecté jusqu'à présent"
# à de nouveaux destinataires, sans dépendre des flux RSS du moment.
# =============================================================================

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("archive")

_ARCHIVE_PATH: str = os.path.join(os.path.dirname(__file__), "../data/articles_archive.json")
_ARCHIVE_MAX_ENTRIES: int = 5000  # garde-fou anti-dérive


def load_archive() -> list[dict[str, Any]]:
    """Charge l'archive cumulative. Retourne [] si fichier absent ou corrompu."""
    if not os.path.exists(_ARCHIVE_PATH):
        return []
    try:
        with open(_ARCHIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("articles", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Lecture archive impossible : %s", e)
        return []


def update_archive(new_articles: list[dict[str, Any]]) -> int:
    """
    Fusionne new_articles dans l'archive en dédoublonnant par URL.
    Conserve la version la plus récente (en cas de re-scoring).

    Returns:
        Nombre d'articles ajoutés (hors doublons).
    """
    if not new_articles:
        return 0

    existing = load_archive()
    by_url: dict[str, dict[str, Any]] = {
        a["link"].strip().rstrip("/"): a for a in existing if a.get("link")
    }

    added = 0
    for art in new_articles:
        url = (art.get("link") or "").strip().rstrip("/")
        if not url:
            continue
        if url not in by_url:
            added += 1
        # On écrase systématiquement → garde le scoring le plus récent
        by_url[url] = art

    merged = list(by_url.values())
    # Tri par date de collecte décroissante puis cap au max
    merged.sort(key=lambda a: a.get("collected_at", ""), reverse=True)
    merged = merged[:_ARCHIVE_MAX_ENTRIES]

    try:
        os.makedirs(os.path.dirname(_ARCHIVE_PATH), exist_ok=True)
        with open(_ARCHIVE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "meta": {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "total":      len(merged),
                },
                "articles": merged,
            }, f, ensure_ascii=False, indent=2)
        logger.info("📚 Archive mise à jour : %d nouveau(x) / %d total", added, len(merged))
    except OSError as e:
        logger.error("Échec écriture archive : %s", e)

    return added


def build_recap_payload(min_score: int = 2, tldr: str = "") -> dict[str, Any]:
    """
    Construit un payload compatible mailer.send_digest() à partir de l'archive,
    pour envoi de rattrapage à de nouveaux destinataires.
    """
    archive = load_archive()
    filtered = [a for a in archive if a.get("score", 0) >= min_score]
    filtered.sort(key=lambda a: (a.get("score", 0), a.get("collected_at", "")), reverse=True)

    return {
        "meta": {
            "run_at":           datetime.now(timezone.utc).isoformat(),
            "model":            "Archive cumulative",
            "input_count":      len(archive),
            "retained_count":   len(filtered),
            "rejected_count":   len(archive) - len(filtered),
            "batch_count":      0,
            "min_score_filter": min_score,
            "tldr":             tldr or _build_default_tldr(filtered),
        },
        "articles": filtered,
    }


def _build_default_tldr(articles: list[dict[str, Any]]) -> str:
    if not articles:
        return "Aucun article archivé pour l'instant."
    n5 = sum(1 for a in articles if a.get("score") == 5)
    n4 = sum(1 for a in articles if a.get("score") == 4)
    return (
        f"Récapitulatif de l'historique de veille — {len(articles)} article(s) "
        f"retenu(s) jusqu'à ce jour, dont {n5} percée(s) majeure(s) et "
        f"{n4} innovation(s) solide(s). Cette synthèse couvre toute la période "
        f"depuis le démarrage du programme."
    )
