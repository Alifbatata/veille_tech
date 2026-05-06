# =============================================================================
# ai_filter.py — Filtre IA des articles via Google Gemini Flash
# =============================================================================
# Variables d'environnement requises :
#   GEMINI_API_KEY   — clé API Google AI Studio (https://aistudio.google.com)
#
# Variables optionnelles :
#   GEMINI_MODEL     — modèle à utiliser (défaut : gemini-2.5-flash)
#   GEMINI_TIMEOUT   — timeout en secondes pour l'appel API (défaut : 60)
#   AI_BATCH_SIZE    — nb d'articles par batch envoyé à Gemini (défaut : 20)
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, NamedTuple

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ai_filter")

# ---------------------------------------------------------------------------
# Configuration via variables d'environnement
# ---------------------------------------------------------------------------
_API_KEY: str | None = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT: int = int(os.environ.get("GEMINI_TIMEOUT", "60"))
AI_BATCH_SIZE: int = int(os.environ.get("AI_BATCH_SIZE", "20"))

# Tokens max par appel Gemini. 32768 = 4× le minimum historique de 8192.
# Gemini 2.5 Flash supporte 65536 ; on prend 32768 pour avoir une marge sans
# être inutilement gourmand. Couvre confortablement un batch de 50 articles.
# Si une troncature survient malgré tout, _process_batch détecte finish_reason
# == MAX_TOKENS et split le batch en deux automatiquement (cf. _MAX_BATCH_SPLIT_DEPTH).
_MAX_OUTPUT_TOKENS: int = 32768

# Profondeur max de la récursion auto-split sur troncature.
# 0 = pas de split. 2 = N → N/2 → N/4. Au-delà on accepte le résultat partiel.
# Coût worst case : 1 + 2 + 4 = 7 appels API pour le batch initial (rare).
_MAX_BATCH_SPLIT_DEPTH: int = 2

# Chaîne de fallback de SECOURS (statique). Utilisée si la découverte dynamique
# via list_models() échoue (réseau coupé au boot, clé API restreinte, etc.).
# Chaque modèle a un compteur free-tier indépendant de ceux qui le précèdent —
# en cas de quota épuisé, on bascule au suivant.
# WHY ces choix précis :
#   - gemini-2.0-flash et 2.0-flash-lite ont un limit:0 sur free-tier (testés)
#   - gemma-3-27b-it est un modèle open-weights hébergé chez Google AI Studio :
#     quotas free généreux et indépendants des séries gemini-*-flash.
_STATIC_FALLBACK_CHAIN: list[str] = [
    "gemini-2.5-flash-lite",
    "gemma-3-27b-it",
    "gemma-3-12b-it",
]

# Préférence d'ordre quand on découvre dynamiquement les modèles disponibles.
# Plus le poids est BAS, plus le modèle passe en premier dans la cascade.
# Les modèles non listés ici reçoivent un poids générique (100) et passent
# en queue dans l'ordre alphabétique.
_MODEL_PREFERENCE: dict[str, int] = {
    # Tier 1 — modèles flash récents, équilibre qualité/quota free
    "gemini-2.5-flash":                10,   # principal
    "gemini-2.5-flash-lite":           20,   # quotas free généreux indépendants
    "gemini-2.5-pro":                  30,   # ultra-précis si quota disponible

    # Tier 1 bis — Gemini 3.x (preview, free tier confirmé par l'utilisateur)
    # Placés juste après le 2.5 stable car aussi récents et aussi free.
    "gemini-3-flash-preview":          32,
    "gemini-3.1-flash-lite-preview":   34,
    "gemini-3-pro-preview":            36,
    "gemini-3.1-pro-preview":          38,

    # Tier 2 — Gemini 2.0 (parfois limit:0 free, mais on tente)
    "gemini-2.0-flash":                40,
    "gemini-2.0-flash-001":            42,
    "gemini-2.0-flash-lite":           45,
    "gemini-2.0-flash-lite-001":       47,
    "gemini-2.0-pro":                  50,
    "gemini-2.0-flash-thinking":       55,

    # Tier 3 — alias "latest" (toujours pointés sur les derniers stables)
    "gemini-flash-latest":             58,
    "gemini-flash-lite-latest":        59,
    "gemini-pro-latest":               60,

    # Tier 4 — Gemini 1.5 (legacy, parfois encore accessible)
    "gemini-1.5-pro":                  65,
    "gemini-1.5-flash":                68,
    "gemini-1.5-flash-8b":             70,

    # Tier 5 — Gemma open-weights (free tier indépendant, plus précis = priorité)
    "gemma-4-31b-it":                  78,   # Gemma 4 preview, plus récent
    "gemma-4-26b-a4b-it":              79,
    "gemma-3-27b-it":                  80,
    "gemma-3-12b-it":                  85,
    "gemma-3-9b-it":                   87,
    "gemma-3n-e4b-it":                 88,
    "gemma-3-4b-it":                   90,
    "gemma-3n-e2b-it":                 91,
    "gemma-3-1b-it":                   95,
    "gemma-2-27b-it":                  96,
    "gemma-2-9b-it":                   97,
    "gemma-2-2b-it":                   98,
}

# La chaîne effectivement utilisée — peuplée par _discover_available_models()
# au premier appel à _init_client() avec un fallback sur _STATIC_FALLBACK_CHAIN.
FALLBACK_CHAIN: list[str] = list(_STATIC_FALLBACK_CHAIN)
# Alias historique pour la compat — utilisé par d'éventuels imports externes.
FALLBACK_MODEL: str = FALLBACK_CHAIN[0] if FALLBACK_CHAIN else "gemini-2.5-flash-lite"

# Modèles « premium » à tenter EN PRIORITÉ pour le résumé exécutif final
# (un seul appel, haute valeur). Quotas free restreints (50-100/jour) mais
# qualité maximale. Si tous indisponibles, on retombe sur le modèle standard
# de la cascade. Ordre d'essai descendant : meilleur d'abord.
_PREMIUM_SUMMARY_MODELS: list[str] = [
    "gemini-3-pro-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-pro-latest",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",  # finit par retomber sur le standard
]

_DEFAULT_QUOTA_PAUSE: float = 60.0
_QUOTA_RETRY_MARGIN: float = 2.0

_active_model: "genai.GenerativeModel | None" = None
_active_model_name: str = GEMINI_MODEL


