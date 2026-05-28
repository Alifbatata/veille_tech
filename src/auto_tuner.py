# =============================================================================
# auto_tuner.py — Boucle d'amelioration continue du systeme de veille
# =============================================================================
#
# Ferme la boucle de feedback automatique :
#
#   1. Backup atomique de targets.json (rotation 10 derniers) avant toute modif
#   2. Auto-promote v2 : acteurs decouverts -> targets.json
#      (seuil bas count >= 5 + stickiness >= 2 runs distincts)
#   3. Auto-purge : retire les cibles STERILE (jamais de hit apres N runs)
#      conservateur : solo_keywords / cross_domain_topics / research_orgs uniquement
#      (les companies/keywords sont combines en OR-groups GNews, attribution
#       individuelle impossible)
#   4. Auto-expansion : recalcule des tiers Hot/Standard/Cold par requete pour
#      ajuster `max_results` au prochain run (economie bandwidth + +hits)
#
# Toutes les actions sont conservatrices : on n'agit qu'avec assez de signal
# historique, on respecte un cap par run pour eviter les degats massifs, et tout
# est rollback-able via data/archived_targets.json + data/backups/.
#
# Mode dry-run via env AUTO_TUNE_DRY_RUN=true (log mais ne touche pas a disque).
# =============================================================================

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("auto_tuner")


# =============================================================================
# Configuration via variables d'environnement
# =============================================================================

