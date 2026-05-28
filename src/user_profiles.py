# =============================================================================
# user_profiles.py — Profils utilisateur pour personnalisation par destinataire
# =============================================================================
#
# Permet a chaque destinataire de configurer ses propres preferences :
#   - boost_keywords : termes qui augmentent le score perso (+0.5 par defaut)
#   - penalty_keywords : termes qui le baissent (-0.5 par defaut)
#   - min_score override : seuil mail perso
#
# Stocke dans data/user_profiles.json. Si le fichier n'existe pas, comportement
# par defaut : tous les destinataires recoivent la meme version (zero impact).
#
# Format JSON :
#   {
#     "profiles": {
#       "alice@example.com": {
#         "boost_keywords":   ["tribology", "DLC"],
#         "penalty_keywords": ["medical"],
#         "min_score":        3
#       },
#       "bob@example.com": {
#         "boost_keywords": ["medical", "biocompatibility"],
#         "min_score":      4
#       }
#     }
#   }
# =============================================================================

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from io_utils import safe_read_json
except ImportError:
    from src.io_utils import safe_read_json

logger = logging.getLogger("user_profiles")

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_PROFILES_PATH = os.path.join(_DATA_DIR, "user_profiles.json")

DEFAULT_BOOST: float = float(os.environ.get("USER_PROFILE_BOOST", "0.5"))
DEFAULT_PENALTY: float = float(os.environ.get("USER_PROFILE_PENALTY", "0.5"))


def load_user_profile(email: str) -> dict[str, Any]:
    """Charge le profil d'un destinataire (email).

    Retourne un dict vide si pas de profil configure (= comportement default).
    Compatible case-insensitive sur l'email.
    """
    if not email:
        return {}
    data = safe_read_json(_PROFILES_PATH, default={})
    if not isinstance(data, dict):
        return {}
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    # Match case-insensitive
    email_lower = email.lower()
    for key, val in profiles.items():
        if key.lower() == email_lower:
            return val if isinstance(val, dict) else {}
    return {}


def apply_user_personalization(
    articles: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reordonne les articles selon les preferences du profil utilisateur.

    Renvoie une NOUVELLE liste (pas mutation en place). Si profil vide,
    retourne articles inchanges.

    Effet : pour chaque article, calcule un `personalization_boost` (additif
    sur le score) base sur boost_keywords (+) et penalty_keywords (-). Trie
    par score effectif (score + boost). Le score original est preserve dans
    chaque dict (clef "score"), seul l'ordre change. Permet differents
    destinataires d'avoir un tri different sans dupliquer les donnees.

    Filtre aussi par min_score personnel si specifie dans le profil.
    """
    if not profile or not articles:
        return list(articles)

    boost_kws = [k.lower() for k in profile.get("boost_keywords", []) if k]
    penalty_kws = [k.lower() for k in profile.get("penalty_keywords", []) if k]
    boost_val = float(profile.get("boost_value", DEFAULT_BOOST))
    penalty_val = float(profile.get("penalty_value", DEFAULT_PENALTY))
    user_min = profile.get("min_score")

    enriched: list[dict[str, Any]] = []
    for art in articles:
        blob = (
            str(art.get("title", "")) + " "
            + str(art.get("summary", "")) + " "
            + " ".join(art.get("tags", []) or [])
        ).lower()
        delta = 0.0
        matched_boost = [k for k in boost_kws if k in blob]
        matched_penalty = [k for k in penalty_kws if k in blob]
        if matched_boost:
            delta += boost_val * min(len(matched_boost), 3)
        if matched_penalty:
            delta -= penalty_val * min(len(matched_penalty), 3)

        score = float(art.get("score", 0))
        effective = score + delta
        # On copie l'article pour ne pas muter l'original
        new_art = dict(art)
        new_art["personalization_boost"] = round(delta, 2)
        new_art["effective_score"] = round(effective, 2)
        if matched_boost:
            new_art["matched_boost_kw"] = matched_boost
        if matched_penalty:
            new_art["matched_penalty_kw"] = matched_penalty
        enriched.append(new_art)

    enriched.sort(key=lambda a: a["effective_score"], reverse=True)

    if user_min is not None:
        try:
            min_int = int(user_min)
            enriched = [a for a in enriched if a.get("score", 0) >= min_int]
        except (ValueError, TypeError):
            pass

    return enriched


def list_profile_emails() -> list[str]:
    """Liste les emails ayant un profil configure (utile pour audit / dashboard)."""
    data = safe_read_json(_PROFILES_PATH, default={})
    if not isinstance(data, dict):
        return []
    return list((data.get("profiles") or {}).keys())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    profile = {
        "boost_keywords": ["tribology", "DLC"],
        "penalty_keywords": ["medical"],
    }
    sample = [
        {"title": "Medical biocompatible coating", "summary": "", "score": 5},
        {"title": "DLC tribological breakthrough", "summary": "Tribology of DLC", "score": 4},
        {"title": "Generic article", "summary": "neutral", "score": 5},
    ]
    out = apply_user_personalization(sample, profile)
    for a in out:
        print(f"{a['effective_score']:.2f} (score {a['score']}, boost {a['personalization_boost']:+.2f}) - {a['title']}")
