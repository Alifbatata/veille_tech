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

# Chargement du .env AVANT les imports src.*, sinon les modules ai_filter/mailer
# capturent os.environ.get(...) au niveau module et obtiennent None.
from dotenv import load_dotenv
load_dotenv()

# Poll feedback IMAP AVANT l'import d'ai_filter : le prompt systeme inclut les
# few-shot examples bases sur data/feedback_history.json, donc on doit avoir
# les feedbacks les plus recents AVANT que SYSTEM_PROMPT soit construit.
# Fail-safe : si IMAP echoue, le pipeline continue avec le feedback_history existant.
try:
    from src.feedback import poll_imap_feedback as _poll_imap_feedback
    _poll_imap_feedback()
except Exception:
    pass  # Boucle de feedback est optionnelle, jamais bloquante

# Import des fonctions principales des modules
from src.scraper import run_scraper
from src.ai_filter import filter_articles_with_ai, GeminiUnavailableError
from src.mailer import send_digest, MailerConfigError, MailerSendError
from src.config import SCRAPE_LIMIT_MONTH
from src.archive import update_archive
from src.io_utils import atomic_write_json

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
    """Pre-run interactif : banner + check heure + edition cibles + choix volume + recap.

    Etapes :
      1. Banner d'accueil
      2. Verification heure locale (warn si avant 9h Suisse)
      3. Affichage et edition optionnelle des cibles (entreprises + mots-cles)
      4. Choix du volume d'articles par source RSS (5 presets + personnalise)
      5. Recapitulatif final avec confirmation (boucle si l'utilisateur veut corriger)

    Returns:
        Nouvelle valeur de MAX_ARTICLES_PER_SOURCE choisie par l'utilisateur,
        ou None si stdin n'est pas un TTY (mode CI/cron, pre-run skippe).
    """
    # En mode non-interactif (CI, redirection), on n'embete personne
    if not sys.stdin.isatty():
        return None

    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt, Confirm, IntPrompt
        from rich.table import Table
        from rich.text import Text
        from rich.align import Align
    except ImportError:
        logger.warning("rich non installe — mode interactif desactive. Lance 'pip install rich' "
                       "pour activer le menu de demarrage stylise.")
        return None

    console = Console()

    # ----- Banner d'accueil -----
    title = Text()
    title.append("\n  🛰️  VEILLE TECHNOLOGIQUE\n", style="bold cyan")
    title.append("  Pipeline : RSS + arXiv + OpenAlex + Crossref + HAL + SS + Tavily + Patents + GNews + IA",
                 style="dim cyan")
    console.print(Panel(Align.center(title), border_style="cyan", padding=(1, 2)))
    console.print()

    # Guide de navigation TOUJOURS visible
    nav = (
        "  [bold yellow]📖  GUIDE DE NAVIGATION — lis-moi avant de continuer[/bold yellow]\n\n"
        "  • Quand tu vois un [bold]TABLEAU NUMEROTE[/bold], tu tapes le [bold cyan]numero[/bold cyan] "
        "de ton choix puis tu appuies sur [bold]Entree[/bold].\n\n"
        "  • Quand tu vois une [bold]QUESTION oui/non[/bold] (style [yellow]\"Continuer ?\"[/yellow]), "
        "tu tapes :\n"
        "      [bold green]y[/bold green]  pour [bold]oui[/bold]   "
        "[dim](attention : c'est la lettre [bold]y[/bold] anglaise comme \"yes\", pas \"o\")[/dim]\n"
        "      [bold red]n[/bold red]  pour [bold]non[/bold]\n\n"
        "  • Quand tu vois [bold cyan](valeur)[/bold cyan] entre parentheses, c'est la "
        "[bold]valeur par defaut[/bold] si tu appuies juste sur [bold]Entree[/bold] sans rien taper.\n\n"
        "  • [bold magenta]Pour revenir a l'etape precedente[/bold magenta] : tape "
        "[bold]r[/bold] quand le programme te le permet (mention explicite a chaque etape concernee).\n\n"
        "  • [bold]Au recap final[/bold], si tu reponds [bold red]n (non)[/bold red], tu reboucles "
        "sur tout depuis le debut (cibles, volume, recap).\n\n"
        "  • Pour [bold]annuler completement[/bold] et fermer le programme a tout moment : "
        "appuie sur [bold red]Ctrl+C[/bold red] (touche Ctrl maintenue + lettre C)."
    )
    console.print(Panel(nav, border_style="magenta", padding=(1, 1)))
    _press_enter_to_continue(console, "Lis bien le guide ci-dessus avant de continuer.")

    # ----- ETAPE 1 : Verification heure -----
    console.print("\n[bold blue]══════════════════════════════════════════════════════════════[/bold blue]")
    console.print("[bold blue]  Etape 1/4 : verification de l'heure                         [/bold blue]")
    console.print("[bold blue]══════════════════════════════════════════════════════════════[/bold blue]\n")
    _check_hour_warning(console, Confirm, Panel, Text)
    _press_enter_to_continue(console, "Etape 1 terminee. Etape suivante : choix de la memoire des articles.")

    # ----- ETAPE 2 : Memoire des articles deja envoyes -----
    console.print("\n[bold blue]══════════════════════════════════════════════════════════════[/bold blue]")
    console.print("[bold blue]  Etape 2/4 : memoire des articles deja envoyes               [/bold blue]")
    console.print("[bold blue]══════════════════════════════════════════════════════════════[/bold blue]\n")
    _memory_choice_step(console, Panel, Prompt, Confirm)
    _press_enter_to_continue(console, "Etape 2 terminee. Etape suivante : tes cibles.")

    # ----- ETAPES 3 (cibles) + 4 (volume) + recap, avec boucle de retour -----
    while True:
        # ----- ETAPE 3 : cibles -----
        console.print("\n[bold blue]══════════════════════════════════════════════════════════════[/bold blue]")
        console.print("[bold blue]  Etape 3/4 : cibles (entreprises + mots-cles)                [/bold blue]")
        console.print("[bold blue]══════════════════════════════════════════════════════════════[/bold blue]\n")
        _show_targets(console, Table, Panel)
        _press_enter_to_continue(console, "Prends le temps de lire les cibles ci-dessus.")

        edit_msg = (
            "  [bold]Veux-tu modifier ces cibles avant de lancer le pipeline ?[/bold]\n"
            "  [dim]Tape [bold]y[/bold] (yes = oui) pour ouvrir le menu d'edition, "
            "ou [bold]n[/bold] (non) pour passer directement a l'etape 4.\n"
            "  Si tu appuies juste sur [bold]Entree[/bold] sans rien taper, ce sera "
            "[bold]n (non)[/bold] par defaut.[/dim]"
        )
        if Confirm.ask(edit_msg, default=False):
            _edit_targets_menu(console, Table, Panel, Prompt, IntPrompt, Confirm)

        # ----- ETAPE 4 : volume + retour eventuel -----
        console.print("\n[bold blue]══════════════════════════════════════════════════════════════[/bold blue]")
        console.print("[bold blue]  Etape 4/4 : volume d'articles par source                    [/bold blue]")
        console.print("[bold blue]══════════════════════════════════════════════════════════════[/bold blue]\n")
        nb = _choose_volume(console, Table, Panel, Prompt, IntPrompt)
        if nb is None:
            # L'utilisateur a tape "r" pour revenir aux cibles
            console.print("[yellow]↩ Retour a l'etape 3 (cibles)…[/yellow]\n")
            continue

        # ----- Verification quotas API -----
        try:
            with open(os.path.join(DATA_DIR, "targets.json"), encoding="utf-8") as f:
                _t = json.load(f)
            _nc = len(_t.get("companies", []))
            _nk = len(_t.get("keywords", []))
            _ns = len(_t.get("solo_keywords", []))
            _nr = len(_t.get("research_orgs", []))
            _nx = len(_t.get("cross_domain_topics", []))
        except (OSError, json.JSONDecodeError):
            _nc = _nk = _ns = _nr = _nx = 0
        console.print()
        if not _check_quotas_panel(console, Table, Panel, Confirm, _nc, _nk, _ns, nb, _nr, _nx):
            console.print("[yellow]↩ Retour a l'etape 3 (cibles) pour reduire le volume…[/yellow]\n")
            continue

        # ----- Recap final + confirmation -----
        if _show_recap_and_confirm(console, Panel, Confirm, nb):
            return nb
        console.print("[yellow]↩ Retour a l'etape 3 (cibles) pour modifier ta config…[/yellow]\n")


def _press_enter_to_continue(console, intro: str = "") -> None:
    """Pause stylisee : attend que l'utilisateur appuie sur Entree pour avancer.

    Le prompt final est INVARIABLE et parfaitement explicite ("Appuie sur la
    touche ENTREE pour continuer") pour qu'aucun novice ne se demande s'il
    faut taper quelque chose ou juste appuyer sur Entree.

    Args:
        intro : phrase optionnelle affichee en gris au-dessus du prompt
                (contexte type "L'etape 1 est terminee").
    """
    try:
        from rich.prompt import Prompt
        if intro:
            console.print(f"\n  [dim]{intro}[/dim]")
        Prompt.ask(
            "  [bold yellow]👉  Appuie sur la touche ENTREE pour continuer[/bold yellow]",
            default="", show_default=False,
        )
    except (ImportError, EOFError, KeyboardInterrupt):
        try:
            if intro:
                print(f"\n  {intro}")
            input("  >>> Appuie sur la touche ENTREE pour continuer... ")
        except (EOFError, KeyboardInterrupt):
            pass


# Module-level : memorise le choix de l'utilisateur pour affichage dans le recap
# (et pour info dans les logs au demarrage du pipeline). Set par
# _memory_choice_step(), lu par _show_recap_and_confirm() et main().
_memory_choice_label: str = ""


