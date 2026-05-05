"""
send_recap.py — Envoie un récapitulatif cumulé de TOUS les articles
                déjà collectés à des destinataires personnalisés.

Usage :
    python send_recap.py "alice@x.com,bob@y.com"
    python send_recap.py "alice@x.com" --min-score 4
    python send_recap.py "alice@x.com" --subject "Veille Tech — Rattrapage"
    python send_recap.py "alice@x.com" --dry-run    # génère un .html sans envoyer

Ce script utilise data/articles_archive.json (alimenté à chaque run de main.py).
Il NE relance PAS le scraping ni le filtrage IA — pas de coût Gemini.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.archive import build_recap_payload, load_archive
from src.mailer import send_digest, build_html_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("recap")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Envoie un récapitulatif cumulé de l'archive à des destinataires.",
    )
    parser.add_argument(
        "recipients",
        help="Destinataires séparés par virgule (ex: 'alice@x.com,bob@y.com').",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=2,
        help="Score minimum à inclure (défaut: 2). Mettre 4 pour ne garder que les innovations solides+.",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="Sujet personnalisé. Défaut auto : 'Veille Tech — Récapitulatif (N articles)'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'envoie pas l'email — génère data/recap_preview.html à inspecter.",
    )
    args = parser.parse_args()

    # Vérification archive
    archive = load_archive()
    if not archive:
        logger.error(
            "❌ Archive vide ou inexistante (data/articles_archive.json).\n"
            "   Lancer d'abord 'python main.py' pour la peupler."
        )
        return 1

    logger.info("📚 Archive contient %d article(s) au total.", len(archive))

    # Construction du payload (compatible send_digest)
    payload = build_recap_payload(min_score=args.min_score)
    retained = len(payload["articles"])
    logger.info("✅ %d article(s) retenu(s) avec score ≥ %d.", retained, args.min_score)

    if retained == 0:
        logger.warning("⚠️ Aucun article ne passe le filtre — rien à envoyer.")
        return 0

    # Sujet par défaut adapté au mode rattrapage
    subject = args.subject or f"Veille Tech — Récapitulatif ({retained} article(s) historiques)"

    if args.dry_run:
        out_path = os.path.join(os.path.dirname(__file__), "data/recap_preview.html")
        html = build_html_email(payload)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("🧪 Dry-run : aperçu HTML écrit dans %s", out_path)
        logger.info("   Ouvre ce fichier dans ton navigateur pour valider le rendu.")
        return 0

    # Envoi réel
    logger.info("📧 Envoi du rattrapage à : %s", args.recipients)
    result = send_digest(payload, subject=subject, recipient=args.recipients)

    if result.get("success"):
        logger.info("✅ Email envoyé avec succès (%d articles, %d KB HTML).",
                    retained, result.get("html_bytes", 0) // 1024)
        return 0

    logger.error("❌ Échec d'envoi : %s", result.get("error"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
