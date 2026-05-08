# =============================================================================
# scraper.py — Module de collecte furtif (Optimisé MDPI/GNews/ScienceDaily)
# =============================================================================

from __future__ import annotations
import email.utils
import json
import logging
import os
import random
import re
import time
import certifi
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus
import feedparser
from curl_cffi import requests as curl_requests
from curl_cffi.requests import RequestsError

# Chargement de la config locale
try:
    from config import KEYWORDS, MAX_ARTICLES_PER_SOURCE, SOURCES_RSS, TARGET_COMPANIES, USE_MEMORY, SCRAPE_LIMIT_MONTH, RECENT_DAYS_LIMIT, SOLO_KEYWORDS, RESEARCH_ORGS
except ImportError:
    from src.config import KEYWORDS, MAX_ARTICLES_PER_SOURCE, SOURCES_RSS, TARGET_COMPANIES, USE_MEMORY, SCRAPE_LIMIT_MONTH, RECENT_DAYS_LIMIT, SOLO_KEYWORDS, RESEARCH_ORGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("scraper")

ROTATING_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# Empreintes TLS curl_cffi à faire tourner. WHY chrome124+131+120 : trois versions
# stables avec ciphers/extensions différentes côté curl_impersonate, suffisant pour
# défaire un fingerprinting basique sans casser les WAF stricts (MDPI Cloudflare).
_IMPERSONATE_ROTATION = ["chrome124", "chrome131", "chrome120"]

_ACCEPT_LANGUAGES = [
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,fr;q=0.7",
    "fr,en-US;q=0.9,en;q=0.8",
]

_persistent_session: curl_requests.Session | None = None
_SEEN_URLS_MAX: int = 10000
_SEEN_URLS_PATH = os.path.join(os.path.dirname(__file__), "../data/seen_urls.json")
_CHECKPOINT_PATH = os.path.join(os.path.dirname(__file__), "../data/scraper_checkpoint.json")
_CHECKPOINT_EVERY: int = 5  # Sauvegarde tous les N appels GNews

# Cooldown progressif par domaine en cas de 403/429. Empêche de marteler un site qui
# vient de nous bloquer — on attend de plus en plus longtemps avant de retenter.
_DOMAIN_COOLDOWN: dict[str, float] = {}

def _domain_of(url: str) -> str:
    """Extrait le domaine d'une URL (ex: 'www.mdpi.com')."""
    try:
        return url.split("/")[2].lower()
    except IndexError:
        return "unknown"

def _respect_domain_cooldown(url: str) -> None:
    """Si on a pris un 403/429 récemment sur ce domaine, attend que la pénalité expire."""
    dom = _domain_of(url)
    expires_at = _DOMAIN_COOLDOWN.get(dom, 0.0)
    delta = expires_at - time.monotonic()
    if delta > 0:
        logger.info(f"⏸️  Cooldown actif pour {dom} : attente {delta:.1f}s")
        time.sleep(delta)

def _record_block(url: str, base_seconds: float = 60.0) -> None:
    """Enregistre un blocage anti-bot (403/429) sur un domaine et fixe un cooldown.
    Le cooldown est multiplicatif si plusieurs blocages s'enchaînent sur le même domaine."""
    dom = _domain_of(url)
    now = time.monotonic()
    # Si déjà en cooldown, on double la pénalité (backoff exponentiel par domaine)
    current_remaining = max(0.0, _DOMAIN_COOLDOWN.get(dom, 0.0) - now)
    new_penalty = max(base_seconds, current_remaining * 2.0)
    _DOMAIN_COOLDOWN[dom] = now + new_penalty
    logger.warning(f"🚧 Blocage détecté sur {dom} — cooldown {new_penalty:.0f}s avant nouvelle requête")

def _client_hints_for(user_agent: str) -> dict[str, str]:
    """Construit les Client Hints Chrome (Sec-Ch-Ua-*) cohérents avec un User-Agent.

    Chrome envoie automatiquement ces en-têtes ; leur absence est un signal anti-bot
    fort utilisé par les WAF modernes (Cloudflare, Akamai, etc.).
    On infère la plateforme et la version Chrome à partir du UA pour rester cohérent.
    """
    # Detecte la plateforme
    if "Macintosh" in user_agent:
        platform = '"macOS"'
    elif "Linux" in user_agent:
        platform = '"Linux"'
    elif "Windows" in user_agent:
        platform = '"Windows"'
    else:
        platform = '"Unknown"'

    # Detecte la version majeure de Chrome (124, 131, ...) ou marque Safari
    m = re.search(r"Chrome/(\d+)", user_agent)
    if m:
        major = m.group(1)
        sec_ua = f'"Chromium";v="{major}", "Not(A:Brand";v="24", "Google Chrome";v="{major}"'
        return {
            "Sec-Ch-Ua":          sec_ua,
            "Sec-Ch-Ua-Mobile":   "?0",
            "Sec-Ch-Ua-Platform": platform,
        }
    # Safari ne envoie pas Client Hints — on retourne un dict vide
    return {}


def _get_session() -> curl_requests.Session:
    """Crée une session avec une empreinte TLS Chrome native (Bypass Cloudflare strict).

    À chaque appel après création, l'User-Agent, l'Accept-Language et les Client
    Hints (Sec-Ch-Ua-*) sont rerollés de façon cohérente sur la session existante
    (sans détruire les cookies).
    L'impersonate TLS est tiré une seule fois à la création pour rester cohérent
    avec les cookies (changer d'empreinte casse les WAF qui lient session+TLS).

    Si un proxy résidentiel est configuré (.env : RESIDENTIAL_PROXY_PRIMARY etc.),
    la session est routée à travers le proxy actif du pool. En cas d'échec proxy
    en cours de run, le caller peut appeler `_handle_proxy_failure()` qui rotate
    automatiquement vers le proxy suivant.
    """
    global _persistent_session
    if _persistent_session is None:
        impersonate = random.choice(_IMPERSONATE_ROTATION)
        logger.debug(f"🛡️ Session créée avec impersonate={impersonate}")
        _persistent_session = curl_requests.Session(impersonate=impersonate)
        _persistent_session.cookies.set("CONSENT", "YES+cb.20230501-14-p0.fr+FX+414", domain=".google.com")
        # Application du proxy actif (si configure). Le ProxyManager est initialise
        # au premier appel et fait son health check. Si aucun proxy sain : mode direct.
        try:
            from .proxy_manager import get_proxy_manager
        except ImportError:
            from proxy_manager import get_proxy_manager  # type: ignore
        proxy_dict = get_proxy_manager().current_proxy_dict()
        if proxy_dict:
            _persistent_session.proxies = proxy_dict
            entry = get_proxy_manager().current_proxy()
            if entry is not None:
                logger.info(f"🌐 Session routee via proxy : {entry.name} ({entry.masked_url()})")
    # Reroll headers a chaque get_session (UA + Accept-Language + Client Hints coherents avec UA)
    new_ua = random.choice(ROTATING_USER_AGENTS)
    headers_update: dict[str, str] = {
        "User-Agent":      new_ua,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        # Sec-Fetch-* sont envoyes par tous les navigateurs modernes — leur absence
        # est un drapeau bot. Les valeurs varient selon le contexte (top-level, embed,
        # etc.) ; pour des requetes GET de pages on utilise le triplet "navigation".
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "none",
        "Sec-Fetch-User":  "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT":             "1",
    }
    headers_update.update(_client_hints_for(new_ua))
    _persistent_session.headers.update(headers_update)
    return _persistent_session


def _handle_proxy_failure(error_kind: str = "unknown") -> bool:
    """À appeler quand une requête échoue de manière compatible avec un échec proxy
    (timeout répété, 407, ProxyError). Marque le proxy en faute et tente de rotate.

    Returns:
        True si on a basculé vers un autre proxy sain (caller doit retry).
        False si plus aucun proxy sain (caller doit accepter et continuer).
    """
    global _persistent_session
    try:
        from .proxy_manager import get_proxy_manager
    except ImportError:
        from proxy_manager import get_proxy_manager  # type: ignore
    mgr = get_proxy_manager()
    if mgr.current_proxy() is None:
        return False  # mode direct, rien à rotate
    mgr.mark_failure()
    logger.warning(f"⚠️ Echec proxy detecte ({error_kind}) — tentative de bascule.")
    if mgr.rotate():
        # Reinit la session pour appliquer le nouveau proxy
        _persistent_session = None
        return True
    logger.error("🛑 Plus aucun proxy sain dans le pool.")
    return False