def _memory_choice_step(console, Panel, Prompt, Confirm) -> None:
    """Etape memoire : filtrer / tout renvoyer / reset.

    Effets de bord :
      - met a jour os.environ['USE_MEMORY'] = 'true' ou 'false'
      - met a jour le label module-level _memory_choice_label
      - en mode reset : supprime data/seen_urls.json (avec confirmation)

    main() doit ensuite propager le choix au scraper via :
        scraper.USE_MEMORY = (os.environ.get('USE_MEMORY') == 'true')
    """
    global _memory_choice_label

    seen_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "seen_urls.json"
    )
    seen_count = 0
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    seen_count = len(data)
        except (json.JSONDecodeError, OSError):
            seen_count = 0

    body = (
        f"  [bold]📚 Etat actuel[/bold]\n"
        f"     • [cyan]{seen_count}[/cyan] URLs deja stockees dans "
        f"[dim]data/seen_urls.json[/dim] [dim](= articles que tu as deja recus auparavant)[/dim]\n\n"
        "  [bold]Que veux-tu pour CE run ?[/bold]\n\n"

        "  [bold green][F][/bold green]iltrer les articles deja envoyes     "
        "[dim]>>> RECOMMANDE pour digest hebdomadaire[/dim]\n"
        "      → Le digest ne contiendra [bold green]QUE des articles jamais envoyes[/bold green].\n"
        "      → Les URLs deja vues sont eliminees AVANT le scoring IA (economie de quota).\n"
        "      → Apres le run, les nouveaux articles vus sont ajoutes a la memoire.\n\n"

        "  [bold yellow][T][/bold yellow]out renvoyer (sans filtre)\n"
        "      → Tu recevras [bold yellow]TOUS les articles trouves[/bold yellow] : nouveaux ET deja envoyes,\n"
        "        [bold]melanges dans le meme digest[/bold] sans section separee.\n"
        "      → Les articles deja vus auront un [bold magenta]badge 'Deja envoye'[/bold magenta] dans l'email\n"
        "        pour que tu les distingues des vrais nouveaux d'un coup d'oeil.\n"
        "      → Utile pour tester le rendu ou rattraper apres un bug.\n\n"

        "  [bold red][R][/bold red]einitialiser la memoire (effacer)\n"
        "      [red]/!\\ irreversible[/red] : supprime [bold]seen_urls.json[/bold] (les "
        f"[cyan]{seen_count}[/cyan] URLs sont perdues).\n"
        "      → Pour CE run, comme la memoire est vide, [bold]tous les articles[/bold] arriveront\n"
        "        comme s'ils etaient nouveaux (aucun badge).\n"
        "      → Aux runs suivants, le filtre normal repart de zero.\n"
        "      → A utiliser si tu changes de destinataires ou apres une tres longue pause."
    )
    console.print(Panel(
        body, border_style="cyan", padding=(1, 2),
        title="🧠 Memoire des articles deja envoyes",
    ))

    choice = Prompt.ask(
        "  [bold]Ton choix[/bold] "
        "[dim](Entree = [bold green]F[/bold green]iltrer)[/dim]",
        choices=["f", "t", "r", "F", "T", "R"],
        default="f",
        show_choices=False,
        show_default=False,
    ).lower()
    console.print()

    if choice == "f":
        os.environ["USE_MEMORY"] = "true"
        _memory_choice_label = (
            f"FILTRER ({seen_count} articles deja envoyes seront exclus)"
            if seen_count > 0
            else "FILTRER (memoire vide pour le moment)"
        )
        console.print(
            f"  [green]✓ Mode FILTRE active. "
            f"{seen_count} articles deja envoyes seront exclus.[/green]\n"
        )
        return

    if choice == "t":
        os.environ["USE_MEMORY"] = "false"
        _memory_choice_label = "TOUT RENVOYER (pas de filtre cette fois)"
        console.print(
            "  [yellow]✓ Mode TOUT RENVOYER active. "
            "Aucun filtre applique pour ce run.[/yellow]\n"
        )
        return

    # choice == "r" : reset
    if seen_count > 0:
        confirmed = Confirm.ask(
            f"  [red]/!\\[/red]  Ceci va effacer [bold]{seen_count}[/bold] "
            "URLs de la memoire. Confirmer ?",
            default=False,
        )
        if not confirmed:
            os.environ["USE_MEMORY"] = "true"
            _memory_choice_label = (
                f"FILTRER ({seen_count} URLs - reset annule)"
            )
            console.print(
                "  [yellow]Reset annule. Mode FILTRE active a la place.[/yellow]\n"
            )
            return
    try:
        if os.path.exists(seen_path):
            os.remove(seen_path)
        console.print(
            f"  [green]✓ Memoire reinitialisee : {seen_count} URLs effacees.[/green]"
        )
    except OSError as e:
        console.print(
            f"  [yellow]⚠️  Impossible de supprimer {seen_path} : {e}[/yellow]"
        )
    os.environ["USE_MEMORY"] = "true"
    _memory_choice_label = f"RESET ({seen_count} URLs effacees) puis FILTRER"
    console.print()


def _check_hour_warning(console, Confirm, Panel, Text) -> None:
    """Verifie l'heure locale et previent si avant 9h (quotas Google reset)."""
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
                    "il est probable que les premiers modeles de la cascade soient deja epuises.\n\n",
                    style="yellow")
        warn.append("💡 Recommandation : attendre ", style="bold")
        warn.append("9h00", style="bold green")
        warn.append(" pour avoir les quotas frais.", style="bold")
        console.print(Panel(warn, title="🕐 Verification de l'heure", border_style="yellow"))
        if not Confirm.ask(
            "\n  [bold]Veux-tu continuer quand meme maintenant ?[/bold]\n"
            "  [dim]Tape [bold]y[/bold] (yes = oui, lancer maintenant), "
            "ou [bold]n[/bold] (non, je relancerai apres 9h).\n"
            "  Si tu appuies juste sur [bold]Entree[/bold] sans rien taper, ce sera "
            "[bold]n (non)[/bold] par defaut.[/dim]",
            default=False,
        ):
            console.print("[yellow]Annule. Relance le programme apres 9h pour avoir des quotas frais.[/yellow]")
            sys.exit(0)
    else:
        ok = Text()
        ok.append("✅ Il est ", style="bold green")
        ok.append(f"{now.strftime('%Hh%M')}", style="bold green")
        ok.append(" — les quotas Google AI Studio devraient etre frais.", style="green")
        console.print(Panel(ok, title="🕐 Verification de l'heure", border_style="green"))
    console.print()


# =============================================================================
# Quotas API (verification pre-run)
# =============================================================================
# Limites connues — verifiees 2026 sur les pages de tarification officielles.
# Tavily et Gemini Flash sont les seules a avoir un quota DUR (free tier).
# Google News n'a pas de quota officiel mais le seuil ~500 req/run protege
# contre les bans IP comportementaux (le code applique deja un break/30 req).
_QUOTA_TAVILY_PER_MONTH      = 1000
_QUOTA_GEMINI_FLASH_PER_DAY  = 20
_QUOTA_GNEWS_SOFT_PER_RUN    = 500   # soft, anti-ban
_QUOTA_GNEWS_DANGER_PER_RUN  = 700   # rouge au-dela
_QUOTA_TAVILY_PER_RUN_WARN   = 200   # 1000/mois ÷ 5 runs/mois ≈ 200


def _compute_request_counts(
    nb_companies: int, nb_keywords: int, nb_solos: int, nb_per_source: int,
    nb_research_orgs: int = 0, nb_cross_topics: int = 0,
) -> dict[str, int]:
    """Estime le nombre de requetes par source pour CE run.

    Le mapping refletre exactement ce que les build_*_queries() produisent
    dans scraper.py + le pipeline d'IA filter (batchs configures via
    AI_BATCH_SIZE, defaut 30, lu depuis l'env comme dans ai_filter.py).

    research_orgs et cross_domain_topics sont broadcastes UNIQUEMENT sur les
    sources scientifiques (arXiv, OpenAlex, Crossref, HAL, S2, Tavily, Patents)
    et PAS sur GNews. cross_domain_topics ouvrent la decouverte d'innovations
    transferables vers PVD/ALD (photonique, MEMS, nanotech, biomim, etc.).
    """
    nb_q_gnews = nb_companies * nb_keywords + nb_solos

    try:
        ai_batch_size = max(1, int(os.environ.get("AI_BATCH_SIZE", "30")))
    except ValueError:
        ai_batch_size = 30

    raw_articles = (
        nb_per_source * 5
        + 100
        + nb_q_gnews * 5
        + (nb_research_orgs + nb_cross_topics) * 8  # broadcast science (7 sources)
    )
    nb_batches = max(3, int(raw_articles * 0.7 / ai_batch_size))

    # Sources scientifiques : base + keywords + solos + research_orgs + cross_topics
    sci_extra = nb_keywords + nb_solos + nb_research_orgs + nb_cross_topics
    return {
        "RSS":              5,
        "arXiv search":     7 + sci_extra,
        "OpenAlex":         6 + sci_extra,
        "Crossref":         8 + sci_extra,
        "HAL":              6 + sci_extra,
        "Semantic Scholar": 7 + sci_extra,
        "Tavily":           4 + sci_extra,
        "Google Patents":   8 + sci_extra,
        "Google News":      nb_q_gnews,
        "Gemini Flash":     nb_batches,
        "_raw_articles_estimate": raw_articles,
        "_ai_batch_size":         ai_batch_size,
    }


