"""Utilitaires I/O partages : ecriture atomique JSON, lecture defensive.

WHY ce module : un `json.dump(f)` direct ouvre le fichier, ecrit progressivement,
et le ferme. Si le process est tue en plein milieu (Ctrl+C, kill, crash, panne
electrique), le fichier reste partiellement ecrit → JSON corrompu, runs suivants
plantent. Le pattern "tempfile + os.replace" garantit qu'on ne voit JAMAIS un
fichier a moitie ecrit : soit l'ancien etat, soit le nouveau, jamais un melange.

Utilise par tous les modules qui persistent du state (scraper, ai_filter,
proxy_manager, config, archive, main).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Ecrit `data` en JSON dans `path` de maniere atomique (tempfile + rename).

    Garantit qu'un lecteur concurrent ne lira jamais un fichier partiellement
    ecrit : il verra soit l'ancien contenu, soit le nouveau. WHY : sur Windows,
    `os.replace` est atomique au niveau du systeme de fichier (NTFS) ; sur Linux
    aussi (rename(2) sur le meme FS). Cross-platform safe.

    Args:
        path: Chemin du fichier final.
        data: Objet serialisable JSON.
        indent: Indentation JSON (defaut 2).
        ensure_ascii: False pour preserver les caracteres unicodes (defaut).

    Raises:
        OSError: si l'ecriture ou le rename echoue.
        TypeError: si `data` n'est pas serialisable.
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    # tempfile.NamedTemporaryFile avec delete=False pour qu'on puisse rename.
    # On le cree dans le MEME dossier que la cible : os.replace garantit
    # l'atomicite seulement si source et destination sont sur le meme FS.
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync peut echouer sur certains FS exotiques (tmpfs, certains
                # NFS) — non bloquant, le rename reste atomique cote OS.
                pass
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup du tempfile en cas d'echec, sinon il s'accumule
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def safe_read_json(path: str, default: Any = None) -> Any:
    """Lit un JSON avec gestion des erreurs courantes (file not found, JSON
    corrompu). Retourne `default` si le fichier est absent ou illisible.

    WHY : evite la duplication du pattern try/except (OSError, JSONDecodeError)
    qui apparait 15+ fois dans le code. Centralise le logging.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "⚠️ Lecture JSON impossible (%s) : %s. Valeur par defaut utilisee.",
            path, e,
        )
        return default
