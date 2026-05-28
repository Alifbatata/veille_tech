# =============================================================================
# prompt_version.py — Versioning des prompts pour tracabilite inter-runs
# =============================================================================
#
# Le prompt systeme evolue (rubrique G-Eval, few-shot feedbacks, listes de
# concurrents auto-promues...). Sans versioning, les scores historiques
# stockes dans articles_archive.json deviennent incomparables entre runs.
#
# Solution : hash SHA1 court du prompt + persistance des prompts vus dans
# data/prompt_versions.json. Chaque article retenu porte `scoring_prompt_version`.
#
# Permet :
#   - audit retrospectif ("le score X a ete calcule avec quel prompt ?")
#   - A/B test offline (comparer scores avant/apres un changement de prompt)
#   - alertes en cas de changement non-intentionnel
# =============================================================================

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("prompt_version")

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_VERSIONS_PATH = os.path.join(_DATA_DIR, "prompt_versions.json")
_MAX_HISTORY = int(os.environ.get("PROMPT_VERSIONS_MAX_HISTORY", "50"))


def compute_prompt_version(prompt: str) -> str:
    """Hash SHA1 court (8 hex chars) du prompt complet.

    Stable, reproductible : meme prompt → meme version.
    Non cryptographique (SHA1 OK pour un ID public).
    """
    return hashlib.sha1((prompt or "").encode("utf-8")).hexdigest()[:8]


def register_prompt(prompt: str, label: str = "system") -> str:
    """Enregistre un prompt dans data/prompt_versions.json si nouveau.

    Idempotent : un meme prompt n'est jamais re-registree.

    Returns:
        Le hash version du prompt (8 chars).
    """
    version = compute_prompt_version(prompt)
    history = safe_read_json(_VERSIONS_PATH, default={"versions": []})
    if not isinstance(history, dict):
        history = {"versions": []}
    versions: list[dict[str, Any]] = history.get("versions", [])

    if any(v.get("version") == version for v in versions):
        return version  # deja enregistre

    entry = {
        "version":      version,
        "label":        label,
        "first_seen":   datetime.now(timezone.utc).isoformat(),
        "prompt_size":  len(prompt or ""),
        # Snippet pour identifier rapidement la version : 1ere ligne + ~200 chars
        "preview":      (prompt or "")[:300].split("\n")[0][:200],
    }
    versions.append(entry)
    history["versions"] = versions[-_MAX_HISTORY:]
    history["last_updated"] = entry["first_seen"]
    try:
        atomic_write_json(_VERSIONS_PATH, history)
        logger.info(f"📝 Nouveau prompt enregistre : version {version} ({label})")
    except OSError as e:
        logger.warning(f"⚠️ Impossible d'enregistrer prompt_versions.json : {e}")
    return version


def get_current_version(prompt: str) -> str:
    """Retourne la version d'un prompt sans modifier l'historique."""
    return compute_prompt_version(prompt)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    test = "You are an AI judge..."
    v = register_prompt(test, "test_demo")
    print(f"Version : {v}")
    v2 = register_prompt(test, "test_demo")  # idempotent
    print(f"Re-register (idempotent) : {v2}")