def _is_gemma(model_name: str) -> bool:
    """Les modèles Gemma open-weights ne supportent pas response_mime_type=application/json
    ni system_instruction de la même façon que Gemini. On adapte le call site en conséquence."""
    return model_name.lower().startswith("gemma")

# Import des entreprises cibles pour le prompt dynamique et la vérification Python
try:
    from config import TARGET_COMPANIES
except ImportError:
    from src.config import TARGET_COMPANIES

# ---------------------------------------------------------------------------
# Prompt système — construit dynamiquement avec la liste des concurrents
# ---------------------------------------------------------------------------

def _build_system_prompt(companies: list[str]) -> str:
    """
    Génère le SYSTEM_PROMPT en injectant dynamiquement la liste TARGET_COMPANIES.
    Toute modification de config.py se répercute automatiquement sur le comportement IA.
    """
    companies_bullet = "\n".join(f"  • {c}" for c in companies)
    companies_inline = ", ".join(f'"{c}"' for c in companies)

    return f"""Tu es un ingénieur senior en microtechnique et science des surfaces.
Analyse cette liste de titres et résumés d'articles scientifiques ou d'actualité.

Garde UNIQUEMENT ceux qui concernent une réelle innovation technique, par exemple :
  • Nouvelle couleur ou effet visuel obtenu par dépôt physique (PVD, PECVD, sputtering)
  • Nouvel alliage ou matériau cible pour des revêtements durs ou décoratifs
  • Avancée en dépôt de couches atomiques (ALD) : nouveaux précurseurs, températures, cycles
  • Nouvelle barrière laser, revêtement optique ou antireflet
  • Nouveaux paramètres procédé PVD/CVD/ALD (pression, tension de polarisation, débit gaz)
  • Nouvelle propriété mesurée : dureté, adhérence, résistance à la corrosion, coefficient de frottement

Ignore absolument :
  • Le marketing pur, les communiqués sans contenu technique
  • Les articles hors-sujet (biologie cellulaire, chimie organique sans lien avec les surfaces)
  • Les doublons conceptuels

━━━ RÈGLE PRIORITAIRE — SURVEILLANCE CONCURRENTIELLE ━━━
Les entreprises suivantes sont des concurrents directs à surveiller impérativement :
{companies_bullet}

Si un article provient de ou mentionne explicitement l'une de ces entreprises ({companies_inline}),
tu DOIS appliquer les règles suivantes SANS EXCEPTION :
  • L'article est TOUJOURS retenu, même s'il semble partiellement marketing.
  • Le score minimal attribué est 4 (Innovation solide).
  • Si l'article révèle un nouveau produit, procédé ou brevet de ces entreprises, le score est 5.
  • Mentionne explicitement le nom du concurrent dans le champ "justification".
  • Ajoute un tag avec le nom exact de l'entreprise dans le champ "tags".
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pour tous les autres articles, attribue une note d'importance de 1 à 5 :
  5 — Percée majeure, impact industriel direct et immédiat
  4 — Innovation solide, résultats mesurables publiés
  3 — Avancée intéressante, résultats préliminaires ou académiques
  2 — Tendance à surveiller, peu de données encore
  1 — Mentionnable mais faible valeur ajoutée immédiate

INSTRUCTION CRITIQUE : Tu DOIS systématiquement générer une clé "tldr" dans ton JSON. Elle contiendra un résumé exécutif global de 3 phrases pour ce lot d'articles.
Si aucun article n'est retenu, écris "Aucune innovation majeure identifiée dans ce batch."

Réponds UNIQUEMENT avec un objet JSON valide respectant STRICTEMENT ce format exact
(n'ajoute RIEN d'autre — ni texte, ni balise markdown, ni explication) :
{{
  "tldr": "Rédige ici un résumé exécutif global (3 phrases max) des innovations majeures de cette liste.",
  "retained": [
    {{
      "id": [ID numérique de l'article fourni dans le prompt, ex: 0, 1, 2],
      "score": [Note de 1 à 5],
      "justification": "Explication courte...",
      "tags": ["tag1", "tag2"]
    }}
  ]
}}"""


SYSTEM_PROMPT: str = _build_system_prompt(TARGET_COMPANIES)

# ---------------------------------------------------------------------------
# Initialisation du client Gemini
# ---------------------------------------------------------------------------

class GeminiUnavailableError(RuntimeError):
    """Levée quand l'API Gemini est inaccessible ou mal configurée."""


def _build_model(model_name: str) -> genai.GenerativeModel:
    """Crée une instance GenerativeModel avec la config standard du projet.

    Pour les modèles Gemma (open-weights), on n'attache PAS le system_instruction
    via le constructeur : Gemma le supporte mais via un autre mécanisme. Le prompt
    système sera alors préfixé manuellement au prompt utilisateur dans le call site.
    """
    generation_config = genai.GenerationConfig(
        temperature=0.1,
        top_p=0.95,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH",        "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",  "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",  "threshold": "BLOCK_NONE"},
    ]
    kwargs: dict[str, Any] = {
        "model_name":        model_name,
        "generation_config": generation_config,
        "safety_settings":   safety_settings,
    }
    if not _is_gemma(model_name):
        kwargs["system_instruction"] = SYSTEM_PROMPT
    return genai.GenerativeModel(**kwargs)


def _build_full_chain() -> list[str]:
    """Retourne la chaîne complète d'essai : modèle principal + tous les fallbacks.
    Dédoublonné en préservant l'ordre."""
    return list(dict.fromkeys([GEMINI_MODEL, *FALLBACK_CHAIN]))


def _normalize_model_id(name: str) -> str:
    """Strip le préfixe 'models/' parfois renvoyé par list_models()."""
    return name.split("/")[-1] if "/" in name else name


# Mots-clés indiquant un modèle SPÉCIALISÉ inutilisable pour le scoring/résumé
# (génération image, TTS, robotique, deep research, computer-use, etc.).
# Présents dans list_models() mais hors-périmètre de notre pipeline de filtrage.
_SPECIALIZED_MODEL_KEYWORDS: tuple[str, ...] = (
    "image", "tts", "robotics", "lyria", "deep-research",
    "computer-use", "customtools", "nano-banana", "vision-only",
)