def _reset_session() -> None:
    """Détruit la session active. Le prochain _get_session() en créera une nouvelle
    avec un impersonate, des cookies et une identité TLS différents.

    WHY : sur un long run (294 requêtes Google News), garder une seule identité
    fait apparaître un volume anormal pour une « personne ». En tournant la session
    toutes les ~50 requêtes, on présente 6 identités différentes à Google plutôt
    qu'une seule identité qui ferait 294 recherches en 2h.
    """
    global _persistent_session
    if _persistent_session is not None:
        try:
            _persistent_session.close()
        except (RequestsError, OSError, AttributeError):
            pass
    _persistent_session = None
    logger.info("🔄 Session réseau réinitialisée — nouvelle identité au prochain appel.")


# Rotation des locales Google News pour varier le profil "intérêt géographique"
# vu par Google. Un humain consulte parfois en anglais, parfois dans sa langue,
# parfois en allemand pour les sources industrielles. Toujours en-US c'est un pattern.
# Inclut fr-CH pour cohérence avec l'utilisateur basé en Suisse (positivecoating.ch).
_GNEWS_LOCALES = [
    ("en-US", "US", "US:en"),
    ("fr-FR", "FR", "FR:fr"),
    ("de-DE", "DE", "DE:de"),
    ("en-GB", "GB", "GB:en"),
    ("fr-CH", "CH", "CH:fr"),
    ("en-CA", "CA", "CA:en"),
    ("it-IT", "IT", "IT:it"),
    ("es-ES", "ES", "ES:es"),
]


def _is_night_time() -> bool:
    """Retourne True si l'heure locale est dans la plage [1h, 6h], peu utilisée par
    un humain. WHY : sur cette plage on multiplie les délais et on saute des breaks
    pour mimer un comportement « insomniaque qui consulte rarement la nuit » plutôt
    qu'un bot qui scrappe à 3h du matin avec le même rythme qu'à 14h."""
    h = datetime.now().hour
    return 1 <= h < 6


def _humanlike_inter_request_delay() -> float:
    """Génère un délai entre 2 requêtes Google News qui mime un comportement humain en lecture.

    WHY mode « weekend » (4 modes au lieu de 3, moyenne ~2 min au lieu de 25s) :
    le programme tourne sur 50h max sans surveillance. On préfère des délais lents
    crédibles (humain qui lit un article complet, fait une pause café, etc.) plutôt
    qu'un volume rapide qui finirait par déclencher la détection comportementale Google.
      - fast    (15%) : 20-50s    — l'humain scrolle un titre sans intérêt
      - normal  (50%) : 60-120s   — il lit le résumé et clique sur 1-2 résultats
      - slow    (25%) : 120-300s  — il lit un article complet
      - v.slow  (10%) : 5-9 min   — pause téléphone / café / autre activité
    Moyenne pondérée ≈ 130-140s par requête.

    Pendant la plage nuit [1h-6h], les délais sont multipliés par 1.8 pour
    refléter qu'un humain insomniaque consulte beaucoup plus lentement.
    """
    mode = random.choices(
        ["fast", "normal", "slow", "very_slow"],
        weights=[0.15, 0.50, 0.25, 0.10],
    )[0]
    if mode == "fast":
        base = max(20.0, random.gauss(35.0, 10.0))
    elif mode == "slow":
        base = max(120.0, random.gauss(180.0, 45.0))
    elif mode == "very_slow":
        base = max(300.0, random.gauss(420.0, 90.0))
    else:
        base = max(60.0, random.gauss(90.0, 25.0))
    return base * (1.8 if _is_night_time() else 1.0)


def _inter_source_break(label: str) -> None:
    """Pause de 3-9 min entre deux blocs de sources différents.

    WHY : un humain ne fait pas 5 recherches arXiv puis 6 OpenAlex puis 5 Crossref
    sans transition. Insérer une pause longue entre blocs casse le rythme machine
    et imite un changement d'activité (relecture des résultats, ouverture d'onglets, etc.).
    """
    pause = max(180.0, random.gauss(360.0, 90.0))
    logger.info(f"🚶 Pause inter-source ({label}) : {pause:.0f}s — changement d'activité.")
    time.sleep(pause)

def _warm_up_session(domain_url: str) -> None:
    """Visite furtivement la racine d'un domaine pour obtenir des cookies validés (Bypass WAF).

    Timeout 20s : MDPI/ScienceDaily peuvent etre temporairement lents (> 10s)
    sans pour autant etre blockes. Mieux vaut attendre que d'echouer le warm-up.
    """
    session = _get_session()
    try:
        base_url = "/".join(domain_url.split("/")[:3]) # Extrait ex: https://www.mdpi.com
        logger.debug(f"🛡️ Warm-up session pour : {base_url}")
        session.get(base_url, timeout=20)
        time.sleep(max(0.5, random.gauss(2.2, 0.5))) # Pause humaine (loi normale)
    except (RequestsError, OSError) as e:
        logger.warning(f"⚠️ Échec du warm-up pour {domain_url}: {e}")

def build_gnews_queries() -> list[str]:
    """Construit des requêtes Google News : produit cartésien entreprise × mot-clé,
    plus les solo_keywords cherchés seuls (sans entreprise associée).

    Pas de regroupement OR : Google News tronque silencieusement les longues requêtes
    OR et renvoie systématiquement les mêmes résultats génériques. Une requête courte
    et précise par couple (entreprise, mot-clé) garantit des résultats pertinents et
    bien distincts, au prix d'un nombre d'appels plus élevé (rate-limité ailleurs).

    solo_keywords : phrases multi-mots cherchées TELLES QUELLES (entre guillemets).
    Utiles pour des thèmes spécifiques qui n'apparaissent jamais avec un nom de société.
    """
    companies = TARGET_COMPANIES or []
    corrected_keywords = [kw.replace("Chemical Layer Deposition", "Chemical Vapor Deposition") for kw in KEYWORDS]
    solos = SOLO_KEYWORDS or []

    queries: list[str] = []
    if companies and corrected_keywords:
        for company in companies:
            for kw in corrected_keywords:
                queries.append(f'"{company}" "{kw}"')
    for kw in solos:
        queries.append(f'"{kw}"')

    return list(dict.fromkeys(queries))

def load_seen_urls() -> list[str]:
    if os.path.exists(_SEEN_URLS_PATH):
        try:
            with open(_SEEN_URLS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return []

def save_seen_urls(seen_list: list[str]) -> None:
    try:
        urls_to_save = seen_list[-_SEEN_URLS_MAX:]
        os.makedirs(os.path.dirname(_SEEN_URLS_PATH), exist_ok=True)
        with open(_SEEN_URLS_PATH, "w", encoding="utf-8") as f:
            json.dump(urls_to_save, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"Erreur de sauvegarde history : {e}")


def _save_checkpoint(articles: list[dict[str, Any]], progress: str) -> None:
    """Sauvegarde un état partiel du scraping pour ne rien perdre en cas d'interruption."""
    try:
        os.makedirs(os.path.dirname(_CHECKPOINT_PATH), exist_ok=True)
        with open(_CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "progress": progress,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "articles": articles,
            }, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"⚠️ Échec checkpoint: {e}")

def _is_recent(entry: dict[str, Any]) -> bool:
    if not SCRAPE_LIMIT_MONTH: return True
    raw_date = entry.get("published") or entry.get("updated")
    if raw_date:
        parsed_tuple = email.utils.parsedate_tz(raw_date)
        if parsed_tuple:
            ts = email.utils.mktime_tz(parsed_tuple)
            return (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc)).days <= RECENT_DAYS_LIMIT
    return True

