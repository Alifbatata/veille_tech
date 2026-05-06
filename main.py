import json
import logging
import os
import shutil
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

# Ajouter le répertoire 'src' au PYTHONPATH pour les imports relatifs
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Import des fonctions principales des modules
from src.scraper import run_scraper
from src.ai_filter import filter_articles_with_ai, GeminiUnavailableError
from src.mailer import send_digest, MailerConfigError, MailerSendError
from src.config import SCRAPE_LIMIT_MONTH
from src.archive import update_archive

# ---------------------------------------------------------------------------
# Configuration du logging — console + fichier rotatif
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_format = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Handler console (stdout)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)

# Handler fichier rotatif : 10 fichiers de 2 Mo max, dans logs/veille.log[.1, .2, ...]
_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "veille.log"),
    maxBytes=2 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_format)

# Configure le root logger : tous les modules (scraper, ai_filter, mailer, …) en héritent
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Évite la duplication si main est ré-importé
if not any(isinstance(h, RotatingFileHandler) for h in _root.handlers):
    _root.addHandler(_console_handler)
    _root.addHandler(_file_handler)

logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Validation des variables d'environnement (fail-fast)
# ---------------------------------------------------------------------------
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GMAIL_USER",
    "GMAIL_PASSWORD",
)

def _validate_env() -> None:
    """Vérifie que les variables critiques sont présentes au démarrage.

    Fail-fast : on préfère un crash net dès le boot avec un message clair plutôt
    qu'un échec sournois 5 minutes plus tard au moment du SMTP login.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        msg = (
            f"❌ Variables d'environnement manquantes : {', '.join(missing)}.\n"
            "   Vérifiez le fichier .env à la racine du projet.\n"
            "   Variables obligatoires : GEMINI_API_KEY, GMAIL_USER, GMAIL_PASSWORD.\n"
            "   Variable optionnelle : MAIL_RECIPIENT, TAVILY_API_KEY."
        )
        logger.error(msg)
        raise SystemExit(2)
    # Optionnels : log informatif si certains modules vont être désactivés
    if not os.environ.get("TAVILY_API_KEY"):
        logger.info("ℹ️  TAVILY_API_KEY absente — module Tavily Web désactivé "
                    "(RSS, GNews, OpenAlex et arXiv search restent actifs).")

# ---------------------------------------------------------------------------
# Chemins des fichiers de données
# ---------------------------------------------------------------------------
PROJECT_ROOT: str = os.path.dirname(__file__)
DATA_DIR: str = os.path.join(PROJECT_ROOT, "data")

SCRAPER_OUTPUT_PATH: str = os.path.join(DATA_DIR, "scraper_output.json")
AI_FILTER_OUTPUT_PATH: str = os.path.join(DATA_DIR, "ai_filter_output.json")
PREVIOUS_AI_OUTPUT_PATH: str = os.path.join(DATA_DIR, "previous_ai_output.json")


def send_error_email(error_msg: str) -> None:
    """
    Envoie un email d'alerte en texte brut en cas d'erreur fatale.
    Utilise les variables d'environnement GMAIL_USER, GMAIL_PASSWORD, MAIL_RECIPIENT.
    Échoue silencieusement (log uniquement) si les credentials sont absents,
    pour ne pas masquer l'erreur originale.
    """
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_PASSWORD")
    mail_recipient_str = os.environ.get("MAIL_RECIPIENT", gmail_user or "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not gmail_user or not gmail_password or not mail_recipient_str:
        logger.error(
            "❌ Impossible d'envoyer l'email d'erreur : "
            "GMAIL_USER, GMAIL_PASSWORD ou MAIL_RECIPIENT manquant."
        )
        return

    # Dédoublonnage des destinataires avec préservation de l'ordre
    to_addrs = list(dict.fromkeys(
        addr.strip() for addr in mail_recipient_str.split(",") if addr.strip()
    ))
    if not to_addrs:
        logger.error("❌ Aucun destinataire valide pour l'email d'erreur.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "❌ Erreur critique Veille Tech"
    msg["From"] = f"Veille Tech Alerte <{gmail_user}>"
    msg["To"] = ", ".join(to_addrs)

    plain_text_body = (
        "Une erreur critique est survenue dans l'orchestrateur de Veille Technologique.\n\n"
        f"{error_msg}"
    )
    msg.attach(MIMEText(plain_text_body, "plain", "utf-8"))

    try:
        logger.info("🔌 Connexion SMTP pour l'email d'erreur à %s:%d…", smtp_host, smtp_port)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, to_addrs, msg.as_string())
        logger.info("✅ Email d'erreur envoyé avec succès à %s", ", ".join(to_addrs))
    except (smtplib.SMTPException, OSError) as e:
        logger.error("❌ Échec de l'envoi de l'email d'erreur : %s", e)


def _interactive_pre_run() -> int | None:
    """Pre-run interactif : banner stylise + check heure + choix volume.

    Affiche un panneau d'accueil, avertit si l'heure locale est avant 9h
    (les quotas Google AI Studio se renouvellent a minuit Pacific Time =
    ~9h heure suisse), puis propose 5 presets de volume d'articles par
    source RSS.

    Returns:
        Nouvelle valeur de MAX_ARTICLES_PER_SOURCE choisie par l'utilisateur,
        ou None si on doit garder la valeur de config.py (mode non-interactif
        par exemple si stdin n'est pas un TTY — execution via cron/CI).
    """
    # En mode non-interactif (CI, redirection), on n'embete personne
    if not sys.stdin.isatty():
        return None

    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt, Confirm
        from rich.table import Table
        from rich.text import Text
        from rich.align import Align
    except ImportError:
        # rich pas installe — on retombe en mode silencieux pour ne pas bloquer
        logger.warning("rich non installe — mode interactif desactive. Lance 'pip install rich' "
                       "pour activer le menu de demarrage stylise.")
        return None

    console = Console()

    # Banner d'accueil
    title = Text()
    title.append("\n  🛰️  VEILLE TECHNOLOGIQUE\n", style="bold cyan")
    title.append("  Pipeline complet : RSS + arXiv + OpenAlex + Crossref + HAL + SS + Tavily + Google News + IA",
                 style="dim cyan")
    console.print(Panel(Align.center(title), border_style="cyan", padding=(1, 2)))
    console.print()

    # Verification heure
    now = datetime.now()
    if 0 <= now.hour < 9:
        warn = Text()
        warn.append("⚠️  Il est ", style="bold yellow")
        warn.append(f"{now.strftime('%Hh%M')}", style="bold red")
        warn.append(" — les quotas gratuits Google AI Studio se renouvellent a ", style="yellow")
        warn.append("minuit Pacific Time", style="bold yellow")
        warn.append(" (= ", style="yellow")
        warn.append("9h heure suisse", style="bold green")
        warn.append("). Si tu lances maintenant et que tu as fait des tests aujourd'hui, "
                    "il est probable que les premiers modeles de la cascade soient deja epuises "
                    "et que le programme bascule vite vers les fallbacks.\n\n",
                    style="yellow")
        warn.append("💡 Recommandation : ", style="bold")
        warn.append("attendre ", style="white")
        warn.append("9h00", style="bold green")
        warn.append(" pour avoir les quotas frais (le programme tournera environ 18h).",
                    style="white")
        console.print(Panel(warn, title="🕐 Verification de l'heure", border_style="yellow"))
        if not Confirm.ask("\n  Continuer quand meme maintenant ?", default=False):
            console.print("[yellow]Annule. Relance le programme apres 9h.[/yellow]")
            sys.exit(0)
    else:
        ok = Text()
        ok.append("✅ Il est ", style="bold green")
        ok.append(f"{now.strftime('%Hh%M')}", style="bold green")
        ok.append(" — les quotas Google AI Studio devraient etre frais.", style="green")
        console.print(Panel(ok, title="🕐 Verification de l'heure", border_style="green"))
    console.print()

    # Choix du volume d'articles par source
    presets = [
        ("1", "🚀  Test rapide",       25,  "~5 min", "Pour valider que tout fonctionne avant un vrai run."),
        ("2", "📰  Standard hebdo",    50,  "~3-4h",  "Usage normal hebdomadaire. Suffisant si tu lances chaque semaine."),
        ("3", "📚  Approfondi",       100,  "~8-10h", "Plus de couverture. Bon compromis si tu lances tous les 15 jours."),
        ("4", "🏆  Marathon weekend", 200,  "~18-22h", "Couverture maximale. Recommande si tu lances 1x/mois ou apres une longue pause."),
        ("5", "✏️   Personnalise",      0,  "?",      "Tu choisis le nombre toi-meme."),
    ]

    table = Table(title="Combien d'articles veux-tu collecter par source RSS ?",
                  border_style="cyan", show_lines=True)
    table.add_column("#", style="bold cyan", justify="center")
    table.add_column("Preset", style="bold")
    table.add_column("Articles / source", justify="right", style="green")
    table.add_column("Duree estimee", justify="center", style="yellow")
    table.add_column("Recommande pour")

    for choice, name, nb, dur, desc in presets:
        nb_label = str(nb) if nb else "personnalise"
        table.add_row(choice, name, nb_label, dur, desc)
    console.print(table)
    console.print(
        "\n  [dim]Note : ce nombre s'applique aux flux RSS uniquement. Les autres sources "
        "(arXiv, OpenAlex, Crossref, HAL, Tavily, Semantic Scholar, Google News) sont fixes.[/dim]\n"
    )

    chosen = Prompt.ask("  [bold]Ton choix[/bold]", choices=["1", "2", "3", "4", "5"], default="2")
    if chosen == "5":
        nb = int(Prompt.ask("  [bold]Nombre d'articles par source[/bold] (entre 5 et 1000)",
                            default="50"))
        nb = max(5, min(1000, nb))
    else:
        nb = next(p[2] for p in presets if p[0] == chosen)

    console.print()
    summary = Text()
    summary.append("✅ Volume choisi : ", style="bold green")
    summary.append(f"{nb}", style="bold cyan")
    summary.append(" articles par source RSS\n", style="green")
    summary.append("   Le programme va maintenant demarrer le pipeline complet.\n", style="dim")
    summary.append("   Les logs detailles defilent ci-dessous. Les fichiers logs sont aussi sauves dans logs/veille.log",
                   style="dim")
    console.print(Panel(summary, title="🎬 Lancement", border_style="green"))
    console.print()
    return nb


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    # Pre-run interactif (banner + check heure + choix volume articles)
    chosen_max = _interactive_pre_run()
    if chosen_max is not None:
        # Override dynamique de la constante MAX_ARTICLES_PER_SOURCE dans scraper
        # (importee depuis config — on patch le module scraper pour propager).
        import src.scraper as _scraper_mod
        _scraper_mod.MAX_ARTICLES_PER_SOURCE = chosen_max
        logger.info("📊 MAX_ARTICLES_PER_SOURCE configure a %d (choix utilisateur)", chosen_max)

    logger.info("🚀 Démarrage de l'orchestrateur de veille technologique")
    _validate_env()

    # =========================================================================
    # Bloc try/except global — toute exception non gérée déclenche send_error_email
    # =========================================================================
    try:
        # Crée le répertoire 'data' s'il n'existe pas.
        os.makedirs(DATA_DIR, exist_ok=True)

        # -----------------------------------------------------------------------
        # Étape 1 : Rotation de l'historique (articles de la semaine précédente)
        # -----------------------------------------------------------------------
        logger.info("⏳ Vérification de l'historique des articles filtrés...")
        if os.path.exists(AI_FILTER_OUTPUT_PATH):
            try:
                shutil.copy2(AI_FILTER_OUTPUT_PATH, PREVIOUS_AI_OUTPUT_PATH)
                logger.info(
                    "✅ Fichier '%s' copié vers '%s' pour l'historique.",
                    os.path.basename(AI_FILTER_OUTPUT_PATH),
                    os.path.basename(PREVIOUS_AI_OUTPUT_PATH),
                )
            except OSError as e:
                # Non fatal : on continue même si la rotation échoue
                logger.warning("⚠️ Impossible de copier le fichier d'historique : %s", e)
        else:
            logger.info(
                "ℹ️ Aucun fichier '%s' trouvé pour l'historique.",
                os.path.basename(AI_FILTER_OUTPUT_PATH),
            )

        # -----------------------------------------------------------------------
        # Étape 2 : Scraping des articles
        # -----------------------------------------------------------------------
        logger.info("📡 Lancement du scraping (RSS + arXiv + OpenAlex + Crossref + HAL + Sem.Scholar + GNews + Tavily)...")
        # include_web=True : si TAVILY_API_KEY est absente, le module fait un graceful skip
        # et le pipeline continue. Aucune raison de désactiver l'option ici.
        scraper_result: dict[str, Any] = run_scraper(include_web=True)
        with open(SCRAPER_OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(scraper_result, f, ensure_ascii=False, indent=2)
        logger.info(
            "✅ Scraping terminé. %d articles bruts sauvegardés dans '%s'.",
            scraper_result.get("meta", {}).get("total_raw", 0),
            os.path.basename(SCRAPER_OUTPUT_PATH),
        )

        # -----------------------------------------------------------------------
        # Étape 3 : Filtrage et notation par l'IA
        # -----------------------------------------------------------------------
        ai_filtered_result: dict[str, Any] = {}
        articles_to_filter: list[dict[str, Any]] = scraper_result.get("articles", [])

        if SCRAPE_LIMIT_MONTH and articles_to_filter:
            logger.info("📅 Filtrage des articles de moins de 30 jours activé.")
            now = datetime.now(timezone.utc)
            recent_articles = []
            for a in articles_to_filter:
                try:
                    # Faute de date de publication extraite, on se base sur la date de collecte
                    art_date = datetime.fromisoformat(a.get("collected_at", now.isoformat()))
                    if (now - art_date).days <= 30:
                        recent_articles.append(a)
                except ValueError:
                    recent_articles.append(a)
            articles_to_filter = recent_articles

        if not articles_to_filter:
            logger.warning("⚠️ Aucun article à filtrer par l'IA. Création d'un résultat IA vide.")
            ai_filtered_result = {
                "meta": {
                    "run_at":           datetime.now(timezone.utc).isoformat(),
                    "model":            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                    "input_count":      0,
                    "retained_count":   0,
                    "rejected_count":   0,
                    "batch_count":      0,
                    "min_score_filter": 2,
                    "tldr":             "",
                },
                "articles": [],
            }
        else:
            logger.info("🤖 Lancement du filtrage IA pour %d articles...", len(articles_to_filter))
            try:
                ai_filtered_result = filter_articles_with_ai(articles_to_filter, min_score=2)
                with open(AI_FILTER_OUTPUT_PATH, "w", encoding="utf-8") as f:
                    json.dump(ai_filtered_result, f, ensure_ascii=False, indent=2)
                logger.info(
                    "✅ Filtrage IA terminé. %d articles retenus sauvegardés dans '%s'.",
                    ai_filtered_result.get("meta", {}).get("retained_count", 0),
                    os.path.basename(AI_FILTER_OUTPUT_PATH),
                )
            except GeminiUnavailableError as e:
                logger.error(
                    "❌ L'API Gemini est indisponible ou mal configurée : %s. "
                    "Le digest sera envoyé sans filtrage IA.", e,
                )
                # Résultat vide pour permettre l'envoi du digest malgré l'échec IA
                ai_filtered_result = {
                    "meta": {
                        "run_at":           datetime.now(timezone.utc).isoformat(),
                        "model":            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                        "input_count":      len(articles_to_filter),
                        "retained_count":   0,
                        "rejected_count":   len(articles_to_filter),
                        "batch_count":      0,
                        "min_score_filter": 2,
                        "tldr":             "⚠️ Filtrage IA indisponible pour cette exécution.",
                    },
                    "articles": [],
                }
            except Exception as e:
                logger.error(
                    "❌ Erreur inattendue lors du filtrage IA : %s. "
                    "Le digest sera envoyé sans filtrage IA.", e,
                )
                ai_filtered_result = {
                    "meta": {
                        "run_at":           datetime.now(timezone.utc).isoformat(),
                        "model":            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                        "input_count":      len(articles_to_filter),
                        "retained_count":   0,
                        "rejected_count":   len(articles_to_filter),
                        "batch_count":      0,
                        "min_score_filter": 2,
                        "tldr":             "⚠️ Erreur inattendue lors du filtrage IA.",
                    },
                    "articles": [],
                }

        # -----------------------------------------------------------------------
        # Étape 3.5 : Mise à jour de l'archive cumulative (rattrapage)
        # -----------------------------------------------------------------------
        # Permet d'envoyer un récapitulatif "tout ce qu'on a vu" à de nouveaux
        # destinataires via send_recap.py, sans dépendre des flux RSS du moment.
        try:
            added = update_archive(ai_filtered_result.get("articles", []))
            logger.info("📚 Archive cumulative : %d nouvel(s) article(s) ajouté(s).", added)
        except Exception as e:
            logger.warning("⚠️ Échec mise à jour de l'archive (non-bloquant) : %s", e)

        # -----------------------------------------------------------------------
        # Étape 4 : Envoi du digest par email
        # -----------------------------------------------------------------------
        logger.info("📧 Préparation de l'envoi du digest par email...")
        try:
            send_digest(ai_filtered_result)
            logger.info("✅ Processus d'envoi du digest terminé.")
        except (MailerConfigError, MailerSendError) as e:
            logger.error("❌ Erreur lors de l'envoi de l'email : %s", e)
        except Exception as e:
            logger.error("❌ Erreur inattendue lors de l'envoi de l'email : %s", e)

        logger.info("🎉 Orchestrateur terminé avec succès.")

    except Exception as e:
        # Toute exception non capturée plus haut (erreur fatale imprévue)
        logger.error("L'orchestrateur a rencontré une erreur fatale : %s", e, exc_info=True)
        send_error_email(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
