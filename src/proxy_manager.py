"""Gestion centralisée des proxies résidentiels avec failover automatique.

Architecture :
- Pool ordonné de proxies (primaire, backup, etc.) chargé depuis .env
- Health check au démarrage : ping httpbin.org/ip via chaque proxy
- En cas d'échec en cours de run : marque le proxy "unhealthy", passe au suivant
- Si tous les proxies sont morts : retombe en mode direct (pas de blocage du programme)
- Compatible avec curl_cffi.Session.proxies (format dict {"http": url, "https": url})

Configuration .env (toutes optionnelles) :
    RESIDENTIAL_PROXY_PRIMARY = http://user:pass@gate.iproyal.com:12321
    RESIDENTIAL_PROXY_BACKUP  = http://user:pass@gate.decodo.com:7000
    RESIDENTIAL_PROXY_TERTIARY = http://...   (optionnel, 3e fallback)
    PROXY_COUNTRY              = CH    (optionnel, geo : CH/FR/DE/...)

Si aucune des 3 variables n'est définie, le programme tourne en direct (mode
identique à avant l'ajout des proxies). Aucun blocage, aucune erreur.

Sécurité : les credentials proxy ne sont JAMAIS loggés en clair (masqués
en `***:***@host:port` dans les logs).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("proxy_manager")

# Constantes
# Liste de health check URLs : on essaie dans l'ordre jusqu'a une qui repond.
# WHY plusieurs URLs : httpbin.org est ponctuellement sature (HTTP 503),
# api.ipify.org est plus stable mais sans User-Agent details, icanhazip.com est
# minimal. Une seule reponse 200 suffit pour declarer le proxy sain.
_HEALTH_CHECK_URLS = (
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://icanhazip.com",
)
_HEALTH_CHECK_TIMEOUT = 15.0
# Seuil d'echecs avant de marquer un proxy "dead". Adapte au nombre de proxies :
#   - 1 seul proxy   : 5 echecs (plus tolerant, on n'a pas de backup)
#   - 2-3 proxies   : 3 echecs (on rotate rapidement vers le suivant)
_MAX_FAILURES_SINGLE = 5
_MAX_FAILURES_POOL   = 3
# Auto-recovery : un proxy marque "dead" est retest apres ce delai. S'il revient
# en ligne, il est reactivé automatiquement (sans intervention de l'utilisateur).
_RECOVERY_RETEST_INTERVAL = 60.0  # secondes
_PROXY_ENV_VARS = (
    "RESIDENTIAL_PROXY_PRIMARY",
    "RESIDENTIAL_PROXY_BACKUP",
    "RESIDENTIAL_PROXY_TERTIARY",
)

# Tracker de bande passante : utile pour les trials a quota (Decodo 100 MB),
# affiche les MB consommees en fin de run, declenche un cap si configure.
# WHY persistance JSON : le compteur survit aux runs successifs (un trial 100 MB
# se consomme sur plusieurs runs), reset manuel via bandwidth_reset() ou suppression
# du fichier data/proxy_bandwidth.json.
_BANDWIDTH_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "proxy_bandwidth.json"
)
# Overhead estime par requete : headers HTTP + TLS handshake + ACKs (approx).
# Decodo facture le trafic NET cote client (in + out), donc on ajoute un fixe
# par requete pour englober les headers TX/RX que `len(response.content)` ignore.
_BANDWIDTH_PER_REQ_OVERHEAD_BYTES = 2500
# Persistance : on flush sur disque tous les N appels add_bytes pour limiter
# les ecritures (un GNews run = ~600 requetes, 1 ecriture par requete = trop).
_BANDWIDTH_FLUSH_EVERY = 25


@dataclass
class _ProxyEntry:
    """Représente un proxy avec son état de santé courant."""
    name: str                       # nom de la variable env (pour logs)
    url: str                        # URL complète avec credentials
    healthy: bool = True            # False -> retiré du pool jusqu'à recovery
    failure_count: int = 0          # incrémenté à chaque échec, reset si succès
    last_used: float = field(default_factory=time.monotonic)
    last_check: float = 0.0         # timestamp du dernier health check
    unhealthy_since: float = 0.0    # timestamp du marquage HORS-LIGNE (pour auto-recovery)

    def masked_url(self) -> str:
        """Retourne l'URL avec les credentials masqués pour les logs."""
        # http://user:pass@host:port -> http://***:***@host:port
        return re.sub(r"(://)([^:]+):([^@]+)(@)", r"\1***:***\4", self.url)