def _estimate_run_duration_h(nb_per_source: int, nb_q: int) -> tuple[float, float]:
    """Estimation de la duree totale du run (GNews + RSS + IA + autres).

    Calibrage empirique :
    - GNews (dominant) : delais inter-requete `_humanlike_inter_request_delay`
      (mix fast/normal/slow/very_slow, moyenne ~137s) + long break ~10 min
      toutes les 30 requetes + pause circadienne 4-6h apres 6h de run.
    - RSS + arXiv search + OpenAlex + Crossref + HAL + S2 + Tavily + IA filter :
      contribution mineure (quelques minutes), proportionnelle au volume RSS.

    Returns:
        (gnews_h, aux_h) : duree GNews et duree des sources auxiliaires en heures.
        Le total = gnews_h + aux_h.
    """
    # GNews — formule progressive (le coefficient augmente avec nb_q car les
    # breaks toutes les 30 req s'amortissent moins bien sur les gros volumes).
    if nb_q == 0:
        gnews_h = 0.0
    elif nb_q < 50:
        gnews_h = nb_q * 0.04
    elif nb_q < 150:
        gnews_h = nb_q * 0.045
    elif nb_q < 300:
        gnews_h = nb_q * 0.05 + 5.0  # +5h pause circadienne (run > 6h)
    else:
        gnews_h = nb_q * 0.055 + 5.0

    # Sources auxiliaires : 5 RSS + ~30 requetes thematiques + IA filter
    # IA : ~0.5 min par batch de 30 articles, articles bruts ~ nb_per_source*5 + 100
    raw_articles = nb_per_source * 5 + 100
    ai_min = (raw_articles * 0.7 / 30) * 0.5
    rss_min = 1.0 + nb_per_source * 0.02      # ~1 min base + scaling RSS
    autres_min = 5.0                          # arXiv search + OpenAlex + ...
    aux_h = (rss_min + ai_min + autres_min) / 60.0

    return gnews_h, aux_h


def _check_quotas_panel(
    console, Table, Panel, Confirm,
    nb_companies: int, nb_keywords: int, nb_solos: int, nb_per_source: int,
    nb_research_orgs: int = 0, nb_cross_topics: int = 0,
) -> bool:
    """Verifie les quotas API pour le run prevu et affiche un tableau pedagogique.

    Calcule les requetes prevues par source, les compare aux limites connues
    (Tavily 1000/mois, Gemini Flash 20/jour, GNews soft ~500/run), affiche
    un tableau colore (vert/jaune/rouge) et propose a l'utilisateur de
    revenir corriger si une limite est depassee.

    Returns:
        True si l'utilisateur veut continuer, False pour revenir aux cibles.
    """
    counts = _compute_request_counts(
        nb_companies, nb_keywords, nb_solos, nb_per_source,
        nb_research_orgs, nb_cross_topics,
    )

    t = Table(
        title="🔒  Verification des quotas API pour CE run",
        border_style="cyan", show_lines=False,
    )
    t.add_column("Source", style="bold")
    t.add_column("Req / run", justify="right", style="cyan")
    t.add_column("Quota officiel", style="dim")
    t.add_column("Runs max possibles", justify="right")
    t.add_column("Statut", justify="center")

    issues: list[str] = []

    # Sources sans quota dur — toujours OK pour des cibles raisonnables
    for src in ("RSS", "arXiv search", "OpenAlex", "Crossref", "HAL",
                "Semantic Scholar", "Google Patents"):
        t.add_row(src, str(counts[src]), "illimite (gratuit)", "[dim]∞[/dim]", "[green]✓ OK[/green]")

    # Tavily — quota DUR mensuel
    tavily = counts["Tavily"]
    runs_tavily_month = _QUOTA_TAVILY_PER_MONTH // max(tavily, 1)
    if tavily >= _QUOTA_TAVILY_PER_RUN_WARN:
        status_t = "[red]✘ ATTENTION[/red]"
        issues.append(
            f"Tavily : {tavily} req/run × 5 runs/mois = {tavily * 5} > "
            f"{_QUOTA_TAVILY_PER_MONTH} (free tier). Tu vas brûler ton quota mensuel."
        )
    elif tavily >= _QUOTA_TAVILY_PER_RUN_WARN // 2:
        status_t = "[yellow]⚠ tendu[/yellow]"
    else:
        status_t = "[green]✓ OK[/green]"
    t.add_row("Tavily", str(tavily), f"{_QUOTA_TAVILY_PER_MONTH}/mois (free)",
              f"{runs_tavily_month}/mois", status_t)

    # Google News — quota SOFT par run (anti-ban IP)
    gnews = counts["Google News"]
    if gnews >= _QUOTA_GNEWS_DANGER_PER_RUN:
        status_g = "[red]✘ RISQUE[/red]"
        issues.append(
            f"Google News : {gnews} req/run dépasse {_QUOTA_GNEWS_DANGER_PER_RUN} — "
            "risque sérieux de ban IP temporaire. Réduis les cibles."
        )
    elif gnews >= _QUOTA_GNEWS_SOFT_PER_RUN:
        status_g = "[yellow]⚠ tendu[/yellow]"
        issues.append(
            f"Google News : {gnews} req/run au-dessus du seuil prudentiel "
            f"({_QUOTA_GNEWS_SOFT_PER_RUN}). Le code applique des breaks mais "
            "reste prudent."
        )
    else:
        status_g = "[green]✓ OK[/green]"
    t.add_row("Google News", str(gnews), f"~{_QUOTA_GNEWS_SOFT_PER_RUN}/run (soft)",
              f"~{_QUOTA_GNEWS_SOFT_PER_RUN // max(gnews, 1)}/run", status_g)

    # Gemini Flash 2.5 — quota DUR journalier, mais cascade gere ~80 batches/run
    nb_b = counts["Gemini Flash"]
    raw_est = counts["_raw_articles_estimate"]
    batch_size = counts["_ai_batch_size"]
    runs_gem_day = _QUOTA_GEMINI_FLASH_PER_DAY // max(nb_b, 1)
    # Seuils : <=20 = OK pur Flash 2.5 ; 20-80 = cascade (informatif) ; >80 = ATTENTION
    if nb_b > 80:
        status_ai = "[red]✘ ATTENTION[/red]"
        issues.append(
            f"Gemini : {nb_b} batches risque de saturer la cascade entiere "
            "(Flash + Lite + Pro + Gemma cumulent ~80 req/jour free tier). "
            "Reduis les cibles ou augmente AI_BATCH_SIZE."
        )
    elif nb_b > _QUOTA_GEMINI_FLASH_PER_DAY:
        status_ai = "[yellow]⚠ cascade[/yellow]"
    else:
        status_ai = "[green]✓ OK[/green]"
    t.add_row("Gemini Flash 2.5", f"~{nb_b} batches",
              f"{_QUOTA_GEMINI_FLASH_PER_DAY}/jour (cascade ~80)",
              f"~{runs_gem_day}/jour", status_ai)

    console.print(t)
    console.print(
        f"  [dim]ℹ Estimation Gemini : ~{raw_est} articles bruts collectes × 70% "
        f"(apres dedup) ÷ AI_BATCH_SIZE={batch_size} = ~{nb_b} batches.[/dim]\n"
    )

    if issues:
        warn_body = "  [yellow bold]⚠ Avertissements detectes :[/yellow bold]\n\n"
        for i, issue in enumerate(issues, 1):
            warn_body += f"     [bold]{i}.[/bold] {issue}\n"
        warn_body += (
            "\n  [bold]Que peux-tu faire pour corriger ?[/bold]\n"
            "     • [cyan]Reduire les entreprises ou mots-cles couples[/cyan] "
            "→ baisse Google News (multiplicatif)\n"
            "     • [cyan]Reduire les solo_keywords[/cyan] → baisse Tavily, GNews "
            "et toutes les sources thematiques\n"
            "     • [cyan]Lancer le programme moins souvent[/cyan] (ex: 1×/quinzaine "
            "au lieu de 1×/semaine) → laisse les quotas se reconstituer"
        )
        console.print(Panel(warn_body, border_style="yellow", padding=(1, 2)))
        console.print()
        if not Confirm.ask(
            "  [bold]Continuer malgre ces avertissements ?[/bold]\n"
            "  [dim]Tape [bold]y[/bold] pour continuer (j'assume), "
            "ou [bold]n[/bold] pour revenir aux cibles et corriger.\n"
            "  Entree seul = [bold]n (non, revenir)[/bold] par defaut.[/dim]",
            default=False,
        ):
            return False
    else:
        console.print(
            "  [green]✓ Tous les quotas sont respectes pour ce run.[/green]\n"
        )

    return True


def _format_duration(h: float) -> str:
    """Formate une duree en heures sous forme '~X min' ou '~Xh' / '~X.Yh'."""
    if h <= 0:
        return "—"
    if h < 1.0:
        return f"~{int(round(h * 60))} min"
    if h < 10.0:
        return f"~{h:.1f}h"
    return f"~{int(round(h))}h"


