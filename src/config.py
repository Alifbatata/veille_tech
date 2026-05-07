# =============================================================================
# config.py — Configuration centrale de la veille technologique
# =============================================================================

import json
import os
import logging

# --- Logging (pour la fonction de chargement) ------------------------------- #
# Utilise un logger basique pour que les erreurs de config soient visibles
# même si le logging principal n'est pas encore configuré.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# --- Paramètres statiques --------------------------------------------------- #

# Flux RSS
SOURCES_RSS: list[dict[str, str]] = [
    # ArXiv — Physique appliquée / Science des matériaux / Revêtements
    {
        "name": "ArXiv – Applied Physics",
        "url": "https://arxiv.org/rss/physics.app-ph",
        "category": "science",
    },
    {
        "name": "ArXiv – Materials Science",
        "url": "https://arxiv.org/rss/cond-mat.mtrl-sci",
        "category": "science",
    },
    {
        "name": "MDPI – Coatings",
        "url": "https://www.mdpi.com/rss/journal/coatings",
        "category": "science",
    },
    {
        "name": "IEEE Spectrum",
        "url": "https://spectrum.ieee.org/rss/fulltext",
        "category": "science",
    },
    {
        "name": "ScienceDaily – Materials Science",
        "url": "https://www.sciencedaily.com/rss/matter_energy/materials_science.xml",
        "category": "science",
    },
]

# Paramètres généraux
DATA_DIR: str = "../data"
MAX_ARTICLES_PER_SOURCE: int = 50   # articles récupérés par flux et par run
LANGUAGE: str = "fr"                # langue de synthèse Gemini ("fr" ou "en")
USE_MEMORY: bool = os.environ.get("USE_MEMORY", "false").lower() in ("true", "1", "yes")
# WHY env var : main.py:_memory_choice_step propose à l'utilisateur de choisir
# au lancement (filtrer / tout renvoyer / reset). Le défaut "false" préserve
# le comportement historique pour les runs non-interactifs (CI, cron).
SCRAPE_LIMIT_MONTH: bool = True      # Si True, applique RECENT_DAYS_LIMIT comme fenêtre de fraîcheur
RECENT_DAYS_LIMIT: int = 90          # Fenêtre de fraîcheur (jours). Veille techno de niche → 90j adapté


# --- Paramètres dynamiques (chargés depuis JSON) ---------------------------- #

_cached_targets: tuple[list[str], list[str], list[str], list[str]] | None = None

def load_targets(path: str | None = None) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Charge les 4 listes de cibles depuis data/targets.json :
      - companies     : entreprises industrielles (couplées avec keywords pour GNews)
      - keywords      : mots-clés techniques (couplés avec companies pour GNews +
                        broadcastés dans toutes les sources scientifiques)
      - solo_keywords : phrases multi-mots cherchées SEULES (sans entreprise),
                        broadcastées dans toutes les sources (GNews + scientifiques)
      - research_orgs : labos / universités / instituts de recherche qui PUBLIENT.
                        Broadcastés UNIQUEMENT dans les sources scientifiques
                        (arXiv, OpenAlex, Crossref, HAL, Semantic Scholar, Tavily,
                        Google Patents). PAS dans GNews (peu de couverture presse).

    Retourne des listes vides en cas d'erreur.
    """
    global _cached_targets
    if _cached_targets is not None:
        return _cached_targets

    if path is None:
        # Chemin relatif au fichier config.py -> remonter d'un niveau -> descendre dans data/
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "targets.json"))

    if not os.path.exists(path):
        logger.error("Fichier de cibles non trouvé : %s. Les listes de cibles seront vides.", path)
        _cached_targets = ([], [], [], [])
        return _cached_targets

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Dédoublonnage optimisé (comportement de set) avec préservation de l'ordre
        companies     = list(dict.fromkeys(data.get("companies", [])))
        keywords      = list(dict.fromkeys(data.get("keywords", [])))
        solo_keywords = list(dict.fromkeys(data.get("solo_keywords", [])))
        research_orgs = list(dict.fromkeys(data.get("research_orgs", [])))

        if not companies or not keywords:
            logger.warning("Le fichier %s contient des listes vides pour 'companies' ou 'keywords'.", path)

        # Utilisation d'une variable d'environnement pour empêcher le double log
        # en cas d'imports croisés depuis plusieurs modules au runtime.
        if not os.environ.get("_TARGETS_LOGGED"):
            logger.info(
                "Cibles chargées depuis %s (%d entreprises, %d mots-clés couplés, %d solo, %d labos).",
                path, len(companies), len(keywords), len(solo_keywords), len(research_orgs),
            )
            os.environ["_TARGETS_LOGGED"] = "1"

        _cached_targets = (companies, keywords, solo_keywords, research_orgs)
        return _cached_targets

    except (json.JSONDecodeError, OSError) as e:
        logger.error("Erreur d'accès ou de parsing de %s: %s. Les listes de cibles seront vides.", path, e)
        _cached_targets = ([], [], [], [])
        return _cached_targets

# --- Initialisation des variables globales ---------------------------------- #

TARGET_COMPANIES: list[str]
KEYWORDS: list[str]
SOLO_KEYWORDS: list[str]
RESEARCH_ORGS: list[str]
TARGET_COMPANIES, KEYWORDS, SOLO_KEYWORDS, RESEARCH_ORGS = load_targets()