AUTO_TUNE_ENABLED: bool = os.environ.get("AUTO_TUNE_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_TUNE_DRY_RUN: bool = os.environ.get("AUTO_TUNE_DRY_RUN", "false").lower() in ("true", "1", "yes")

# Auto-purge des cibles steriles
AUTO_PURGE_ENABLED: bool = os.environ.get("AUTO_PURGE_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_PURGE_MIN_RUNS: int = int(os.environ.get("AUTO_PURGE_MIN_RUNS", "8"))
AUTO_PURGE_MIN_CONSECUTIVE_ZEROS: int = int(os.environ.get("AUTO_PURGE_MIN_CONSECUTIVE_ZEROS", "8"))
AUTO_PURGE_MAX_PER_RUN: int = int(os.environ.get("AUTO_PURGE_MAX_PER_RUN", "5"))

# Auto-promote v2 (seuil bas + stickiness)
AUTO_PROMOTE_MIN_COUNT: int = int(os.environ.get("AUTO_PROMOTE_MIN_COUNT", "5"))
AUTO_PROMOTE_MIN_RUNS: int = int(os.environ.get("AUTO_PROMOTE_MIN_RUNS", "2"))
AUTO_PROMOTE_MAX_PER_RUN: int = int(os.environ.get("AUTO_PROMOTE_MAX_PER_RUN", "10"))

# Auto-expansion par tier
AUTO_EXPAND_ENABLED: bool = os.environ.get("AUTO_EXPAND_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_EXPAND_HOT_MULTIPLIER: float = float(os.environ.get("AUTO_EXPAND_HOT_MULTIPLIER", "1.5"))
AUTO_EXPAND_COLD_MULTIPLIER: float = float(os.environ.get("AUTO_EXPAND_COLD_MULTIPLIER", "0.5"))
AUTO_EXPAND_HOT_PERCENTILE: int = int(os.environ.get("AUTO_EXPAND_HOT_PERCENTILE", "10"))
AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS: int = int(os.environ.get("AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS", "3"))

# Chemins
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_TARGETS_PATH = os.path.join(_DATA_DIR, "targets.json")
_QUERY_STATS_PATH = os.path.join(_DATA_DIR, "query_stats.json")
_DISCOVERED_ACTORS_PATH = os.path.join(_DATA_DIR, "discovered_actors.json")
_ARCHIVED_TARGETS_PATH = os.path.join(_DATA_DIR, "archived_targets.json")
_BACKUPS_DIR = os.path.join(_DATA_DIR, "backups")
_BACKUP_ROTATION = 10

# Cache des tiers (recalcule au demarrage de chaque run via invalidate_tier_cache())
_tier_cache: dict[str, str] | None = None


# =============================================================================
# Backups
# =============================================================================

def backup_targets() -> str | None:
    """Snapshot horodate de targets.json dans data/backups/ (rotation _BACKUP_ROTATION).

    Returns:
        Chemin du backup cree, ou None si source absente / erreur.
    """
    if not os.path.exists(_TARGETS_PATH):
        return None
    os.makedirs(_BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(_BACKUPS_DIR, f"targets_{stamp}.json")
    try:
        shutil.copy2(_TARGETS_PATH, backup_path)
    except OSError as e:
        logger.warning(f"⚠️ Backup targets.json impossible : {e}")
        return None
    try:
        backups = sorted(
            f for f in os.listdir(_BACKUPS_DIR)
            if f.startswith("targets_") and f.endswith(".json")
        )
        for old in backups[:-_BACKUP_ROTATION]:
            try:
                os.remove(os.path.join(_BACKUPS_DIR, old))
            except OSError:
                pass
    except OSError:
        pass
    return backup_path


# =============================================================================
# Normalisation pour matching cible <-> query
# =============================================================================

def _aggressive_normalize(s: str) -> str:
    """Lowercase + strip ponctuation entourante + collapse whitespace.

    Permet de comparer 'Atomic layer deposition' (target) avec
    '\"Atomic layer deposition\"' (query OpenAlex quoted).
    """
    s = (s or "").strip().lower()
    # Strip quotes externes (peuvent etre echappees ou non selon la source)
    s = s.strip().strip('"').strip("'").strip()
    s = re.sub(r"\s+", " ", s)
    return s


# =============================================================================
# Auto-purge : retire les cibles steriles
# =============================================================================

def _index_queries_by_target(historical: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Construit un index normalize(query_text) -> liste d'entries (toutes sources confondues).

    Permet de juger une cible donnee en regardant TOUTES les sources qui l'ont essayee.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in historical.values():
        q = entry.get("query", "")
        if not q:
            continue
        key = _aggressive_normalize(q)
        index.setdefault(key, []).append(entry)
    return index


def _is_target_sterile(target_norm: str, query_index: dict[str, list[dict[str, Any]]]) -> bool:
    """Une cible est STERILE si :
      - elle a au moins 1 query enregistree ET
      - AUCUNE source ne lui a jamais donne de hit (hits_total == 0 partout) ET
      - sur AU MOINS UNE source elle a runs_total >= MIN_RUNS et consecutive_zeros >= MIN_ZEROS

    Si une seule source remonte ne serait-ce qu'un hit historique, la cible reste.
    """
    entries = query_index.get(target_norm)
    if not entries:
        return False
    # Au moins une source doit avoir hits_total > 0 → productive, ne pas purger
    if any(e.get("hits_total", 0) > 0 for e in entries):
        return False
    # Maintenant on cherche la preuve qu'on a essaye assez longtemps sans succes
    has_strong_sterile_signal = any(
        e.get("runs_total", 0) >= AUTO_PURGE_MIN_RUNS
        and e.get("consecutive_zeros", 0) >= AUTO_PURGE_MIN_CONSECUTIVE_ZEROS
        for e in entries
    )
    return has_strong_sterile_signal


def auto_purge_sterile_targets() -> dict[str, list[str]]:
    """Retire les solo_keywords / cross_domain_topics / research_orgs qui sont
    STERILE depuis assez longtemps (jamais de hit, runs cumul >= seuil).

    Companies + keywords ne sont PAS purges (combines en OR-groups dans GNews,
    attribution individuelle des hits impossible).

    Returns:
        {"solo_keywords": [...], "cross_domain_topics": [...], "research_orgs": [...]}
    """
    empty = {"solo_keywords": [], "cross_domain_topics": [], "research_orgs": []}
    if not AUTO_PURGE_ENABLED:
        return empty

    stats_data = safe_read_json(_QUERY_STATS_PATH, default={})
    historical = stats_data.get("queries", {}) if isinstance(stats_data, dict) else {}
    if not historical:
        return empty

    query_index = _index_queries_by_target(historical)

    targets_data = safe_read_json(_TARGETS_PATH, default={})
    if not isinstance(targets_data, dict):
        return empty

    purged: dict[str, list[str]] = {"solo_keywords": [], "cross_domain_topics": [], "research_orgs": []}
    remaining_cap = AUTO_PURGE_MAX_PER_RUN

    for field in ("solo_keywords", "cross_domain_topics", "research_orgs"):
        original = list(targets_data.get(field, []))
        new_list: list[str] = []
        for item in original:
            norm = _aggressive_normalize(item)
            if remaining_cap > 0 and _is_target_sterile(norm, query_index):
                purged[field].append(item)
                remaining_cap -= 1
            else:
                new_list.append(item)
        targets_data[field] = new_list

    total_purged = sum(len(v) for v in purged.values())
    if total_purged == 0:
        return purged

    if AUTO_TUNE_DRY_RUN:
        logger.info(f"🧪 [DRY-RUN] Auto-purge : {total_purged} cible(s) auraient ete supprimee(s) (non applique).")
        for field, items in purged.items():
            if items:
                logger.info(f"   • {field} : {', '.join(items[:5])}{'...' if len(items) > 5 else ''}")
        return purged

    backup_path = backup_targets()
    if backup_path:
        logger.info(f"💾 Backup pre-purge : {os.path.basename(backup_path)}")

    archived = safe_read_json(_ARCHIVED_TARGETS_PATH, default={"archived": []})
    if not isinstance(archived, dict):
        archived = {"archived": []}
    archived.setdefault("archived", []).append({
        "date":   datetime.now(timezone.utc).isoformat(),
        "purged": purged,
        "reason": f"runs_total>={AUTO_PURGE_MIN_RUNS} AND hits_total==0 AND consecutive_zeros>={AUTO_PURGE_MIN_CONSECUTIVE_ZEROS}",
    })
    archived["archived"] = archived["archived"][-50:]
    try:
        atomic_write_json(_ARCHIVED_TARGETS_PATH, archived)
    except OSError as e:
        logger.warning(f"⚠️ Sauvegarde archive impossible : {e}")

    try:
        from src.config import save_targets
        save_targets(
            targets_data.get("companies", []),
            targets_data.get("keywords", []),
            targets_data.get("solo_keywords", []),
            targets_data.get("research_orgs", []),
            targets_data.get("cross_domain_topics", []),
        )
    except (OSError, ImportError, ValueError) as e:
        logger.warning(f"⚠️ Sauvegarde targets.json (purge) impossible : {e}")
        return empty

    logger.info(f"🗑️ Auto-purge : {total_purged} cible(s) sterile(s) retiree(s) (rollback : data/archived_targets.json) :")
    for field, items in purged.items():
        if items:
            logger.info(f"   • {field} : {', '.join(items)}")
    return purged


# =============================================================================
# Auto-promote v2 (seuil bas + stickiness inter-runs)
# =============================================================================

_LAB_KEYWORDS = (
    "university", "université", "universidad", "universität", "universiteit", "universita",
    "institute", "institut", "instituto",
    "academy", "académie", "akademie", "academia",
    "college",
    "school of", "école", "escola",
    "laboratory", "laboratoire", "laboratorio",
    "national center", "national centre", "centre national", "centro nacional",
    "research center", "research centre", "centre de recherche",
    "polytechnic", "politecnico", "polytechnique",
    "fraunhofer", "helmholtz", "max planck", "leibniz", "cnrs", "inserm", "inria",
    "synchrotron", "observatory", "observatoire",
    "hochschule",
    " epfl ", " ethz ", " mit ", " caltech ", " kaist ",
)
_COMPANY_SUFFIXES = (
    " gmbh", " ag ", " inc", " inc.", " ltd", " ltd.", " corp", " corp.", " llc",
    " s.a.", " s.a", " b.v.", " bv", " co.", " k.k.", " pty", " s.r.l.", " s.p.a.",
    " sa ", " plc", " ab ", " oy",
    " technologies", " systems", " industries", " manufacturing",
    " coatings", " materials",
)


def _classify_actor(name: str, sources: list[str]) -> str:
    """Retourne 'research_org' ou 'company'.

    Heuristique :
      1. mot-cle labo dans le nom -> research_org
      2. suffixe entreprise (GmbH/Inc/Ltd/...) -> company
      3. openalex seul -> research_org (OpenAlex indexe les institutions)
      4. defaut -> company
    """
    lname = " " + name.lower() + " "
    if any(kw in lname for kw in _LAB_KEYWORDS):
        return "research_org"
    if any(suf in lname for suf in _COMPANY_SUFFIXES):
        return "company"
    if "openalex" in sources and "patents" not in sources:
        return "research_org"
    return "company"


def auto_promote_actors_v2() -> dict[str, list[str]]:
    """Promotion accelerée des acteurs decouverts vers targets.json.

    Critères (TOUS doivent etre vrais) :
      - count >= AUTO_PROMOTE_MIN_COUNT (5 par defaut, vs 30 v1)
      - appearances_runs >= AUTO_PROMOTE_MIN_RUNS (2 par defaut) → STICKINESS
      - non present dans targets.json (companies/research_orgs)

    La stickiness evite les faux positifs : un acteur qui spike sur un seul run
    (conf, breakthrough ponctuel) doit revenir sur 2+ runs pour etre promu.

    Cap : AUTO_PROMOTE_MAX_PER_RUN par execution.

    Returns:
        {"companies": [...], "research_orgs": [...]}
    """
    historical = safe_read_json(_DISCOVERED_ACTORS_PATH, default={})
    actors = historical.get("actors", {}) if isinstance(historical, dict) else {}
    if not actors:
        return {"companies": [], "research_orgs": []}

    targets_data = safe_read_json(_TARGETS_PATH, default={})
    if not isinstance(targets_data, dict):
        return {"companies": [], "research_orgs": []}

    known_norm: set[str] = set()
    known_norm.update(_aggressive_normalize(c) for c in targets_data.get("companies", []))
    known_norm.update(_aggressive_normalize(o) for o in targets_data.get("research_orgs", []))

    candidates = sorted(
        actors.items(),
        key=lambda kv: kv[1].get("count", 0),
        reverse=True,
    )

    promoted: dict[str, list[str]] = {"companies": [], "research_orgs": []}
    for _norm_key, entry in candidates:
        if len(promoted["companies"]) + len(promoted["research_orgs"]) >= AUTO_PROMOTE_MAX_PER_RUN:
            break
        count = entry.get("count", 0)
        if count < AUTO_PROMOTE_MIN_COUNT:
            break  # liste triee : suivants ont count plus bas
        appearances = entry.get("appearances_runs", 1)
        if appearances < AUTO_PROMOTE_MIN_RUNS:
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        if _aggressive_normalize(name) in known_norm:
            continue
        kind = _classify_actor(name, entry.get("sources", []))
        if kind == "research_org":
            targets_data.setdefault("research_orgs", []).append(name)
            promoted["research_orgs"].append(name)
        else:
            targets_data.setdefault("companies", []).append(name)
            promoted["companies"].append(name)
        known_norm.add(_aggressive_normalize(name))

    total = len(promoted["companies"]) + len(promoted["research_orgs"])
    if total == 0:
        return promoted

    if AUTO_TUNE_DRY_RUN:
        logger.info(f"🧪 [DRY-RUN] Auto-promote v2 : {total} acteur(s) auraient ete promu(s) (non applique).")
        if promoted["companies"]:
            logger.info(f"   🏢 → {', '.join(promoted['companies'])}")
        if promoted["research_orgs"]:
            logger.info(f"   🔬 → {', '.join(promoted['research_orgs'])}")
        return promoted

    backup_path = backup_targets()
    if backup_path:
        logger.debug(f"💾 Backup pre-promote : {os.path.basename(backup_path)}")

    try:
        from src.config import save_targets
        save_targets(
            targets_data.get("companies", []),
            targets_data.get("keywords", []),
            targets_data.get("solo_keywords", []),
            targets_data.get("research_orgs", []),
            targets_data.get("cross_domain_topics", []),
        )
    except (OSError, ImportError, ValueError) as e:
        logger.warning(f"⚠️ Sauvegarde targets.json (promote) impossible : {e}")
        return {"companies": [], "research_orgs": []}

    if promoted["companies"]:
        logger.info(
            f"🏢 Auto-promote v2 : {len(promoted['companies'])} entreprise(s) ajoutee(s) (count>={AUTO_PROMOTE_MIN_COUNT}, "
            f"runs>={AUTO_PROMOTE_MIN_RUNS}) → {', '.join(promoted['companies'])}"
        )
    if promoted["research_orgs"]:
        logger.info(
            f"🔬 Auto-promote v2 : {len(promoted['research_orgs'])} labo(s) ajoute(s) (count>={AUTO_PROMOTE_MIN_COUNT}, "
            f"runs>={AUTO_PROMOTE_MIN_RUNS}) → {', '.join(promoted['research_orgs'])}"
        )
    return promoted


# =============================================================================
# Auto-expansion par tier
# =============================================================================

def _compute_tiers() -> dict[str, str]:
    """Calcule le tier de chaque (query|||source) a partir de l'historique.

    Hot      : top AUTO_EXPAND_HOT_PERCENTILE% des hits_total (parmi productives)
    Cold     : consecutive_zeros >= AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS
    Standard: reste

    Cache process-wide (invalide via invalidate_tier_cache()).
    """
    global _tier_cache
    if _tier_cache is not None:
        return _tier_cache
    if not AUTO_EXPAND_ENABLED:
        _tier_cache = {}
        return _tier_cache

    stats_data = safe_read_json(_QUERY_STATS_PATH, default={})
    historical = stats_data.get("queries", {}) if isinstance(stats_data, dict) else {}
    if not historical:
        _tier_cache = {}
        return _tier_cache

    productive = [(k, e.get("hits_total", 0)) for k, e in historical.items() if e.get("hits_total", 0) > 0]
    productive.sort(key=lambda kv: kv[1], reverse=True)
    n_hot = max(1, len(productive) * AUTO_EXPAND_HOT_PERCENTILE // 100) if productive else 0
    hot_keys = set(k for k, _ in productive[:n_hot])

    tiers: dict[str, str] = {}
    for k, entry in historical.items():
        if k in hot_keys:
            tiers[k] = "hot"
        elif entry.get("consecutive_zeros", 0) >= AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS:
            tiers[k] = "cold"
        else:
            tiers[k] = "standard"

    _tier_cache = tiers
    n_hot_real = sum(1 for v in tiers.values() if v == "hot")
    n_cold = sum(1 for v in tiers.values() if v == "cold")
    if n_hot_real or n_cold:
        logger.info(
            f"⚙️ Auto-tuner tiers : {n_hot_real} hot (×{AUTO_EXPAND_HOT_MULTIPLIER:.2f}), "
            f"{n_cold} cold (×{AUTO_EXPAND_COLD_MULTIPLIER:.2f}), "
            f"{len(tiers) - n_hot_real - n_cold} standard."
        )
    return tiers


def compute_max_results(query: str, source: str, base: int) -> int:
    """Retourne max_results ajuste par tier. Borne : [5, 200].

    Hot      : base × AUTO_EXPAND_HOT_MULTIPLIER (1.5 par defaut)
    Cold     : base × AUTO_EXPAND_COLD_MULTIPLIER (0.5 par defaut → economie bandwidth)
    Standard: base (inchange)
    """
    if not AUTO_EXPAND_ENABLED or not query:
        return base
    tiers = _compute_tiers()
    key = f"{query[:200]}|||{source}"
    tier = tiers.get(key, "standard")
    if tier == "hot":
        return max(5, min(200, int(round(base * AUTO_EXPAND_HOT_MULTIPLIER))))
    if tier == "cold":
        return max(5, min(200, int(round(base * AUTO_EXPAND_COLD_MULTIPLIER))))
    return base


def invalidate_tier_cache() -> None:
    """Force le recalcul des tiers (apres mise a jour query_stats.json)."""
    global _tier_cache
    _tier_cache = None


# =============================================================================
# Orchestrateur
# =============================================================================

def run_full_tuning() -> dict[str, Any]:
    """Appele en fin de run par scraper.run_scraper().

    Sequence :
      1. Auto-promote v2 (ajoute des cibles)
      2. Auto-purge (retire des cibles steriles)
      3. Invalidation tier cache (prochain run recalculera)

    Ordre : promouvoir AVANT purger evite qu'une cible fraichement promue se
    fasse purger immediatement si ses runs cumules avant promotion etaient steriles.

    Returns:
        Resume {"promoted": {...}, "purged": {...}, "dry_run": bool, "enabled": bool}.
    """
    if not AUTO_TUNE_ENABLED:
        logger.info("⚙️ Auto-tuning desactive (AUTO_TUNE_ENABLED=false).")
        return {"promoted": {}, "purged": {}, "dry_run": False, "enabled": False}

    summary: dict[str, Any] = {"dry_run": AUTO_TUNE_DRY_RUN, "enabled": True}

    try:
        summary["promoted"] = auto_promote_actors_v2()
    except (OSError, ValueError, KeyError) as e:
        logger.warning(f"⚠️ Auto-promote v2 : echec non-bloquant : {e}")
        summary["promoted"] = {"companies": [], "research_orgs": []}

    try:
        summary["purged"] = auto_purge_sterile_targets()
    except (OSError, ValueError, KeyError) as e:
        logger.warning(f"⚠️ Auto-purge : echec non-bloquant : {e}")
        summary["purged"] = {"solo_keywords": [], "cross_domain_topics": [], "research_orgs": []}

    invalidate_tier_cache()
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    result = run_full_tuning()
    print(f"\nResume : {result}")
