# =============================================================================
# epo_enrichment.py — Enrichissement brevets via EPO OPS (European Patent Office)
# =============================================================================
#
# Permet d'enrichir les brevets Google Patents avec :
#   - Citations FORWARD (combien de brevets posterieurs citent celui-ci = signal de force)
#   - Family size (nombre d'extensions internationales = signal de protection geographique)
#   - Classifications CPC complete
#
# GRATUIT mais necessite INSCRIPTION sur https://developers.epo.org (free tier).
#
# ━━━ INSCRIPTION EPO OPS (etape par etape) ━━━
#   1. Aller sur https://developers.epo.org
#   2. Cliquer "Sign up" (haut a droite)
#   3. Remplir le formulaire (email + mot de passe + nom)
#   4. Confirmer l'email (verifier inbox)
#   5. Se connecter, cliquer "My apps" dans le menu utilisateur
#   6. Cliquer "Create a new app" → donner un nom (ex: "Veille PVD")
#   7. Selectionner toutes les APIs disponibles (gratuit)
#   8. Recuperer "Consumer Key" et "Consumer Secret"
#   9. Ajouter dans .env :
#        EPO_CONSUMER_KEY=ta_consumer_key
#        EPO_CONSUMER_SECRET=ton_consumer_secret
#   10. Relancer python main.py — l'enrichissement s'active automatiquement.
#
# Quotas gratuits : 4 GB / semaine, ~4M requetes / semaine.
# Cache local : data/epo_cache.json (evite les re-calls inutiles).
# =============================================================================

from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("epo_enrichment")


# =============================================================================
# Configuration
# =============================================================================

EPO_CONSUMER_KEY: str = os.environ.get("EPO_CONSUMER_KEY", "")
EPO_CONSUMER_SECRET: str = os.environ.get("EPO_CONSUMER_SECRET", "")
EPO_ENRICHMENT_ENABLED: bool = bool(EPO_CONSUMER_KEY and EPO_CONSUMER_SECRET)
# Cap : 20 brevets enrichis par run pour proteger le quota
EPO_MAX_ENRICHED_PER_RUN: int = int(os.environ.get("EPO_MAX_ENRICHED_PER_RUN", "20"))
# Minimum score IA pour qu'un brevet soit enrichi (eviter de gaspiller sur les 1-2★)
EPO_MIN_SCORE: int = int(os.environ.get("EPO_MIN_SCORE", "3"))

_BASE_URL = "https://ops.epo.org/3.2"
_AUTH_URL = f"{_BASE_URL}/auth/accesstoken"
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_CACHE_PATH = os.path.join(_DATA_DIR, "epo_cache.json")
# Cache TTL : un brevet n'est ré-enrichi que si > 60j depuis dernier enrichissement
_CACHE_TTL_DAYS = int(os.environ.get("EPO_CACHE_TTL_DAYS", "60"))


# =============================================================================
# Cache (evite les re-calls inutiles)
# =============================================================================

def _load_cache() -> dict[str, Any]:
    data = safe_read_json(_CACHE_PATH, default={"entries": {}})
    if not isinstance(data, dict):
        return {"entries": {}}
    data.setdefault("entries", {})
    return data


def _save_cache(data: dict[str, Any]) -> None:
    try:
        atomic_write_json(_CACHE_PATH, data)
    except OSError as e:
        logger.warning(f"⚠️ Persistance epo_cache.json impossible : {e}")


def _cache_is_fresh(entry: dict[str, Any]) -> bool:
    """Verifie si une entree cache est encore valide (< TTL)."""
    enriched_at = entry.get("enriched_at", "")
    if not enriched_at:
        return False
    try:
        d = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - d).total_seconds() / 86400
        return age_days < _CACHE_TTL_DAYS
    except (ValueError, TypeError):
        return False


# =============================================================================
# OAuth2 — obtenir un access_token EPO OPS
# =============================================================================

_access_token: str | None = None
_token_expires_at: float = 0.0


def _get_access_token() -> str | None:
    """OAuth2 client_credentials flow.

    Cache le token en memoire pour la duree de vie (20 min typique).
    """
    global _access_token, _token_expires_at
    now = time.time()
    if _access_token and now < _token_expires_at - 60:
        return _access_token
    if not EPO_ENRICHMENT_ENABLED:
        return None

    try:
        import requests
        creds = base64.b64encode(
            f"{EPO_CONSUMER_KEY}:{EPO_CONSUMER_SECRET}".encode()
        ).decode()
        response = requests.post(
            _AUTH_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
            timeout=15,
        )
        if response.status_code != 200:
            logger.warning(
                f"⚠️ EPO OPS : auth echec HTTP {response.status_code}. "
                "Verifier EPO_CONSUMER_KEY + EPO_CONSUMER_SECRET dans .env."
            )
            return None
        token_data = response.json()
        _access_token = token_data.get("access_token")
        expires_in = int(token_data.get("expires_in", "1200"))
        _token_expires_at = now + expires_in
        return _access_token
    except (ImportError, ValueError, KeyError, OSError) as e:
        logger.warning(f"⚠️ EPO OPS auth : {e}")
        return None


# =============================================================================
# Normalisation des numeros de publication (Google → EPO format)
# =============================================================================

_GPATENT_NUM_RE = re.compile(r"^([A-Z]{2})([\d/-]+?)([A-Z]\d?)?$")