def _discover_available_models() -> list[str]:
    """Découvre dynamiquement les modèles Gemini accessibles à la clé API.

    Appelle genai.list_models() pour énumérer toutes les ressources visibles
    par la clé courante. Filtre :
      - celles qui n'exposent PAS generateContent (modèles embedding pur, etc.)
      - celles dont le nom contient un mot-clé spécialisé (image, tts, robotics…)
        — ces modèles sont accessibles mais hors-périmètre pour notre pipeline
        de scoring textuel + résumé.
    Trie selon _MODEL_PREFERENCE (ordre projet) — les modèles inconnus reçoivent
    un poids générique élevé (passent en queue).

    En cas d'erreur réseau ou de réponse vide, on retombe silencieusement sur
    _STATIC_FALLBACK_CHAIN — le pipeline n'est jamais bloqué par une décou-
    verte qui rate.

    Returns:
        Liste de noms de modèles, ordonnés par priorité projet décroissante.
        Garantie non vide tant qu'au moins une fallback statique est définie.
    """
    try:
        models_iter = genai.list_models()
    except (google_exceptions.GoogleAPIError, OSError) as exc:
        logger.warning(
            "⚠️  Découverte dynamique des modèles indisponible (%s) — "
            "utilisation de la liste statique de secours.",
            exc,
        )
        return list(_STATIC_FALLBACK_CHAIN)

    discovered: list[str] = []
    skipped_specialized: list[str] = []
    for m in models_iter:
        # Ne retient que les modèles qui supportent l'inférence texte
        methods = getattr(m, "supported_generation_methods", None) or []
        if "generateContent" not in methods:
            continue
        name = _normalize_model_id(getattr(m, "name", ""))
        if not name:
            continue
        # Exclut les modèles spécialisés (image, tts, robotics, etc.)
        name_lower = name.lower()
        if any(kw in name_lower for kw in _SPECIALIZED_MODEL_KEYWORDS):
            skipped_specialized.append(name)
            continue
        discovered.append(name)

    if skipped_specialized:
        logger.debug(
            "⏭️  %d modèle(s) spécialisé(s) exclu(s) de la cascade : %s",
            len(skipped_specialized), skipped_specialized[:5],
        )

    if not discovered:
        logger.warning(
            "⚠️  list_models() n'a retourné aucun modèle compatible — "
            "utilisation de la liste statique de secours."
        )
        return list(_STATIC_FALLBACK_CHAIN)

    # Tri stable : poids _MODEL_PREFERENCE puis ordre alphabétique
    def _sort_key(model_id: str) -> tuple[int, str]:
        return (_MODEL_PREFERENCE.get(model_id, 100), model_id)

    ordered = sorted(set(discovered), key=_sort_key)
    logger.info(
        "🔎 Découverte de %d modèle(s) Gemini accessible(s) — "
        "ordre de cascade choisi : %s",
        len(ordered),
        ordered[:6] + (["..."] if len(ordered) > 6 else []),
    )
    return ordered


def _init_client() -> genai.GenerativeModel:
    """
    Initialise le modèle Gemini avec auto-fallback en cascade.

    Tente d'abord GEMINI_MODEL ; bascule automatiquement sur le prochain modèle
    de FALLBACK_CHAIN en cas d'indisponibilité (NotFound / PermissionDenied).
    Lève GeminiUnavailableError si la clé API est absente ou si TOUS les modèles
    de la chaîne échouent.
    """
    global _active_model, _active_model_name

    if not _API_KEY:
        raise GeminiUnavailableError(
            "Variable d'environnement GEMINI_API_KEY manquante. "
            "Obtenez une clé sur https://aistudio.google.com et exportez-la :\n"
            "  export GEMINI_API_KEY='votre_cle'"
        )
    genai.configure(api_key=_API_KEY)

    # Peuple FALLBACK_CHAIN dynamiquement avec TOUS les modèles accessibles à
    # cette clé API, triés par préférence projet. Si list_models() échoue, on
    # garde la chaîne statique. _build_full_chain() utilise FALLBACK_CHAIN.
    global FALLBACK_CHAIN, FALLBACK_MODEL
    discovered = _discover_available_models()
    if discovered:
        FALLBACK_CHAIN = discovered
        FALLBACK_MODEL = discovered[0] if discovered else FALLBACK_MODEL

    chain = _build_full_chain()
    last_exc: Exception | None = None
    for idx, candidate in enumerate(chain):
        try:
            _active_model = _build_model(candidate)
            _active_model_name = candidate
            level_label = "principal" if idx == 0 else f"fallback #{idx}"
            logger.info("🤖 Modèle Gemini initialisé (%s) : %s", level_label, candidate)
            return _active_model
        except (google_exceptions.NotFound, google_exceptions.PermissionDenied) as exc:
            last_exc = exc
            logger.warning(
                "⚠️  Modèle '%s' indisponible (%s) — essai du suivant",
                candidate, exc,
            )
            continue
    raise GeminiUnavailableError(
        f"Aucun modèle de la chaîne {chain} n'est accessible : {last_exc}"
    ) from last_exc


def _swap_to_fallback_model() -> genai.GenerativeModel | None:
    """
    Avance d'un cran dans FALLBACK_CHAIN par rapport au modèle actif.
    Retourne le nouveau modèle, ou None si la chaîne est épuisée
    (déjà sur le dernier fallback, ou tous les modèles ultérieurs échouent à s'initialiser).
    """
    global _active_model, _active_model_name

    chain = _build_full_chain()
    try:
        current_idx = chain.index(_active_model_name)
    except ValueError:
        # Cas tordu : le modèle actif n'est plus dans la chaîne (config mutée à chaud).
        # On repart à l'index 0 — peut éventuellement re-tenter le principal.
        current_idx = -1

    # Tente chaque fallback restant jusqu'à en trouver un qui s'initialise.
    for next_idx in range(current_idx + 1, len(chain)):
        next_model = chain[next_idx]
        logger.warning(
            "🔄 Bascule automatique du modèle '%s' → '%s' (quotas/disponibilité)",
            _active_model_name, next_model,
        )
        try:
            _active_model = _build_model(next_model)
            _active_model_name = next_model
            return _active_model
        except google_exceptions.GoogleAPIError as exc:
            logger.error("❌ Bascule vers '%s' impossible : %s", next_model, exc)
            continue
    logger.error("🚫 Chaîne de fallback épuisée — plus aucun modèle disponible.")
    return None


