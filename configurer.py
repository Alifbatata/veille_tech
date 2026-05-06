"""
Assistant de configuration interactif — Veille Tech.

Guide l'utilisateur pour creer ou modifier le fichier .env :
- Demande chaque cle API une par une
- Explique ce que fait chaque cle et OU l'obtenir
- Propose des valeurs par defaut intelligentes
- Detecte les .env existants et propose de modifier ou repartir de zero
- Affichage stylise via la lib `rich` (compatible Windows nativement)

USAGE :
    python configurer.py            # mode interactif complet
    python configurer.py --check    # verifie juste que toutes les cles sont presentes
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.align import Align

console = Console()
ENV_PATH = Path(__file__).parent / ".env"


# =============================================================================
# Definition des champs de configuration
# =============================================================================
class Field:
    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        url: str = "",
        instructions: str = "",
        default: str = "",
        required: bool = True,
        secret: bool = False,
        validator=None,
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.url = url
        self.instructions = instructions
        self.default = default
        self.required = required
        self.secret = secret
        self.validator = validator


def _validate_gemini(value: str) -> str | None:
    if not value.startswith("AIza") or len(value) < 35:
        return "La cle Gemini commence par 'AIza' et fait ~39 caracteres."
    return None


def _validate_email(value: str) -> str | None:
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        return "Adresse email invalide."
    return None


def _validate_emails_list(value: str) -> str | None:
    for addr in value.split(","):
        if addr.strip() and _validate_email(addr.strip()):
            return f"Email invalide : {addr.strip()}"
    return None


def _validate_app_password(value: str) -> str | None:
    cleaned = value.replace(" ", "")
    if len(cleaned) != 16:
        return ("Le mot de passe d'application Gmail fait exactement 16 caracteres "
                "(les espaces sont optionnels).")
    return None


def _validate_tavily(value: str) -> str | None:
    if value and not value.startswith("tvly-"):
        return "La cle Tavily commence par 'tvly-'."
    return None


def _validate_int_range(min_val: int, max_val: int):
    def _v(value: str) -> str | None:
        try:
            n = int(value)
        except ValueError:
            return "Doit etre un nombre entier."
        if not min_val <= n <= max_val:
            return f"Doit etre entre {min_val} et {max_val}."
        return None
    return _v


FIELDS: list[Field] = [
    Field(
        key="GEMINI_API_KEY",
        label="🤖 Cle API Google Gemini",
        description=(
            "C'est l'IA qui va lire chaque article et lui donner une note de pertinence (1 a 5).\n"
            "Sans cette cle, le programme ne peut pas filtrer les articles."
        ),
        url="https://aistudio.google.com/app/apikey",
        instructions=(
            "1. Va sur Google AI Studio (lien ci-dessus)\n"
            "2. Connecte-toi avec ton compte Google\n"
            "3. Clique 'Create API key' -> 'Create API key in new project'\n"
            "4. Copie la cle qui commence par 'AIza...'\n"
            "5. C'est GRATUIT (250 requetes/jour avec gemini-2.5-flash)"
        ),
        secret=True,
        validator=_validate_gemini,
    ),
    Field(
        key="GEMINI_MODEL",
        label="🧠 Modele Gemini principal",
        description=(
            "Le modele de depart. Si son quota est epuise, le programme bascule\n"
            "automatiquement vers 15 autres modeles de secours (cascade dynamique)."
        ),
        default="gemini-2.5-flash",
        instructions="Recommande : gemini-2.5-flash (equilibre vitesse/qualite/quota gratuit)",
    ),
    Field(
        key="GMAIL_USER",
        label="📧 Adresse Gmail expediteur",
        description=(
            "L'adresse Gmail qui ENVOIE le digest hebdomadaire.\n"
            "Doit etre une adresse Gmail (pas Outlook ni Yahoo) car on utilise SMTP Gmail."
        ),
        instructions=(
            "Utilise ton adresse Gmail standard.\n"
            "Si tu n'as pas de compte Gmail, cree-en un sur https://accounts.google.com/signup"
        ),
        validator=_validate_email,
    ),
    Field(
        key="GMAIL_PASSWORD",
        label="🔐 Mot de passe d'application Gmail",
        description=(
            "Un mot de passe SPECIAL de 16 caracteres genere par Google pour les applications.\n"
            "Different du mot de passe normal de ton compte. Plus securise (revocable, ne donne\n"
            "pas acces au reste du compte)."
        ),
        url="https://myaccount.google.com/apppasswords",
        instructions=(
            "1. Active la double-authentification sur ton compte Google si pas deja fait :\n"
            "   https://myaccount.google.com/security\n"
            "2. Va sur https://myaccount.google.com/apppasswords\n"
            "3. Donne un nom (ex: 'Veille Tech')\n"
            "4. Google te donne un mot de passe de 16 caracteres (ex: abcd efgh ijkl mnop)\n"
            "5. Copie-le ici (avec ou sans espaces)"
        ),
        secret=True,
        validator=_validate_app_password,
    ),
    Field(
        key="MAIL_RECIPIENT",
        label="📬 Destinataires du digest",
        description=(
            "Liste des emails qui vont RECEVOIR le digest. Plusieurs adresses\n"
            "separees par des virgules. Par defaut on envoie a l'expediteur."
        ),
        default="",
        instructions=(
            "Exemples :\n"
            "  - Pour t'envoyer a toi-meme : laisse vide (utilisera l'adresse Gmail expediteur)\n"
            "  - Pour plusieurs : alice@x.com,bob@y.com,charlie@z.com"
        ),
        required=False,
        validator=_validate_emails_list,
    ),
    Field(
        key="TAVILY_API_KEY",
        label="🌐 Cle API Tavily (optionnel)",
        description=(
            "Service de recherche web specialise IA. Permet d'elargir la couverture\n"
            "au-dela des sources scientifiques (RSS, OpenAlex, etc.).\n"
            "OPTIONNEL : si tu ne mets rien, le programme saute simplement Tavily."
        ),
        url="https://app.tavily.com",
        instructions=(
            "1. Cree un compte gratuit sur https://app.tavily.com\n"
            "2. Va dans 'API Keys' -> copie la cle qui commence par 'tvly-'\n"
            "3. GRATUIT : 1000 requetes/mois (notre programme en utilise ~4 par run)"
        ),
        required=False,
        secret=True,
        validator=_validate_tavily,
    ),
    Field(
        key="SEMANTIC_SCHOLAR_API_KEY",
        label="🎓 Cle API Semantic Scholar (optionnel)",
        description=(
            "Service academique d'Allen Institute (200M+ papers scientifiques).\n"
            "SANS cette cle : la source est rate-limitee (l'IP se fait flagger).\n"
            "AVEC cette cle : 1 requete/sec garanti."
        ),
        url="https://www.semanticscholar.org/product/api",
        instructions=(
            "1. Demande une cle gratuite sur https://www.semanticscholar.org/product/api\n"
            "2. Validation manuelle 24-48h\n"
            "3. Tu recoiras un email avec ta cle (commence par 's2k-')\n"
            "4. OPTIONNEL : si pas de cle, la source est skipped, pas grave"
        ),
        required=False,
        secret=True,
    ),
    Field(
        key="MAIL_MIN_SCORE",
        label="⭐ Score minimum pour afficher un article",
        description=(
            "Tous les articles avec un score IA inferieur seront masques du digest final.\n"
            "1 = tres permissif (tout affiche), 5 = ultra-strict (innovations majeures uniquement).\n"
            "Recommande : 2 (filtre les articles peu pertinents sans rater les tendances)."
        ),
        default="2",
        instructions="Choisis entre 1 et 5",
        required=False,
        validator=_validate_int_range(1, 5),
    ),
    Field(
        key="AI_BATCH_SIZE",
        label="📦 Taille des batchs envoyes a l'IA",
        description=(
            "Nombre d'articles que l'IA analyse en un seul appel.\n"
            "Plus c'est grand, moins d'appels = moins de quota consomme.\n"
            "Mais trop grand : risque de tronquer la reponse."
        ),
        default="20",
        instructions="Recommande : 20 (compromis quota / fiabilite)",
        required=False,
        validator=_validate_int_range(5, 50),
    ),
]


# =============================================================================
# UI helpers
# =============================================================================
def _print_banner() -> None:
    title = Text()
    title.append("\n  VEILLE TECHNOLOGIQUE\n", style="bold cyan")
    title.append("  Assistant de configuration", style="dim cyan")
    console.print(Panel(Align.center(title), border_style="cyan", padding=(1, 4)))
    console.print()


def _print_field_card(field: Field, current_value: str | None) -> None:
    body = Text()
    body.append(field.description.strip() + "\n\n", style="white")
    if field.url:
        body.append("🔗 OU OBTENIR : ", style="bold yellow")
        body.append(field.url + "\n\n", style="underline blue")
    if field.instructions:
        body.append("📋 COMMENT FAIRE :\n", style="bold yellow")
        body.append(field.instructions.strip() + "\n", style="dim white")

    if current_value:
        body.append("\n✅ Valeur actuelle : ", style="bold green")
        masked = ("•" * 8 + current_value[-4:]) if field.secret and len(current_value) > 4 else current_value
        body.append(masked, style="green")
    elif field.default:
        body.append(f"\n💡 Defaut propose : ", style="bold yellow")
        body.append(field.default, style="yellow")

    title = field.label + ("  (optionnel)" if not field.required else "")
    console.print(Panel(body, title=title, border_style="cyan", padding=(1, 2)))


_GO_BACK = "__GO_BACK__"  # sentinelle pour indiquer "retour au champ precedent"


def _ask_field(field: Field, current_value: str | None, can_go_back: bool) -> str:
    """Pose la question pour un champ. Retourne la valeur OU _GO_BACK si l'utilisateur
    veut revenir au champ precedent."""
    while True:
        _print_field_card(field, current_value)

        if current_value:
            actions = ["g", "m", "v"] + (["p"] if can_go_back else [])
            actions_label = (
                "  [bold]g[/bold]arder • [bold]m[/bold]odifier • [bold]v[/bold]oir en clair"
                + (" • [bold]p[/bold]recedent" if can_go_back else "")
            )
            console.print(actions_label)
            choice = Prompt.ask(
                "  [bold]Que veux-tu faire ?[/bold]",
                choices=actions,
                default="g",
                show_choices=False,
            )
            console.print()
            if choice == "g":
                return current_value
            if choice == "v":
                console.print(f"  Valeur en clair : [bold]{current_value}[/bold]\n")
                continue
            if choice == "p":
                return _GO_BACK
        else:
            if can_go_back:
                console.print(
                    "  [dim]Tape [bold]p[/bold] pour revenir au champ precedent, "
                    "ou laisse vide pour saisir maintenant.[/dim]"
                )
                first = Prompt.ask(
                    "  [bold]Action[/bold]",
                    choices=["s", "p"],
                    default="s",
                    show_choices=False,
                )
                if first == "p":
                    return _GO_BACK

        prompt_text = "  [bold]Saisis la valeur[/bold]"
        if not field.required:
            prompt_text += " [dim](laisse vide et appuie Entree pour passer)[/dim]"
        if field.default and not current_value:
            value = Prompt.ask(prompt_text, default=field.default, password=field.secret)
        else:
            value = Prompt.ask(prompt_text, default="", password=field.secret)

        value = value.strip()
        if not value and not field.required:
            console.print("  [dim]Champ laisse vide[/dim]\n")
            return ""
        if not value and field.required:
            console.print("  [red]✘ Ce champ est obligatoire.[/red]\n")
            continue
        if field.validator:
            err = field.validator(value)
            if err:
                console.print(f"  [red]✘ {err}[/red]\n")
                continue
        return value


def _legend_actions() -> None:
    console.print(
        "  [dim]💡 A chaque etape tu pourras taper :\n"
        "     [bold]g[/bold] = garder la valeur actuelle  •  "
        "[bold]m[/bold] = modifier  •  [bold]v[/bold] = voir en clair  •  "
        "[bold]p[/bold] = revenir au champ precedent[/dim]\n"
    )


# =============================================================================
# Lecture / ecriture .env
# =============================================================================
def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env(values: dict[str, str]) -> None:
    lines = [
        "# Configuration Veille Tech — fichier confidentiel, ne pas commiter",
        "# Genere par configurer.py",
        "",
    ]
    for f in FIELDS:
        v = values.get(f.key, "")
        if v or f.required:
            lines.append(f"{f.key}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary_table(values: dict[str, str]) -> None:
    table = Table(title="📋 Resume de la configuration", border_style="cyan", show_header=True)
    table.add_column("Cle", style="cyan", no_wrap=True)
    table.add_column("Statut", style="green")
    table.add_column("Valeur (masquee)", style="dim white")
    for f in FIELDS:
        v = values.get(f.key, "")
        if v:
            statut = "✅ OK"
            if f.secret and len(v) > 4:
                masked = "•" * 8 + v[-4:]
            else:
                masked = v
        else:
            statut = "⚪ vide" if not f.required else "[red]✘ MANQUE[/red]"
            masked = ""
        table.add_row(f.label, statut, masked)
    console.print(table)


# =============================================================================
# Mode --check
# =============================================================================
def _check_mode() -> int:
    console.rule("[bold cyan]Verification de la configuration .env")
    values = _read_env()
    if not values:
        console.print("\n[red]✘ Aucun fichier .env trouve.[/red]")
        console.print("  Lance [bold]python configurer.py[/bold] pour le creer.\n")
        return 1
    _summary_table(values)
    missing = [f.key for f in FIELDS if f.required and not values.get(f.key)]
    if missing:
        console.print(f"\n[red]✘ Champs obligatoires manquants : {', '.join(missing)}[/red]\n")
        return 2
    console.print("\n[green]✅ Configuration complete et valide.[/green]\n")
    return 0


# =============================================================================
# Main
# =============================================================================
def main() -> int:
    if "--check" in sys.argv:
        return _check_mode()

    _print_banner()
    existing = _read_env()
    if existing:
        console.print(Panel(
            f"Un fichier [bold].env[/bold] existe deja avec [bold cyan]{len(existing)}[/bold cyan] valeurs.\n"
            "Tu vas pouvoir parcourir chaque champ et choisir de le garder ou de le modifier.",
            title="ℹ️  Fichier existant detecte",
            border_style="yellow",
        ))
        if not Confirm.ask("\n  Continuer ?", default=True):
            console.print("[yellow]Annule.[/yellow]")
            return 0

    console.rule("[bold cyan]Configuration etape par etape", style="cyan")
    console.print()
    _legend_actions()

    values: dict[str, str] = {}
    idx = 0
    while idx < len(FIELDS):
        f = FIELDS[idx]
        current = existing.get(f.key, "") or values.get(f.key, "")
        v = _ask_field(f, current if current else None, can_go_back=(idx > 0))
        if v == _GO_BACK:
            # Retour au champ precedent : on enleve sa valeur saisie et on recule
            idx = max(0, idx - 1)
            prev_key = FIELDS[idx].key
            if prev_key in values:
                values.pop(prev_key)
            console.print(f"[yellow]↩ Retour au champ precedent : {FIELDS[idx].label}[/yellow]\n")
            continue
        if v:
            values[f.key] = v
        idx += 1

    console.rule("[bold cyan]Recapitulatif", style="cyan")
    console.print()
    _summary_table(values)
    console.print()

    if not Confirm.ask("  💾 Enregistrer dans [bold].env[/bold] ?", default=True):
        console.print("\n[yellow]Annule, aucune modification ecrite.[/yellow]")
        return 0

    _write_env(values)
    console.print(Panel(
        f"[green]✅ Fichier .env enregistre :[/green] {ENV_PATH}\n\n"
        "[bold]Prochaine etape :[/bold] lance [bold cyan]python main.py[/bold cyan] "
        "pour demarrer le pipeline complet.\n"
        "Ou double-clique sur [bold cyan]lancer.bat[/bold cyan] depuis l'Explorateur Windows.",
        title="✅ Configuration terminee",
        border_style="green",
        padding=(1, 2),
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
