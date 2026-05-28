# =============================================================================
# feedback.py — Feedback loop utilisateur (👍/👎 via email)
# =============================================================================
#
# Boucle de feedback fermee humaine sans aucune infrastructure web :
#
#   1. mailto: links dans chaque carte article de l'email (👍/👎)
#      → un clic ouvre le client mail avec un email pre-rempli vers
#        GMAIL_USER (auto-recu sur le compte de la veille)
#   2. IMAP poll au lancement de main.py : lit les emails [FEEDBACK]
#      → extrait article_id + rating, stocke dans data/feedback_history.json
#   3. Injection few-shot dans le prompt systeme : les N derniers 👍 et
#      les N derniers 👎 sont fournis a Gemini comme exemples calibrants
#
# Tout est gratuit, utilise imaplib (stdlib Python) pour IMAP — aucune
# dependance supplementaire. Compatible avec n'importe quel client email.
# =============================================================================

from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import imaplib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

try:
    from io_utils import atomic_write_json, safe_read_json
except ImportError:
    from src.io_utils import atomic_write_json, safe_read_json

logger = logging.getLogger("feedback")


# =============================================================================
# Configuration via variables d'environnement
# =============================================================================

FEEDBACK_ENABLED: bool = os.environ.get("FEEDBACK_ENABLED", "true").lower() in ("true", "1", "yes")
FEEDBACK_POLL_IMAP: bool = os.environ.get("FEEDBACK_POLL_IMAP", "true").lower() in ("true", "1", "yes")
# Adresse mail qui recoit les feedbacks (default = GMAIL_USER)
FEEDBACK_RECIPIENT: str = os.environ.get("FEEDBACK_RECIPIENT", "") or os.environ.get("GMAIL_USER", "")
# IMAP server (Gmail par defaut)
FEEDBACK_IMAP_HOST: str = os.environ.get("FEEDBACK_IMAP_HOST", "imap.gmail.com")
FEEDBACK_IMAP_PORT: int = int(os.environ.get("FEEDBACK_IMAP_PORT", "993"))
# Nb d'exemples few-shot a injecter dans le prompt
FEEDBACK_FEW_SHOT_COUNT: int = int(os.environ.get("FEEDBACK_FEW_SHOT_COUNT", "3"))
# Rotation historique
FEEDBACK_HISTORY_MAX: int = int(os.environ.get("FEEDBACK_HISTORY_MAX", "500"))

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_FEEDBACK_PATH = os.path.join(_DATA_DIR, "feedback_history.json")


# =============================================================================
# Identifiant stable d'article (hash de l'URL)
# =============================================================================

def compute_article_id(article: dict[str, Any]) -> str:
    """Identifiant court (12 chars) et stable d'un article via hash MD5 de l'URL.

    Stable inter-runs : tant que l'URL ne change pas, l'id reste le meme.
    Non-cryptographique (MD5 OK pour un ID public).
    """
    link = (article.get("link") or "").strip()
    if not link:
        # Fallback : hash titre + summary (rare, URL devrait toujours etre present)
        link = (article.get("title", "") + article.get("summary", ""))[:500]
    return hashlib.md5(link.encode("utf-8")).hexdigest()[:12]


# =============================================================================
# Construction des liens mailto (utilises par mailer.py)
# =============================================================================

_FEEDBACK_SUBJECT_PREFIX = "[FEEDBACK]"


def make_mailto_link(article: dict[str, Any], rating: str, recipient: str | None = None) -> str:
    """Construit un lien mailto: pour 👍 ou 👎 sur un article.

    Le subject contient :  [FEEDBACK] rating=up|down id=<12-hex>
    On encode l'URL article dans le BODY pour qu'un humain qui ouvre l'inbox
    voit le titre. Pas de donnees sensibles dans le mailto.

    Args:
        article: dict avec au moins "link" (et idealement "title").
        rating: "up" ou "down".
        recipient: adresse cible (default = FEEDBACK_RECIPIENT).

    Returns:
        URL mailto: prete a etre mise dans un <a href=...>.
    """
    to = recipient or FEEDBACK_RECIPIENT
    if not to:
        return ""
    aid = compute_article_id(article)
    rating_clean = "up" if rating == "up" else "down"
    title = (article.get("title") or "").strip()[:200]
    subject = f"{_FEEDBACK_SUBJECT_PREFIX} rating={rating_clean} id={aid}"
    body = f"Article : {title}\nURL : {article.get('link', '')}\n\n(Confirme l'envoi pour enregistrer ton feedback)"
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"


# =============================================================================
# Persistance des feedbacks
# =============================================================================

def _load_feedback_history() -> dict[str, Any]:
    """Charge l'historique cumulatif des feedbacks."""
    data = safe_read_json(_FEEDBACK_PATH, default={"feedbacks": [], "last_imap_uid": 0})
    if not isinstance(data, dict):
        data = {"feedbacks": [], "last_imap_uid": 0}
    data.setdefault("feedbacks", [])
    data.setdefault("last_imap_uid", 0)
    return data


