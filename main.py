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
    title.append("  Pipeline complet : RSS + arXiv + OpenAlex + Crossref + HAL + SS + Tavily + Google News + IA",
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
    companies = targets.get("companies", [])
    keywords = targets.get("keywords", [])
    solo_keywords = targets.get("solo_keywords", [])

    # Nombre total de requetes GNews = (entreprises × mots-cles couples) + solos
    nb_q = len(companies) * len(keywords) + len(solo_keywords)
    # Estimation duree GNews (~141s/req moyen + circadien 5h pour gros runs)
    if nb_q == 0:
        dur_est = "0 (aucune cible)"
    elif nb_q < 50:
        dur_est = f"~{nb_q * 0.04:.1f}h (court)"
    elif nb_q < 150:
        dur_est = f"~{nb_q * 0.045:.1f}h (modere)"
    elif nb_q < 300:
        dur_est = f"~{(nb_q * 0.05) + 5:.1f}h (long, avec pause nuit)"
    else:
        dur_est = f"~{(nb_q * 0.055) + 5:.1f}h (TRES long)"

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

    # Recommandations
    reco = (
        f"  📊 [bold]Total requetes Google News[/bold] = "
        f"({len(companies)} × {len(keywords)}) + {len(solo_keywords)} solo = "
        f"[bold cyan]{nb_q}[/bold cyan] requetes\n"
        f"  ⏱  [bold]Duree GNews estimee[/bold] : [yellow]{dur_est}[/yellow]\n\n"
        f"  💡 [bold]Recommandations[/bold] :\n"
        f"     • [green]5-10 entreprises × 5-7 mots-cles[/green]    = ~25-70 req     (1-3h, pour test)\n"
        f"     • [yellow]15 entreprises × 10 mots-cles[/yellow]        = ~150 req       (6-10h, hebdo classique)\n"
        f"     • [cyan]21 entreprises × 14 mots-cles[/cyan] (actuel) = ~294 req       (18-22h, weekend marathon)\n"
        f"     • [red]>30 entreprises × >15 mots-cles[/red]         = >450 req       (>30h, deconseille)\n\n"
        f"  💡 [bold]A propos des mots-cles SOLO[/bold] :\n"
        f"     Chaque solo ajoute 1 requete GNews mais aussi 1 requete sur arXiv,\n"
        f"     OpenAlex, Crossref, HAL, Semantic Scholar et Tavily. Reserve ce champ\n"
        f"     a des phrases TRES specifiques de 4+ mots (sinon trop de bruit)."
    )
    console.print(Panel(reco, title="📈  Volume actuel", border_style="cyan"))
    console.print()


def _print_mini_list(console, Table, label: str, items: list[str], color: str) -> None:
    """Affiche une mini-liste numerotee des items courants — utilisee apres chaque
    modification pour donner un retour visuel immediat de l'etat in-memory."""
    if not items:
        console.print(f"  [dim]({label} : liste vide)[/dim]")
        return
    t = Table(title=f"{label} ({len(items)})", border_style=color, show_header=False, expand=False)
    t.add_column("#", style="dim", justify="right", width=4)
    t.add_column("Valeur", style=color)
    for i, item in enumerate(items, 1):
        t.add_row(str(i), item)
    console.print(t)


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
    # Copie de travail in-memory (modifs uniquement persistees sur action 8)
    targets = {
        "companies":     list(targets_disk.get("companies", [])),
        "keywords":      list(targets_disk.get("keywords", [])),
        "solo_keywords": list(targets_disk.get("solo_keywords", [])),
    }

    while True:
        console.print(Panel(
            "  [bold green]1[/bold green]  ➕  Ajouter une entreprise\n"
            "  [bold yellow]2[/bold yellow]  ➖  Supprimer une entreprise\n"
            "  [bold green]3[/bold green]  ➕  Ajouter un mot-cle [dim](couple avec chaque entreprise)[/dim]\n"
            "  [bold yellow]4[/bold yellow]  ➖  Supprimer un mot-cle couple\n"
            "  [bold magenta]5[/bold magenta]  ➕  Ajouter un mot-cle SOLO [dim](cherche seul, sans entreprise)[/dim]\n"
            "  [bold yellow]6[/bold yellow]  ➖  Supprimer un mot-cle SOLO\n"
            "  [bold cyan]7[/bold cyan]  📋  Revoir la liste actuelle (in-memory)\n"
            "  [bold green]8[/bold green]  ✅  [green]Sauvegarder et continuer[/green]\n"
            "  [bold yellow]9[/bold yellow]  ↩  [yellow]Quitter sans sauvegarder[/yellow] (annule toutes les modifs)",
            title="✏️  Editer les cibles",
            border_style="yellow",
        ))
        action = Prompt.ask(
            "  [bold]Que veux-tu faire ?[/bold] "
            "[dim](tape 1-9, Entree = [bold green]8[/bold green] sauvegarder)[/dim]",
            choices=["1", "2", "3", "4", "5", "6", "7", "8", "9"],
            default="8",
            show_default=False,
        )

        if action == "1":
            new = Prompt.ask("  [bold]Nom EXACT de l'entreprise a ajouter[/bold] "
                             "[dim](respecte la casse)[/dim]").strip()
            if new and new not in targets["companies"]:
                targets["companies"].append(new)
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
            # Affiche la liste IN-MEMORY (avec indicateur 'non sauvegarde')
            _show_targets(console, Table, Panel, targets_dict=targets)
            continue
        elif action == "8":
            with open(targets_path, "w", encoding="utf-8") as f:
                json.dump(targets, f, ensure_ascii=False, indent=2)
            console.print(Panel(
                f"[green]✅ Cibles sauvegardees dans {targets_path}[/green]\n"
                f"   {len(targets['companies'])} entreprise(s), "
                f"{len(targets['keywords'])} mot(s)-cle(s) couple(s), "
                f"{len(targets['solo_keywords'])} solo\n\n"
                f"[dim]Ces modifications sont desormais permanentes : elles seront\n"
                f"utilisees par defaut a chaque prochain lancement du programme.[/dim]",
                border_style="green",
            ))
            console.print()
            # Re-import live : patch a la fois le module config (source) et le
            # module scraper (qui a copie les noms a son import top-level).
            # Sans patcher scraper, les modifs ne prendraient effet qu'au prochain
            # lancement du programme.
            try:
                import src.config as _cfg
                _cfg.TARGET_COMPANIES = list(targets["companies"])
                _cfg.KEYWORDS = list(targets["keywords"])
                _cfg.SOLO_KEYWORDS = list(targets["solo_keywords"])
                import src.scraper as _scraper_mod
                _scraper_mod.TARGET_COMPANIES = list(targets["companies"])
                _scraper_mod.KEYWORDS = list(targets["keywords"])
                _scraper_mod.SOLO_KEYWORDS = list(targets["solo_keywords"])
            except Exception:
                pass
            return
        elif action == "9":
            # Annulation : on n'ecrit rien, l'etat sur disque reste celui d'origine
            modified_companies = targets["companies"] != targets_disk.get("companies", [])
            modified_keywords  = targets["keywords"]  != targets_disk.get("keywords", [])
            modified_solo      = targets["solo_keywords"] != targets_disk.get("solo_keywords", [])
            if modified_companies or modified_keywords or modified_solo:
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

    Retourne None si l'utilisateur veut revenir a l'etape precedente (touche r/p).
    """
    presets = [
        ("1", "🚀  Test rapide",       25,  "~5 min", "Pour valider que tout fonctionne avant un vrai run."),
        ("2", "📰  Standard hebdo",    50,  "~3-4h",  "Usage normal hebdomadaire. Suffisant si tu lances chaque semaine."),
        ("3", "📚  Approfondi",       100,  "~8-10h", "Plus de couverture. Bon compromis si tu lances tous les 15 jours."),
        ("4", "🏆  Marathon weekend", 200,  "~18-22h", "Couverture maximale. Recommande si tu lances 1x/mois ou apres une longue pause."),
        ("5", "✏️   Personnalise",      0,  "?",      "Tu choisis le nombre toi-meme."),
    ]
    table = Table(title="📦  Combien d'articles veux-tu collecter par source RSS ?",
                  border_style="cyan", show_lines=True)
    table.add_column("Numero", style="bold cyan", justify="center")
    table.add_column("Preset", style="bold")
    table.add_column("Articles / source", justify="right", style="green")
    table.add_column("Duree estimee", justify="center", style="yellow")
    table.add_column("Recommande pour")
    for choice, name, nb, dur, desc in presets:
        nb_label = str(nb) if nb else "tu choisis"
        table.add_row(choice, name, nb_label, dur, desc)
    console.print(table)

    console.print(
        "\n  [dim]💡 Ce nombre s'applique aux flux RSS (ArXiv, MDPI, IEEE, ScienceDaily). "
        "Les autres sources (arXiv search, OpenAlex, Crossref, HAL, Tavily, Semantic Scholar, "
        "Google News) ont leurs volumes propres deja parametres.[/dim]\n"
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
        nb = next(p[2] for p in presets if p[0] == chosen)
        preset_name = next(p[1] for p in presets if p[0] == chosen)
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
    nb_q = nb_companies * nb_keywords + nb_solo

    mem_label = _memory_choice_label or "mode par defaut (config.py)"
    summary = (
        f"  [bold]🏢  Entreprises surveillees :[/bold] {nb_companies}\n"
        f"  [bold]🔑  Mots-cles couples :[/bold] {nb_keywords}\n"
        f"  [bold]🎯  Mots-cles SOLO :[/bold] [magenta]{nb_solo}[/magenta]"
        f"{' [dim](aucun — recherches solo desactivees)[/dim]' if nb_solo == 0 else ''}\n"
        f"  [bold]📦  Articles par source RSS :[/bold] [cyan]{nb_articles}[/cyan]\n"
        f"  [bold]🔍  Requetes Google News :[/bold] [cyan]{nb_q}[/cyan] "
        f"[dim]({nb_companies}×{nb_keywords} + {nb_solo} solo)[/dim]\n"
        f"  [bold]🧠  Memoire :[/bold] [cyan]{mem_label}[/cyan]\n"
        f"\n  [dim]Si tout est correct, le programme va demarrer le pipeline complet "
        f"(RSS + arXiv + OpenAlex + Crossref + HAL + Semantic Scholar + Tavily + Google News + IA + email).[/dim]"
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