def fetch_rss_feed(source: dict[str, str]) -> list[dict[str, Any]]:
    name = source.get("name", "Inconnu")
    url = source.get("url", "")

    # Respect d'un éventuel cooldown anti-bot enregistré sur ce domaine
    _respect_domain_cooldown(url)

    # Pre-flight pour les sites sensibles (MDPI, ScienceDaily)
    is_sensitive = "mdpi.com" in url or "sciencedaily.com" in url
    if is_sensitive:
        _warm_up_session(url)

    # Timeout adapte : sources sensibles parfois lentes (CDN, anti-bot, queue
    # interne), on tolere jusqu'a 30s avant d'abandonner. Pour les autres
    # (arXiv, IEEE), 15s suffit largement.
    fetch_timeout = 30 if is_sensitive else 15

    session = _get_session()
    try:
        response = session.get(url, timeout=fetch_timeout)
        if response.status_code in (403, 429):
            _record_block(url, base_seconds=120.0 if response.status_code == 429 else 60.0)
            logger.warning(f"🚫 {name} : statut HTTP {response.status_code} (anti-bot ?)")
            return []
        response.raise_for_status()

        # Sécurité : Si le serveur renvoie une page d'erreur HTML au lieu du XML, on ignore proprement
        if "text/html" in response.headers.get("Content-Type", ""):
            _record_block(url, base_seconds=60.0)
            logger.warning(f"⚠️ {name} a renvoyé du HTML (blocage anti-bot) au lieu de XML.")
            return []

        feed = feedparser.parse(response.content)
    except (RequestsError, OSError) as e:
        logger.warning(f"❌ Erreur réseau sur {name} : {e}")
        return []

    articles = []
    for entry in feed.entries:
        if not _is_recent(entry): continue
        
        summary = entry.get("summary") or entry.get("description") or ""
        articles.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "summary": re.sub(r"<[^>]+>", " ", summary).strip(),
            "source": name,
            "category": source.get("category", "general"),
            "collected_at": datetime.now(timezone.utc).isoformat()
        })
        if len(articles) >= MAX_ARTICLES_PER_SOURCE: break
    
    logger.info(f"   └─ {len(articles)} article(s) collecté(s)")
    return articles