class ProxyManager:
    """Pool de proxies résidentiels avec failover automatique.

    Usage typique :
        mgr = get_proxy_manager()
        if mgr.has_healthy_proxy():
            session.proxies = mgr.current_proxy_dict()
        # ... requete ...
        # En cas d'echec :
        mgr.mark_failure()
        if mgr.rotate():
            session.proxies = mgr.current_proxy_dict()
    """

    def __init__(self):
        self._pool: list[_ProxyEntry] = []
        self._current_idx: int = 0
        self._initialized: bool = False
        # Tracker bande passante (cumulatif sur tous les runs)
        self._bandwidth_used_bytes: int = 0
        self._bandwidth_session_bytes: int = 0  # juste pour ce run
        self._bandwidth_request_count: int = 0
        self._bandwidth_cap_bytes: int = 0  # 0 = pas de cap
        self._bandwidth_cap_triggered: bool = False
        self._bandwidth_flush_counter: int = 0

    def _load_from_env(self) -> None:
        """Charge les proxies depuis les variables d'environnement, dans l'ordre.

        Si PROXY_COUNTRY est défini ET que l'URL ne contient pas déjà un paramètre
        country, on l'injecte dans le username (format compatible avec la majorité
        des providers : user-country-XX:pass).
        """
        country = os.environ.get("PROXY_COUNTRY", "").strip().upper()
        for env_var in _PROXY_ENV_VARS:
            url = os.environ.get(env_var, "").strip()
            if not url:
                continue
            # Injection du country si absent et PROXY_COUNTRY défini
            if country and "country-" not in url.lower():
                # Insert -country-XX dans le username : http://user:pass@... -> http://user-country-XX:pass@...
                m = re.match(r"^(https?://)([^:]+)(:[^@]+@.+)$", url)
                if m:
                    url = f"{m.group(1)}{m.group(2)}-country-{country}{m.group(3)}"
            self._pool.append(_ProxyEntry(name=env_var, url=url))

    @staticmethod
    def _check_proxy_health(url: str) -> tuple[bool, str]:
        """Test rapide : tente plusieurs URLs de healthcheck via le proxy.

        On essaie chaque URL de _HEALTH_CHECK_URLS dans l'ordre. La premiere
        qui repond 200 declare le proxy sain. WHY : httpbin.org est ponctuellement
        sature, ipify et icanhazip servent de filets.

        Returns:
            (ok, message) — ok=True si au moins une URL repond 200.
        """
        from curl_cffi import requests as curl_requests
        last_err = "unknown"
        for check_url in _HEALTH_CHECK_URLS:
            try:
                r = curl_requests.get(
                    check_url,
                    proxies={"http": url, "https": url},
                    impersonate="chrome124",
                    timeout=_HEALTH_CHECK_TIMEOUT,
                )
                if r.status_code == 200:
                    # Extrait l'IP de la reponse (format variable selon URL)
                    ip_seen = "?"
                    try:
                        if "json" in (r.headers.get("Content-Type", "")).lower() or check_url.endswith("?format=json"):
                            data = r.json()
                            ip_seen = data.get("ip") or data.get("origin") or "?"
                        else:
                            ip_seen = r.text.strip()[:30]
                    except (ValueError, KeyError):
                        ip_seen = "?"
                    return True, f"IP visible : {ip_seen}"
                last_err = f"HTTP {r.status_code} via {check_url}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
        return False, last_err

    def initialize(self) -> None:
        """Charge les proxies depuis .env et fait un health check initial.

        Idempotent : un seul health check global même si appelé plusieurs fois.
        """
        if self._initialized:
            return
        self._initialized = True
        # Lecture du cap bande passante (.env : PROXY_BANDWIDTH_CAP_MB)
        try:
            cap_mb = float(os.environ.get("PROXY_BANDWIDTH_CAP_MB", "0").strip() or "0")
            self._bandwidth_cap_bytes = int(cap_mb * 1024 * 1024) if cap_mb > 0 else 0
        except ValueError:
            self._bandwidth_cap_bytes = 0
        # Restaure le compteur cumulatif si fichier existe
        self._bandwidth_load_from_disk()
        self._load_from_env()
        if not self._pool:
            logger.info("ℹ️ Aucun proxy résidentiel configuré (.env). Mode direct.")
            return
        logger.info(f"🌐 Pool de {len(self._pool)} proxy(s) résidentiel(s) configuré(s).")
        if self._bandwidth_cap_bytes > 0:
            cap_mb = self._bandwidth_cap_bytes / (1024 * 1024)
            used_mb = self._bandwidth_used_bytes / (1024 * 1024)
            logger.info(
                f"📊 Bande passante cumulee : {used_mb:.2f} MB / cap {cap_mb:.0f} MB "
                f"({100*used_mb/cap_mb:.1f}%)"
            )
            if self._bandwidth_used_bytes >= self._bandwidth_cap_bytes:
                self._bandwidth_cap_triggered = True
                logger.warning(
                    "⚠️ Cap bande passante DEJA atteint. Le proxy ne sera pas utilise "
                    "pour ce run (mode direct force). Reset : supprime data/proxy_bandwidth.json"
                )
        for entry in self._pool:
            ok, msg = self._check_proxy_health(entry.url)
            entry.healthy = ok
            entry.last_check = time.monotonic()
            if ok:
                logger.info(f"   ✅ {entry.name} : {msg}")
            else:
                logger.warning(f"   ❌ {entry.name} HORS-LIGNE — {msg}")

        if not self.has_healthy_proxy():
            logger.error(
                "🛑 Aucun proxy disponible (tous les health checks ont échoué). "
                "Le programme va continuer en mode DIRECT (sans proxy). "
                "Vérifie tes credentials dans .env."
            )

    def has_healthy_proxy(self) -> bool:
        """True si au moins un proxy est marqué comme sain ET le cap n'est pas atteint."""
        if self._bandwidth_cap_triggered:
            return False
        return any(p.healthy for p in self._pool)

    # =========================================================================
    # Tracker de bande passante
    # =========================================================================
    def _bandwidth_load_from_disk(self) -> None:
        """Charge le compteur cumulatif depuis data/proxy_bandwidth.json."""
        try:
            if os.path.exists(_BANDWIDTH_STATE_PATH):
                with open(_BANDWIDTH_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._bandwidth_used_bytes = int(data.get("total_bytes", 0))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.debug(f"Lecture proxy_bandwidth.json: {e} (compteur reset a 0)")
            self._bandwidth_used_bytes = 0

    def _bandwidth_save_to_disk(self) -> None:
        """Persiste le compteur cumulatif (snapshot atomic via tempfile + rename)."""
        try:
            os.makedirs(os.path.dirname(_BANDWIDTH_STATE_PATH), exist_ok=True)
            tmp_path = _BANDWIDTH_STATE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "total_bytes": self._bandwidth_used_bytes,
                    "total_mb": round(self._bandwidth_used_bytes / (1024 * 1024), 3),
                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, f, indent=2)
            os.replace(tmp_path, _BANDWIDTH_STATE_PATH)
        except OSError as e:
            logger.debug(f"Persistance proxy_bandwidth.json: {e}")

    def add_bytes(self, content_bytes: int) -> None:
        """Comptabilise une requete passee via le proxy.

        Args:
            content_bytes: taille du `response.content` (peut etre 0 si erreur).
                On ajoute un overhead fixe pour englober headers TX/RX que cette
                taille ignore. Pas precis au byte pres mais correct ordre de grandeur.
        """
        # Pas de track si proxy desactive (mode direct)
        if not self._pool or self._bandwidth_cap_triggered:
            return
        total = max(0, content_bytes) + _BANDWIDTH_PER_REQ_OVERHEAD_BYTES
        self._bandwidth_used_bytes += total
        self._bandwidth_session_bytes += total
        self._bandwidth_request_count += 1
        self._bandwidth_flush_counter += 1
        # Persiste tous les N appels pour limiter IO
        if self._bandwidth_flush_counter >= _BANDWIDTH_FLUSH_EVERY:
            self._bandwidth_save_to_disk()
            self._bandwidth_flush_counter = 0
        # Declenche le cap si depasse
        if self._bandwidth_cap_bytes > 0 and self._bandwidth_used_bytes >= self._bandwidth_cap_bytes:
            if not self._bandwidth_cap_triggered:
                self._bandwidth_cap_triggered = True
                cap_mb = self._bandwidth_cap_bytes / (1024 * 1024)
                logger.warning(
                    f"🛑 Cap bande passante atteint ({cap_mb:.0f} MB). "
                    f"Bascule en mode DIRECT pour le reste du run."
                )

    def bandwidth_report(self) -> str:
        """Resume textuel du tracker pour les logs / fin de run."""
        if not self._pool:
            return "Pas de proxy configure (pas de tracking)."
        used_mb = self._bandwidth_used_bytes / (1024 * 1024)
        session_mb = self._bandwidth_session_bytes / (1024 * 1024)
        lines = [
            f"📊 Bande passante proxy :",
            f"   • Cette session : {session_mb:.2f} MB ({self._bandwidth_request_count} requetes)",
            f"   • Cumul total : {used_mb:.2f} MB",
        ]
        if self._bandwidth_cap_bytes > 0:
            cap_mb = self._bandwidth_cap_bytes / (1024 * 1024)
            pct = 100 * used_mb / cap_mb if cap_mb > 0 else 0
            lines.append(f"   • Cap : {cap_mb:.0f} MB ({pct:.1f}% atteint)")
            if self._bandwidth_cap_triggered:
                lines.append("   • ⚠️ Cap declenchee : proxy desactive pour la suite")
        return "\n".join(lines)

    def bandwidth_flush(self) -> None:
        """Force la sauvegarde sur disque du compteur (a appeler en fin de run)."""
        if self._pool:
            self._bandwidth_save_to_disk()

    def bandwidth_reset(self) -> None:
        """Reset complet du compteur (utile en debut de nouveau quota mensuel)."""
        self._bandwidth_used_bytes = 0
        self._bandwidth_session_bytes = 0
        self._bandwidth_request_count = 0
        self._bandwidth_cap_triggered = False
        self._bandwidth_save_to_disk()

    def _try_recover_proxies(self) -> None:
        """Auto-recovery : retente un health check sur les proxies marques HORS-LIGNE
        depuis plus de _RECOVERY_RETEST_INTERVAL secondes. S'ils repondent, on
        les reactive automatiquement.

        Particulierement utile en mode mono-provider : sans backup, perdre le
        seul proxy = perte de protection. Cette boucle de recovery permet de
        recuperer apres une panne transitoire sans intervention.
        """
        now = time.monotonic()
        for entry in self._pool:
            if entry.healthy:
                continue
            if now - entry.unhealthy_since < _RECOVERY_RETEST_INTERVAL:
                continue
            logger.info(f"🔁 Auto-recovery : retest de {entry.name}…")
            ok, msg = self._check_proxy_health(entry.url)
            entry.last_check = now
            if ok:
                entry.healthy = True
                entry.failure_count = 0
                entry.unhealthy_since = 0.0
                logger.info(f"   ✅ {entry.name} de retour en ligne — {msg}")
            else:
                entry.unhealthy_since = now  # decale le prochain retest
                logger.debug(f"   ❌ {entry.name} toujours HORS-LIGNE — {msg}")

    def current_proxy(self) -> _ProxyEntry | None:
        """Retourne l'entrée proxy active (premier sain à partir de _current_idx).

        Si aucun proxy n'est sain, tente une auto-recovery sur les proxies
        marqués HORS-LIGNE depuis assez longtemps avant de renvoyer None.
        """
        if not self._pool:
            return None
        # Auto-recovery : si tous les proxies sont down, retente les plus vieux
        if not self.has_healthy_proxy():
            self._try_recover_proxies()
        n = len(self._pool)
        for offset in range(n):
            idx = (self._current_idx + offset) % n
            if self._pool[idx].healthy:
                if idx != self._current_idx:
                    # On a sauté des proxies morts, mettre à jour le pointeur
                    self._current_idx = idx
                self._pool[idx].last_used = time.monotonic()
                return self._pool[idx]
        return None

    def current_proxy_dict(self) -> dict[str, str] | None:
        """Renvoie le proxy courant au format curl_cffi : {http, https}."""
        entry = self.current_proxy()
        if entry is None:
            return None
        return {"http": entry.url, "https": entry.url}

    def mark_failure(self) -> None:
        """Incrémente le compteur d'échecs du proxy courant.

        Le seuil de disable est adaptatif :
        - Pool de 1 seul proxy : 5 échecs (mode mono-provider, plus tolérant)
        - Pool de 2-3 proxies : 3 échecs (on rotate vite vers le suivant)

        Au-dela du seuil, le proxy est marqué HORS-LIGNE. Mais grace a
        _try_recover_proxies(), il pourra etre reactive automatiquement
        s'il revient en ligne dans les minutes suivantes.
        """
        entry = self.current_proxy()
        if entry is None:
            return
        entry.failure_count += 1
        threshold = _MAX_FAILURES_SINGLE if len(self._pool) == 1 else _MAX_FAILURES_POOL
        if entry.failure_count >= threshold:
            entry.healthy = False
            entry.unhealthy_since = time.monotonic()
            logger.warning(
                f"⚠️ Proxy {entry.name} marqué HORS-LIGNE après "
                f"{threshold} échecs consécutifs (sera retesté dans "
                f"{int(_RECOVERY_RETEST_INTERVAL)}s pour auto-recovery)."
            )

    def mark_success(self) -> None:
        """Reset du compteur d'échecs sur le proxy courant (requête réussie)."""
        entry = self.current_proxy()
        if entry is not None:
            entry.failure_count = 0

    def rotate(self) -> bool:
        """Passe au proxy suivant dans le pool (s'il y en a un de sain).

        Returns:
            True si un nouveau proxy sain a été sélectionné, False si plus aucun.
        """
        if not self._pool:
            return False
        n = len(self._pool)
        for offset in range(1, n + 1):
            idx = (self._current_idx + offset) % n
            if self._pool[idx].healthy:
                old_name = self._pool[self._current_idx].name
                self._current_idx = idx
                logger.info(
                    f"🔄 Bascule de proxy : {old_name} -> "
                    f"{self._pool[idx].name} ({self._pool[idx].masked_url()})"
                )
                return True
        return False

    def status_report(self) -> str:
        """Renvoie un résumé textuel pour les logs / debug."""
        if not self._pool:
            return "Aucun proxy configuré (mode direct)."
        lines = [f"Pool de {len(self._pool)} proxy(s) :"]
        for i, p in enumerate(self._pool):
            marker = "▶" if i == self._current_idx else " "
            state = "✅ sain" if p.healthy else "❌ hors-ligne"
            lines.append(f"  {marker} {p.name} : {state} ({p.failure_count} échecs) — {p.masked_url()}")
        return "\n".join(lines)