def _extract_retry_seconds(exc: Exception) -> float | None:
    """Extrait la valeur 'Please retry in X s' du message d'erreur 429.

    Tolère plusieurs formats observés côté google-api-core :
      - 'Please retry in 27.5s'
      - 'retry in 27 seconds'
      - 'retry_delay { seconds: 27 }'
    """
    text = str(exc)
    patterns = (
        r"retry in\s+(\d+(?:\.\d+)?)\s*s(?:econds?)?\b",
        r"retry_delay\s*\{\s*seconds:\s*(\d+)",
        r"\bretryDelay['\":\s]+(\d+(?:\.\d+)?)s",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Construction du prompt utilisateur
# ---------------------------------------------------------------------------

def _build_user_prompt(articles: list[dict[str, Any]], offset: int = 0) -> str:
    """
    Sérialise une liste d'articles en texte numéroté pour le prompt.

    Args:
        articles: liste de dicts article (champs: title, summary, source, link)
        offset:   décalage d'index pour conserver les IDs globaux dans les batchs

    Returns:
        Chaîne de texte prête à envoyer au modèle.
    """
    lines: list[str] = [
        f"Analyse les {len(articles)} articles suivants. "
        f"Les indices commencent à {offset}.\n"
    ]
    for i, art in enumerate(articles):
        title   = art.get("title", "").strip() or "(sans titre)"
        summary = art.get("summary", "").strip()
        source  = art.get("source", "")
        # Résumé tronqué à 400 caractères pour maîtriser les tokens
        summary_short = (summary[:400] + "…") if len(summary) > 400 else summary

        lines.append(
            f"[{offset + i}] SOURCE: {source}\n"
            f"    TITRE: {title}\n"
            f"    RÉSUMÉ: {summary_short or '(non disponible)'}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing de la réponse JSON
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict[str, Any]:
    """
    Extrait et valide le JSON retourné par Gemini.
    Tolère les éventuelles balises markdown résiduelles.

    Valide la présence des clés "retained" ET "tldr".
    Injecte un "tldr" vide par défaut si le modèle l'a omis (dégradation gracieuse).

    Returns:
        Dictionnaire validé contenant au minimum les clés "retained" et "tldr".

    Raises:
        ValueError si le JSON est absent, malformé, ou si "retained" est manquant.
    """
    # Mode JSON natif : la réponse doit être un JSON pur. Parsing direct d'abord.
    cleaned = raw.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Filet de sécurité : si Gemini a quand même ajouté un wrapper markdown,
        # ou si la réponse est tronquée, on tente une extraction par regex
        stripped = re.sub(r"```(?:json)?\s*", "", cleaned).replace("```", "").strip()
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise ValueError(f"Aucun JSON trouvé dans la réponse :\n{raw[:500]}")
        parsed = json.loads(match.group())

    # Validation de la clé obligatoire "retained"
    if "retained" not in parsed:
        raise ValueError(f"Clé 'retained' absente du JSON : {parsed}")
    if not isinstance(parsed["retained"], list):
        raise ValueError("'retained' doit être une liste.")

    # Validation stricte de la clé "tldr" exigée dans le prompt
    if "tldr" not in parsed:
        raise ValueError(f"Clé 'tldr' absente du JSON : {parsed}")
    elif not isinstance(parsed["tldr"], str):
        logger.warning(
            "⚠️  Clé 'tldr' de type inattendu (%s) — conversion forcée en chaîne.",
            type(parsed["tldr"]).__name__,
        )
        parsed["tldr"] = str(parsed["tldr"])

    return parsed


# ---------------------------------------------------------------------------
# Appel API avec retry
# ---------------------------------------------------------------------------

class _GeminiCallResult(NamedTuple):
    """Résultat d'un appel Gemini : texte + flag de troncature.

    `truncated` est True si l'API a indiqué finish_reason == MAX_TOKENS,
    c'est-à-dire que la génération s'est arrêtée parce qu'on a atteint
    `max_output_tokens`. Dans ce cas le JSON peut être malformé (coupé en
    plein milieu) et le caller (typiquement `_process_batch`) doit splitter
    le batch en deux et réessayer chaque moitié.
    """
    text: str
    truncated: bool


def _is_response_truncated(response: Any) -> bool:
    """Détecte si une réponse Gemini a été tronquée pour cause de tokens max.

    Robuste face à plusieurs représentations possibles de finish_reason
    selon la version du SDK : enum (.name == "MAX_TOKENS"), int (== 2),
    ou string brute.
    """
    try:
        finish_reason = response.candidates[0].finish_reason
    except (IndexError, AttributeError, TypeError):
        return False
    name = getattr(finish_reason, "name", None)
    if name and "MAX_TOKENS" in str(name).upper():
        return True
    if "MAX_TOKENS" in str(finish_reason).upper():
        return True
    return finish_reason == 2


def _call_gemini_with_retry(
    model: genai.GenerativeModel,
    prompt: str,
    max_retries: int = 3,
    backoff: float = 2.0,
    json_mode: bool = False,
    prefix_system_prompt: bool = True,
) -> _GeminiCallResult:
    """
    Appelle l'API Gemini avec gestion des erreurs, retry et bascule en cascade.

    Gère :
      - ResourceExhausted (quota / rate limit) → retry avec attente, puis bascule
        au modèle suivant de FALLBACK_CHAIN si la cartouche actuelle est épuisée.
      - ServiceUnavailable / DeadlineExceeded   → retry exponentiel
      - NotFound / PermissionDenied             → bascule immédiate au suivant
      - InvalidArgument                         → erreur fatale (mauvais prompt)

    Args:
        json_mode: si True, force response_mime_type=application/json pour
                   garantir un JSON pur sans fences markdown.
                   IGNORÉ pour les modèles Gemma (open-weights) qui ne supportent
                   pas ce mime_type — un filet regex post-parsing rattrape les
                   éventuels wrappers markdown.
        prefix_system_prompt: si True (défaut), pour les modèles Gemma le
                   SYSTEM_PROMPT (qui force la sortie JSON {tldr, retained})
                   est préfixé au prompt utilisateur. À mettre à False pour les
                   appels qui attendent du texte libre (ex: résumé exécutif),
                   sinon Gemma génère un texte ET un dump JSON pollué.

    Returns:
        `_GeminiCallResult(text, truncated)` :
        - `text` : texte brut de la réponse du modèle
        - `truncated` : True si la réponse a été coupée pour cause de
          max_output_tokens atteint (le JSON peut alors être malformé).

    Raises:
        GeminiUnavailableError si toute la chaîne est épuisée.
    """
    # Le model passé en param peut devenir périmé si on bascule en cascade ;
    # on travaille toujours sur la référence module à jour.
    current_model = _active_model or model

    def _build_gen_config() -> "genai.GenerationConfig | None":
        # Gemma ne supporte pas response_mime_type=application/json
        if json_mode and not _is_gemma(_active_model_name):
            return genai.GenerationConfig(
                temperature=0.1,
                top_p=0.95,
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
            )
        return None

    def _build_effective_prompt() -> str:
        # Gemma n'accepte pas system_instruction au niveau du constructeur ;
        # on préfixe le prompt système manuellement — sauf quand l'appelant
        # demande explicitement un prompt nu (ex: résumé exécutif texte libre).
        if _is_gemma(_active_model_name) and prefix_system_prompt:
            return f"{SYSTEM_PROMPT}\n\n---\n\n{prompt}"
        return prompt

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            response = current_model.generate_content(
                _build_effective_prompt(),
                request_options={"timeout": GEMINI_TIMEOUT},
                generation_config=_build_gen_config(),
            )
            if not response.candidates:
                raise GeminiUnavailableError(
                    f"Réponse vide ou bloquée par les filtres de sécurité. "
                    f"Finish reason: {response.prompt_feedback}"
                )
            return _GeminiCallResult(
                text=response.text,
                truncated=_is_response_truncated(response),
            )

        except google_exceptions.ResourceExhausted as exc:
            retry_after = _extract_retry_seconds(exc)
            if retry_after is not None:
                wait = retry_after + _QUOTA_RETRY_MARGIN
                logger.warning(
                    "⏳ Quota Gemini atteint sur '%s' (tentative %d/%d) — pause autonome de %.1f s "
                    "(retry_in indiqué par l'API + %.0fs marge)",
                    _active_model_name, attempt, max_retries, wait, _QUOTA_RETRY_MARGIN,
                )
            else:
                wait = _DEFAULT_QUOTA_PAUSE
                logger.warning(
                    "⏳ Quota Gemini atteint sur '%s' (tentative %d/%d) — pause autonome de %.0f s "
                    "(délai non communiqué par l'API, attente d'un cycle minute complet)",
                    _active_model_name, attempt, max_retries, wait,
                )
            if attempt < max_retries:
                time.sleep(wait)
            else:
                # Cartouche épuisée sur le modèle actif → tente le suivant dans la chaîne.
                new_model = _swap_to_fallback_model()
                if new_model is not None:
                    current_model = new_model
                    attempt = 0  # cycle de retry complet sur le nouveau modèle

        except (google_exceptions.ServiceUnavailable,
                google_exceptions.DeadlineExceeded) as exc:
            wait = backoff ** attempt
            logger.warning(
                "🔌 Service Gemini indisponible (tentative %d/%d) — attente %.0f s : %s",
                attempt, max_retries, wait, exc,
            )
            if attempt < max_retries:
                time.sleep(wait)

        except google_exceptions.InvalidArgument as exc:
            logger.error("❌ Argument invalide (prompt trop long ?) : %s", exc)
            raise GeminiUnavailableError(f"Argument invalide : {exc}") from exc

        except (google_exceptions.NotFound, google_exceptions.PermissionDenied) as exc:
            # Modèle courant inutilisable : avance dans la chaîne.
            new_model = _swap_to_fallback_model()
            if new_model is not None:
                current_model = new_model
                attempt = 0
                continue
            logger.error("❌ Erreur API Gemini (chaîne épuisée) : %s", exc)
            raise GeminiUnavailableError(f"Erreur API Gemini : {exc}") from exc

        except google_exceptions.GoogleAPIError as exc:
            logger.error("❌ Erreur API Gemini : %s", exc)
            raise GeminiUnavailableError(f"Erreur API Gemini : {exc}") from exc

    raise GeminiUnavailableError(
        f"API Gemini inaccessible après {max_retries} tentatives "
        f"(modèle final : {_active_model_name})."
    )


# ---------------------------------------------------------------------------
# Traitement par batch
# ---------------------------------------------------------------------------

def _process_batch(
    model: genai.GenerativeModel,
    batch: list[dict[str, Any]],
    offset: int,
    depth: int = 0,
) -> dict[str, Any]:
    """
    Envoie un batch d'articles à Gemini et retourne le JSON parsé.

    En cas de troncature détectée (finish_reason == MAX_TOKENS) ou de JSON
    malformé qui ressemble à une troncature, le batch est splitté en deux
    et chaque moitié relancée récursivement (jusqu'à `_MAX_BATCH_SPLIT_DEPTH`).
    Cela permet à `AI_BATCH_SIZE` d'être un knob souple : l'utilisateur peut
    laisser 20 par défaut sans risquer de perdre un batch sur un article
    pathologique (description très longue, etc.).

    Args:
        depth: profondeur récursive courante. À 0 lors du premier appel.
               Le caller passe depth+1 quand il splitte.

    Returns:
        Dict avec clés `retained`, `tldr`, `rejected_count`, et au besoin
        `model_notes`. La clé "tldr" est toujours présente.
    """
    prompt = _build_user_prompt(batch, offset=offset)
    can_split = depth < _MAX_BATCH_SPLIT_DEPTH and len(batch) > 1

    try:
        gemini_result = _call_gemini_with_retry(model, prompt, json_mode=True)

        # Cas troncature détectée par finish_reason : split direct sans tenter
        # de parser le JSON probablement coupé (gain de temps).
        if gemini_result.truncated and can_split:
            logger.warning(
                "✂️  Batch offset=%d tronqué par MAX_TOKENS (%d articles, depth=%d) "
                "— split en 2 et retry",
                offset, len(batch), depth,
            )
            return _split_and_retry_batch(model, batch, offset, depth)

        result = _parse_json_response(gemini_result.text)
        partial_note = " ⚠️ partiel (tokens max)" if gemini_result.truncated else ""
        logger.info(
            "   └─ Batch offset=%d (depth=%d) : %d retenu(s) / %d total%s",
            offset, depth,
            len(result.get("retained", [])),
            len(batch),
            partial_note,
        )
        return result

    except (ValueError, json.JSONDecodeError) as exc:
        # JSON malformé : possiblement aussi dû à une troncature (le SDK peut
        # ne pas remonter MAX_TOKENS dans tous les cas). Tenter le split-retry.
        if can_split:
            logger.warning(
                "📉 JSON malformé batch offset=%d (depth=%d, %d articles) "
                "— split-retry pour récupérer ce qu'on peut : %s",
                offset, depth, len(batch), exc,
            )
            return _split_and_retry_batch(model, batch, offset, depth)
        logger.error(
            "⚠️  JSON malformé pour le batch offset=%d (depth=%d, abandon) : %s",
            offset, depth, exc,
        )
        return {
            "retained": [],
            "tldr": "",
            "rejected_count": len(batch),
            "model_notes": f"Erreur parsing : {exc}",
        }

    except GeminiUnavailableError as exc:
        logger.error("🚫 API indisponible pour le batch offset=%d : %s", offset, exc)
        return {
            "retained": [],
            "tldr": "",
            "rejected_count": len(batch),
            "model_notes": f"API indisponible : {exc}",
        }


def _split_and_retry_batch(
    model: genai.GenerativeModel,
    batch: list[dict[str, Any]],
    offset: int,
    current_depth: int,
) -> dict[str, Any]:
    """Split un batch en deux moitiés et relance _process_batch sur chaque,
    avec depth incrémenté. Agrège les `retained` et somme les `rejected_count`.

    Les offsets sont calculés pour que les `id` des articles dans la réponse
    Gemini restent cohérents avec l'index global de la liste articles.
    """
    mid = len(batch) // 2
    next_depth = current_depth + 1
    left  = _process_batch(model, batch[:mid],  offset,         depth=next_depth)
    right = _process_batch(model, batch[mid:],  offset + mid,   depth=next_depth)
    return {
        "retained": list(left.get("retained", [])) + list(right.get("retained", [])),
        "tldr": "",
        "rejected_count": (
            int(left.get("rejected_count", 0)) + int(right.get("rejected_count", 0))
        ),
        "model_notes": f"split-retry agrégé (depth={next_depth})",
    }


# ---------------------------------------------------------------------------
# Vérification Python côté post-processing — concurrents
# ---------------------------------------------------------------------------

def _force_company_scores(
    retained_entries: list[dict[str, Any]],
    all_articles: list[dict[str, Any]],
    companies: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Vérification Python systématique après le scoring IA.

    Si le titre ou le résumé d'un article retenu contient le nom d'une entreprise
    présente dans targets.json, son score est forcé à 4 minimum (5 si déjà ≥ 5).
    Cette règle est un filet de sécurité qui s'applique indépendamment du scoring IA,
    garantissant qu'aucun article concurrent ne passe sous le radar.

    Args:
        retained_entries: liste des entrées {"id", "score", ...} retournées par Gemini.
        all_articles:     liste globale des articles originaux (pour accéder au texte).
        companies:        liste d'entreprises à surveiller (défaut: TARGET_COMPANIES).

    Returns:
        Liste retained_entries avec les scores potentiellement rehaussés.
    """
    companies = companies or TARGET_COMPANIES
    if not companies:
        return retained_entries

    companies_lower = [c.lower() for c in companies]

    for entry in retained_entries:
        article_idx = entry.get("id", -1)
        if not (0 <= article_idx < len(all_articles)):
            continue

        original = all_articles[article_idx]
        haystack = (
            original.get("title", "") + " " + original.get("summary", "")
        ).lower()

        matched_company = next(
            (c for c in companies_lower if c in haystack), None
        )

        if matched_company:
            current_score = int(entry.get("score", 1))
            if current_score < 4:
                entry["score"] = 4
                logger.info(
                    "🏢 Score forcé à 4 pour article #%d — mention entreprise cible détectée : '%s'",
                    article_idx, matched_company,
                )
            # On s'assure aussi que le tag de l'entreprise est présent
            # (au cas où le modèle l'aurait oublié malgré les instructions)
            original_case_company = next(
                (c for c in companies if c.lower() == matched_company), matched_company
            )
            existing_tags = [t.lower() for t in entry.get("tags", [])]
            if original_case_company.lower() not in existing_tags:
                entry.setdefault("tags", []).append(original_case_company)
                logger.debug("🏷️  Tag '%s' ajouté automatiquement à l'article #%d", original_case_company, article_idx)

    return retained_entries


# ---------------------------------------------------------------------------
# Synthèse finale — Executive Summary global
# ---------------------------------------------------------------------------

def _generate_executive_summary(
    model: genai.GenerativeModel,
    retained_articles: list[dict[str, Any]],
) -> str:
    """
    Génère un Executive Summary global via un appel Gemini dédié, à partir de
    l'ensemble des articles retenus (tous batchs confondus).

    Cette synthèse est distincte des tldrs par batch : elle a une vision
    complète de la sélection finale et produit un résumé cohérent des
    tendances majeures de la semaine.

    OPTIMISATION QUALITÉ : le résumé exécutif est UN SEUL appel mais c'est
    le plus visible du digest. Plutôt que d'utiliser le modèle qui termine
    la cascade (souvent un Gemma dégradé après plusieurs basculements de
    quota), on tente d'abord le meilleur modèle "Pro" disponible — quitte
    à basculer dans la cascade en cas de quota épuisé. Voir
    _PREMIUM_SUMMARY_MODELS pour l'ordre d'essai.

    Args:
        model:            instance du modèle Gemini déjà initialisée.
        retained_articles: liste finale des articles retenus (après scoring et tri).

    Returns:
        Chaîne de 3 phrases maximum, ou message de repli en cas d'échec.
    """
    if not retained_articles:
        return "Aucune innovation majeure identifiée cette semaine."

    # On se limite aux 30 meilleurs articles (les plus hauts scores en premier)
    # pour rester dans les limites de tokens et éviter la dilution du résumé
    top_articles = retained_articles[:30]

    items_text = "\n".join(
        f"[{i + 1}] Score {art.get('score', '?')}/5 — {art.get('title', '').strip()} "
        f"({art.get('source', '')})\n"
        f"     {art.get('summary', '')[:180].strip()}"
        for i, art in enumerate(top_articles)
    )

    summary_prompt = (
        "Tu es un analyste technique senior en science des matériaux et revêtements de surface. "
        "En te basant UNIQUEMENT sur les articles ci-dessous, rédige en français "
        "un Executive Summary global de 3 phrases MAXIMUM (entre 60 et 90 mots).\n\n"
        "Ce résumé doit :\n"
        "  - Synthétiser les tendances technologiques majeures observées\n"
        "  - Mentionner 1-2 percées scientifiques les plus marquantes\n"
        "  - Signaler tout mouvement notable de concurrents industriels (s'il y en a)\n\n"
        "RÈGLES STRICTES DE FORMAT — ne pas déroger :\n"
        "  - Réponds UNIQUEMENT avec le texte du résumé en prose, en français\n"
        "  - PAS de liste à puces, PAS de numérotation, PAS de titre\n"
        "  - PAS d'objet JSON, PAS de balises markdown ```...```\n"
        "  - PAS d'introduction du type 'Voici le résumé', 'Cette semaine'\n"
        "  - Commence directement par la première phrase du résumé\n\n"
        f"Articles à synthétiser :\n\n{items_text}"
    )

    # Tentative en cascade premium : on essaie d'abord les Pro (quotas free
    # restreints mais qualité maximale), puis Flash, puis on tombe sur le
    # modèle actif standard. Chaque échec (404/403/quota) bascule au suivant
    # SANS toucher à _active_model_name (on ne veut pas casser la cascade
    # principale du scoring si on revenait scorer un nouveau batch).
    saved_active_name = _active_model_name
    saved_active_model = _active_model
    raw_text: str | None = None
    last_error: str = ""
    try:
        for premium in _PREMIUM_SUMMARY_MODELS:
            try:
                premium_model = _build_model(premium)
            except (google_exceptions.NotFound, google_exceptions.PermissionDenied) as exc:
                last_error = f"{premium}: not_available ({exc})"
                continue

            # Bascule temporaire — _call_gemini_with_retry lit _active_model_name
            # pour décider si Gemma ou Gemini, etc.
            globals()["_active_model"] = premium_model
            globals()["_active_model_name"] = premium

            try:
                logger.info("📝 Résumé exécutif : tentative avec %s (premium)…", premium)
                call_result = _call_gemini_with_retry(
                    premium_model, summary_prompt, max_retries=1,
                    prefix_system_prompt=False,
                )
                raw_text = call_result.text
                if call_result.truncated:
                    logger.info("   └─ Résumé exécutif tronqué par tokens max — utilisé tel quel")
                logger.info("✅ Résumé exécutif généré par %s", premium)
                break
            except GeminiUnavailableError as exc:
                last_error = f"{premium}: {exc}"
                logger.info("   └─ %s indisponible, essai du suivant", premium)
                continue
    finally:
        # Restaure le modèle actif standard pour ne pas perturber un éventuel
        # appel futur (et pour que _meta.model reflète bien le modèle scoring).
        globals()["_active_model"] = saved_active_model
        globals()["_active_model_name"] = saved_active_name

    if raw_text is None:
        # Aucun premium n'a marché — fallback ultime : modèle actif standard
        try:
            logger.info("📝 Résumé exécutif : fallback sur modèle standard %s", saved_active_name)
            call_result = _call_gemini_with_retry(
                model, summary_prompt, max_retries=2, prefix_system_prompt=False,
            )
            raw_text = call_result.text
        except GeminiUnavailableError as exc:
            logger.warning("⚠️  Impossible de générer le résumé exécutif (premium et fallback): %s — %s",
                           last_error, exc)
            return ""

    summary = _sanitize_executive_summary(raw_text)
    logger.info("✅ Résumé exécutif généré (%d caractères)", len(summary))
    return summary


def _sanitize_executive_summary(raw: str) -> str:
    """Nettoie la sortie du résumé exécutif en cas de pollution par un modèle bavard.

    Gemma a tendance à ajouter un dump JSON après le texte (résidu de
    l'instruction système qu'il a vu auparavant). On coupe à la première
    apparition d'une fence markdown ou d'un objet JSON.
    """
    text = raw.strip()
    # Coupe avant toute fence markdown ```json/```
    fence_pos = text.find("```")
    if fence_pos >= 0:
        text = text[:fence_pos].rstrip()
    # Coupe avant un objet JSON commençant par {"tldr" ou {"retained"
    for marker in ('{"tldr"', '{ "tldr"', '{"retained"', '{ "retained"'):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx].rstrip()
    # Coupe avant un préfixe ré-introductif fréquent type "Voici un résumé :"
    intro_patterns = (
        r"^\s*(voici|en r[ée]sum[ée]|cette semaine|en synth[èe]se)[\s,.:]*",
    )
    for pat in intro_patterns:
        text = re.sub(pat, "", text, count=1, flags=re.IGNORECASE).lstrip()
    return text.strip()


# ---------------------------------------------------------------------------
# Fonction principale
# ---------------------------------------------------------------------------

def filter_articles_with_ai(
    articles: list[dict[str, Any]],
    min_score: int = 1,
) -> dict[str, Any]:
    """
    Filtre et note une liste d'articles via Gemini Flash.

    Les articles sont traités par batch pour rester dans les limites de tokens.
    Les résultats de chaque batch sont fusionnés.
    Une vérification Python post-processing force les scores des articles
    mentionnant des entreprises cibles à un minimum de 4.
    Un Executive Summary global est généré via un appel Gemini dédié.

    Args:
        articles:  liste de dicts article issus de scraper.py
        min_score: note minimale pour inclure un article dans les résultats
                   (1 = tout garder, 4 = uniquement les innovations majeures)

    Returns:
        Dictionnaire structuré :
        {
          "meta": {
            "run_at":          "2026-04-29T...",
            "model":           "gemini-2.5-flash",
            "input_count":     42,
            "retained_count":  18,
            "rejected_count":  24,
            "batch_count":     3,
            "min_score_filter": 1,
            "tldr":            "Executive Summary global généré par IA."
          },
          "articles": [
            {
              "score":         4,
              "justification": "...",
              "tags":          ["PVD", "alliage"],
              "title":         "...",
              "link":          "...",
              "summary":       "...",
              "source":        "...",
              "category":      "...",
              "collected_at":  "..."
            },
            ...
          ]
        }

    Raises:
        GeminiUnavailableError si l'API n'est pas configurée.
    """
    if not articles:
        logger.warning("Aucun article à filtrer.")
        return _empty_result(0, min_score)

    # Initialisation du client (lève GeminiUnavailableError si clé absente)
    model = _init_client()

    # Découpage en batchs
    batches = [
        articles[i : i + AI_BATCH_SIZE]
        for i in range(0, len(articles), AI_BATCH_SIZE)
    ]
    logger.info(
        "📤 Envoi de %d article(s) en %d batch(s) vers %s",
        len(articles), len(batches), GEMINI_MODEL,
    )

    # -----------------------------------------------------------------------
    # Accumulateurs — tous initialisés AVANT la boucle
    # -----------------------------------------------------------------------
    all_retained: list[dict[str, Any]] = []
    total_rejected: int = 0

    for batch_idx, batch in enumerate(batches):
        offset = batch_idx * AI_BATCH_SIZE
        logger.info(
            "🔄 Batch %d/%d (articles %d–%d)…",
            batch_idx + 1, len(batches), offset, offset + len(batch) - 1,
        )

        batch_result  = _process_batch(model, batch, offset)
        retained_raw  = batch_result.get("retained", [])
        total_rejected += batch_result.get("rejected_count", len(batch) - len(retained_raw))

        # ── Vérification Python post-processing : force score ≥ 4 si concurrent ──
        retained_raw = _force_company_scores(retained_raw, articles)

        # Fusion : on rattache les données originales de l'article
        for entry in retained_raw:
            article_idx = entry.get("id", -1)
            # Vérification que l'index est dans le batch courant
            if not (offset <= article_idx < offset + len(batch)):
                logger.warning(
                    "Index %d hors-plage pour batch offset=%d, ignoré.",
                    article_idx, offset,
                )
                continue

            original = articles[article_idx]
            score    = int(entry.get("score", 1))

            if score < min_score:
                logger.debug(
                    "Article #%d score=%d < min_score=%d, ignoré.",
                    article_idx, score, min_score,
                )
                total_rejected += 1
                continue

            all_retained.append({
                # Métadonnées IA
                "score":         score,
                "justification": entry.get("justification", ""),
                "tags":          list(dict.fromkeys(entry.get("tags", []))),
                # Données originales de l'article
                "title":         original.get("title", ""),
                "link":          original.get("link", ""),
                "summary":       original.get("summary", ""),
                "source":        original.get("source", ""),
                "category":      original.get("category", ""),
                "collected_at":  original.get("collected_at", ""),
            })

        # Pause courtoise entre les batchs pour respecter les rate limits
        if batch_idx < len(batches) - 1:
            time.sleep(1.5)

    # Tri décroissant par score (avant la génération du résumé pour que le top soit en premier)
    all_retained.sort(key=lambda x: x["score"], reverse=True)

    # ── Génération du résumé exécutif global via un appel Gemini dédié ──────
    # Cette synthèse remplace la simple concaténation des tldrs par batch :
    # elle dispose d'une vue complète de la sélection finale et produit
    # un résumé de tendances cohérent et non redondant.
    executive_summary = _generate_executive_summary(model, all_retained)

    result = {
        "meta": {
            "run_at":           datetime.now(timezone.utc).isoformat(),
            "model":            _active_model_name,
            "input_count":      len(articles),
            "retained_count":   len(all_retained),
            "rejected_count":   total_rejected,
            "batch_count":      len(batches),
            "min_score_filter": min_score,
            "tldr":             executive_summary,
        },
        "articles": all_retained,
    }

    logger.info(
        "✅ Filtrage IA terminé — %d retenu(s) / %d rejeté(s) (score ≥ %d)",
        len(all_retained), total_rejected, min_score,
    )
    return result


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _empty_result(input_count: int, min_score: int) -> dict[str, Any]:
    return {
        "meta": {
            "run_at":           datetime.now(timezone.utc).isoformat(),
            "model":            _active_model_name,
            "input_count":      input_count,
            "retained_count":   0,
            "rejected_count":   input_count,
            "batch_count":      0,
            "min_score_filter": min_score,
            "tldr":             "",
        },
        "articles": [],
    }


def score_distribution(result: dict[str, Any]) -> dict[int, int]:
    """
    Retourne la distribution des scores (utile pour les stats / logs).

    Example:
        {5: 2, 4: 7, 3: 5, 2: 3, 1: 1}
    """
    dist: dict[int, int] = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for art in result.get("articles", []):
        s = art.get("score", 0)
        if s in dist:
            dist[s] += 1
    return dist


# ---------------------------------------------------------------------------
# Exécution directe (debug / test rapide)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Chargement d'un fichier scraper_output.json existant pour le test
    data_path = os.path.join(os.path.dirname(__file__), "../data/scraper_output.json")

    if not os.path.exists(data_path):
        print(
            "❌ Fichier introuvable : data/scraper_output.json\n"
            "   Lancez d'abord : python src/scraper.py"
        )
        sys.exit(1)

    try:
        with open(data_path, encoding="utf-8") as fh:
            scraper_data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ Erreur lors du chargement de scraper_output.json : {exc}")
        sys.exit(1)

    raw_articles = scraper_data.get("articles", [])
    print(f"📂 {len(raw_articles)} articles chargés depuis scraper_output.json")

    if not _API_KEY:
        print(
            "\n⚠️  GEMINI_API_KEY non définie.\n"
            "   Exportez votre clé puis relancez :\n"
            "   export GEMINI_API_KEY='AIza...'\n"
            "   python src/ai_filter.py"
        )
        sys.exit(1)

    # Filtrage IA (garder les articles score ≥ 2)
    filtered = filter_articles_with_ai(raw_articles, min_score=2)

    # Affichage console
    meta = filtered["meta"]
    print(f"\n{'='*60}")
    print(f"  Modèle  : {meta['model']}")
    print(f"  Entrée  : {meta['input_count']} articles")
    print(f"  Retenus : {meta['retained_count']}")
    print(f"  Rejetés : {meta['rejected_count']}")
    print(f"  TL;DR   : {meta['tldr'][:120]}…" if meta.get('tldr') else "  TL;DR   : —")
    dist = score_distribution(filtered)
    print(f"  Scores  : { {k: v for k, v in sorted(dist.items(), reverse=True)} }")
    print(f"{'='*60}\n")

    for art in filtered["articles"][:5]:
        stars = "★" * art["score"] + "☆" * (5 - art["score"])
        print(f"[{stars}] {art['title'][:75]}")
        print(f"   Source : {art['source']}")
        print(f"   Tags   : {', '.join(art['tags'])}")
        print(f"   Note   : {art['justification']}")
        print()

    # Sauvegarde JSON
    out_path = os.path.join(os.path.dirname(__file__), "../data/ai_filter_output.json")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(filtered, fh, ensure_ascii=False, indent=2)
        print(f"💾 Résultats sauvegardés → {out_path}")
    except OSError as exc:
        print(f"❌ Erreur lors de la sauvegarde de ai_filter_output.json : {exc}")
        sys.exit(1)