def _save_feedback_history(data: dict[str, Any]) -> None:
    # Rotation : garde au max FEEDBACK_HISTORY_MAX entrees
    data["feedbacks"] = data.get("feedbacks", [])[-FEEDBACK_HISTORY_MAX:]
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write_json(_FEEDBACK_PATH, data)
    except OSError as e:
        logger.warning(f"⚠️ Persistance feedback_history.json impossible : {e}")


def record_feedback(article_id: str, rating: str, source: str, article_meta: dict[str, Any] | None = None) -> None:
    """Enregistre un feedback dans l'historique.

    Idempotent : si (article_id, rating) existe deja avec meme source, no-op.
    Sinon ajoute. Permet aussi le changement d'avis (un nouveau rating ecrase l'ancien).

    Args:
        article_id: hash MD5[:12] de l'URL article.
        rating: "up" ou "down".
        source: "imap" | "manual" | "file" (origine du feedback).
        article_meta: optionnel, titre + score original + autres pour audit.
    """
    if rating not in ("up", "down"):
        return
    data = _load_feedback_history()
    feedbacks: list[dict[str, Any]] = data["feedbacks"]
    # Idempotence : retire un eventuel feedback precedent sur le meme article_id
    feedbacks = [fb for fb in feedbacks if fb.get("article_id") != article_id]
    entry = {
        "article_id": article_id,
        "rating":     rating,
        "source":     source,
        "date":       datetime.now(timezone.utc).isoformat(),
    }
    if article_meta:
        # On garde titre + summary + score pour le few-shot dans le prompt
        entry["title"] = (article_meta.get("title") or "")[:200]
        entry["summary"] = (article_meta.get("summary") or "")[:400]
        entry["original_score"] = article_meta.get("score")
    feedbacks.append(entry)
    data["feedbacks"] = feedbacks
    _save_feedback_history(data)


# =============================================================================
# Poll IMAP : recolte automatique des feedbacks recus par mail
# =============================================================================

_SUBJECT_RE = re.compile(
    r"\[FEEDBACK\]\s+rating=(up|down)\s+id=([a-f0-9]{12})",
    re.IGNORECASE,
)


def _decode_subject(raw: str | bytes | None) -> str:
    """Decode un Subject MIME (peut etre en =?utf-8?b?...?= ou plain)."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    parts = email.header.decode_header(raw)
    decoded = ""
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                decoded += chunk.decode(enc or "utf-8", errors="ignore")
            except (LookupError, UnicodeDecodeError):
                decoded += chunk.decode("utf-8", errors="ignore")
        else:
            decoded += chunk
    return decoded


def poll_imap_feedback() -> int:
    """Lit l'inbox Gmail (IMAP SSL), extrait les feedbacks [FEEDBACK].

    Filet de securite : si IMAP indispo ou mot de passe invalide, log et continue
    (le pipeline ne doit jamais crasher pour ca). Idempotent : ne re-traite jamais
    un email deja vu (via last_imap_uid persiste).

    Returns:
        Nombre de feedbacks nouvellement collectes.
    """
    if not FEEDBACK_ENABLED or not FEEDBACK_POLL_IMAP:
        return 0
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        logger.debug("Feedback IMAP : GMAIL_USER / GMAIL_PASSWORD manquant, skip.")
        return 0

    data = _load_feedback_history()
    last_uid = int(data.get("last_imap_uid", 0))
    new_count = 0

    mail: imaplib.IMAP4_SSL | None = None
    try:
        mail = imaplib.IMAP4_SSL(FEEDBACK_IMAP_HOST, FEEDBACK_IMAP_PORT)
        mail.login(gmail_user, gmail_pass)
        mail.select("INBOX")

        # Recherche des emails avec subject contenant [FEEDBACK]
        # On utilise UID FETCH apres SEARCH pour ne pas dependre des seq numbers
        status, data_search = mail.uid("search", None, f'(SUBJECT "{_FEEDBACK_SUBJECT_PREFIX}")')
        if status != "OK" or not data_search or not data_search[0]:
            logger.debug("Feedback IMAP : aucun email [FEEDBACK] dans l'inbox.")
            return 0

        uids = [int(u) for u in data_search[0].split()]
        # Ne traite que les UID superieurs au dernier vu
        new_uids = [u for u in uids if u > last_uid]
        if not new_uids:
            logger.debug(f"Feedback IMAP : pas de nouveau email (last_uid={last_uid}).")
            return 0

        max_uid_seen = last_uid
        for uid in new_uids:
            status, msg_data = mail.uid("fetch", str(uid).encode("ascii"), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if isinstance(raw, bytes):
                msg = email.message_from_bytes(raw)
            else:
                msg = email.message_from_string(str(raw))
            subject = _decode_subject(msg.get("Subject", ""))
            m = _SUBJECT_RE.search(subject)
            if m:
                rating = m.group(1).lower()
                aid = m.group(2).lower()
                record_feedback(aid, rating, source="imap")
                new_count += 1
                logger.info(f"📩 Feedback IMAP : {rating} sur article {aid}")
            max_uid_seen = max(max_uid_seen, uid)

        # Persistance du dernier UID traite
        data = _load_feedback_history()
        data["last_imap_uid"] = max_uid_seen
        _save_feedback_history(data)
    except (imaplib.IMAP4.error, OSError, ValueError) as e:
        logger.warning(f"⚠️ Feedback IMAP : echec ({e}). Continue sans feedback.")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except imaplib.IMAP4.error:
                pass

    if new_count > 0:
        logger.info(f"📩 Feedback : {new_count} nouveau(x) signal(aux) recolte(s) depuis l'inbox.")
    return new_count


# =============================================================================
# Few-shot examples : injection dans le prompt systeme
# =============================================================================

def get_few_shot_examples(n: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Retourne les N derniers feedbacks 👍 et 👎 pour few-shot.

    Critere : feedbacks avec title et summary non vides (sinon inutilisable comme exemple).

    Returns:
        {"up": [{title, summary, original_score}, ...], "down": [...]}
    """
    if not FEEDBACK_ENABLED:
        return {"up": [], "down": []}
    n_actual = n if n is not None else FEEDBACK_FEW_SHOT_COUNT
    data = _load_feedback_history()
    feedbacks: list[dict[str, Any]] = data.get("feedbacks", [])
    # Trie chronologique : plus recent en dernier
    feedbacks.sort(key=lambda fb: fb.get("date", ""))
    usable = [fb for fb in feedbacks if fb.get("title") and fb.get("summary")]
    ups = [fb for fb in usable if fb.get("rating") == "up"][-n_actual:]
    downs = [fb for fb in usable if fb.get("rating") == "down"][-n_actual:]
    return {"up": ups, "down": downs}


