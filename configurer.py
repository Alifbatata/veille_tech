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
            "Influence directement le nombre de requetes API consommees par run.\n"
            "Exemple : 210 articles + batch=30 -> 7 requetes ; batch=20 -> 11 ; batch=10 -> 21.\n\n"
            "  - PETIT (5-10)  : plus de requetes, reponses tres fiables\n"
            "  - MOYEN (20-30) : equilibre - DEFAUT (economise le quota gratuit\n"
            "                    sans risque grace a l'auto-split en cas de troncature)\n"
            "  - GRAND (40-50) : encore moins de requetes, pousse le modele a fond\n\n"
            "Bonne nouvelle : le pipeline gere automatiquement les troncatures.\n"
            "Si un batch est trop gros pour le modele (cas pathologique avec\n"
            "des articles a longues descriptions), il est detecte (finish_reason\n"
            "== MAX_TOKENS) et splitte en 2 automatiquement. Tu peux donc monter\n"
            "le batch sans risquer de perdre des articles."
        ),
        default="30",
        instructions=(
            "Tu peux laisser le defaut (30) dans 99 % des cas.\n"
            "Ne change que si :\n"
            "  - Tu veux economiser encore plus de quota -> monte a 40-50\n"
            "  - Tu vois beaucoup de 'split-retry' dans les logs -> baisse a 20\n"
            "    (signifie que ton modele tronque souvent ; baisser evite\n"
            "     les appels supplementaires de split)"
        ),
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


def _mask_value(field: Field, value: str) -> str:
    if not value:
        return "(vide)"
    if field.secret and len(value) > 4:
        return "•" * 8 + value[-4:]
    return value


def _print_field_card(
    field: Field,
    existing_value: str | None,
    staged_value: str | None,
) -> None:
    body = Text()
    body.append(field.description.strip() + "\n\n", style="white")
    if field.url:
        body.append("🔗 OU OBTENIR : ", style="bold yellow")
        body.append(field.url + "\n\n", style="underline blue")
    if field.instructions:
        body.append("📋 COMMENT FAIRE :\n", style="bold yellow")
        body.append(field.instructions.strip() + "\n", style="dim white")

    has_staged = staged_value is not None
    if has_staged and staged_value != (existing_value or ""):
        body.append("\n🟢 NOUVELLE valeur (sera enregistree) : ", style="bold bright_green")
        body.append(_mask_value(field, staged_value), style="bright_green")
        body.append("\n📂 Ancienne valeur dans .env       : ", style="yellow")
        body.append(
            _mask_value(field, existing_value) if existing_value else "(aucune)",
            style="dim yellow",
        )
    elif has_staged:
        body.append("\n✅ Valeur enregistree (inchangee depuis .env) : ", style="bold green")
        body.append(_mask_value(field, staged_value), style="green")
    elif existing_value:
        body.append("\n✅ Valeur actuelle dans .env : ", style="bold green")
        body.append(_mask_value(field, existing_value), style="green")
    elif field.default:
        body.append("\n💡 Defaut propose : ", style="bold yellow")
        body.append(field.default, style="yellow")

    title = field.label + ("  (optionnel)" if not field.required else "")
    console.print(Panel(body, title=title, border_style="cyan", padding=(1, 2)))


_GO_BACK = "__GO_BACK__"  # sentinelle pour indiquer "retour au champ precedent"