def _to_epodoc(pub_num: str) -> str | None:
    """Convertit un numero Google Patents en format EPODOC accepte par EPO OPS.

    Exemples :
      US10867856 → US.10867856
      EP3456789A1 → EP.3456789.A1
      WO2020012345 → WO.2020012345
    """
    if not pub_num:
        return None
    pub_num = pub_num.strip().replace(" ", "")
    m = _GPATENT_NUM_RE.match(pub_num)
    if not m:
        return None
    country, number, kind = m.group(1), m.group(2), m.group(3) or ""
    number = number.replace("-", "").replace("/", "")
    return f"{country}.{number}.{kind}" if kind else f"{country}.{number}"


# =============================================================================
# Enrichissement : citations forward + family
# =============================================================================

def _fetch_epo_endpoint(path: str, token: str) -> dict[str, Any] | None:
    """Generic GET sur EPO OPS API avec gestion robuste des erreurs."""
    try:
        import requests
        response = requests.get(
            f"{_BASE_URL}/{path.lstrip('/')}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/json",
            },
            timeout=15,
        )
        if response.status_code == 403:
            logger.info("ℹ️ EPO OPS : quota epuise (HTTP 403). Skip pour ce run.")
            return None
        if response.status_code == 404:
            return {}  # Brevet pas dans la base EPO (souvent les patents US recents)
        if response.status_code != 200:
            return None
        return response.json()
    except (ImportError, ValueError, OSError) as e:
        logger.debug(f"EPO OPS fetch : {e}")
        return None


def _enrich_single_patent(pub_num: str, token: str) -> dict[str, Any]:
    """Recupere citations forward + family pour un brevet.

    Retourne dict avec citations_count, family_size, source_epo (bool).
    Vide si echec.
    """
    epodoc = _to_epodoc(pub_num)
    if not epodoc:
        return {}

    enriched: dict[str, Any] = {"source_epo": True}

    # Citations forward : list of patents citing this one
    citation_data = _fetch_epo_endpoint(
        f"rest-services/published-data/publication/epodoc/{epodoc}/citation",
        token,
    )
    if citation_data:
        try:
            doc_list = (
                citation_data.get("ops:world-patent-data", {})
                .get("ops:patent-family", {})
                .get("ops:family-member", [])
            )
            if isinstance(doc_list, dict):
                doc_list = [doc_list]
            enriched["citations_count"] = len(doc_list or [])
        except (AttributeError, TypeError, KeyError):
            pass

    # Family (par defaut tres rapide, juste compter les membres)
    family_data = _fetch_epo_endpoint(
        f"rest-services/family/publication/epodoc/{epodoc}",
        token,
    )
    if family_data:
        try:
            members = (
                family_data.get("ops:world-patent-data", {})
                .get("ops:patent-family", {})
                .get("ops:family-member", [])
            )
            if isinstance(members, dict):
                members = [members]
            enriched["family_size"] = len(members or [])
        except (AttributeError, TypeError, KeyError):
            pass

    enriched["enriched_at"] = datetime.now(timezone.utc).isoformat()
    return enriched


def enrich_patents(articles: list[dict[str, Any]]) -> int:
    """Enrichit les top brevets avec donnees EPO OPS (citations + family).

    Filtre :
      - category == "patent"
      - score >= EPO_MIN_SCORE
      - patent_pub_num present
      - non deja enrichi dans le cache TTL recent

    Cap EPO_MAX_ENRICHED_PER_RUN pour proteger le quota.

    Returns:
        Nombre de brevets enrichis avec succes.
    """
    if not EPO_ENRICHMENT_ENABLED:
        return 0
    if not articles:
        return 0

    # Selectionner les brevets eligibles
    patents = [
        a for a in articles
        if a.get("category") == "patent"
        and int(a.get("score", 0)) >= EPO_MIN_SCORE
        and a.get("patent_pub_num")
    ]
    # Tri par score decroissant pour prioriser le top
    patents.sort(key=lambda a: int(a.get("score", 0)), reverse=True)
    candidates = patents[:EPO_MAX_ENRICHED_PER_RUN]
    if not candidates:
        return 0

    token = _get_access_token()
    if not token:
        logger.info("ℹ️ EPO OPS : token indispo, skip enrichissement.")
        return 0

    cache = _load_cache()
    cache_entries: dict[str, Any] = cache["entries"]
    n_enriched = 0
    n_from_cache = 0

    for art in candidates:
        pub_num = art["patent_pub_num"]
        cached = cache_entries.get(pub_num)
        if cached and _cache_is_fresh(cached):
            # Cache hit
            art["epo_citations_count"] = cached.get("citations_count")
            art["epo_family_size"] = cached.get("family_size")
            n_from_cache += 1
            continue

        enriched = _enrich_single_patent(pub_num, token)
        if enriched:
            art["epo_citations_count"] = enriched.get("citations_count")
            art["epo_family_size"] = enriched.get("family_size")
            cache_entries[pub_num] = enriched
            n_enriched += 1
            # Politesse entre requetes
            time.sleep(0.5)

    if n_enriched > 0 or n_from_cache > 0:
        cache["entries"] = cache_entries
        _save_cache(cache)
        logger.info(
            f"🔍 EPO OPS : {n_enriched} brevet(s) enrichi(s) en live, "
            f"{n_from_cache} depuis cache."
        )
    return n_enriched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    if not EPO_ENRICHMENT_ENABLED:
        print("EPO OPS desactive (EPO_CONSUMER_KEY/SECRET manquants).")
        print("Voir https://developers.epo.org pour s'inscrire (gratuit).")
        print("Cf. instructions detaillees dans MANUEL.md section 'EPO OPS'.")
    else:
        # Test sur un brevet d'exemple
        sample = [{
            "category": "patent",
            "score": 5,
            "patent_pub_num": "EP3456789A1",
        }]
        enrich_patents(sample)
        print(f"Citations: {sample[0].get('epo_citations_count')}, Family: {sample[0].get('epo_family_size')}")
