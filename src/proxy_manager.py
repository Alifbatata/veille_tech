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

import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("proxy_manager")

# Constantes
_HEALTH_CHECK_URL = "https://httpbin.org/ip"
_HEALTH_CHECK_TIMEOUT = 15.0
_MAX_FAILURES_BEFORE_DISABLE = 3   # une fois ce seuil atteint, proxy marqué dead
_PROXY_ENV_VARS = (
    "RESIDENTIAL_PROXY_PRIMARY",
    "RESIDENTIAL_PROXY_BACKUP",
    "RESIDENTIAL_PROXY_TERTIARY",
)


@dataclass
class _ProxyEntry:
    """Représente un proxy avec son état de santé courant."""
    name: str               # nom de la variable env (pour logs)
    url: str                # URL complète avec credentials
    healthy: bool = True    # False -> retiré du pool jusqu'à reset manuel
    failure_count: int = 0  # incrémenté à chaque échec, reset si succès
    last_used: float = field(default_factory=time.monotonic)
    last_check: float = 0.0  # timestamp du dernier health check

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
        """Test rapide : ping httpbin.org/ip via le proxy.

        Returns:
            (ok, message) — ok=True si HTTP 200 reçu, message diagnostic.
        """
        try:
            from curl_cffi import requests as curl_requests
            r = curl_requests.get(
                _HEALTH_CHECK_URL,
                proxies={"http": url, "https": url},
                impersonate="chrome124",
                timeout=_HEALTH_CHECK_TIMEOUT,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            data = r.json()
            ip_seen = data.get("origin", "?")
            return True, f"IP visible : {ip_seen}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def initialize(self) -> None:
        """Charge les proxies depuis .env et fait un health check initial.

        Idempotent : un seul health check global même si appelé plusieurs fois.
        """
        if self._initialized:
            return
        self._initialized = True
        self._load_from_env()
        if not self._pool:
            logger.info("ℹ️ Aucun proxy résidentiel configuré (.env). Mode direct.")
            return
        logger.info(f"🌐 Pool de {len(self._pool)} proxy(s) résidentiel(s) configuré(s).")
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
        """True si au moins un proxy est marqué comme sain."""
        return any(p.healthy for p in self._pool)

    def current_proxy(self) -> _ProxyEntry | None:
        """Retourne l'entrée proxy active (premier sain à partir de _current_idx)."""
        if not self._pool:
            return None
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

        Au-delà de _MAX_FAILURES_BEFORE_DISABLE échecs consécutifs, le proxy est
        marqué unhealthy et exclu du pool jusqu'à un éventuel reset manuel.
        """
        entry = self.current_proxy()
        if entry is None:
            return
        entry.failure_count += 1
        if entry.failure_count >= _MAX_FAILURES_BEFORE_DISABLE:
            entry.healthy = False
            logger.warning(
                f"⚠️ Proxy {entry.name} marqué HORS-LIGNE après "
                f"{_MAX_FAILURES_BEFORE_DISABLE} échecs consécutifs."
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
    mgr = get_proxy_manager()
    print()
    print(mgr.status_report())
    print()
    if mgr.has_healthy_proxy():
        entry = mgr.current_proxy()
        print(f"[OK] Proxy actif : {entry.masked_url() if entry else 'inconnu'}")
    else:
        print("[!] Aucun proxy sain - le programme tournera en mode direct.")
