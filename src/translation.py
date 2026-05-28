# =============================================================================
# translation.py — Traduction optionnelle EN -> FR des resumes
# =============================================================================
#
# Pour les lecteurs qui ne maitrisent pas l'anglais technique pointu, ce module
# traduit les resumes des N meilleurs articles via un seul appel Gemini batch
# (1 requete pour tout le digest, ~5-15 articles).
#
# Activation : TRANSLATION_ENABLED=true dans .env. Cap TRANSLATION_MAX_ARTICLES
# (par defaut 15) pour ne pas exploser le quota gratuit.
#
# Strategie batch : on envoie les N resumes en un seul appel structure JSON
# au lieu de N appels separes. Economie : N-1 appels API.
#
# Cas d'echec : si Gemini indispo ou parsing rate, fallback silencieux sur
# les resumes EN originaux (le pipeline ne crashe jamais sur la traduction).
# =============================================================================

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("translation")


# =============================================================================
# Configuration
# =============================================================================

TRANSLATION_ENABLED: bool = os.environ.get("TRANSLATION_ENABLED", "false").lower() in ("true", "1", "yes")
TRANSLATION_TARGET_LANG: str = os.environ.get("TRANSLATION_TARGET_LANG", "fr").lower()
TRANSLATION_MAX_ARTICLES: int = int(os.environ.get("TRANSLATION_MAX_ARTICLES", "15"))
TRANSLATION_MIN_SCORE: int = int(os.environ.get("TRANSLATION_MIN_SCORE", "4"))


def translate_summaries(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traduit les resumes des top articles (score >= TRANSLATION_MIN_SCORE).

    Ne MUTE PAS les articles : ajoute un champ `summary_translated` a coté
    de `summary` (preserve l'original pour reference). Le mailer peut choisir
    d'afficher l'un ou l'autre.

    Si TRANSLATION_ENABLED=false : no-op, retour direct.
    Si Gemini indispo : no-op, log info, pipeline continue.

    Returns:
        La liste articles enrichie en place (modifications dans chaque dict).
    """
    if not TRANSLATION_ENABLED or not articles:
        return articles

    # Filtre : seulement top articles, eviter de gaspiller le quota
    candidates = [
        a for a in articles
        if int(a.get("score", 0)) >= TRANSLATION_MIN_SCORE
        and a.get("summary", "").strip()
    ][:TRANSLATION_MAX_ARTICLES]

    if not candidates:
        return articles

    try:
        # Lazy import pour eviter de charger google.generativeai sans necessite
        try:
            from ai_filter import _init_client, _call_gemini_with_retry, GeminiUnavailableError
        except ImportError:
            from src.ai_filter import _init_client, _call_gemini_with_retry, GeminiUnavailableError

        model = _init_client()
    except (ImportError, Exception) as e:
        logger.info(f"ℹ️ Translation : modele Gemini indispo ({e}), skip.")
        return articles

    # Construit un mini-prompt batch JSON
    payload_lines = [
        f"[{i}] {a.get('summary', '')[:500]}"
        for i, a in enumerate(candidates)
    ]
    user_prompt = (
        f"Translate the following {len(candidates)} scientific article summaries "
        f"from English to {TRANSLATION_TARGET_LANG.upper()}. Preserve technical terms "
        f"(PVD, ALD, HiPIMS, etc.) but translate the surrounding sentences naturally.\n\n"
        "Reply with ONLY a JSON object in this exact format (no markdown, no commentary):\n"
        '{"translations": [{"id": 0, "text": "..."}, {"id": 1, "text": "..."}]}\n\n'
        + "\n\n".join(payload_lines)
    )

    try:
        result = _call_gemini_with_retry(model, user_prompt, json_mode=True)
        raw = result.text if hasattr(result, "text") else str(result)
    except GeminiUnavailableError as e:
        logger.info(f"ℹ️ Translation : Gemini indispo ({e}), skip.")
        return articles
    except Exception as e:
        logger.warning(f"⚠️ Translation : echec appel Gemini ({e}), skip.")
        return articles

    try:
        parsed = json.loads(raw.strip())
        translations = parsed.get("translations", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"⚠️ Translation : JSON malforme ({e}), skip.")
        return articles

    # Map id -> traduction
    by_id = {int(t["id"]): t.get("text", "") for t in translations if "id" in t}
    n_done = 0
    for i, art in enumerate(candidates):
        translated = by_id.get(i, "").strip()
        if translated:
            art["summary_translated"] = translated
            art["summary_lang"] = TRANSLATION_TARGET_LANG
            n_done += 1

    if n_done > 0:
        logger.info(f"🌐 Traduction : {n_done}/{len(candidates)} resume(s) traduits en {TRANSLATION_TARGET_LANG}.")
    return articles