def build_openalex_queries() -> list[str]:
    """Construit des requêtes OpenAlex (search=) sur les grands axes thématiques.

    OpenAlex indexe ~250M d'œuvres scientifiques (papers, pré-prints) avec
    métadonnées riches (DOI, concepts, institutions). Gratuit, sans clé,
    sans limite de débit pratique pour notre usage. C'est notre meilleure source
    pour rattraper les papers universitaires non syndiqués via RSS.

    Les solo_keywords sont ajoutés en plus des thématiques hardcodées.
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    base = [
        '"physical vapor deposition"',
        '"chemical vapor deposition"',
        '"atomic layer deposition"',
        '"magnetron sputtering" OR HiPIMS',
        '"thin film coating" OR "hard coating" OR DLC',
        '"surface treatment" tribology',
    ]
    # Broadcast : on cherche aussi chaque mot-cle (couple), chaque solo et
    # chaque organisme de recherche (research_org) dans OpenAlex.
    # Les research_orgs sont cibles via leur nom : ca trouve les papers ou
    # le labo apparait dans les affiliations d'auteurs ou le texte.
    kw_q    = [f'"{kw}"' for kw in (KEYWORDS or [])]
    solo_q  = [f'"{kw}"' for kw in (SOLO_KEYWORDS or [])]
    org_q   = [f'"{org}"' for org in (RESEARCH_ORGS or [])]
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_openalex_works(query: str, max_results: int = 25) -> list[dict[str, Any]]:
    """Interroge l'API OpenAlex (gratuite, sans clé) pour récupérer des œuvres scientifiques.

    Filtre côté API par date de publication (RECENT_DAYS_LIMIT) pour limiter le bruit.
    Tri par date décroissante : on prend les plus récents en premier.

    Args:
        query: terme de recherche (supporte les guillemets et OR/AND).
        max_results: borne supérieure (OpenAlex permet jusqu'à 200 par page).

    Returns:
        Liste d'articles au format unifié du projet. Liste vide si erreur.
    """
    from datetime import timedelta

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS_LIMIT)).strftime("%Y-%m-%d")
    # Politesse OpenAlex : champ mailto pour identifier le client (recommandé, débit prioritaire)
    polite_email = os.environ.get("GMAIL_USER", "veille-tech@example.com")

    params = {
        "search":                query,
        "filter":                f"from_publication_date:{cutoff_date},type:article",
        "sort":                  "publication_date:desc",
        "per-page":              max_results,
        "mailto":                polite_email,
    }

    _respect_domain_cooldown("https://api.openalex.org/works")
    session = _get_session()
    try:
        response = session.get("https://api.openalex.org/works", params=params, timeout=20)
        if response.status_code in (403, 429):
            _record_block("https://api.openalex.org/works", base_seconds=60.0)
            logger.warning(f"🚫 OpenAlex : statut HTTP {response.status_code}")
            return []
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ OpenAlex : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ OpenAlex : réponse non-JSON ({exc})")
        return []

    articles: list[dict[str, Any]] = []
    for work in data.get("results", []):
        # Privilégier le DOI/URL canonique, fallback sur les open access locations
        link = work.get("doi") or work.get("id") or ""
        if work.get("open_access", {}).get("oa_url"):
            link = work["open_access"]["oa_url"]
        if not link:
            continue

        title = (work.get("title") or work.get("display_name") or "").strip()
        if not title:
            continue

        # OpenAlex stocke les abstracts sous forme inversée (mot → positions). Reconstruction.
        abstract_inv = work.get("abstract_inverted_index") or {}
        if abstract_inv:
            positions: list[tuple[int, str]] = []
            for word, idx_list in abstract_inv.items():
                for idx in idx_list:
                    positions.append((idx, word))
            positions.sort(key=lambda p: p[0])
            summary = " ".join(w for _, w in positions)[:600]
        else:
            summary = ""

        # Source : nom du venue/journal pour la lisibilité
        primary_loc = work.get("primary_location") or {}
        venue = (primary_loc.get("source") or {}).get("display_name") or "OpenAlex"

        articles.append({
            "title":        title,
            "link":         link,
            "summary":      summary,
            "source":       f"OpenAlex — {venue}",
            "category":     "science",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ OpenAlex : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


def build_crossref_queries() -> list[str]:
    """Construit des requêtes Crossref sur les grands axes thématiques.

    Crossref indexe ~140M d'œuvres avec DOI. Complémentaire d'OpenAlex (qui s'appuie
    en partie sur Crossref) — capte parfois des publications plus récentes ou les
    versions auteurs (preprints) avant qu'OpenAlex les enrichisse.

    Les solo_keywords sont ajoutés en plus (Crossref accepte le texte brut).
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    # Single-concept par requete (Crossref AND-narrow les mots multiples,
    # ce qui ratait les papers ne mentionnant qu'un seul des termes accoles).
    base = [
        "physical vapor deposition",
        "chemical vapor deposition",
        "atomic layer deposition",
        "magnetron sputtering",
        "HiPIMS",
        "thin film coating",
        "hard coating",
        "DLC coating",
    ]
    # Broadcast keywords + solos + research_orgs (Crossref accepte texte brut)
    kw_q   = list(KEYWORDS or [])
    solo_q = list(SOLO_KEYWORDS or [])
    org_q  = list(RESEARCH_ORGS or [])
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_crossref_works(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Interroge l'API Crossref (gratuite, sans clé) pour récupérer des œuvres scientifiques.

    Politesse Crossref : User-Agent identifié + champ mailto. Le pool « polite » a
    une priorité d'accès supérieure au pool « public » non identifié.
    """
    from datetime import timedelta
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS_LIMIT)).strftime("%Y-%m-%d")
    polite_email = os.environ.get("GMAIL_USER", "veille-tech@example.com")
    headers = {
        "User-Agent": f"VeilleTech/1.0 (mailto:{polite_email})",
    }
    params = {
        "query":  query,
        "rows":   max_results,
        # Crossref a déprécié `from-pub-date` au profit de la syntaxe `filter=`
        "filter": f"from-pub-date:{cutoff_date}",
        "sort":   "published",
        "order":  "desc",
        "select": "DOI,title,abstract,URL,published-print,published-online,container-title,author",
        "mailto": polite_email,
    }

    _respect_domain_cooldown("https://api.crossref.org/works")
    session = _get_session()
    try:
        response = session.get("https://api.crossref.org/works", params=params, headers=headers, timeout=20)
        if response.status_code in (403, 429):
            _record_block("https://api.crossref.org/works", base_seconds=60.0)
            logger.warning(f"🚫 Crossref : statut HTTP {response.status_code}")
            return []
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ Crossref : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ Crossref : réponse non-JSON ({exc})")
        return []

    items = (data.get("message") or {}).get("items", [])
    articles: list[dict[str, Any]] = []
    for it in items:
        # Title est une liste chez Crossref, on prend la 1ère entrée
        title_list = it.get("title") or []
        title = (title_list[0] if title_list else "").strip()
        if not title:
            continue
        link = it.get("URL") or (f"https://doi.org/{it['DOI']}" if it.get("DOI") else "")
        if not link:
            continue
        # Abstract Crossref est en JATS XML (souvent absent), on retire les balises
        abstract = it.get("abstract") or ""
        abstract = re.sub(r"<[^>]+>", " ", abstract).strip()[:600]
        venue_list = it.get("container-title") or []
        venue = (venue_list[0] if venue_list else "Crossref").strip()
        articles.append({
            "title":        title,
            "link":         link,
            "summary":      abstract,
            "source":       f"Crossref — {venue}",
            "category":     "science",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ Crossref : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


def build_hal_queries() -> list[str]:
    """Construit des requêtes HAL (archive ouverte CNRS).

    WHY HAL : couverture FR particulièrement forte sur CEA-Leti, CNRS, ONERA
    (présents dans targets.json). Beaucoup de pré-prints français ne sont pas
    encore indexés par OpenAlex/Crossref au moment de leur dépôt sur HAL.

    Les solo_keywords sont ajoutés en plus des thématiques bilingues hardcodées.
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    # Paires bilingues FR/EN (HAL est l'archive CNRS, beaucoup de papiers FR)
    base = [
        '"physical vapor deposition" OR "dépôt physique"',
        '"chemical vapor deposition" OR "dépôt chimique"',
        '"atomic layer deposition" OR "dépôt par couche atomique"',
        '"magnetron sputtering" OR "pulvérisation magnétron"',
        '"thin film coating" OR "couche mince"',
        '"hard coating" OR "revêtement dur"',
    ]
    # Broadcast keywords + solos + research_orgs (chacun en phrase exacte).
    # HAL est l'archive CNRS donc particulierement pertinent pour les
    # research_orgs francaises (CEA-Leti, ONERA, Institut Neel, CIRIMAT...).
    kw_q   = [f'"{kw}"' for kw in (KEYWORDS or [])]
    solo_q = [f'"{kw}"' for kw in (SOLO_KEYWORDS or [])]
    org_q  = [f'"{org}"' for org in (RESEARCH_ORGS or [])]
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_hal_publications(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Interroge l'API HAL Search (Solr-based, gratuite, sans clé)."""
    from datetime import timedelta
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS_LIMIT)).strftime("%Y-%m-%d")
    params = {
        "q":    query,
        "wt":   "json",
        "rows": max_results,
        "fl":   "title_s,abstract_s,uri_s,producedDate_s,journalTitle_s,docType_s",
        "fq":   f"producedDate_s:[{cutoff_date} TO NOW]",
        "sort": "producedDate_s desc",
    }

    _respect_domain_cooldown("https://api.archives-ouvertes.fr/search/")
    session = _get_session()
    try:
        response = session.get("https://api.archives-ouvertes.fr/search/", params=params, timeout=20)
        if response.status_code in (403, 429):
            _record_block("https://api.archives-ouvertes.fr/search/", base_seconds=60.0)
            logger.warning(f"🚫 HAL : statut HTTP {response.status_code}")
            return []
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ HAL : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ HAL : réponse non-JSON ({exc})")
        return []

    docs = ((data.get("response") or {}).get("docs") or [])
    articles: list[dict[str, Any]] = []
    for d in docs:
        title_field = d.get("title_s")
        title = ((title_field[0] if isinstance(title_field, list) else title_field) or "").strip()
        link = (d.get("uri_s") or "").strip()
        if not title or not link:
            continue
        abstract_field = d.get("abstract_s")
        abstract = ((abstract_field[0] if isinstance(abstract_field, list) else abstract_field) or "").strip()[:600]
        venue = (d.get("journalTitle_s") or d.get("docType_s") or "HAL").strip()
        articles.append({
            "title":        title,
            "link":         link,
            "summary":      abstract,
            "source":       f"HAL — {venue}",
            "category":     "science",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ HAL : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


def build_semantic_scholar_queries() -> list[str]:
    """Construit des requêtes Semantic Scholar.

    Semantic Scholar (Allen Institute for AI) indexe ~200M papers avec
    enrichissement IA (TLDR auto, contextes citation). Couvre certains papers
    que ni Crossref ni OpenAlex n'ont encore vus.

    Les solo_keywords sont ajoutés en plus (texte brut accepté par S2).
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    # Single-concept par requete (S2 AND-narrow les mots accoles, ce qui
    # ratait les papers ne contenant pas TOUS les termes du compose).
    base = [
        "physical vapor deposition",
        "chemical vapor deposition",
        "atomic layer deposition",
        "magnetron sputtering",
        "HiPIMS",
        "thin film coating",
        "hard coating",
    ]
    # Broadcast keywords + solos + research_orgs (Semantic Scholar texte brut)
    kw_q   = list(KEYWORDS or [])
    solo_q = list(SOLO_KEYWORDS or [])
    org_q  = list(RESEARCH_ORGS or [])
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_semantic_scholar(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Interroge l'API Semantic Scholar (rate-limit 1 req/s, avec ou sans clé).

    Avec SEMANTIC_SCHOLAR_API_KEY : 1 req/s garanti, pas de bannissement IP.
    Sans clé : 1 req/s best-effort, l'API renvoie souvent 429 même en respectant.

    WHY assouplissement du filtre date à 365 jours (au lieu de RECENT_DAYS_LIMIT=90j) :
    le endpoint `paper/search` ne supporte pas de tri par date — il classe par
    pertinence. Avec un filtre 90j côté client, on coupe TOUS les "best match"
    qui sont typiquement des reviews fondamentales de 2022-2024. On accepte
    donc une fenêtre de 12 mois pour ne pas perdre les papers pertinents que
    les autres sources (OpenAlex, Crossref, HAL) auraient pu manquer. Le
    scoring IA filtre ensuite la pertinence réelle.
    """
    from datetime import timedelta
    # SS ne supporte pas un filtre date par jour — on borne par année (2 dernières)
    current_year = datetime.now(timezone.utc).year
    year_filter = f"{current_year - 1}-{current_year}"

    params = {
        "query":  query,
        "limit":  max_results,
        "year":   year_filter,
        "fields": "title,abstract,url,publicationDate,venue,externalIds",
    }
    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    _respect_domain_cooldown(url)
    session = _get_session()
    try:
        response = session.get(url, params=params, headers=headers, timeout=20)
        if response.status_code == 429:
            _record_block(url, base_seconds=120.0)
            logger.warning(f"⏳ Semantic Scholar : rate limit (429), requête ignorée")
            return []
        if response.status_code in (403, 401):
            _record_block(url, base_seconds=60.0)
            logger.warning(f"🚫 Semantic Scholar : statut HTTP {response.status_code} "
                           f"(clé invalide ou non encore activée — validation 24-48h)")
            return []
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ Semantic Scholar : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ Semantic Scholar : réponse non-JSON ({exc})")
        return []

    # Cf. docstring : 365j au lieu de RECENT_DAYS_LIMIT (90j) car SS classe par pertinence
    ss_window_days = max(365, RECENT_DAYS_LIMIT)
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=ss_window_days)).date()
    articles: list[dict[str, Any]] = []
    for p in data.get("data", []):
        title = (p.get("title") or "").strip()
        link = (p.get("url") or "").strip()
        if not title or not link:
            continue
        # Filtre date côté client puisque SS ne le fait pas par jour
        pub_date_str = p.get("publicationDate") or ""
        try:
            pub_date = datetime.fromisoformat(pub_date_str).date() if pub_date_str else None
        except ValueError:
            pub_date = None
        if pub_date and pub_date < cutoff_date:
            continue
        abstract = (p.get("abstract") or "").strip()[:600]
        venue = (p.get("venue") or "Semantic Scholar").strip()
        articles.append({
            "title":        title,
            "link":         link,
            "summary":      abstract,
            "source":       f"Semantic Scholar — {venue}",
            "category":     "science",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ Semantic Scholar : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


def build_arxiv_search_queries() -> list[str]:
    """Construit des requêtes pour l'arXiv Search API (en plus du flux RSS générique).

    WHY : les flux RSS arXiv ne donnent que les ~50 derniers articles soumis aux
    catégories `physics.app-ph` et `cond-mat.mtrl-sci`. Une recherche par mot-clé
    sur tout l'index permet de trouver les papers PVD/CVD/ALD dans n'importe
    quelle catégorie (chemistry, electrical engineering, etc.) qu'on louperait
    sinon. Format : opérateur arXiv = ti:"..." OR abs:"..." (titre OU résumé).
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    # Requetes symetriques : meme terme dans titre ET resume (pas un mot dans
    # ti et un autre dans abs, ce qui rate les papiers ou le terme n'apparait
    # que dans un seul des deux champs). Une ligne = un concept.
    base = [
        'ti:"atomic layer deposition" OR abs:"atomic layer deposition"',
        'ti:"physical vapor deposition" OR abs:"physical vapor deposition"',
        'ti:"chemical vapor deposition" OR abs:"chemical vapor deposition"',
        'ti:"magnetron sputtering" OR abs:"magnetron sputtering"',
        'ti:"HiPIMS" OR abs:"HiPIMS"',
        'ti:"thin film coating" OR abs:"thin film coating"',
        'ti:"hard coating" OR abs:"hard coating"',
    ]
    # Broadcast : chaque keyword (couple), chaque solo et chaque research_org
    # cherches dans titre OU resume. Format arXiv : ti:"X" OR abs:"X".
    # Pour les research_orgs, on utilise aussi le champ 'all' (tout le doc)
    # car les noms de labos apparaissent dans les affiliations d'auteurs,
    # pas forcement dans titre/abstract.
    kw_q   = [f'ti:"{kw}" OR abs:"{kw}"' for kw in (KEYWORDS or [])]
    solo_q = [f'ti:"{kw}" OR abs:"{kw}"' for kw in (SOLO_KEYWORDS or [])]
    org_q  = [f'all:"{org}"' for org in (RESEARCH_ORGS or [])]
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_arxiv_search(query: str, max_results: int = 20) -> list[dict[str, Any]] | None:
    """Interroge l'API arXiv (Atom) par mot-clé.

    L'API publique arXiv exige un User-Agent identifiable et un débit
    raisonnable (1 req/3s recommandé). HTTPS obligatoire désormais sur
    cet endpoint (HTTP redirige mais arXiv applique des restrictions).

    Args:
        query: requête arXiv (ex: 'ti:"PVD" OR abs:"physical vapor deposition"').
        max_results: borne supérieure (arXiv accepte jusqu'à 2000).

    Returns:
        Liste d'articles au format unifié, déjà filtrés par RECENT_DAYS_LIMIT.
        Retourne None si HTTP 429/403 -> permet au caller (run_scraper) de
        compter les blocages consécutifs et déclencher un circuit breaker.
    """
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query={quote_plus(query)}"
        f"&start=0&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )

    # User-Agent identifiable conformement a la politique arXiv (RFC 7231).
    # arXiv demande explicitement de fournir un UA distinct des navigateurs
    # standards pour les acces API automatises (mailto facultatif mais aide).
    arxiv_headers = {
        "User-Agent": "VeilleTechno-Pipeline/1.0 (research; +https://github.com/)",
        "Accept": "application/atom+xml",
    }

    _respect_domain_cooldown(url)
    session = _get_session()
    try:
        response = session.get(url, timeout=20, headers=arxiv_headers)
        if response.status_code in (403, 429):
            _record_block(url, base_seconds=180.0)
            logger.warning(f"🚫 arXiv : statut HTTP {response.status_code}")
            return None  # Signale un blocage au caller (circuit breaker)
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ arXiv : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    feed = feedparser.parse(response.content)
    articles: list[dict[str, Any]] = []
    for entry in feed.entries:
        if not _is_recent(entry):
            continue
        link = entry.get("link", "").strip()
        if not link:
            continue
        summary = entry.get("summary", "") or entry.get("description", "")
        articles.append({
            "title":        re.sub(r"\s+", " ", entry.get("title", "")).strip(),
            "link":         link,
            "summary":      re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", summary)).strip(),
            "source":       "arXiv (search)",
            "category":     "science",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ arXiv search : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


def build_web_queries() -> list[str]:
    """Construit des requêtes Tavily groupées par grands axes thématiques.

    WHY le regroupement OR plutôt que produit cartésien : Tavily est rate-limité
    (1000 req/mois en free tier) et chaque requête coûte. Le scoring IA filtrera
    de toute façon les résultats, donc on privilégie la couverture large par
    thème plutôt que la spécificité par couple. Un suffixe « research / academic »
    biaise vers les sources universitaires que les RSS et GNews loupent.
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    base = [
        '("PVD" OR "Physical Vapor Deposition" OR "magnetron sputtering" OR "HiPIMS") research publications',
        '("CVD" OR "Chemical Vapor Deposition" OR "ALD" OR "Atomic Layer Deposition") academic breakthroughs',
        '("thin film" OR "coating" OR "DLC" OR "hard coating") materials science innovation',
        '("surface treatment" OR "tribology" OR "wear resistance") industrial coating research',
    ]
    # Broadcast : chaque keyword, solo et research_org en phrase exacte
    # avec biais academique ("research academic" oriente Tavily vers les
    # sources universitaires plutot que les blogs/wikis).
    kw_q   = [f'"{kw}" research academic' for kw in (KEYWORDS or [])]
    solo_q = [f'"{kw}" research academic' for kw in (SOLO_KEYWORDS or [])]
    org_q  = [f'"{org}" PVD CVD ALD coating publication' for org in (RESEARCH_ORGS or [])]
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_broad_web_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Interroge l'API Tavily Search pour élargir la couverture au-delà de RSS+GNews.

    Tavily est conçu pour les LLMs : résultats déjà résumés, pertinence triée.
    Idéal pour rattraper les papers universitaires non syndiqués via RSS.

    Args:
        query: requête en langage naturel (peut contenir des opérateurs OR/AND).
        max_results: borne supérieure sur le nombre de résultats à demander (défaut 10).

    Returns:
        Liste d'articles au format unifié du projet. Liste vide en cas d'erreur
        ou si TAVILY_API_KEY est absente (dégradation gracieuse).
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        logger.info("ℹ️ TAVILY_API_KEY absente — recherche Web Tavily désactivée pour cette requête.")
        return []

    session = _get_session()
    payload = {
        "api_key":             api_key,
        "query":               query,
        "search_depth":        "basic",
        "max_results":         max_results,
        "include_answer":      False,
        "include_raw_content": False,
        # Borne la fraîcheur à RECENT_DAYS_LIMIT pour éviter les vieux papiers.
        # Tavily ignore silencieusement les paramètres qu'il ne connaît pas.
        "days":                RECENT_DAYS_LIMIT,
        "topic":               "general",
    }
    try:
        response = session.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=20,
        )
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ Tavily : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    if response.status_code == 401:
        logger.error("❌ Tavily : TAVILY_API_KEY invalide (401 Unauthorized).")
        return []
    if response.status_code == 429:
        logger.warning("⏳ Tavily : quota dépassé (429). Requête ignorée.")
        return []
    if response.status_code >= 400:
        logger.warning(f"❌ Tavily : statut HTTP {response.status_code} — {response.text[:200]}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ Tavily : réponse non-JSON ({exc}) — {response.text[:200]}")
        return []

    articles: list[dict[str, Any]] = []
    for r in data.get("results", []):
        url = (r.get("url") or "").strip()
        if not url:
            continue
        domain = url.split("/")[2] if url.count("/") >= 2 else "unknown"
        articles.append({
            "title":        (r.get("title") or "").strip(),
            "link":         url,
            "summary":      (r.get("content") or "").strip(),
            "source":       f"Tavily Web — {domain}",
            "category":     "web",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info(f"   └─ Tavily : {len(articles)} résultat(s) pour « {query[:60]}... »")
    return articles


# =============================================================================
# Google Patents — brevets industriels (PVD/CVD/ALD massivement protégés)
# =============================================================================

def build_patents_queries() -> list[str]:
    """Construit des requêtes pour Google Patents.

    WHY Patents : la R&D PVD/CVD/ALD industrielle se publie d'abord en brevets
    (Applied Materials, ASM International, Veeco, Picosun, Oerlikon, Lam Research,
    KLA, Tokyo Electron déposent des centaines de brevets par an). Ces innovations
    n'apparaissent souvent ni dans la presse (pas de communiqué) ni dans les
    papers académiques (R&D propriétaire). Patents = SEULE source pour ce signal.

    Format : phrases entre guillemets, comme arXiv/HAL. Google Patents cherche
    par défaut dans titre + abstract + claims, pas besoin d'opérateurs spéciaux.
    """
    if not KEYWORDS and not SOLO_KEYWORDS:
        return []
    base = [
        '"physical vapor deposition"',
        '"chemical vapor deposition"',
        '"atomic layer deposition"',
        '"magnetron sputtering"',
        '"HiPIMS"',
        '"thin film coating"',
        '"hard coating"',
        '"diamond-like carbon"',  # central en brevets DLC
    ]
    # Broadcast keywords + solos + research_orgs (les labos sont parfois
    # cessionnaires de brevets, surtout les organismes publics : CEA, CNRS,
    # Fraunhofer, MIT, NREL deposent des centaines de brevets/an).
    kw_q   = [f'"{kw}"' for kw in (KEYWORDS or [])]
    solo_q = [f'"{kw}"' for kw in (SOLO_KEYWORDS or [])]
    org_q  = [f'assignee:"{org}"' for org in (RESEARCH_ORGS or [])]
    return list(dict.fromkeys(base + kw_q + solo_q + org_q))


def fetch_google_patents(query: str, max_results: int = 15) -> list[dict[str, Any]]:
    """Interroge Google Patents via l'endpoint xhr/query (gratuit, sans clé).

    Endpoint : https://patents.google.com/xhr/query?url=q%3D...&exp=
    Filtre par date de priorité avec RECENT_DAYS_LIMIT pour exclure les brevets
    anciens. Limite par défaut à 15 résultats / requête (suffisant après dedup).

    Robuste : si Google bloque (403/429) ou change le format JSON, retourne []
    sans crash et marque le domaine en cooldown.

    Args:
        query: requête en phrase (ex: '"physical vapor deposition"').
        max_results: borne supérieure de résultats par requête.

    Returns:
        Liste d'articles unifiés (catégorie 'patent'). Vide en cas d'erreur.
    """
    from datetime import timedelta
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS_LIMIT)).strftime("%Y%m%d")

    # Querystring INTERNE (à URL-encoder dans le paramètre 'url=')
    inner = f"q={query}&num={max_results}&after=priority:{cutoff_date}&language=ENGLISH"
    url = f"https://patents.google.com/xhr/query?url={quote_plus(inner)}&exp="

    _respect_domain_cooldown(url)
    session = _get_session()
    try:
        response = session.get(
            url, timeout=20,
            headers={"Accept": "application/json", "Referer": "https://patents.google.com/"},
        )
        if response.status_code in (403, 429):
            _record_block(url, base_seconds=180.0)
            logger.warning(f"🚫 Google Patents : statut HTTP {response.status_code}")
            return []
        response.raise_for_status()
    except (RequestsError, OSError) as exc:
        logger.warning(f"❌ Google Patents : erreur réseau pour « {query[:60]}... » : {exc}")
        return []

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"❌ Google Patents : réponse non-JSON ({exc})")
        return []

    # Structure attendue : results.cluster[*].result[*].patent.{...}
    clusters = (data.get("results") or {}).get("cluster") or []
    articles: list[dict[str, Any]] = []
    for cluster in clusters:
        for item in (cluster.get("result") or []):
            patent = item.get("patent") or {}
            pub_num = (patent.get("publication_number") or "").strip()
            title   = (patent.get("title") or "").strip()
            if not pub_num or not title:
                continue
            snippet  = (patent.get("snippet") or "").strip()[:600]
            assignee = (patent.get("assignee") or "").strip()
            summary  = snippet
            if assignee:
                summary = f"{snippet} [Déposant : {assignee}]" if snippet else f"Déposant : {assignee}"
            articles.append({
                "title":        title,
                "link":         f"https://patents.google.com/patent/{pub_num}/en",
                "summary":      summary,
                "source":       f"Google Patents — {assignee or pub_num}",
                "category":     "patent",
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })
    logger.info(f"   └─ Google Patents : {len(articles)} brevet(s) pour « {query[:60]}... »")
    return articles


def fetch_google_news(query: str) -> list[dict[str, Any]] | None:
    # Rotation de locale (hl/gl/ceid) pour varier le profil vu par Google
    hl, gl, ceid = random.choice(_GNEWS_LOCALES)
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"

    _respect_domain_cooldown(url)
    session = _get_session()
    # Referer crédible : un humain arrive sur Google News depuis google.com ou via favori,
    # rarement via une URL nue avec query string.
    request_headers = {"Referer": "https://www.google.com/"}
    try:
        response = session.get(url, headers=request_headers, timeout=20)
        if response.status_code in (403, 429):
            _record_block(url, base_seconds=180.0 if response.status_code == 429 else 90.0)
            logger.warning(f"🚫 Google News : statut HTTP {response.status_code}")
            return None
        response.raise_for_status()

        # Détection précoce de blocage : si Google ne renvoie pas du XML c'est un captcha/HTML d'erreur
        if not response.text.lstrip().startswith("<?xml"):
            _record_block(url, base_seconds=120.0)
            logger.warning(f"⚠️ Réponse non-XML de Google News (probable blocage): {response.text[:120]}")
            return None

        feed = feedparser.parse(response.content)
        articles: list[dict[str, Any]] = []
        for entry in feed.entries:
            if not _is_recent(entry): continue
            source_name = entry.get("source", {}).get("title") or entry.get("publisher") or "Google News"
            articles.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": "",
                "source": f"Google News — {source_name}",
                "category": "news",
                "collected_at": datetime.now(timezone.utc).isoformat()
            })
        logger.info(f"   └─ {len(articles)} article(s) trouvé(s) pour: {query[:50]}...")
        return articles
    except (RequestsError, OSError) as e:
        logger.error(f"❌ Erreur réseau Google News: {e}")
        return None

_TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid",
    "_ga", "yclid", "msclkid", "spm",
)


def _normalize_url(url: str) -> str:
    """Normalise une URL pour le dédoublonnage : strip params trackers, slash final, fragment.

    WHY : la même page peut arriver via 3 sources différentes avec des UTM params variés.
    Sans normalisation, on envoie 3 fois le même article au filtrage IA (gaspille du quota)
    puis dans le digest (mauvaise expérience lecteur).
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parts = urlparse(url.strip())
        # Strip params trackers tout en gardant ceux qui changent réellement le contenu
        clean_qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                    if k.lower() not in _TRACKING_PARAMS]
        clean = parts._replace(
            query=urlencode(clean_qs),
            fragment="",                       # # navigation interne, jamais discriminant
            path=parts.path.rstrip("/") or "/", # 'foo/' et 'foo' = même page
        )
        return urlunparse(clean).lower()
    except (ValueError, TypeError):
        return url.strip().rstrip("/").lower()


# Suffixes typiques rajoutes par les flux d'agregation (Google News, etc.)
# Ex: "Article title - Source name" ou "Article title — Le Monde". On les retire
# pour comparer les titres entre flux RSS et flux d'agregation.
# WHY l'espace OBLIGATOIRE avant le tiret : sans ca, on stripperait les tirets
# internes des mots composes (ex: "Industrial-Grade Coatings" -> "Industrial").
_AGGREGATOR_TITLE_SUFFIX = re.compile(r"\s+[-–—]\s+[^-–—]{1,60}$")


def _normalize_title(title: str) -> str:
    """Normalise un titre pour la dedup : minuscules, ponctuation strippee,
    espaces normalises, suffixe ' - SourceName' typique de Google News retire.

    WHY : le meme article peut etre publie sur le site source ET releve par
    Google News, avec des URLs differentes (-> _normalize_url ne dedup pas)
    mais des titres quasi-identiques. Sans cette etape, on envoie le meme
    contenu deux fois au filtrage IA (gaspille du quota).
    """
    if not title:
        return ""
    s = title.strip()
    # Retire le suffixe " - Source" / " — Source" typique de Google News
    s = _AGGREGATOR_TITLE_SUFFIX.sub("", s)
    s = s.lower()
    # Strip ponctuation et caracteres speciaux (garde lettres/chiffres/espaces)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    # Normalise espaces multiples
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedup_by_title(articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Deduplique par titre normalise. Si plusieurs articles ont le meme titre,
    on garde celui dont le resume est le plus long (-> plus de signal pour l'IA).

    Retourne (articles_dedup, nb_doublons_supprimes).
    Articles sans titre exploitable sont conserves tels quels (pas de signal
    pour dedup).
    """
    by_title: dict[str, dict[str, Any]] = {}
    no_title: list[dict[str, Any]] = []
    for a in articles:
        norm = _normalize_title(a.get("title", ""))
        if not norm:
            no_title.append(a)
            continue
        existing = by_title.get(norm)
        if existing is None:
            by_title[norm] = a
            continue
        # Garde celui avec le resume le plus long
        if len(a.get("summary", "")) > len(existing.get("summary", "")):
            by_title[norm] = a
    deduped = list(by_title.values()) + no_title
    return deduped, len(articles) - len(deduped)