def _ask_field(
    field: Field,
    existing_value: str | None,
    staged_value: str | None,
    can_go_back: bool,
) -> str:
    """Pose la question pour un champ.

    Retourne la valeur saisie (chaine, eventuellement vide) OU la sentinelle
    `_GO_BACK` si l'utilisateur veut revenir au champ precedent.

    `existing_value` = valeur actuellement dans .env (avant cette session).
    `staged_value`   = valeur deja saisie pendant cette session (None si jamais visite).
    """
    has_staged = staged_value is not None
    has_existing = bool(existing_value)
    has_default = bool(field.default)

    # Valeur "effective" qui serait conservee si l'utilisateur tape 'g'.
    if has_staged:
        effective_value = staged_value
        effective_source = "staged"
    elif has_existing:
        effective_value = existing_value
        effective_source = "existing"
    elif has_default:
        effective_value = field.default
        effective_source = "default"
    else:
        effective_value = ""
        effective_source = "none"

    while True:
        _print_field_card(field, existing_value, staged_value)

        actions: list[str] = []
        label_parts: list[str] = []

        # 'g' (garder) si on a une valeur a proposer (staged / existing / default)
        if effective_source != "none":
            actions.append("g")
            if effective_source == "default":
                label_parts.append("[bold green]g[/bold green]arder le defaut suggere")
            else:
                label_parts.append("[bold green]g[/bold green]arder cette valeur")
            actions.append("m")
            label_parts.append("[bold yellow]m[/bold yellow]odifier")
            actions.append("v")
            label_parts.append("[bold cyan]v[/bold cyan]oir en clair")
        else:
            actions.append("s")
            label_parts.append("[bold green]s[/bold green]aisir une valeur")

        # 'r' (restaurer) uniquement si la staged differe d'une existing reelle
        staged_differs = has_staged and staged_value != (existing_value or "")
        if staged_differs and has_existing:
            actions.append("r")
            label_parts.append("[bold yellow]r[/bold yellow]estaurer la valeur .env")

        # 'i' (ignorer) uniquement pour champ optionnel
        if not field.required:
            actions.append("i")
            label_parts.append("[bold cyan]i[/bold cyan]gnorer (laisser vide)")

        if can_go_back:
            actions.append("p")
            label_parts.append("[bold cyan]p[/bold cyan]recedent")

        default_choice = "g" if effective_source != "none" else "s"
        default_label = {
            "g": "garder",
            "s": "saisir maintenant",
        }[default_choice]
        # Premiere lettre coloree DANS le mot (ex: "garder" -> "[g]arder")
        # Suppression de la redondance "Entree = g garder" -> "Entree = garder"
        default_label_styled = (
            f"[bold green]{default_label[0]}[/bold green]{default_label[1:]}"
        )

        console.print("  " + " • ".join(label_parts))
        choice = Prompt.ask(
            f"  [bold]Que veux-tu faire ?[/bold] "
            f"[dim](Entree = {default_label_styled})[/dim]",
            choices=actions,
            default=default_choice,
            show_choices=False,
            show_default=False,
        )
        console.print()

        if choice == "g":
            if effective_source == "default":
                console.print(
                    f"  [bright_green]✓ Defaut accepte : "
                    f"[bold]{effective_value}[/bold][/bright_green]\n"
                )
            return effective_value
        if choice == "v":
            console.print(f"  Valeur en clair : [bold]{effective_value}[/bold]\n")
            continue
        if choice == "r":
            restored = existing_value or ""
            console.print(
                f"  [yellow]↺ Restauration de la valeur .env : "
                f"[bold]{_mask_value(field, restored)}[/bold][/yellow]\n"
            )
            return restored
        if choice == "i":
            console.print(
                "  [bright_green]✓ Champ ignore (laisse vide pour cette session).[/bright_green]\n"
            )
            return ""
        if choice == "p":
            return _GO_BACK
        # choice == "m" ou "s" -> on tombe dans la saisie

        prompt_text = "  [bold]Saisis la valeur[/bold]"
        if not field.required:
            prompt_text += " [dim](laisse vide pour ignorer)[/dim]"
        # Pre-remplir avec le defaut UNIQUEMENT si rien n'existe encore
        # (on ne veut pas pre-remplir si l'utilisateur tape 'm' apres avoir vu
        # son ancienne valeur - il veut taper depuis zero)
        prefill = field.default if (has_default and not has_staged and not has_existing) else ""
        value = Prompt.ask(prompt_text, default=prefill, password=field.secret)

        value = value.strip()
        if not value and not field.required:
            console.print(
                "  [bright_green]✓ Champ enregistre comme [bold]vide[/bold] "
                "pour cette session.[/bright_green]\n"
            )
            return ""
        if not value and field.required:
            console.print("  [red]✘ Ce champ est obligatoire.[/red]\n")
            continue
        if field.validator:
            err = field.validator(value)
            if err:
                console.print(f"  [red]✘ {err}[/red]\n")
                continue
        if value != (existing_value or ""):
            console.print(
                f"  [bright_green]✓ Nouvelle valeur enregistree pour cette session : "
                f"[bold]{_mask_value(field, value)}[/bold][/bright_green]"
            )
            console.print(
                "  [dim](tu la reverras si tu reviens ici avec [bold]p[/bold] ; "
                "ecrite dans .env quand tu confirmeras a la fin)[/dim]\n"
            )
        else:
            console.print(
                "  [dim]✓ Valeur identique a celle deja dans .env (rien a changer)[/dim]\n"
            )
        return value


def _legend_actions() -> None:
    console.print(
        "  [dim]💡 A chaque etape tu pourras taper :\n"
        "     [bold]g[/bold] = garder la valeur proposee   •  "
        "[bold]s[/bold] = saisir une valeur (si aucune proposee)\n"
        "     [bold]m[/bold] = modifier la valeur          •  "
        "[bold]v[/bold] = voir en clair\n"
        "     [bold]i[/bold] = ignorer ce champ (optionnel)•  "
        "[bold]r[/bold] = restaurer la valeur .env d'origine\n"
        "     [bold]p[/bold] = revenir au champ precedent\n"
        "     (Appuie juste sur Entree pour accepter l'action par defaut surlignee)[/dim]\n"
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
        elif f.required:
            statut = "[red]✘ MANQUE[/red]"
            masked = ""
        else:
            # Champ optionnel non renseigne : on affiche la valeur par defaut
            # (codee en dur dans le code Python) plutot que "vide", pour que
            # l'utilisateur sache QUELLE valeur sera reellement utilisee.
            if f.default:
                statut = "[dim]⚙ defaut[/dim]"
                masked = f"[dim](valeur par defaut : {f.default})[/dim]"
            else:
                statut = "[dim]⚪ vide (skip)[/dim]"
                masked = "[dim]source desactivee[/dim]"
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
        existing_v = existing.get(f.key, "")
        staged_v = values.get(f.key)  # None = jamais visite, str (incl "") = visite
        v = _ask_field(
            f,
            existing_v if existing_v else None,
            staged_v,
            can_go_back=(idx > 0),
        )
        if v == _GO_BACK:
            idx = max(0, idx - 1)
            console.print(
                f"[yellow]↩ Retour au champ precedent : {FIELDS[idx].label}[/yellow]\n"
            )
            continue
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