def build_few_shot_prompt_section() -> str:
    """Section du prompt systeme contenant les exemples calibrants.

    Renvoie une chaine vide si aucun feedback exploitable, sinon une section
    formatee a injecter dans _build_system_prompt.
    """
    examples = get_few_shot_examples()
    if not examples["up"] and not examples["down"]:
        return ""

    lines = [
        "",
        "━━━ CALIBRATION — EXEMPLES VALIDES PAR L'UTILISATEUR ━━━",
        "L'utilisateur a explicitement valide ou rejete les articles suivants par feedback email.",
        "Utilise ces exemples comme reference de calibration pour scorer le batch courant.",
        "",
    ]
    if examples["up"]:
        lines.append("ARTICLES JUGÉS PERTINENTS (style 4-5★) :")
        for fb in examples["up"]:
            score = fb.get("original_score", "?")
            lines.append(f"  ✅ « {fb['title'][:140]} » (score {score})")
            if fb.get("summary"):
                lines.append(f"     [{fb['summary'][:200]}]")
        lines.append("")
    if examples["down"]:
        lines.append("ARTICLES JUGÉS NON PERTINENTS (style 1-2★ ou trop generaux) :")
        for fb in examples["down"]:
            score = fb.get("original_score", "?")
            lines.append(f"  ❌ « {fb['title'][:140]} » (score {score})")
            if fb.get("summary"):
                lines.append(f"     [{fb['summary'][:200]}]")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def attach_feedback_to_articles(articles: list[dict[str, Any]]) -> None:
    """Marque chaque article avec son feedback_signal connu (si historique).

    Permet a l'auto-tuner / scoring_v2 de penaliser au prochain run les articles
    similaires aux 👎.
    """
    if not FEEDBACK_ENABLED or not articles:
        return
    data = _load_feedback_history()
    feedbacks: list[dict[str, Any]] = data.get("feedbacks", [])
    by_id = {fb["article_id"]: fb for fb in feedbacks if "article_id" in fb}
    for art in articles:
        aid = compute_article_id(art)
        fb = by_id.get(aid)
        if fb:
            art["user_feedback"] = fb.get("rating")
            art["feedback_date"] = fb.get("date")


# =============================================================================
# Helpers pour les commandes CLI optionnelles
# =============================================================================

def manual_record(article_id: str, rating: str) -> None:
    """Helper pour enregistrer un feedback manuellement (CLI ou script)."""
    record_feedback(article_id, rating, source="manual")
    logger.info(f"Feedback manuel enregistre : {rating} sur {article_id}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    n = poll_imap_feedback()
    print(f"Feedbacks IMAP collectes : {n}")
    examples = get_few_shot_examples()
    print(f"Few-shot disponibles : {len(examples['up'])} 👍 / {len(examples['down'])} 👎")