def run_scraper(
    include_rss:               bool = True,
    include_gnews:             bool = True,
    include_openalex:          bool = True,    # ON : gratuit, illimité, sans clé
    include_arxiv_search:      bool = True,    # ON : gratuit, complète les RSS arXiv
    include_crossref:          bool = True,    # ON : gratuit, sans clé, ~140M papers
    include_hal:               bool = True,    # ON : gratuit, sans clé, fort sur sources FR (CEA/CNRS/ONERA)
    include_semantic_scholar:  bool = True,    # ON : gratuit (rate-limit 1 r/s sans clé), ~200M papers
    include_web:               bool = False,   # OFF : nécessite TAVILY_API_KEY
    include_patents:           bool = True,    # ON : gratuit, sans clé, brevets industriels PVD/CVD/ALD
    apply_filter:              bool = True,
) -> dict[str, Any]:
    all_articles: list[dict[str, Any]] = []

    # Liste ordonnée des blocs activés, pour insérer une pause inter-source seulement
    # entre 2 blocs réellement exécutés (pas en début ni après le dernier).
    _executed_blocks: list[str] = []

    def _maybe_inter_source_pause(next_label: str) -> None:
        if _executed_blocks:
            _inter_source_break(f"{_executed_blocks[-1]} → {next_label}")

    if include_rss:
        _maybe_inter_source_pause("RSS")
        for source in SOURCES_RSS:
            logger.info(f"📡 Récupération RSS : {source.get('name')}")
            all_articles.extend(fetch_rss_feed(source))
            # Pause inter-RSS allongée (10-30s) — un humain ne consulte pas 10 flux en 10s
            time.sleep(max(8.0, random.gauss(15.0, 5.0)))
        _executed_blocks.append("RSS")

    if include_arxiv_search:
        _maybe_inter_source_pause("arXiv")
        ax_queries = build_arxiv_search_queries()
        # Pre-flight : 1 requete test pour detecter un ban IP avant de lancer
        # 75 requetes qui echoueraient toutes. Economise ~9 min vs circuit breaker.
        # Si le pre-flight passe : on lance le bloc complet.
        # Si 429/403 : on skip directement, message clair, le pipeline continue.
        logger.info(f"🔬 arXiv pre-flight : test connectivite sur 1 requete simple…")
        preflight = fetch_arxiv_search('ti:"thin film"', max_results=1)
        if preflight is None:
            logger.error(
                "🛑 arXiv pre-flight : HTTP 429/403 — ton IP est probablement en cooldown "
                "cote serveur arXiv (suite a un ban temporaire). Abandon de arXiv search "
                "pour CE run. Pas d'inquietude : OpenAlex + Crossref + Semantic Scholar "
                "couvrent ~85-95%% de l'index arXiv. Reessaie dans 4-6h pour reset."
            )
        else:
            logger.info(
                f"🔬 arXiv pre-flight OK ({len(preflight)} resultat(s)) — lancement "
                f"de {len(ax_queries)} requetes thematiques."
            )
            # Circuit breaker : si arXiv blackliste l'IP (429/403 consecutifs) en cours
            # de run (rare apres pre-flight), on abandonne pour preserver le temps.
            _ARXIV_MAX_CONSEC_BLOCKS = 3
            consecutive_blocks = 0
            # Le pre-flight a deja ramene 1 article, on l'inclut dans la collecte.
            all_articles.extend(preflight)
            for idx, q in enumerate(ax_queries, 1):
                logger.info(f"🔬 arXiv search [{idx}/{len(ax_queries)}] : « {q[:80]}... »")
                result = fetch_arxiv_search(q)
                if result is None:
                    consecutive_blocks += 1
                    if consecutive_blocks >= _ARXIV_MAX_CONSEC_BLOCKS:
                        logger.error(
                            f"🛑 arXiv : {_ARXIV_MAX_CONSEC_BLOCKS} blocages consecutifs (429/403) — "
                            "abandon de arXiv search pour ce run. Les autres sources continuent."
                        )
                        break
                else:
                    consecutive_blocks = 0  # reset si une requete passe
                    all_articles.extend(result)
                # arXiv recommande >=3s, on monte a 15-30s pour rester ultra-poli
                if idx < len(ax_queries):
                    time.sleep(max(12.0, random.gauss(20.0, 5.0)))
            _executed_blocks.append("arXiv")

    if include_openalex:
        _maybe_inter_source_pause("OpenAlex")
        oa_queries = build_openalex_queries()
        logger.info(f"📚 Lancement OpenAlex : {len(oa_queries)} requêtes thématiques.")
        for idx, q in enumerate(oa_queries, 1):
            logger.info(f"📚 OpenAlex [{idx}/{len(oa_queries)}] : « {q[:80]}... »")
            all_articles.extend(fetch_openalex_works(q))
            if idx < len(oa_queries):
                time.sleep(max(5.0, random.gauss(10.0, 3.0)))
        _executed_blocks.append("OpenAlex")

    if include_crossref:
        _maybe_inter_source_pause("Crossref")
        cr_queries = build_crossref_queries()
        logger.info(f"📖 Lancement Crossref : {len(cr_queries)} requêtes thématiques.")
        for idx, q in enumerate(cr_queries, 1):
            logger.info(f"📖 Crossref [{idx}/{len(cr_queries)}] : « {q[:80]}... »")
            all_articles.extend(fetch_crossref_works(q))
            if idx < len(cr_queries):
                time.sleep(max(5.0, random.gauss(10.0, 3.0)))
        _executed_blocks.append("Crossref")

    if include_hal:
        _maybe_inter_source_pause("HAL")
        hal_queries = build_hal_queries()
        logger.info(f"🇫🇷 Lancement HAL (CNRS) : {len(hal_queries)} requêtes thématiques.")
        for idx, q in enumerate(hal_queries, 1):
            logger.info(f"🇫🇷 HAL [{idx}/{len(hal_queries)}] : « {q[:80]}... »")
            all_articles.extend(fetch_hal_publications(q))
            if idx < len(hal_queries):
                time.sleep(max(5.0, random.gauss(10.0, 3.0)))
        _executed_blocks.append("HAL")

    if include_semantic_scholar:
        _maybe_inter_source_pause("SemanticScholar")
        ss_queries = build_semantic_scholar_queries()
        logger.info(f"🧠 Lancement Semantic Scholar : {len(ss_queries)} requêtes thématiques.")
        for idx, q in enumerate(ss_queries, 1):
            logger.info(f"🧠 Semantic Scholar [{idx}/{len(ss_queries)}] : « {q[:80]}... »")
            all_articles.extend(fetch_semantic_scholar(q))
            # Rate-limit public ~1 req/s ; on monte largement au-dessus
            if idx < len(ss_queries):
                time.sleep(max(8.0, random.gauss(12.0, 3.0)))
        _executed_blocks.append("SemanticScholar")

    if include_web:
        _maybe_inter_source_pause("Tavily")
        web_queries = build_web_queries()
        logger.info(f"🌐 Lancement de Tavily Web Search : {len(web_queries)} requêtes thématiques.")
        for idx, q in enumerate(web_queries, 1):
            logger.info(f"🌐 Tavily [{idx}/{len(web_queries)}] : « {q[:80]}... »")
            web_results = fetch_broad_web_search(q)
            all_articles.extend(web_results)
            if idx < len(web_queries):
                time.sleep(max(5.0, random.gauss(10.0, 3.0)))
        _executed_blocks.append("Tavily")

    if include_patents:
        _maybe_inter_source_pause("Patents")
        pat_queries = build_patents_queries()
        logger.info(f"📜 Lancement Google Patents : {len(pat_queries)} requêtes thématiques.")
        for idx, q in enumerate(pat_queries, 1):
            logger.info(f"📜 Google Patents [{idx}/{len(pat_queries)}] : « {q[:80]}... »")
            all_articles.extend(fetch_google_patents(q))
            # Pause inter-requête : Patents tolère bien plus que GNews mais
            # on reste poli (8-15s) pour éviter ban IP comportemental.
            if idx < len(pat_queries):
                time.sleep(max(6.0, random.gauss(10.0, 3.0)))
        _executed_blocks.append("Patents")

    if include_gnews:
        _maybe_inter_source_pause("GoogleNews")
        queries = build_gnews_queries()
        # Shuffle aléatoire de l'ordre des requêtes : un humain ne fait pas
        # « toutes les recherches Oerlikon » puis « toutes les recherches Bodycote »
        # de manière alphabétique. Mélanger casse ce pattern qui est sinon trivialement
        # détectable côté Google (séquence de N requêtes corrélées au même nom).
        random.shuffle(queries)
        logger.info(
            f"🚀 Lancement de Google News : {len(queries)} requêtes unitaires "
            f"(entreprise × mot-clé), ordre randomisé."
        )

        # Configuration anti-détection comportementale GNews (mode weekend).
        _GNEWS_ROTATE_EVERY        = 30     # rotation session toutes les 30 req (10 identités au lieu de 6)
        _GNEWS_MAX_CONSEC_BLOCKS   = 3      # circuit breaker : 3 strikes consécutifs = abandon
        _GNEWS_CIRCADIAN_AFTER_HRS = 6.0    # pause "humain qui dort" après 6h de run continu
        _GNEWS_CIRCADIAN_DUR_HRS   = (4.0, 6.0)  # durée tirée aléatoirement dans cet intervalle

        gnews_start_time      = time.monotonic()
        consecutive_blocks    = 0
        circadian_pause_done  = False

        for idx, q in enumerate(queries, 1):
            # Pause circadienne : après ~6h de run, simuler une nuit de sommeil
            elapsed_hours = (time.monotonic() - gnews_start_time) / 3600.0
            if not circadian_pause_done and elapsed_hours >= _GNEWS_CIRCADIAN_AFTER_HRS:
                sleep_hours = random.uniform(*_GNEWS_CIRCADIAN_DUR_HRS)
                logger.info(f"🌙 Pause circadienne {sleep_hours:.1f}h "
                            f"({idx}/{len(queries)} req déjà faites) — simulation 'humain qui dort'.")
                _save_checkpoint(all_articles, f"circadian_pause_at_{idx}/{len(queries)}")
                _reset_session()
                time.sleep(sleep_hours * 3600.0)
                circadian_pause_done = True

            logger.info(f"🔍 Google News [{idx}/{len(queries)}] : « {q[:80]}... »")
            news = fetch_google_news(q)

            if news is None:
                consecutive_blocks += 1
                if consecutive_blocks >= _GNEWS_MAX_CONSEC_BLOCKS:
                    logger.error(f"🛑 {_GNEWS_MAX_CONSEC_BLOCKS} blocages consécutifs — "
                                 f"abandon de Google News. Les autres sources sont conservées.")
                    _save_checkpoint(all_articles, f"aborted_at_{idx}/{len(queries)}")
                    break
                # Blocage transitoire : long break + reset session, puis on retente
                recovery_break = max(900.0, random.gauss(1500.0, 300.0))  # ~25 min
                logger.warning(f"⚠️ Blocage transitoire ({consecutive_blocks}/{_GNEWS_MAX_CONSEC_BLOCKS}) — "
                               f"récupération {recovery_break:.0f}s + nouvelle identité réseau.")
                _save_checkpoint(all_articles, f"recovery_at_{idx}/{len(queries)}")
                _reset_session()
                time.sleep(recovery_break)
                continue

            consecutive_blocks = 0
            all_articles.extend(news)
            logger.info(f"   └─ Total cumulé : {len(all_articles)} articles bruts")

            if idx % _CHECKPOINT_EVERY == 0:
                _save_checkpoint(all_articles, f"{idx}/{len(queries)}")

            if idx == len(queries):
                break

            # Tous les N req : long break humain (~10 min) + nouvelle identité réseau
            if idx % _GNEWS_ROTATE_EVERY == 0:
                long_break = max(360.0, random.gauss(600.0, 150.0))
                logger.info(f"😴 Break humain {long_break:.0f}s + rotation de session "
                            f"({idx}/{len(queries)} req)…")
                _reset_session()
                time.sleep(long_break)
                continue

            base_delay = _humanlike_inter_request_delay()
            logger.debug(f"⏱️ Pause {base_delay:.1f}s avant la requête suivante...")
            time.sleep(base_delay)
        _executed_blocks.append("GoogleNews")

    # Dédoublonnage URL avec normalisation (strip UTM, fragment, slash final)
    unique_dict: dict[str, dict[str, Any]] = {}
    for a in all_articles:
        link = a.get("link", "")
        if not link:
            continue
        norm = _normalize_url(link)
        if norm and norm not in unique_dict:
            unique_dict[norm] = a
    raw_articles = list(unique_dict.values())
    deduped_count = len(all_articles) - len(raw_articles)
    if deduped_count > 0:
        logger.info(f"🧹 Dédoublonnage URL : {deduped_count} doublon(s) éliminé(s).")

    # Dédoublonnage par titre normalisé : capture le cas où le même article
    # est référencé par RSS source ET Google News avec des URLs distinctes.
    # Économise du quota Gemini (un article retenu dans le digest = un seul exemplaire).
    raw_articles, title_dup_count = _dedup_by_title(raw_articles)
    if title_dup_count > 0:
        logger.info(
            f"🧹 Dédoublonnage TITRE : {title_dup_count} doublon(s) éliminé(s) "
            f"(même titre depuis sources différentes)."
        )
    deduped_count += title_dup_count

    # Filtrage mémoire (FIFO) : on compare les URLs normalisées vs seen_urls (qui contient des URLs brutes existantes)
    # Pour la rétro-compat, seen_urls.json continue à stocker les URLs brutes ; le check se fait sur la normalisée.
    # Note : on capture seen_normalized AVANT la mise à jour pour pouvoir tagger
    # was_seen sur chaque article (utilisé par mailer.py pour afficher un badge
    # "Déjà envoyé" en mode TOUT_RENVOYER).
    seen_normalized = {_normalize_url(u) for u in load_seen_urls()}
    for a in raw_articles:
        a["was_seen"] = _normalize_url(a.get("link", "")) in seen_normalized
    if USE_MEMORY:
        raw_articles = [a for a in raw_articles if not a["was_seen"]]

    seen_raw = list(load_seen_urls())
    for a in raw_articles:
        seen_raw.append(a["link"])
    save_seen_urls(seen_raw)

    # Métriques par source pour observabilité (utile pour détecter une régression)
    source_counts: dict[str, int] = {}
    for a in raw_articles:
        src = a.get("source", "?")
        # On agrège par "famille" (ex: tous les Google News ensemble)
        family = src.split(" — ")[0] if " — " in src else src
        source_counts[family] = source_counts.get(family, 0) + 1
    if source_counts:
        logger.info("📊 Répartition par source :")
        for family, count in sorted(source_counts.items(), key=lambda kv: kv[1], reverse=True):
            logger.info(f"   • {family}: {count}")

    logger.info(f"✅ Collecte terminée : {len(raw_articles)} nouveaux articles.")
    return {
        "meta": {
            "total_raw":     len(raw_articles),
            "run_at":        datetime.now(timezone.utc).isoformat(),
            "source_counts": source_counts,
            "deduped":       deduped_count,
        },
        "articles": raw_articles,
    }

if __name__ == "__main__":
    res = run_scraper()
    print(f"Total collecté : {len(res['articles'])}")