# Singleton accessible depuis tout le projet
_singleton: ProxyManager | None = None


def get_proxy_manager() -> ProxyManager:
    """Renvoie le ProxyManager global, l'initialise au premier appel."""
    global _singleton
    if _singleton is None:
        _singleton = ProxyManager()
        _singleton.initialize()
        # Garantit la persistance du tracker meme si le process est tue (Ctrl+C
        # ou exception non capturee). atexit s'execute apres sys.exit() / exception.
        atexit.register(_singleton.bandwidth_flush)
    return _singleton


# Permet de tester depuis la ligne de commande : python -m src.proxy_manager
if __name__ == "__main__":
    import sys
    # Forcer UTF-8 sur stdout/stderr pour eviter UnicodeEncodeError sur Windows
    # console (cp1252) qui ne sait pas afficher les emojis du status_report.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    # Argument optionnel --reset pour remettre le compteur bandwidth a 0
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        mgr = get_proxy_manager()
        mgr.bandwidth_reset()
        print("[OK] Compteur bandwidth remis a 0.")
        sys.exit(0)
    mgr = get_proxy_manager()
    print()
    print(mgr.status_report())
    print()
    print(mgr.bandwidth_report())
    print()
    if mgr.has_healthy_proxy():
        entry = mgr.current_proxy()
        print(f"[OK] Proxy actif : {entry.masked_url() if entry else 'inconnu'}")
    else:
        print("[!] Aucun proxy sain - le programme tournera en mode direct.")
