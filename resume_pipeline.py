"""
resume_pipeline.py — Reprend le pipeline depuis data/scraper_output.json.

A utiliser quand le scraping a reussi mais que le filtrage IA ou l'envoi
email a echoue (variables d'environnement manquantes, quota Gemini, etc.).
Ne re-scrape PAS : economise ~14-22h.

Usage :
    python resume_pipeline.py                              # prompt interactif
    python resume_pipeline.py "alice@x.com,bob@y.com"      # override destinataires
    python resume_pipeline.py "alice@x.com" --dry-run      # genere preview HTML
    python resume_pipeline.py "alice@x.com" --no-ai        # envoie sans re-filtrer (utilise ai_filter_output.json si present)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("resume")

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRAPER_OUTPUT_PATH = os.path.join(ROOT, "data", "scraper_output.json")
AI_FILTER_OUTPUT_PATH = os.path.join(ROOT, "data", "ai_filter_output.json")
RECAP_PREVIEW_PATH = os.path.join(ROOT, "data", "recap_preview.html")


def _prompt_recipients(default: str | None) -> str:
    print("\n📧 Destinataires email")
    print("   Format : separer par virgule (ex : alice@x.com,bob@y.com)")
    if default:
        print(f"   Defaut (.env MAIL_RECIPIENT) : {default}")
        prompt = "   Adresses (Entree = defaut) : "
    else:
        prompt = "   Adresses : "
    answer = input(prompt).strip()
    if not answer and default:
        return default
    if not answer:
        print("❌ Aucun destinataire fourni.")
        sys.exit(1)
    return answer


def _filter_recent(articles: list[dict[str, Any]], days: int = 30) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for a in articles:
        try:
            art_date = datetime.fromisoformat(a.get("collected_at", now.isoformat()))
            if (now - art_date).days <= days:
                out.append(a)
        except ValueError:
            out.append(a)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reprend le pipeline depuis scraper_output.json (filtrage IA + email).",
    )
    parser.add_argument(
        "recipients",
        nargs="?",
        default=None,
        help="Destinataires separes par virgule. Sinon prompt interactif.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Genere data/recap_preview.html sans envoyer d'email.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip le filtrage IA. Utilise data/ai_filter_output.json existant.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=2,
        help="Score minimum (defaut 2).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip la confirmation finale avant envoi.",
    )
    args = parser.parse_args()

    # Sanity check .env
    if not os.environ.get("GEMINI_API_KEY") and not args.no_ai:
        logger.error("❌ GEMINI_API_KEY manquante dans .env. Ajoute-la ou utilise --no-ai.")
        return 1
    if not os.environ.get("GMAIL_USER") or not os.environ.get("GMAIL_PASSWORD"):
        if not args.dry_run:
            logger.error("❌ GMAIL_USER / GMAIL_PASSWORD manquants dans .env. Utilise --dry-run pour generer un preview.")
            return 1

    # Lecture scraper_output
    if not os.path.exists(SCRAPER_OUTPUT_PATH):
        logger.error("❌ %s introuvable. Lance d'abord python main.py.", SCRAPER_OUTPUT_PATH)
        return 1

    if args.no_ai:
        # Mode skip-IA : on lit l'ai_filter_output.json existant
        if not os.path.exists(AI_FILTER_OUTPUT_PATH):
            logger.error("❌ --no-ai mais %s introuvable. Lance sans --no-ai.", AI_FILTER_OUTPUT_PATH)
            return 1
        with open(AI_FILTER_OUTPUT_PATH, encoding="utf-8") as f:
            ai_filtered_result = json.load(f)
        logger.info("📂 ai_filter_output.json charge : %d articles retenus.",
                    ai_filtered_result.get("meta", {}).get("retained_count", 0))
    else:
        with open(SCRAPER_OUTPUT_PATH, encoding="utf-8") as f:
            scraper_data = json.load(f)
        raw_articles = scraper_data.get("articles", [])
        logger.info("📂 scraper_output.json charge : %d articles bruts.", len(raw_articles))

        # Imports differes apres validation env
        from src.config import SCRAPE_LIMIT_MONTH
        from src.ai_filter import filter_articles_with_ai, GeminiUnavailableError
        from src.archive import update_archive

        if SCRAPE_LIMIT_MONTH:
            before = len(raw_articles)
            raw_articles = _filter_recent(raw_articles, days=30)
            logger.info("📅 Filtre 30 jours : %d → %d articles.", before, len(raw_articles))

        if not raw_articles:
            logger.warning("⚠️ Aucun article a filtrer apres filtre date.")
            return 1

        logger.info("🤖 Filtrage IA pour %d articles…", len(raw_articles))
        try:
            ai_filtered_result = filter_articles_with_ai(raw_articles, min_score=args.min_score)
        except GeminiUnavailableError as e:
            logger.error("❌ Gemini indisponible : %s", e)
            return 1

        from src.io_utils import atomic_write_json
        atomic_write_json(AI_FILTER_OUTPUT_PATH, ai_filtered_result)
        retained = ai_filtered_result.get("meta", {}).get("retained_count", 0)
        logger.info("✅ Filtrage IA termine : %d articles retenus, sauvegarde dans %s.",
                    retained, os.path.basename(AI_FILTER_OUTPUT_PATH))

        try:
            added = update_archive(ai_filtered_result.get("articles", []))
            logger.info("📚 Archive cumulative : +%d articles.", added)
        except Exception as e:
            logger.warning("⚠️ Echec mise a jour archive (non-bloquant) : %s", e)

    retained_count = ai_filtered_result.get("meta", {}).get("retained_count", 0)
    if retained_count == 0:
        logger.warning("⚠️ 0 article retenu — rien a envoyer.")
        return 1

    from src.mailer import send_digest, build_html_email, MailerConfigError, MailerSendError

    # Override destinataires
    default_recipient = os.environ.get("MAIL_RECIPIENT") or os.environ.get("GMAIL_USER")
    if args.recipients:
        recipients = args.recipients.strip()
        logger.info("📧 Destinataires (CLI) : %s", recipients)
    else:
        recipients = _prompt_recipients(default_recipient)
        logger.info("📧 Destinataires : %s", recipients)

    if args.dry_run:
        html = build_html_email(ai_filtered_result)
        with open(RECAP_PREVIEW_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("🧪 Dry-run : preview ecrit dans %s", RECAP_PREVIEW_PATH)
        logger.info("   Ouvre-le dans un navigateur, puis relance sans --dry-run pour envoyer.")
        return 0

    # Confirmation
    if not args.yes:
        print(f"\n📤 Pret a envoyer {retained_count} articles a : {recipients}")
        answer = input("   Confirmer l'envoi ? (o/N) : ").strip().lower()
        if answer not in ("o", "oui", "y", "yes"):
            logger.info("❌ Envoi annule par l'utilisateur.")
            return 0

    try:
        send_digest(ai_filtered_result, recipient=recipients)
        logger.info("✅ Email envoye avec succes.")
        return 0
    except (MailerConfigError, MailerSendError) as e:
        logger.error("❌ Erreur d'envoi : %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