def _show_targets(console, Table, Panel, targets_dict: dict | None = None) -> None:
    """Affiche les entreprises et mots-cles courants avec recommandations volume.

    Args:
        targets_dict : si fourni, affiche cette liste en memoire (utile dans le menu
            d'edition pour voir les modifs non encore sauvegardees). Sinon, charge
            depuis data/targets.json sur disque.
    """
    if targets_dict is not None:
        targets = targets_dict
        is_live = True
    else:
        targets_path = os.path.join(DATA_DIR, "targets.json")
        if not os.path.exists(targets_path):
            console.print("[red]✘ data/targets.json introuvable.[/red]")
            return
        with open(targets_path, encoding="utf-8") as f:
            targets = json.load(f)
        is_live = False
    # Tri alphabetique (insensible a la casse) pour reperer rapidement les doublons
    companies = sorted(targets.get("companies", []), key=str.lower)
    keywords = sorted(targets.get("keywords", []), key=str.lower)
    solo_keywords = sorted(targets.get("solo_keywords", []), key=str.lower)
    research_orgs = sorted(targets.get("research_orgs", []), key=str.lower)
    cross_domain_topics = sorted(targets.get("cross_domain_topics", []), key=str.lower)

    # Nombre total de requetes GNews = (entreprises × mots-cles couples) + solos
    nb_q = len(companies) * len(keywords) + len(solo_keywords)
    # Duree GNews seule (volume dominant). On suppose un volume RSS Standard (50)
    # pour cette estimation a vue d'oeil ; le volume reel est choisi a l'etape
    # suivante par _choose_volume.
    gnews_h, aux_h_default = _estimate_run_duration_h(nb_per_source=50, nb_q=nb_q)
    if nb_q == 0:
        dur_est = "0 (aucune cible)"
    else:
        if nb_q < 50:
            note = "(court)"
        elif nb_q < 150:
            note = "(modere)"
        elif nb_q < 300:
            note = "(long, avec pause circadienne ~5h)"
        else:
            note = "(TRES long, avec pause circadienne ~5h)"
        dur_est = f"{_format_duration(gnews_h + aux_h_default)} {note}"

    live_suffix = " [yellow](non encore sauvegarde)[/yellow]" if is_live else ""

    # Tableau companies
    t1 = Table(title=f"🏢  Entreprises surveillees ({len(companies)}){live_suffix}",
               border_style="cyan")
    t1.add_column("#", style="dim", justify="right")
    t1.add_column("Nom", style="cyan")
    for i, c in enumerate(companies, 1):
        t1.add_row(str(i), c)
    console.print(t1)

    # Tableau keywords (couples avec entreprises)
    t2 = Table(title=f"🔑  Mots-cles couples ({len(keywords)}){live_suffix}",
               border_style="cyan")
    t2.add_column("#", style="dim", justify="right")
    t2.add_column("Mot-cle", style="green")
    for i, k in enumerate(keywords, 1):
        t2.add_row(str(i), k)
    console.print(t2)

    # Tableau solo_keywords (cherches seuls)
    t3 = Table(
        title=f"🎯  Mots-cles SOLO — cherches seuls, sans entreprise ({len(solo_keywords)}){live_suffix}",
        border_style="magenta",
    )
    t3.add_column("#", style="dim", justify="right")
    t3.add_column("Phrase / mot-cle", style="magenta")
    if not solo_keywords:
        t3.add_row("—", "[dim](liste vide — aucune recherche solo programmee)[/dim]")
    else:
        for i, k in enumerate(solo_keywords, 1):
            t3.add_row(str(i), k)
    console.print(t3)

    # Tableau research_orgs (broadcastes uniquement sur sources scientifiques)
    t4 = Table(
        title=f"🎓  Organismes de recherche — labos / universites qui PUBLIENT ({len(research_orgs)}){live_suffix}",
        border_style="blue",
    )
    t4.add_column("#", style="dim", justify="right")
    t4.add_column("Nom (broadcaste sur arXiv/OpenAlex/Crossref/HAL/SS/Tavily/Patents)", style="blue")
    if not research_orgs:
        t4.add_row("—", "[dim](liste vide — aucun labo cible specifiquement)[/dim]")
    else:
        for i, org in enumerate(research_orgs, 1):
            t4.add_row(str(i), org)
    console.print(t4)

    # Tableau cross_domain_topics (themes transversaux pour decouverte d'innovations)
    t5 = Table(
        title=f"🌐  Themes cross-domaine — innovations transferables a PVD/ALD ({len(cross_domain_topics)}){live_suffix}",
        border_style="bright_magenta",
    )
    t5.add_column("#", style="dim", justify="right")
    t5.add_column("Theme (photonique, MEMS, nanotech, biomim, IA, decoratif...)", style="bright_magenta")
    if not cross_domain_topics:
        t5.add_row("—", "[dim](liste vide — aucune recherche transversale)[/dim]")
    else:
        for i, topic in enumerate(cross_domain_topics, 1):
            t5.add_row(str(i), topic)
    console.print(t5)

    # Recommandations — on marque dynamiquement la ligne correspondant a ta config
    # actuelle (basee sur nb_q = entreprises × mots-cles + solos).
    if nb_q == 0:
        tier = -1
    elif nb_q < 100:
        tier = 0
    elif nb_q < 200:
        tier = 1
    elif nb_q < 400:
        tier = 2
    else:
        tier = 3
    actuel = " [bold yellow](actuel)[/bold yellow]"
    marks = [actuel if tier == i else "        " for i in range(4)]
    reco = (
        f"  📊 [bold]Total requetes Google News[/bold] = "
        f"({len(companies)} × {len(keywords)}) + {len(solo_keywords)} solo = "
        f"[bold cyan]{nb_q}[/bold cyan] requetes\n"
        f"  ⏱  [bold]Duree GNews estimee[/bold] : [yellow]{dur_est}[/yellow]\n\n"
        f"  💡 [bold]Recommandations[/bold] (la ligne marquee [bold yellow](actuel)[/bold yellow] reflete ta config) :\n"
        f"     • [green]5-10 entreprises × 5-7 mots-cles[/green]   = ~25-70 req{marks[0]}  (1-3h, pour test)\n"
        f"     • [yellow]15 entreprises × 10 mots-cles[/yellow]       = ~150 req{marks[1]}    (6-10h, hebdo classique)\n"
        f"     • [cyan]21 entreprises × 14 mots-cles[/cyan]        = ~294 req{marks[2]}    (18-22h, weekend marathon)\n"
        f"     • [red]>30 entreprises × >15 mots-cles[/red]        = >450 req{marks[3]}    (>30h, deconseille)\n\n"
        f"  💡 [bold]A propos des mots-cles SOLO[/bold] :\n"
        f"     Chaque solo ajoute 1 requete GNews mais aussi 1 requete sur arXiv,\n"
        f"     OpenAlex, Crossref, HAL, Semantic Scholar et Tavily. Reserve ce champ\n"
        f"     a des phrases TRES specifiques de 4+ mots (sinon trop de bruit)."
    )
    console.print(Panel(reco, title="📈  Volume actuel", border_style="cyan"))
    console.print()


def _print_mini_list(console, Table, label: str, items: list[str], color: str) -> None:
    """Affiche une mini-liste numerotee des items courants — utilisee apres chaque
    modification pour donner un retour visuel immediat de l'etat in-memory.

    Tri alphabetique (insensible a la casse) pour reperer rapidement un doublon.
    """
    if not items:
        console.print(f"  [dim]({label} : liste vide)[/dim]")
        return
    items_sorted = sorted(items, key=str.lower)
    t = Table(title=f"{label} ({len(items_sorted)})", border_style=color, show_header=False, expand=False)
    t.add_column("#", style="dim", justify="right", width=4)
    t.add_column("Valeur", style=color)
    for i, item in enumerate(items_sorted, 1):
        t.add_row(str(i), item)
    console.print(t)


# =============================================================================
# Acteurs decouverts : revue interactive (action 11 du menu d'edition)
# =============================================================================

_DISCOVERED_ACTORS_PATH = os.path.join(DATA_DIR, "discovered_actors.json")


def _load_discovered_actors_for_review() -> list[dict]:
    """Charge le fichier des acteurs decouverts trie par count decroissant.

    Filtre : exclut les acteurs deja presents dans companies ou research_orgs
    en in-memory (l'utilisateur peut les avoir acceptes au cours de la session).
    """
    if not os.path.exists(_DISCOVERED_ACTORS_PATH):
        return []
    try:
        with open(_DISCOVERED_ACTORS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        actors = list(data.get("actors", {}).values())
        actors.sort(key=lambda a: a.get("count", 0), reverse=True)
        return actors
    except (OSError, json.JSONDecodeError):
        return []


def _count_discovered_actors(targets: dict) -> int:
    """Renvoie le nombre d'acteurs decouverts pertinents non deja dans les listes."""
    known = set(c.lower() for c in targets.get("companies", []))
    known.update(o.lower() for o in targets.get("research_orgs", []))
    return sum(
        1 for a in _load_discovered_actors_for_review()
        if a.get("name", "").lower() not in known
    )


def _review_discovered_actors(console, Table, Panel, Prompt, IntPrompt, targets: dict) -> None:
    """Revue interactive des acteurs decouverts pendant les runs precedents.

    Affiche un tableau classe par occurrences (les plus vus en haut). L'utilisateur
    peut accepter (-> ajout a companies ou research_orgs) ou rejeter (-> sera
    re-propose au prochain run) ou ignorer (-> reste dans la liste candidats).
    """
    known_companies = set(c.lower() for c in targets.get("companies", []))
    known_orgs      = set(o.lower() for o in targets.get("research_orgs", []))
    known           = known_companies | known_orgs

    candidates = [
        a for a in _load_discovered_actors_for_review()
        if a.get("name", "").lower() not in known
    ]

    if not candidates:
        console.print(Panel(
            "[dim]Aucun acteur découvert pour le moment.\n\n"
            "Lance d'abord un run complet (python main.py) — le pipeline va\n"
            "extraire les déposants Patents et institutions OpenAlex non encore\n"
            "présents dans tes listes companies/research_orgs. Reviens ici après\n"
            "le run pour les valider.[/dim]",
            title="🔍  Acteurs découverts (vide)",
            border_style="dim",
        ))
        console.print()
        return

    # Affichage sous forme de tableau
    t = Table(
        title=f"🔍  Acteurs découverts au fil des runs ({len(candidates)} candidats)",
        border_style="bright_cyan",
    )
    t.add_column("#", style="dim", justify="right")
    t.add_column("Nom", style="bright_cyan", no_wrap=True)
    t.add_column("Vu", justify="right", style="bold")
    t.add_column("Sources", style="dim")
    t.add_column("Dernière vue", style="dim")
    for i, a in enumerate(candidates, 1):
        last_seen = a.get("last_seen", "")[:10]
        srcs = ", ".join(a.get("sources", []))
        t.add_row(str(i), a.get("name", "?"), str(a.get("count", 0)), srcs, last_seen)
    console.print(t)
    console.print(
        "  [dim]💡 [bold]Vu[/bold] = nombre d'occurrences cumulees (compte au fil des runs).\n"
        "  Plus une entree apparait souvent, plus c'est un signal fort.[/dim]\n"
    )

    while True:
        choice = Prompt.ask(
            "  [bold]Que veux-tu faire ?[/bold]\n"
            "    [bold green]a[/bold green]N  Ajouter l'acteur N a [cyan]companies[/cyan] (ex: a3)\n"
            "    [bold blue]l[/bold blue]N  Ajouter l'acteur N a [blue]research_orgs[/blue] (ex: l5)\n"
            "    [bold red]r[/bold red]N  Rejeter (supprime de la liste candidats) (ex: r2)\n"
            "    [bold cyan]q[/bold cyan]   Retour au menu principal\n"
            "  [dim](Entree = q quitter la revue)[/dim]",
            default="q", show_default=False,
        ).strip().lower()
        if choice in ("q", ""):
            return
        # Parse format "aN" / "lN" / "rN"
        if len(choice) < 2 or choice[0] not in ("a", "l", "r"):
            console.print("  [yellow]⚠ Format invalide. Utilise aN / lN / rN / q.[/yellow]")
            continue
        try:
            idx = int(choice[1:])
        except ValueError:
            console.print("  [yellow]⚠ Numero invalide.[/yellow]")
            continue
        if not (1 <= idx <= len(candidates)):
            console.print(f"  [yellow]⚠ Numero hors plage (1-{len(candidates)}).[/yellow]")
            continue
        actor = candidates[idx - 1]
        name  = actor.get("name", "")
        if choice[0] == "a":
            if name not in targets["companies"]:
                targets["companies"].append(name)
                targets["companies"].sort(key=str.lower)
                console.print(f"  [green]✓ '{name}' ajoute a companies.[/green]")
            _remove_actor_from_disk(name)
            candidates.pop(idx - 1)
        elif choice[0] == "l":
            if name not in targets["research_orgs"]:
                targets["research_orgs"].append(name)
                targets["research_orgs"].sort(key=str.lower)
                console.print(f"  [blue]✓ '{name}' ajoute a research_orgs.[/blue]")
            _remove_actor_from_disk(name)
            candidates.pop(idx - 1)
        else:  # 'r' = rejet
            _remove_actor_from_disk(name)
            candidates.pop(idx - 1)
            console.print(f"  [red]✓ '{name}' rejete (retire des candidats).[/red]")
        if not candidates:
            console.print("  [green]✓ Tous les candidats ont ete traites.[/green]\n")
            return


def _remove_actor_from_disk(name: str) -> None:
    """Retire un acteur du fichier discovered_actors.json (apres accept ou reject)."""
    if not os.path.exists(_DISCOVERED_ACTORS_PATH):
        return
    try:
        with open(_DISCOVERED_ACTORS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        actors = data.get("actors", {})
        norm = name.strip().lower()
        to_delete = [k for k, v in actors.items() if v.get("name", "").lower() == norm]
        for k in to_delete:
            del actors[k]
        atomic_write_json(_DISCOVERED_ACTORS_PATH, data)
    except (OSError, json.JSONDecodeError):
        pass


# =============================================================================
# Stats requetes : action 12 du menu d'edition (consultation interactive)
# =============================================================================

_QUERY_STATS_PATH = os.path.join(DATA_DIR, "query_stats.json")


def _show_query_stats_panel(console, Table, Panel, Prompt) -> None:
    """Action 12 : revue des stats cumulatives des requetes par source.

    Affiche un menu interactif avec 3 vues :
      - Top productives (hits_total desc) : requetes a garder absolument
      - Top steriles (consecutive_zeros desc) : requetes candidates a retirer/reformuler
      - Par source : groupe par source, classement par hits_total
    """
    if not os.path.exists(_QUERY_STATS_PATH):
        console.print(Panel(
            "[dim]Aucune statistique disponible.\n\nLance d'abord un run complet (python main.py).\n"
            "Le pipeline collectera les stats automatiquement et les persistera ici.[/dim]",
            title="📊 Statistiques des requetes (vide)", border_style="dim",
        ))
        console.print()
        return

    try:
        with open(_QUERY_STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        queries = list(data.get("queries", {}).values())
    except (OSError, json.JSONDecodeError) as e:
        console.print(f"[red]✘ Erreur lecture {_QUERY_STATS_PATH} : {e}[/red]\n")
        return

    if not queries:
        console.print(Panel(
            "[dim]Le fichier de stats existe mais est vide. Lance un run pour le peupler.[/dim]",
            title="📊 Statistiques des requetes (vide)", border_style="dim",
        ))
        console.print()
        return

    last_updated = (data.get("last_updated") or "")[:10]
    total_q = len(queries)
    sterile_4plus = sum(1 for q in queries if q.get("consecutive_zeros", 0) >= 4)

    while True:
        console.print(Panel(
            f"  [dim]Total : {total_q} requetes uniques tracées · Derniere MAJ : {last_updated} · "
            f"[bold red]{sterile_4plus}[/bold red] requetes avec [bold]4+ runs consecutifs a 0[/bold] (candidates a reformuler)[/dim]\n\n"
            "  [bold green]p[/bold green]  Top 30 requetes les + [bold green]productives[/bold green] (hits cumules desc)\n"
            "  [bold red]s[/bold red]  Top 30 requetes les + [bold red]steriles[/bold red] (cons. zeros desc)\n"
            "  [bold cyan]a[/bold cyan]  Vue [bold]par source[/bold] (top 20 par source, classement hits)\n"
            "  [bold yellow]q[/bold yellow]  Retour au menu principal",
            title="📊 Statistiques cumulatives des requetes",
            border_style="cyan",
        ))
        choice = Prompt.ask(
            "  [bold]Quelle vue ?[/bold] [dim](Entree = [bold yellow]q[/bold yellow] retour)[/dim]",
            choices=["p", "s", "a", "q"],
            default="q", show_default=False, show_choices=False,
        ).lower()

        if choice == "q":
            return

        if choice == "p":
            sorted_q = sorted(queries, key=lambda x: x.get("hits_total", 0), reverse=True)[:30]
            t = Table(
                title=f"Top 30 requetes les + productives (sur {total_q} totales)",
                border_style="green",
            )
            t.add_column("Requete", style="bold cyan", overflow="fold", max_width=60)
            t.add_column("Source", style="yellow")
            t.add_column("Hits cumules", justify="right", style="bold green")
            t.add_column("Last run", justify="right")
            t.add_column("Runs", justify="right", style="dim")
            for q in sorted_q:
                t.add_row(
                    q.get("query", "?"),
                    q.get("source", "?"),
                    str(q.get("hits_total", 0)),
                    str(q.get("hits_last_run", 0)),
                    str(q.get("runs_total", 0)),
                )
            console.print(t)
        elif choice == "s":
            sorted_q = sorted(queries, key=lambda x: x.get("consecutive_zeros", 0), reverse=True)[:30]
            t = Table(
                title=f"Top 30 requetes les + steriles (consecutive_zeros desc)",
                border_style="red",
            )
            t.add_column("Requete", style="cyan", overflow="fold", max_width=60)
            t.add_column("Source", style="yellow")
            t.add_column("Cons. 0", justify="right", style="bold red")
            t.add_column("Hits total", justify="right", style="dim")
            t.add_column("Runs", justify="right", style="dim")
            t.add_column("Last hit", style="dim")
            for q in sorted_q:
                last_hit = (q.get("last_hit_run") or "")[:10] or "jamais"
                t.add_row(
                    q.get("query", "?"),
                    q.get("source", "?"),
                    str(q.get("consecutive_zeros", 0)),
                    str(q.get("hits_total", 0)),
                    str(q.get("runs_total", 0)),
                    last_hit,
                )
            console.print(t)
            console.print(
                "  [dim]💡 Les requetes avec [bold red]Cons. 0[/bold red] >= 4 sont candidates "
                "a retrait ou reformulation.[/dim]\n"
                "  [dim]Pour les retirer, utilise les actions 1-10 du menu principal "
                "selon la liste source (entreprise/keyword/solo/labo/cross-domaine).[/dim]\n"
            )
        elif choice == "a":
            by_source: dict = {}
            for q in queries:
                by_source.setdefault(q.get("source", "?"), []).append(q)
            for src, qs in sorted(by_source.items()):
                qs.sort(key=lambda x: x.get("hits_total", 0), reverse=True)
                shown = qs[:20]
                t = Table(
                    title=f"Source : [bold]{src}[/bold] ({len(qs)} requetes uniques)",
                    border_style="cyan",
                )
                t.add_column("Requete", style="bold cyan", overflow="fold", max_width=60)
                t.add_column("Hits cumules", justify="right", style="green")
                t.add_column("Last run", justify="right")
                t.add_column("Cons. 0", justify="right", style="red")
                for q in shown:
                    t.add_row(
                        q.get("query", "?"),
                        str(q.get("hits_total", 0)),
                        str(q.get("hits_last_run", 0)),
                        str(q.get("consecutive_zeros", 0)),
                    )
                console.print(t)
                if len(qs) > 20:
                    console.print(f"  [dim]({len(qs) - 20} requetes masquees pour {src})[/dim]\n")

        console.print()


def _edit_targets_menu(console, Table, Panel, Prompt, IntPrompt, Confirm) -> None:
    """Menu d'edition des cibles : ajouter/supprimer entreprises et mots-cles.

    Apres chaque modification, affiche la liste in-memory mise a jour (avec
    indicateur 'non encore sauvegarde') pour que l'utilisateur voie immediatement
    ce qu'il vient de taper et puisse corriger une faute de frappe.

    Option 0 (Annuler) revient a l'etat sauvegarde sur disque.
    Option 7 (Quitter sans sauvegarder) quitte le menu sans toucher au fichier.
    Option 6 (Sauvegarder et continuer) ecrit data/targets.json.
    """
    targets_path = os.path.join(DATA_DIR, "targets.json")
    with open(targets_path, encoding="utf-8") as f:
        targets_disk = json.load(f)
    # Copie de travail in-memory (modifs uniquement persistees sur action 13).
    # Tri alphabetique : ainsi l'index affiche correspond toujours a l'index
    # interne, et les suppressions par numero ciblent le bon item.
    targets = {
        "companies":           sorted(targets_disk.get("companies", []), key=str.lower),
        "keywords":            sorted(targets_disk.get("keywords", []), key=str.lower),
        "solo_keywords":       sorted(targets_disk.get("solo_keywords", []), key=str.lower),
        "research_orgs":       sorted(targets_disk.get("research_orgs", []), key=str.lower),
        "cross_domain_topics": sorted(targets_disk.get("cross_domain_topics", []), key=str.lower),
    }

    while True:
        # Compteur d'acteurs decouverts pour afficher dans le menu
        nb_discovered = _count_discovered_actors(targets)

        console.print(Panel(
            "  [bold green]1[/bold green]   ➕  Ajouter une entreprise [dim](couple avec keywords sur GNews)[/dim]\n"
            "  [bold yellow]2[/bold yellow]   ➖  Supprimer une entreprise\n"
            "  [bold green]3[/bold green]   ➕  Ajouter un mot-cle COUPLE [dim](GNews × entreprises + broadcast science)[/dim]\n"
            "  [bold yellow]4[/bold yellow]   ➖  Supprimer un mot-cle couple\n"
            "  [bold magenta]5[/bold magenta]   ➕  Ajouter un mot-cle SOLO [dim](broadcast partout, sans entreprise)[/dim]\n"
            "  [bold yellow]6[/bold yellow]   ➖  Supprimer un mot-cle SOLO\n"
            "  [bold blue]7[/bold blue]   ➕  Ajouter un labo / organisme de recherche [dim](broadcast science)[/dim]\n"
            "  [bold yellow]8[/bold yellow]   ➖  Supprimer un labo / organisme de recherche\n"
            "  [bold bright_magenta]9[/bold bright_magenta]   ➕  Ajouter un theme CROSS-DOMAINE "
            "[dim](photonique, MEMS, nanotech, biomim... transferable a PVD/ALD)[/dim]\n"
            "  [bold yellow]10[/bold yellow]  ➖  Supprimer un theme cross-domaine\n"
            f"  [bold bright_cyan]11[/bold bright_cyan]  🔍  Revoir les acteurs DECOUVERTS automatiquement "
            f"[dim](nouveaux deposants/labos reperes dans les resultats : "
            f"[bold]{nb_discovered}[/bold] candidats)[/dim]\n"
            "  [bold bright_cyan]12[/bold bright_cyan]  📊  Voir les STATS des requetes "
            "[dim](top productives, top steriles, par source — pour optimiser tes listes)[/dim]\n"
            "  [bold cyan]13[/bold cyan]  📋  Revoir la liste actuelle (in-memory)\n"
            "  [bold green]14[/bold green]  ✅  [green]Sauvegarder et continuer[/green]\n"
            "  [bold yellow]15[/bold yellow]  ↩  [yellow]Quitter sans sauvegarder[/yellow] (annule tout)",
            title="✏️  Editer les cibles",
            border_style="yellow",
        ))
        action = Prompt.ask(
            "  [bold]Que veux-tu faire ?[/bold] "
            "[dim](tape 1-15, Entree = [bold green]14[/bold green] sauvegarder)[/dim]",
            choices=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
            default="14",
            show_default=False,
        )

        if action == "1":
            new = Prompt.ask("  [bold]Nom EXACT de l'entreprise a ajouter[/bold] "
                             "[dim](respecte la casse)[/dim]").strip()
            if new and new not in targets["companies"]:
                targets["companies"].append(new)
                targets["companies"].sort(key=str.lower)
                console.print(f"\n  [green]✓ Ajoute : '{new}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous (verifie l'orthographe) :[/dim]")
                _print_mini_list(console, Table, "🏢  Entreprises", targets["companies"], "cyan")
            elif new in targets["companies"]:
                console.print(f"  [yellow]⚠ '{new}' est deja dans la liste.[/yellow]")
            else:
                console.print("  [yellow]⚠ Saisie vide, rien ajoute.[/yellow]")
        elif action == "2":
            if not targets["companies"]:
                console.print("  [yellow]Aucune entreprise a supprimer.[/yellow]")
                continue
            _print_mini_list(console, Table, "🏢  Entreprises", targets["companies"], "cyan")
            idx = IntPrompt.ask("  [bold]Numero de l'entreprise a supprimer[/bold] "
                                "[dim](Entree = [bold cyan]0[/bold cyan] annuler)[/dim]",
                                default=0, show_default=False)
            if 1 <= idx <= len(targets["companies"]):
                removed = targets["companies"].pop(idx - 1)
                console.print(f"\n  [green]✓ Supprime : '{removed}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous :[/dim]")
                _print_mini_list(console, Table, "🏢  Entreprises", targets["companies"], "cyan")
        elif action == "3":
            new = Prompt.ask("  [bold]Nouveau mot-cle COUPLE (sera associe a chaque entreprise)[/bold] "
                             "[dim](ex: plasma deposition)[/dim]").strip()
            if new and new not in targets["keywords"]:
                targets["keywords"].append(new)
                targets["keywords"].sort(key=str.lower)
                console.print(f"\n  [green]✓ Ajoute : '{new}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous (verifie l'orthographe) :[/dim]")
                _print_mini_list(console, Table, "🔑  Mots-cles couples", targets["keywords"], "green")
            elif new in targets["keywords"]:
                console.print(f"  [yellow]⚠ '{new}' est deja dans la liste.[/yellow]")
            else:
                console.print("  [yellow]⚠ Saisie vide, rien ajoute.[/yellow]")
        elif action == "4":
            if not targets["keywords"]:
                console.print("  [yellow]Aucun mot-cle a supprimer.[/yellow]")
                continue
            _print_mini_list(console, Table, "🔑  Mots-cles couples", targets["keywords"], "green")
            idx = IntPrompt.ask("  [bold]Numero du mot-cle couple a supprimer[/bold] "
                                "[dim](Entree = [bold cyan]0[/bold cyan] annuler)[/dim]",
                                default=0, show_default=False)
            if 1 <= idx <= len(targets["keywords"]):
                removed = targets["keywords"].pop(idx - 1)
                console.print(f"\n  [green]✓ Supprime : '{removed}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous :[/dim]")
                _print_mini_list(console, Table, "🔑  Mots-cles couples", targets["keywords"], "green")
        elif action == "5":
            console.print(
                "  [bold magenta]ℹ Mot-cle SOLO[/bold magenta] : phrase cherchee SEULE, "
                "sans nom d'entreprise. Utile pour des thematiques tres specifiques\n"
                "  qui n'apparaissent jamais avec un nom de societe.\n"
                "  [yellow]⚠ A reserver aux phrases multi-mots de 4+ mots[/yellow] "
                "(ex: 'Physical vapor deposition coating process').\n"
                "  Un mot court (ex: 'PVD') ramenerait des milliers de resultats generiques.\n"
            )
            new = Prompt.ask("  [bold]Phrase SOLO a ajouter[/bold] "
                             "[dim](sera cherchee telle quelle, sans entreprise)[/dim]").strip()
            if new and new not in targets["solo_keywords"]:
                if len(new.split()) < 3:
                    if not Confirm.ask(
                        f"  [yellow]⚠ '{new}' a moins de 3 mots — risque de tres nombreux "
                        "resultats generiques. Confirmer l'ajout ?[/yellow]",
                        default=False,
                    ):
                        console.print("  [dim]↩ Annule, rien ajoute.[/dim]")
                        continue
                targets["solo_keywords"].append(new)
                targets["solo_keywords"].sort(key=str.lower)
                console.print(f"\n  [green]✓ Solo ajoute : '{new}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous (verifie l'orthographe) :[/dim]")
                _print_mini_list(console, Table, "🎯  Mots-cles SOLO", targets["solo_keywords"], "magenta")
            elif new in targets["solo_keywords"]:
                console.print(f"  [yellow]⚠ '{new}' est deja dans la liste solo.[/yellow]")
            else:
                console.print("  [yellow]⚠ Saisie vide, rien ajoute.[/yellow]")
        elif action == "6":
            if not targets["solo_keywords"]:
                console.print("  [yellow]Aucun mot-cle solo a supprimer (liste vide).[/yellow]")
                continue
            _print_mini_list(console, Table, "🎯  Mots-cles SOLO", targets["solo_keywords"], "magenta")
            idx = IntPrompt.ask("  [bold]Numero du mot-cle SOLO a supprimer[/bold] "
                                "[dim](Entree = [bold cyan]0[/bold cyan] annuler)[/dim]",
                                default=0, show_default=False)
            if 1 <= idx <= len(targets["solo_keywords"]):
                removed = targets["solo_keywords"].pop(idx - 1)
                console.print(f"\n  [green]✓ Solo supprime : '{removed}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous :[/dim]")
                _print_mini_list(console, Table, "🎯  Mots-cles SOLO", targets["solo_keywords"], "magenta")
        elif action == "7":
            console.print(
                "  [bold blue]ℹ Organisme de recherche[/bold blue] : labo, universite, "
                "institut public ou prive QUI PUBLIE des papers / depose des brevets.\n"
                "  Exemples : CEA-Leti, CNRS, EPFL, Fraunhofer IST, MIT, NIMS...\n"
                "  Le nom sera cherche [bold]UNIQUEMENT dans les sources scientifiques[/bold] "
                "(arXiv, OpenAlex, Crossref, HAL, Semantic Scholar, Tavily, Patents).\n"
                "  [yellow]Pas de recherche dans Google News[/yellow] : ces organismes "
                "publient peu de communiques de presse.\n"
            )
            new = Prompt.ask("  [bold]Nom EXACT du labo / organisme a ajouter[/bold] "
                             "[dim](respecte la casse)[/dim]").strip()
            if new and new not in targets["research_orgs"]:
                targets["research_orgs"].append(new)
                targets["research_orgs"].sort(key=str.lower)
                console.print(f"\n  [green]✓ Labo ajoute : '{new}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous (verifie l'orthographe) :[/dim]")
                _print_mini_list(console, Table, "🎓  Organismes de recherche", targets["research_orgs"], "blue")
            elif new in targets["research_orgs"]:
                console.print(f"  [yellow]⚠ '{new}' est deja dans la liste.[/yellow]")
            else:
                console.print("  [yellow]⚠ Saisie vide, rien ajoute.[/yellow]")
        elif action == "8":
            if not targets["research_orgs"]:
                console.print("  [yellow]Aucun labo / organisme a supprimer (liste vide).[/yellow]")
                continue
            _print_mini_list(console, Table, "🎓  Organismes de recherche", targets["research_orgs"], "blue")
            idx = IntPrompt.ask("  [bold]Numero du labo / organisme a supprimer[/bold] "
                                "[dim](Entree = [bold cyan]0[/bold cyan] annuler)[/dim]",
                                default=0, show_default=False)
            if 1 <= idx <= len(targets["research_orgs"]):
                removed = targets["research_orgs"].pop(idx - 1)
                console.print(f"\n  [green]✓ Labo supprime : '{removed}'[/green]")
                console.print("  [dim]Liste a jour ci-dessous :[/dim]")
                _print_mini_list(console, Table, "🎓  Organismes de recherche", targets["research_orgs"], "blue")
        elif action == "9":
            console.print(
                "  [bold bright_magenta]ℹ Theme CROSS-DOMAINE[/bold bright_magenta] : phrase decrivant un\n"
                "  domaine technologique transversal (photonique, MEMS, nanotech, biomim, IA, decoratif, etc.)\n"
                "  qui pourrait etre INTEGRE a tes procedes PVD/ALD pour creer une innovation.\n"
                "  [yellow]Le scoring IA evalue specifiquement le potentiel d'integration.[/yellow]\n"
                "  Exemples : 'metasurfaces optical thin film', 'lotus effect superhydrophobic coating',\n"
                "  'machine learning thin film optimization', 'structural color watch dial coating'.\n"
                "  [dim]Cherche UNIQUEMENT dans les sources scientifiques (pas GNews).[/dim]\n"
            )
            new = Prompt.ask("  [bold]Theme cross-domaine a ajouter[/bold] "
                             "[dim](phrase descriptive 3+ mots)[/dim]").strip()
            if new and new not in targets["cross_domain_topics"]:
                if len(new.split()) < 2:
                    if not Confirm.ask(
                        f"  [yellow]⚠ '{new}' a moins de 2 mots — risque de bruit eleve. "
                        "Confirmer l'ajout ?[/yellow]",
                        default=False,
                    ):
                        console.print("  [dim]↩ Annule, rien ajoute.[/dim]")
                        continue
                targets["cross_domain_topics"].append(new)
                targets["cross_domain_topics"].sort(key=str.lower)
                console.print(f"\n  [green]✓ Theme ajoute : '{new}'[/green]")
                _print_mini_list(console, Table, "🌐  Themes cross-domaine",
                                 targets["cross_domain_topics"], "bright_magenta")
            elif new in targets["cross_domain_topics"]:
                console.print(f"  [yellow]⚠ '{new}' est deja dans la liste.[/yellow]")
            else:
                console.print("  [yellow]⚠ Saisie vide, rien ajoute.[/yellow]")
        elif action == "10":
            if not targets["cross_domain_topics"]:
                console.print("  [yellow]Aucun theme cross-domaine a supprimer (liste vide).[/yellow]")
                continue
            _print_mini_list(console, Table, "🌐  Themes cross-domaine",
                             targets["cross_domain_topics"], "bright_magenta")
            idx = IntPrompt.ask("  [bold]Numero du theme cross-domaine a supprimer[/bold] "
                                "[dim](Entree = [bold cyan]0[/bold cyan] annuler)[/dim]",
                                default=0, show_default=False)
            if 1 <= idx <= len(targets["cross_domain_topics"]):
                removed = targets["cross_domain_topics"].pop(idx - 1)
                console.print(f"\n  [green]✓ Theme supprime : '{removed}'[/green]")
                _print_mini_list(console, Table, "🌐  Themes cross-domaine",
                                 targets["cross_domain_topics"], "bright_magenta")
        elif action == "11":
            # Revue des acteurs decouverts automatiquement par le scraping.
            # L'utilisateur peut accepter (ajout a companies ou research_orgs)
            # ou rejeter (suppression de la liste de candidats).
            _review_discovered_actors(console, Table, Panel, Prompt, IntPrompt, targets)
        elif action == "12":
            # Stats des requetes : top productives, top steriles, par source.
            # Aide a reperer les requetes a reformuler ou retirer manuellement.
            _show_query_stats_panel(console, Table, Panel, Prompt)
        elif action == "13":
            # Affiche la liste IN-MEMORY (avec indicateur 'non sauvegarde')
            _show_targets(console, Table, Panel, targets_dict=targets)
            continue
        elif action == "14":
            targets["companies"].sort(key=str.lower)
            targets["keywords"].sort(key=str.lower)
            targets["solo_keywords"].sort(key=str.lower)
            targets["research_orgs"].sort(key=str.lower)
            targets["cross_domain_topics"].sort(key=str.lower)
            atomic_write_json(targets_path, targets)
            console.print(Panel(
                f"[green]✅ Cibles sauvegardees dans {targets_path}[/green]\n"
                f"   {len(targets['companies'])} entreprise(s), "
                f"{len(targets['keywords'])} mot(s)-cle(s) couple(s), "
                f"{len(targets['solo_keywords'])} solo, "
                f"{len(targets['research_orgs'])} labo(s), "
                f"{len(targets['cross_domain_topics'])} theme(s) cross-domaine\n\n"
                f"[dim]Ces modifications sont desormais permanentes : elles seront\n"
                f"utilisees par defaut a chaque prochain lancement du programme.[/dim]",
                border_style="green",
            ))
            console.print()
            # Re-import live : patch config (source) et scraper (qui a copie les noms).
            try:
                import src.config as _cfg
                _cfg.TARGET_COMPANIES = list(targets["companies"])
                _cfg.KEYWORDS = list(targets["keywords"])
                _cfg.SOLO_KEYWORDS = list(targets["solo_keywords"])
                _cfg.RESEARCH_ORGS = list(targets["research_orgs"])
                _cfg.CROSS_DOMAIN_TOPICS = list(targets["cross_domain_topics"])
                import src.scraper as _scraper_mod
                _scraper_mod.TARGET_COMPANIES = list(targets["companies"])
                _scraper_mod.KEYWORDS = list(targets["keywords"])
                _scraper_mod.SOLO_KEYWORDS = list(targets["solo_keywords"])
                _scraper_mod.RESEARCH_ORGS = list(targets["research_orgs"])
                _scraper_mod.CROSS_DOMAIN_TOPICS = list(targets["cross_domain_topics"])
            except Exception:
                pass
            return
        elif action == "15":
            # Annulation : on n'ecrit rien, l'etat sur disque reste celui d'origine
            modified_companies = targets["companies"] != targets_disk.get("companies", [])
            modified_keywords  = targets["keywords"]  != targets_disk.get("keywords", [])
            modified_solo      = targets["solo_keywords"] != targets_disk.get("solo_keywords", [])
            modified_orgs      = targets["research_orgs"] != targets_disk.get("research_orgs", [])
            modified_cross     = targets["cross_domain_topics"] != targets_disk.get("cross_domain_topics", [])
            if modified_companies or modified_keywords or modified_solo or modified_orgs or modified_cross:
                if not Confirm.ask(
                    "\n  [yellow]Tu as fait des modifications NON sauvegardees. "
                    "Vraiment tout annuler et tout perdre ?[/yellow]\n"
                    "  [dim]Tape [bold]y[/bold] (yes = oui, perdre les modifs), "
                    "ou [bold]n[/bold] (non, retourner au menu pour les sauvegarder).\n"
                    "  Entree seul = [bold]n (non)[/bold] par defaut.[/dim]",
                    default=False,
                ):
                    continue
            console.print("[yellow]↩ Annulation : aucune modification sauvegardee. "
                          "Le fichier targets.json reste inchange.[/yellow]\n")
            return


def _choose_volume(console, Table, Panel, Prompt, IntPrompt) -> int | None:
    """Affiche les 5 presets de volume RSS et retourne le choix utilisateur.

    Les durees sont calculees dynamiquement a partir des cibles courantes
    (entreprises × mots-cles + solo) pour refleter la duree REELLE du run.

    Retourne None si l'utilisateur veut revenir a l'etape precedente (touche r/p).
    """
    # Lire les cibles pour calculer le nombre de requetes GNews
    targets_path = os.path.join(DATA_DIR, "targets.json")
    nb_q = 0
    try:
        with open(targets_path, encoding="utf-8") as f:
            t = json.load(f)
        nb_q = (
            len(t.get("companies", [])) * len(t.get("keywords", []))
            + len(t.get("solo_keywords", []))
        )
    except (OSError, json.JSONDecodeError):
        pass

    presets_def = [
        ("1", "🚀  Test rapide",        25,  "Pour valider que tout fonctionne avant un vrai run."),
        ("2", "📰  Standard hebdo",     50,  "Usage normal hebdomadaire. Suffisant si tu lances chaque semaine."),
        ("3", "📚  Approfondi",        100,  "Plus de couverture. Bon compromis si tu lances tous les 15 jours."),
        ("4", "🏆  Marathon weekend",  200,  "Couverture maximale. Recommande si tu lances 1x/mois ou apres une longue pause."),
        ("5", "✏️   Personnalise",       0,  "Tu choisis le nombre toi-meme."),
    ]

    # Pre-calcul GNews (incompressible, identique pour tous les presets)
    gnews_h, _ = _estimate_run_duration_h(nb_per_source=50, nb_q=nb_q)
    gnews_label = _format_duration(gnews_h) if nb_q > 0 else "0 (aucune cible)"

    info_panel = (
        f"  📊 [bold]Tes cibles actuelles[/bold] : "
        f"[cyan]{nb_q}[/cyan] requetes Google News a faire\n"
        f"  ⏱  [bold]GNews seul[/bold] (incompressible, identique pour tous les presets) : "
        f"[bold yellow]{gnews_label}[/bold yellow]\n"
        f"  💡 [dim]Le preset RSS ci-dessous ne change que de quelques minutes le total — "
        f"le facteur dominant est le nombre de cibles.[/dim]"
    )
    console.print(Panel(info_panel, border_style="yellow", padding=(0, 2)))

    table = Table(title="📦  Combien d'articles veux-tu collecter par source RSS ?",
                  border_style="cyan", show_lines=True)
    table.add_column("Numero", style="bold cyan", justify="center")
    table.add_column("Preset", style="bold")
    table.add_column("Articles / source", justify="right", style="green")
    table.add_column("Duree TOTALE estimee", justify="center", style="yellow")
    table.add_column("Recommande pour")
    for choice, name, nb, desc in presets_def:
        if nb == 0:
            nb_label = "tu choisis"
            dur_label = "depend du choix"
        else:
            nb_label = str(nb)
            g_h, a_h = _estimate_run_duration_h(nb_per_source=nb, nb_q=nb_q)
            dur_label = _format_duration(g_h + a_h)
        table.add_row(choice, name, nb_label, dur_label, desc)
    console.print(table)

    console.print(
        "\n  [dim]💡 Le nombre par source s'applique uniquement aux flux RSS "
        "(ArXiv, MDPI, IEEE, ScienceDaily). Les autres sources (arXiv search, OpenAlex, "
        "Crossref, HAL, Tavily, Semantic Scholar, Google News) ont leurs volumes propres "
        "deja parametres.[/dim]\n"
    )
    console.print(
        "  [bold]Tape le numero du preset que tu veux choisir[/bold] "
        "[dim](1, 2, 3, 4 ou 5) puis Entree.[/dim]\n"
        "  [dim]Si tu appuies juste sur Entree sans rien taper, le preset "
        "[bold cyan]2 (Standard hebdo)[/bold cyan] est selectionne.[/dim]\n"
        "  [dim magenta]💡 Tape [bold]r[/bold] pour revenir a l'etape precedente "
        "(modifier les cibles).[/dim magenta]"
    )
    chosen = Prompt.ask(
        "  [bold]Ton choix[/bold] "
        "[dim](Entree = [bold green]2[/bold green] Standard hebdo)[/dim]",
        choices=["1", "2", "3", "4", "5", "r", "p"],
        default="2", show_choices=False, show_default=False,
    )
    if chosen in ("r", "p"):
        return None
    if chosen == "5":
        nb = IntPrompt.ask(
            "  [bold]Nombre d'articles par source[/bold] "
            "[dim](entre 5 et 1000, Entree = [bold green]50[/bold green])[/dim]",
            default=50, show_default=False,
        )
        nb = max(5, min(1000, nb))
    else:
        nb = next(p[2] for p in presets_def if p[0] == chosen)
        preset_name = next(p[1] for p in presets_def if p[0] == chosen)
        console.print(f"\n  [green]✓ Selectionne : {preset_name} ({nb} articles/source)[/green]\n")
    return nb


def _show_recap_and_confirm(console, Panel, Confirm, nb_articles: int) -> bool:
    """Affiche un recap final et demande confirmation. Retourne True pour lancer."""
    targets_path = os.path.join(DATA_DIR, "targets.json")
    with open(targets_path, encoding="utf-8") as f:
        targets = json.load(f)
    nb_companies = len(targets.get("companies", []))
    nb_keywords  = len(targets.get("keywords", []))
    nb_solo      = len(targets.get("solo_keywords", []))
    nb_orgs      = len(targets.get("research_orgs", []))
    nb_cross     = len(targets.get("cross_domain_topics", []))
    nb_q = nb_companies * nb_keywords + nb_solo

    mem_label = _memory_choice_label or "mode par defaut (config.py)"
    summary = (
        f"  [bold]🏢  Entreprises surveillees :[/bold] {nb_companies}\n"
        f"  [bold]🔑  Mots-cles couples :[/bold] {nb_keywords}\n"
        f"  [bold]🎯  Mots-cles SOLO :[/bold] [magenta]{nb_solo}[/magenta]"
        f"{' [dim](aucun)[/dim]' if nb_solo == 0 else ''}\n"
        f"  [bold]🎓  Organismes de recherche :[/bold] [blue]{nb_orgs}[/blue]"
        f"{' [dim](aucun)[/dim]' if nb_orgs == 0 else ' [dim](broadcast science uniquement)[/dim]'}\n"
        f"  [bold]🌐  Themes cross-domaine :[/bold] [bright_magenta]{nb_cross}[/bright_magenta]"
        f"{' [dim](aucun — decouverte cross-domaine desactivee)[/dim]' if nb_cross == 0 else ' [dim](potentiel innovation transversale)[/dim]'}\n"
        f"  [bold]📦  Articles par source RSS :[/bold] [cyan]{nb_articles}[/cyan]\n"
        f"  [bold]🔍  Requetes Google News :[/bold] [cyan]{nb_q}[/cyan] "
        f"[dim]({nb_companies}×{nb_keywords} + {nb_solo} solo)[/dim]\n"
        f"  [bold]🧠  Memoire :[/bold] [cyan]{mem_label}[/cyan]\n"
        f"\n  [dim]Si tout est correct, le programme va demarrer le pipeline complet "
        f"(RSS + arXiv + OpenAlex + Crossref + HAL + Semantic Scholar + Tavily + Google Patents + Google News + IA + email).[/dim]"
    )
    console.print(Panel(summary, title="📋  Recapitulatif final",
                        border_style="green", padding=(1, 2)))
    return Confirm.ask(
        "\n  [bold]✅ Tout est correct ? Lancer le pipeline maintenant ?[/bold]\n"
        "  [dim]Tape [bold]y[/bold] (yes = oui, demarrer le pipeline), "
        "ou [bold]n[/bold] (non, retourner au menu pour corriger).\n"
        "  Si tu appuies juste sur [bold]Entree[/bold] sans rien taper, ce sera "
        "[bold]y (oui)[/bold] par defaut (lancement immediat).[/dim]",
        default=True,
    )


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    # Pre-run interactif (banner + check heure + memoire + cibles + volume)
    chosen_max = _interactive_pre_run()
    if chosen_max is not None:
        # Override dynamique de la constante MAX_ARTICLES_PER_SOURCE dans scraper
        # (importee depuis config — on patch le module scraper pour propager).
        import src.scraper as _scraper_mod
        _scraper_mod.MAX_ARTICLES_PER_SOURCE = chosen_max
        logger.info("📊 MAX_ARTICLES_PER_SOURCE configure a %d (choix utilisateur)", chosen_max)

        # Propage le choix memoire de l'utilisateur (set par _memory_choice_step)
        # via os.environ vers le module scraper. Patch direct car config.py a
        # deja ete lu au top-level import.
        env_use_memory = os.environ.get("USE_MEMORY", "false").lower() in ("true", "1", "yes")
        _scraper_mod.USE_MEMORY = env_use_memory
        logger.info(
            "🧠 USE_MEMORY=%s (choix utilisateur : %s)",
            env_use_memory,
            _memory_choice_label or "non specifie",
        )

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
        logger.info(
            "📡 Lancement du scraping (RSS + arXiv + OpenAlex + Crossref + HAL + "
            "Sem.Scholar + Tavily + Patents + GNews)..."
        )
        # include_web=True : si TAVILY_API_KEY est absente, le module fait un graceful skip
        # et le pipeline continue. Aucune raison de désactiver l'option ici.
        # include_patents=True (par défaut dans run_scraper) : Google Patents activé.
        scraper_result: dict[str, Any] = run_scraper(include_web=True)
        atomic_write_json(SCRAPER_OUTPUT_PATH, scraper_result)
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
                atomic_write_json(AI_FILTER_OUTPUT_PATH, ai_filtered_result)
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

        # Affichage rapport bande passante proxy (si proxy configure)
        try:
            from src.proxy_manager import get_proxy_manager
            _proxy_mgr = get_proxy_manager()
            _proxy_mgr.bandwidth_flush()
            if _proxy_mgr._pool:
                logger.info("\n" + _proxy_mgr.bandwidth_report())
        except Exception:
            pass

        logger.info("🎉 Orchestrateur terminé avec succès.")

    except Exception as e:
        # Toute exception non capturée plus haut (erreur fatale imprévue)
        logger.error("L'orchestrateur a rencontré une erreur fatale : %s", e, exc_info=True)
        send_error_email(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
