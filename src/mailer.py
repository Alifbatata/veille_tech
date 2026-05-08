# =============================================================================
# mailer.py — Digest hebdomadaire par email (SMTP Gmail + template HTML)
# =============================================================================
# Variables d'environnement requises :
#   GMAIL_USER       — adresse Gmail expéditrice (ex: veille@gmail.com)
#   GMAIL_PASSWORD   — mot de passe d'application Gmail (16 caractères)
#                      → https://myaccount.google.com/apppasswords
#
# Variables optionnelles :
#   MAIL_RECIPIENT   — destinataire (défaut = GMAIL_USER)
#   MAIL_SUBJECT     — sujet personnalisé
#   MAIL_MIN_SCORE   — score minimum à afficher dans le digest (défaut: 2)
#   SMTP_HOST        — hôte SMTP (défaut: smtp.gmail.com)
#   SMTP_PORT        — port SMTP (défaut: 587)
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mailer")

# ---------------------------------------------------------------------------
# Configuration via variables d'environnement
# ---------------------------------------------------------------------------
GMAIL_USER: str | None = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD: str | None = os.environ.get("GMAIL_PASSWORD")
MAIL_RECIPIENT: str = os.environ.get("MAIL_RECIPIENT", GMAIL_USER or "")
MAIL_MIN_SCORE: int = int(os.environ.get("MAIL_MIN_SCORE", "2"))
SMTP_HOST: str = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))

# Libellés des scores pour les badges — couleurs maximalement contrastées
# Inspiré de Linear/Vercel/Stripe (palette à hue distincte, saturation forte)
SCORE_LABELS: dict[int, tuple[str, str]] = {
    5: ("Percée majeure",     "#7C3AED"),  # violet vif — urgence stratégique
    4: ("Innovation solide",  "#059669"),  # vert émeraude — positif fort
    3: ("À surveiller",       "#2563EB"),  # bleu vif — informatif
    2: ("Signal faible",      "#D97706"),  # ambre — attention
    1: ("Note",               "#6B7280"),  # gris neutre
}

# Étoiles : dichotomie pleine/vide très contrastée (UX standard Amazon/Trustpilot)
STAR_FILLED_COLOR: str = "#F59E0B"  # doré vif (amber-500)
STAR_EMPTY_COLOR:  str = "#E5E7EB"  # gris très clair (gray-200)

# Emojis de catégorie
CATEGORY_ICONS: dict[str, str] = {
    "science": "🔬",
    "news":    "📰",
    "general": "📄",
}

# Emojis de source
SOURCE_ICONS: dict[str, str] = {
    "arxiv": "🏛️",
    "nature": "🌿",
    "google news": "📰",
    "ieee": "⚡",
    "mdpi": "📖",
    "sciencedaily": "🧪",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MailerConfigError(ValueError):
    """Levée quand les identifiants SMTP sont manquants."""

class MailerSendError(RuntimeError):
    """Levée quand l'envoi SMTP échoue."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_fr() -> str:
    """Retourne la date du jour au format français, ex: 'mercredi 29 avril 2026'."""
    now = datetime.now(timezone.utc)
    months = [
        "", "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    return f"{days[now.weekday()]} {now.day} {months[now.month]} {now.year}"


def _week_number() -> int:
    return datetime.now(timezone.utc).isocalendar()[1]


def _truncate(text: str, length: int = 220) -> str:
    text = text.strip()
    return (text[:length] + "…") if len(text) > length else text


def _render_stars(score: int, size_px: int = 16) -> str:
    """
    Rend les 5 étoiles avec dichotomie pleine (doré vif) / vide (gris clair).
    Standard UI Amazon/Trustpilot : on voit instantanément 1★ vs 5★.
    """
    stars_html = ""
    for i in range(1, 6):
        color = STAR_FILLED_COLOR if i <= score else STAR_EMPTY_COLOR
        stars_html += (
            f'<span class="star-glyph" style="color:{color};font-size:{size_px}px;'
            f'line-height:1;letter-spacing:1px;">★</span>'
        )
    return stars_html


def _score_badge(score: int) -> str:
    """
    Badge composé de deux blocs :
      1. Rangée d'étoiles très contrastées (5 étoiles, pleines dorées vs vides gris)
      2. Pastille colorée avec le label sémantique (couleur dépend du score)
    """
    label, color = SCORE_LABELS.get(score, ("—", "#6B7280"))
    stars_html = _render_stars(score, size_px=15)
    return (
        f'<span class="score-badge-wrap" style="display:inline-block;line-height:1;">'
        # Étoiles (visibles immédiatement)
        f'<span class="score-stars" style="display:inline-block;vertical-align:middle;'
        f'margin-right:8px;">{stars_html}</span>'
        # Pastille label
        f'<span class="score-badge" style="display:inline-block;vertical-align:middle;'
        f'background:{color};color:#ffffff;font-size:10px;font-weight:700;'
        f'letter-spacing:.1em;text-transform:uppercase;padding:5px 10px;'
        f'border-radius:3px;">{escape(label)} · {score}/5</span>'
        f'</span>'
    )


def _category_pill(category: str) -> str:
    icon = CATEGORY_ICONS.get(category, "📎")
    return (
        f'<span class="category-pill" style="font-size:10px;color:#8a9baa;letter-spacing:.06em;'
        f'text-transform:uppercase;">{icon} {escape(category)}</span>'
    )


def _get_source_icon(source: str) -> str:
    """Retourne une icône spécifique en fonction du nom de la source."""
    s_lower = source.lower()
    for key, icon in SOURCE_ICONS.items():
        if key in s_lower:
            return icon
    return "🔗"


def _tag_chip(tag: str) -> str:
    """Génère un badge (chip) HTML pour un tag."""
    return (
        f'<span class="tag-chip" style="display:inline-block;font-size:10px;'
        f'background:#eef3f8;color:#4a5a63;padding:3px 8px;border-radius:2px;'
        f'margin:0 4px 4px 0;letter-spacing:.05em;">{escape(tag)}</span>'
    )


def _seen_badge() -> str:
    """Badge 'Déjà envoyé' affiché dans les cartes article en mode TOUT_RENVOYER.

    En mode FILTRER, les articles déjà vus sont éliminés AVANT le scoring IA, donc
    ce badge n'apparaît jamais. En mode TOUT_RENVOYER (USE_MEMORY=False), il
    permet à l'utilisateur de distinguer en un coup d'œil les articles qu'il a
    déjà reçus précédemment des vrais nouveaux.
    """
    return (
        '<span class="seen-badge" style="display:inline-block;font-size:10px;'
        'background:#f3e8ff;color:#6b21a8;padding:4px 10px;border-radius:2px;'
        'margin-left:8px;letter-spacing:.06em;font-weight:600;'
        'text-transform:uppercase;border:1px solid #d8b4fe;'
        'vertical-align:middle;">📌 Deja envoye</span>'
    )


_PREV_DATA_PATH = os.path.join(os.path.dirname(__file__), "../data/previous_ai_output.json")
_DISCOVERED_ACTORS_PATH_MAILER = os.path.join(os.path.dirname(__file__), "../data/discovered_actors.json")


def _load_top_discovered_actors(min_count: int = 2, max_actors: int = 15) -> list[dict[str, Any]]:
    """Charge les acteurs découverts les plus fréquents (cumul des runs).

    Filtres :
    - Seulement les acteurs vus au moins `min_count` fois (signal robuste)
    - Top `max_actors` triés par count décroissant
    """
    if not os.path.exists(_DISCOVERED_ACTORS_PATH_MAILER):
        return []
    try:
        with open(_DISCOVERED_ACTORS_PATH_MAILER, "r", encoding="utf-8") as f:
            data = json.load(f)
        actors = list(data.get("actors", {}).values())
    except (OSError, json.JSONDecodeError):
        return []
    actors = [a for a in actors if a.get("count", 0) >= min_count]
    actors.sort(key=lambda a: a.get("count", 0), reverse=True)
    return actors[:max_actors]


def _render_discovered_actors_section(actors: list[dict[str, Any]]) -> str:
    """Génère la section 'Acteurs découverts' du digest email.

    S'affiche à la fin du digest, après les articles, juste avant la rétro
    'Déjà vu la semaine passée'. Liste les entreprises/labos NON présents dans
    tes listes companies/research_orgs mais qui apparaissent régulièrement
    dans les résultats Patents/OpenAlex — candidats à ajouter à ta veille.
    """
    if not actors:
        return ""

    rows_html = ""
    for a in actors:
        name = escape(a.get("name", "?"))
        count = a.get("count", 0)
        srcs = ", ".join(escape(s) for s in a.get("sources", []))
        rows_html += f"""
        <tr>
          <td class="discovered-row" style="padding:8px 16px; border-bottom:1px solid #e8edf0;">
            <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;">
              <tr>
                <td style="font-size:13px; font-weight:600; color:#0c4a6e;">{name}</td>
                <td style="text-align:right; font-size:11px; color:#64748b; white-space:nowrap;">
                  <span style="background:#e0f2fe; color:#0369a1; padding:2px 8px; border-radius:2px;
                               font-weight:700; letter-spacing:.05em;">vu {count}×</span>
                  &nbsp;·&nbsp; {srcs}
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    return f"""
    <tr>
      <td style="padding:32px 0 16px;">
        <table role="presentation" cellpadding="0" cellspacing="0"
              class="discovered-actors-table"
              style="width:100%; background:#f0f9ff; border:1px solid #bae6fd; border-radius:4px;">
          <tr>
            <td class="discovered-actors-header"
                style="padding:12px 16px; background:#bae6fd; border-bottom:1px solid #7dd3fc; border-radius:4px 4px 0 0;">
              <span style="font-size:10px; font-weight:700; color:#0c4a6e;
                           letter-spacing:.1em; text-transform:uppercase;">
                🔍 Acteurs découverts automatiquement &mdash; candidats à ajouter à ta veille
              </span>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 16px 4px; font-size:11px; color:#475569; line-height:1.5;">
              Ces entreprises et laboratoires apparaissent dans les résultats Google Patents
              et OpenAlex mais ne sont pas (encore) dans tes listes <i>companies</i>/<i>research_orgs</i>.
              Plus une entrée revient (vu N×), plus c'est un signal robuste qu'il vaut la peine
              de l'ajouter pour les prochains runs.
              <br>
              <span style="color:#94a3b8;">→ Pour valider/rejeter : <code>python main.py</code>
              → menu d'édition → action 11 (Revoir les acteurs DECOUVERTS)</span>
            </td>
          </tr>
          {rows_html}
        </table>
      </td>
    </tr>"""


def _load_previous_top_articles() -> list[dict[str, Any]]:
    """Charge les articles de la semaine passée ayant un score de 4 ou 5."""
    if not os.path.exists(_PREV_DATA_PATH):
        return []
    try:
        with open(_PREV_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        articles = data.get("articles", [])
        return [a for a in articles if a.get("score", 0) >= 4]
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Erreur lors de la lecture de %s : %s", _PREV_DATA_PATH, e)
        return []

# ---------------------------------------------------------------------------
# Template HTML — style Swiss Industrial
# ---------------------------------------------------------------------------

def _render_article_card(article: dict[str, Any], index: int) -> str:
    """Génère le HTML d'une carte article."""
    score       = article.get("score", 1)
    title       = escape(article.get("title", "(Sans titre)"))
    summary     = escape(_truncate(article.get("summary", "")))
    source      = escape(article.get("source", ""))
    link        = article.get("link", "#")
    category    = article.get("category", "general")
    justif      = escape(article.get("justification", ""))
    tags        = article.get("tags", [])
    collected   = article.get("collected_at", "")[:10]
    was_seen    = bool(article.get("was_seen", False))

    tags_html    = "".join(_tag_chip(t) for t in tags[:6]) if tags else ""
    border_color = SCORE_LABELS.get(score, ("", "#dde4ea"))[1]
    source_icon  = _get_source_icon(source)
    seen_html    = _seen_badge() if was_seen else ""

    return f"""
    <tr>
      <td style="padding:0 0 24px 0;">
        <div style="height: 16px; line-height: 16px; font-size: 16px;">&nbsp;</div>

        <table role="presentation" cellpadding="0" cellspacing="0"
               class="article-card"
               style="width:100%;
                      border:1px solid #e5e5e5;
                      border-left:4px solid {border_color};
                      background:#fff;border-radius:0;">
          <tr>
            <td style="padding:24px 24px 0 24px;">
              <!-- Score + catégorie -->
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;margin-bottom:12px;">
                <tr>
                  <td>{_score_badge(score)}{seen_html}</td>
                  <td style="text-align:right;">{_category_pill(category)}</td>
                </tr>
              </table>
              <!-- Numéro + Titre -->
              <p style="margin:0 0 8px;font-size:11px;color:#a0b0bc;
                        letter-spacing:.15em;font-weight:700;
                        text-transform:uppercase;">№ {index:02d}</p>
              <h2 style="margin:0 0 12px;font-size:18px;font-weight:700;
                         line-height:1.3;color:#1c2b36;letter-spacing:-.02em;">
                {title}
              </h2>
            </td>
          </tr>
          {'<tr><td style="padding:0 24px 16px;"><p class="article-summary" style="margin:0;font-size:14px;color:#333333;line-height:1.6;">' + summary + '</p></td></tr>' if summary else ''}
          {'<tr><td style="padding:0 24px 16px;"><p class="article-justification" style="margin:0;font-size:13px;color:#666666;line-height:1.5;font-style:italic;border-left:2px solid #e0e8ed;padding-left:12px;">💡 ' + justif + '</p></td></tr>' if justif else ''}
          {'<tr><td style="padding:0 24px 16px;">' + tags_html + '</td></tr>' if tags_html else ''}
          <!-- Footer carte -->
          <tr>
            <td class="article-footer-border" style="padding:16px 24px;border-top:1px solid #e5e5e5;background:#fafafa;">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;">
                <tr>
                  <td style="font-size:11px;color:#666666;font-weight:500;letter-spacing:.02em;">
                    <span style="font-size:14px;vertical-align:middle;margin-right:4px;">{source_icon}</span>
                    {escape(source)}
                    {'<span style="color:#c5d0d8;margin-left:6px;">· ' + collected + '</span>' if collected else ''}
                  </td>
                  <td style="text-align:right;">
                    <a href="{escape(link)}" class="read-source-link"
                       style="display:inline-block;font-size:10px;font-weight:700;
                              color:#1c2b36;text-decoration:none;
                              border:1px solid #1c2b36;padding:6px 16px;
                              letter-spacing:.08em;
                              text-transform:uppercase;">
                      LIRE →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def _render_score_section(group: list[dict[str, Any]], score: int, label: str, start_index: int) -> tuple[str, int]:
    """Génère une section de score avec son en-tête et ses cartes."""
    if not group:
        return "", start_index

    _, color = SCORE_LABELS.get(score, ("—", "#9aafbd"))
    cards_html = ""
    idx = start_index
    for art in group:
        cards_html += _render_article_card(art, idx)
        idx += 1

    stars_html = _render_stars(score, size_px=14)
    section = f"""
    <tr>
      <td style="padding:32px 0 8px;">
        <table role="presentation" cellpadding="0" cellspacing="0"
               style="width:100%;">
          <tr>
            <td style="border-bottom:2px solid {color};padding-bottom:8px;">
              <span style="display:inline-block;vertical-align:middle;margin-right:10px;">{stars_html}</span>
              <span style="display:inline-block;vertical-align:middle;font-size:11px;font-weight:700;color:{color};
                           letter-spacing:.15em;text-transform:uppercase;">
                {escape(label)} &nbsp;·&nbsp; {len(group)}
              </span>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    {cards_html}"""
    return section, idx


def _render_previous_section(articles: list[dict[str, Any]]) -> str:
    """Génère une section compacte pour les articles phares de la semaine passée."""
    if not articles:
        return ""

    cards_html = ""
    for art in articles:
        title = escape(art.get("title", "(Sans titre)"))
        link = art.get("link", "#")
        score = art.get("score", 0)
        source = escape(art.get("source", ""))

        stars_html = _render_stars(score, size_px=11)

        cards_html += f"""
        <tr>
          <td style="padding: 10px 16px; border-bottom: 1px solid #e8edf0;">
            <p class="previous-article-title" style="margin:0 0 4px; font-size:12px; font-weight:600; line-height:1.4;">
              <a href="{escape(link)}" style="color:#1c2b36; text-decoration:none;">{title}</a>
            </p>
            <p class="previous-article-meta" style="margin:0; font-size:10px; color:#7a8e99;">
              <span>{stars_html}</span> &nbsp;·&nbsp; {source}
            </p>
          </td>
        </tr>"""

    return f"""
    <tr>
      <td style="padding:32px 0 16px;">
        <table role="presentation" cellpadding="0" cellspacing="0"
              class="previous-articles-table"
              style="width:100%; background:#f8fafc; border:1px solid #e2e8f0; border-radius:4px;">
          <tr>
            <td class="previous-articles-header"
                style="padding:12px 16px; background:#e2e8f0; border-bottom:1px solid #cbd5e1; border-radius:4px 4px 0 0;">
              <span style="font-size:10px; font-weight:700; color:#475569; letter-spacing:.1em; text-transform:uppercase;">
                ⏪ Déjà vu la semaine passée
              </span>
            </td>
          </tr>
          {cards_html}
        </table>
      </td>
    </tr>"""


def build_html_email(filtered_data: dict[str, Any]) -> str:
    """
    Construit le HTML complet du digest à partir du résultat de ai_filter.

    Structure Swiss Industrial :
      1. Header (titre + date)
      2. Barre de statistiques 📊
      3. Executive Summary 💡
      4. Articles groupés par score décroissant (★★★★★ → ★★☆☆☆)
      5. Section "Semaine passée"
      6. Footer

    Args:
        filtered_data: dict retourné par ai_filter.filter_articles_with_ai()

    Returns:
        Chaîne HTML valide, prête à envoyer.
    """
    meta         = filtered_data.get("meta", {})
    articles     = filtered_data.get("articles", [])
    date_str     = _now_fr()
    week_num     = _week_number()
    total        = meta.get("retained_count", len(articles))
    input_count  = meta.get("input_count", "—")
    model_name   = meta.get("model", "Gemini")
    run_at       = meta.get("run_at", "")[:10]
    tldr_summary = meta.get("tldr", "")

    # ── 1. Executive Summary 💡 ──────────────────────────────────────────────
    tldr_html = ""
    if tldr_summary:
        tldr_html = f"""
        <tr>
          <td style="padding:32px 0 0;">
            <table role="presentation" cellpadding="0" cellspacing="0"
                   class="tldr-summary-bg"
                   style="width:100%;background:#ffffff;
                          border-top:4px solid #1c2b36;
                          border-left:none;border-right:none;border-bottom:none;
                          border:1px solid #e5e5e5;border-top-width:4px;">
              <tr>
                <td style="padding:24px;">
                  <p style="margin:0 0 12px;font-size:10px;color:#1c2b36;
                             text-transform:uppercase;letter-spacing:.15em;
                             font-weight:700;border-bottom:1px solid #e5e5e5;
                             padding-bottom:8px;">
                    💡 EXECUTIVE SUMMARY
                  </p>
                  <p class="tldr-text" style="margin:0;font-size:15px;line-height:1.7;
                     color:#1c2b36;letter-spacing:-0.01em;">
                    {escape(tldr_summary)}
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    # Filtrer par MAIL_MIN_SCORE
    articles = [a for a in articles if a.get("score", 0) >= MAIL_MIN_SCORE]

    # Distribution des scores pour la barre de stats
    score_dist: dict[int, int] = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for a in articles:
        s = a.get("score", 1)
        if s in score_dist:
            score_dist[s] += 1

    score_dist_html = " &nbsp;·&nbsp; ".join(
        f'<span style="color:#4a8fa8;font-weight:700;">{score_dist[s]}</span>'
        f'&nbsp;<span style="letter-spacing:1px;">{"★" * s}</span>'
        for s in [5, 4, 3, 2]
        if score_dist[s] > 0
    )

    # Pré-triage par score (évite les itérations O(N²))
    articles_by_score: dict[int, list[dict[str, Any]]] = {5: [], 4: [], 3: [], 2: [], 1: []}
    for a in articles:
        s = a.get("score", 1)
        if s in articles_by_score:
            articles_by_score[s].append(a)

    # Sections par score décroissant
    body_sections = ""
    idx = 1
    for score in [5, 4, 3, 2, 1]:
        label = SCORE_LABELS.get(score, ("—", ""))[0]
        section_html, idx = _render_score_section(articles_by_score[score], score, label, idx)
        body_sections += section_html

    empty_state = "" if articles else """
    <tr><td style="padding:60px 0;text-align:center;color:#9aafbd;">
      <p style="font-size:15px;margin:0;">Aucun article retenu cette semaine.</p>
    </td></tr>"""

    prev_articles = _load_previous_top_articles()
    prev_section_html = _render_previous_section(prev_articles)

    discovered_actors = _load_top_discovered_actors(min_count=2, max_actors=15)
    discovered_section_html = _render_discovered_actors_section(discovered_actors)

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Veille Technologique — Semaine {week_num}</title>
  <style>
    /* ── Reset & Base ──────────────────────────────────────────────────────── */
    body {{
      margin: 0;
      padding: 0;
      background-color: #f4f5f7;
      /* Inter via Google Fonts est chargé dans la balise <link> ci-dessous ;
         Helvetica Neue reste le fallback système Swiss Minimalist. */
      font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      color: #1c2b36;
    }}
    table {{ border-collapse: collapse; }}
    td {{ font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
    a {{ text-decoration: none; }}

    /* ── Dark Mode ─────────────────────────────────────────────────────────── */
    @media (prefers-color-scheme: dark) {{
      body {{
        background-color: #0d1117 !important;
        color: #e6edf3 !important;
      }}
      .main-wrapper {{
        background-color: #0d1117 !important;
      }}
      .container-table {{
        background-color: #0d1117 !important;
      }}
      /* Header */
      .header-bg {{
        background-color: #161b22 !important;
        border-bottom: 1px solid #30363d !important;
      }}
      .header-eyebrow {{ color: #8b949e !important; }}
      .header-title   {{ color: #e6edf3 !important; }}
      .header-date    {{ color: #58a6ff !important; }}
      /* Stats bar */
      .stats-bar-bg {{
        background-color: #161b22 !important;
        border-top-color: #30363d !important;
        border-bottom: 1px solid #30363d !important;
      }}
      .stats-bar-bg td   {{ color: #8b949e !important; }}
      .stats-count       {{ color: #58a6ff !important; }}
      .stats-stars       {{ color: #e3b341 !important; }}
      /* Main content */
      .main-content-bg {{
        background-color: #0d1117 !important;
      }}
      /* Article cards */
      .article-card {{
        background-color: #161b22 !important;
        border-color: #30363d !important;
        box-shadow: none !important;
      }}
      .article-card h2 {{
        color: #e6edf3 !important;
      }}
      .article-card p {{
        color: #c9d1d9 !important;
      }}
      .article-summary {{
        color: #c9d1d9 !important;
      }}
      .article-justification {{
        color: #8b949e !important;
        border-left-color: #30363d !important;
      }}
      .article-footer-border {{
        border-top-color: #30363d !important;
        background-color: #13181f !important;
      }}
      .read-source-link {{
        color: #58a6ff !important;
        border-color: #58a6ff !important;
      }}
      /* Score badges — déjà sombres mais on force le texte blanc */
      .score-badge {{ color: #ffffff !important; opacity: 1 !important; }}
      .score-badge span {{ color: #ffffff !important; }}
      /* Category + tags */
      .category-pill {{ color: #58a6ff !important; }}
      .tag-chip {{
        background-color: #21262d !important;
        color: #8b949e !important;
      }}
      /* Seen badge en dark mode */
      .seen-badge {{
        background-color: #2e1065 !important;
        color: #d8b4fe !important;
        border-color: #6b21a8 !important;
      }}
      /* TL;DR Executive Summary */
      .tldr-summary-bg {{
        background-color: #161b22 !important;
        border-color: #30363d !important;
        border-top-color: #58a6ff !important;
      }}
      .tldr-summary-bg p {{ color: #c9d1d9 !important; }}
      .tldr-summary-bg .tldr-label {{ color: #e6edf3 !important; border-bottom-color: #30363d !important; }}
      .tldr-text {{ color: #e6edf3 !important; }}
      /* Semaine passée */
      .previous-articles-table {{
        background-color: #161b22 !important;
        border-color: #30363d !important;
      }}
      .previous-articles-header {{
        background-color: #21262d !important;
        border-bottom-color: #30363d !important;
      }}
      .previous-articles-header span {{ color: #8b949e !important; }}
      .previous-article-title a {{ color: #e6edf3 !important; }}
      .previous-article-meta    {{ color: #8b949e !important; }}
      .previous-articles-table td {{ border-bottom-color: #30363d !important; }}
      /* Footer */
      .footer-bg {{
        background-color: #161b22 !important;
        border-top-color: #30363d !important;
      }}
      .footer-bg td {{ color: #8b949e !important; }}
      .footer-strong {{ color: #8b949e !important; }}
    }}
  </style>
  <!--[if mso]><style>table{{border-collapse:collapse;}}td{{font-family:Arial,sans-serif;}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;
             font-family:'Inter','Helvetica Neue',Helvetica,Arial,sans-serif;
             -webkit-font-smoothing:antialiased;">

  <!-- Wrapper : align="center" garantit le centrage sur Outlook qui ignore margin auto -->
  <table role="presentation" cellpadding="0" cellspacing="0" align="center"
         class="main-wrapper" style="width:100%;background:#f4f5f7;">
    <tr>
      <td align="center" style="padding:40px 20px;">

        <!-- Container : align="center" + margin auto pour double sécurité multi-clients -->
        <table role="presentation" cellpadding="0" cellspacing="0" align="center"
               class="container-table" style="max-width:640px;margin:0 auto;width:100%;">

          <!-- ── HEADER ─────────────────────────────────────────────────── -->
          <tr>
            <td class="header-bg"
                style="background:#1c2b36;padding:48px 40px 40px;border-radius:0;">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;">
                <tr>
                  <td>
                    <p class="header-eyebrow"
                       style="margin:0 0 8px;font-size:10px;color:#7a9fb5;
                              letter-spacing:.2em;text-transform:uppercase;
                              font-weight:600;">Surface Science Intelligence</p>
                    <h1 class="header-title"
                        style="margin:0;font-size:26px;font-weight:700;
                               color:#ffffff;letter-spacing:-.02em;line-height:1.2;">
                      Digest Hebdomadaire
                    </h1>
                    <p class="header-date"
                       style="margin:8px 0 0;font-size:13px;color:#7a9fb5;">
                      Semaine {week_num} &nbsp;·&nbsp; {date_str}
                    </p>
                  </td>
                  <td style="text-align:right;vertical-align:top;">
                    <div style="display:inline-block;background:#243d4d;
                                border-radius:50%;width:52px;height:52px;
                                line-height:52px;text-align:center;
                                font-size:24px;">⚗️</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- ── BARRE DE STATISTIQUES 📊 ──────────────────────────────── -->
          <tr>
            <td class="stats-bar-bg"
                style="background:#15222b;padding:14px 40px;
                       border-top:1px solid #2a3b47;
                       border-bottom:1px solid #2a3b47;">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;">
                <tr>
                  <td style="font-size:11px;color:#7a9fb5;letter-spacing:.04em;">
                    <span style="font-size:13px;vertical-align:middle;margin-right:4px;">📊</span>
                    <span class="stats-count" style="color:#4a8fa8;font-weight:700;">{total}</span>
                    &nbsp;innovation(s) retenue(s)&nbsp;·&nbsp;
                    <span class="stats-count" style="color:#4a8fa8;font-weight:700;">{input_count}</span>
                    &nbsp;source(s) analysée(s)
                    {('&nbsp;·&nbsp;' + score_dist_html) if score_dist_html else ''}
                  </td>
                  <td style="text-align:right;font-size:10px;color:#4a6070;white-space:nowrap;">
                    {escape(model_name)} &nbsp;·&nbsp; {run_at}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- ── MAIN CONTENT ───────────────────────────────────────────── -->
          <tr>
            <td class="main-content-bg"
                style="background:#f4f5f7;padding:8px 24px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;">
                {tldr_html}
                {body_sections}
                {empty_state}
                {discovered_section_html}
                {prev_section_html}
              </table>
            </td>
          </tr>

          <!-- ── FOOTER ─────────────────────────────────────────────────── -->
          <tr>
            <td class="footer-bg"
                style="background:#fff;padding:24px 40px;
                       border-top:1px solid #e8edf0;border-radius:0 0 4px 4px;">
              <table role="presentation" cellpadding="0" cellspacing="0"
                     style="width:100%;">
                <tr>
                  <td style="font-size:11px;color:#9aafbd;line-height:1.7;">
                    <strong class="footer-strong"
                            style="color:#4a6070;letter-spacing:.04em;">
                      Veille Technologique
                    </strong><br>
                    Généré automatiquement · PVD · CVD · ALD · Surface Science<br>
                    Ce message vous est envoyé dans le cadre d'un projet de veille interne.
                  </td>
                  <td style="text-align:right;vertical-align:bottom;">
                    <span style="font-size:18px;color:#cdd8de;">⬡</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
        <!-- /Container -->

      </td>
    </tr>
  </table>
  <!-- /Wrapper -->

</body>
</html>"""


# ---------------------------------------------------------------------------
# Envoi SMTP
# ---------------------------------------------------------------------------

def _validate_config() -> None:
    """Vérifie que les variables d'environnement SMTP sont définies."""
    missing = []
    if not GMAIL_USER:
        missing.append("GMAIL_USER")
    if not GMAIL_PASSWORD:
        missing.append("GMAIL_PASSWORD")
    if missing:
        raise MailerConfigError(
            f"Variables d'environnement manquantes : {', '.join(missing)}\n"
            "Pour Gmail, utilisez un mot de passe d'application (pas votre mot de passe principal) :\n"
            "  https://myaccount.google.com/apppasswords\n"
            "Puis exportez :\n"
            "  export GMAIL_USER='votre@gmail.com'\n"
            "  export GMAIL_PASSWORD='xxxx xxxx xxxx xxxx'"
        )
    if not MAIL_RECIPIENT:
        raise MailerConfigError(
            "Aucun destinataire. Définissez MAIL_RECIPIENT ou GMAIL_USER."
        )


def send_digest(
    filtered_data: dict[str, Any],
    subject: str | None = None,
    recipient: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Construit le digest HTML et l'envoie par email via Gmail SMTP.

    Args:
        filtered_data: dictionnaire issu de ai_filter.filter_articles_with_ai()
        subject:       sujet de l'email (optionnel, génération automatique sinon)
        recipient:     adresse destinataire (optionnel, surcharge MAIL_RECIPIENT)
        dry_run:       si True, génère le HTML mais n'envoie pas l'email

    Returns:
        Dict de statut :
        {
          "success":    bool,
          "recipient":  str,
          "subject":    str,
          "html_bytes": int,
          "error":      str | None
        }

    Raises:
        MailerConfigError si les identifiants sont manquants (hors dry_run).
    """
    # Transforme la chaîne des destinataires en liste propre, ordonnée et sans doublons
    raw_addrs = (recipient or MAIL_RECIPIENT).split(",")
    to_addrs = list(dict.fromkeys(addr.strip() for addr in raw_addrs if addr.strip()))
    week_num = _week_number()
    total = len([a for a in filtered_data.get("articles", [])
                     if a.get("score", 0) >= MAIL_MIN_SCORE])

    final_subject = (
        subject
        or os.environ.get("MAIL_SUBJECT")
        or f"⚗️ Veille Tech — Semaine {week_num} · {total} innovation(s)"
    )

    logger.info("📧 Génération du digest HTML…")
    html_body = build_html_email(filtered_data)

    result: dict[str, Any] = {
        "success":    False,
        "recipient":  ", ".join(to_addrs),
        "subject":    final_subject,
        "html_bytes": len(html_body.encode("utf-8")),
        "error":      None,
    }

    if dry_run:
        logger.info("🧪 Mode dry_run — email non envoyé (%d octets HTML générés)", result["html_bytes"])
        result["success"] = True
        return result

    # Validation des credentials
    _validate_config()

    # Construction du message MIME
    msg = MIMEMultipart("alternative")
    msg["Subject"] = final_subject
    msg["From"]    = f"Veille Tech <{GMAIL_USER}>"
    msg["To"]      = ", ".join(to_addrs)
    msg["X-Mailer"] = "VeilleTechBot/1.0"

    # Fallback texte brut
    plain_articles = filtered_data.get("articles", [])
    plain_lines    = [f"Digest Semaine {week_num}\n{'='*40}"]
    tldr = filtered_data.get("meta", {}).get("tldr", "")
    if tldr:
        plain_lines.append(f"\nEXECUTIVE SUMMARY\n{tldr}\n{'─'*40}")
    for art in plain_articles[:20]:
        plain_lines.append(
            f"\n[{art.get('score','?')}/5] {art.get('title','')}\n"
            f"  {art.get('source','')} — {art.get('link','')}"
        )
    plain_text = "\n".join(plain_lines)

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    # Envoi SMTP avec STARTTLS
    try:
        logger.info("🔌 Connexion à %s:%d…", SMTP_HOST, SMTP_PORT)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            logger.info("🔑 Authentification SMTP pour %s…", GMAIL_USER)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, to_addrs, msg.as_string())

        logger.info("✅ Email envoyé avec succès → %s", ", ".join(to_addrs))
        result["success"] = True

    except smtplib.SMTPAuthenticationError as exc:
        msg_err = (
            "Authentification Gmail échouée. Vérifiez GMAIL_USER et GMAIL_PASSWORD.\n"
            "Rappel : utilisez un mot de passe d'application, pas votre mot de passe Gmail.\n"
            f"Détail : {exc}"
        )
        logger.error("❌ %s", msg_err)
        result["error"] = msg_err
        raise MailerSendError(msg_err) from exc

    except smtplib.SMTPRecipientsRefused as exc:
        msg_err = f"Destinataire(s) refusé(s) : {', '.join(to_addrs)} — {exc}"
        logger.error("❌ %s", msg_err)
        result["error"] = msg_err
        raise MailerSendError(msg_err) from exc

    except smtplib.SMTPException as exc:
        msg_err = f"Erreur SMTP : {exc}"
        logger.error("❌ %s", msg_err)
        result["error"] = msg_err
        raise MailerSendError(msg_err) from exc

    except socket.timeout:
        msg_err = f"Timeout de connexion SMTP ({SMTP_HOST}:{SMTP_PORT})"
        logger.error("❌ %s", msg_err)
        result["error"] = msg_err
        raise MailerSendError(msg_err) from None

    except OSError as exc:
        msg_err = f"Erreur réseau : {exc}"
        logger.error("❌ %s", msg_err)
        result["error"] = msg_err
        raise MailerSendError(msg_err) from exc

    return result


# ---------------------------------------------------------------------------
# Exécution directe (debug / prévisualisation)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    data_path = os.path.join(os.path.dirname(__file__), "../data/ai_filter_output.json")

    if os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as fh:
            ai_data = json.load(fh)
        logger.info("📂 Chargé : %s", data_path)
    else:
        # Données de démonstration si pas de vrai fichier
        logger.warning("⚠️  Fichier introuvable, utilisation de données fictives")
        ai_data = {
            "meta": {
                "run_at": "2026-04-30T10:00:00+00:00",
                "model": "gemini-2.5-flash",
                "input_count": 48,
                "retained_count": 5,
                "tldr": "Cette semaine, une percée en HiPIMS pour les revêtements quaternaires TiAlSiN atteint 43 GPa de dureté, tandis qu'un procédé ALD basse température (60°C) ouvre la voie aux barrières sur polymères flexibles. Oerlikon Balzers annonce une nouvelle ligne DLC réduisant le frottement de 30% sur pièces automobiles, signal concurrentiel à suivre.",
            },
            "articles": [
                {
                    "score": 5, "category": "science",
                    "title": "Novel TiAlSiN quaternary coating via HiPIMS: 43 GPa hardness achieved",
                    "summary": "Researchers demonstrate a new quaternary hard coating deposited by High Power Impulse Magnetron Sputtering, reaching record-breaking hardness values through optimized N2 partial pressure.",
                    "source": "ArXiv – Materials Science",
                    "link": "https://arxiv.org/abs/example1",
                    "justification": "Nouveau revêtement quaternaire avec paramètres HiPIMS optimisés et dureté record mesurée.",
                    "tags": ["HiPIMS", "TiAlSiN", "dureté", "PVD"],
                    "collected_at": "2026-04-30",
                },
                {
                    "score": 4, "category": "science",
                    "title": "ALD of Al₂O₃ moisture barriers at 60°C for flexible electronics",
                    "summary": "A low-temperature ALD process yields conformal alumina barriers on polymer substrates with water vapor transmission rates below 10⁻⁴ g/m²/day.",
                    "source": "Nature Materials",
                    "link": "https://www.nature.com/example2",
                    "justification": "Procédé ALD basse température sur polymère avec perméabilité mesurée, pertinent pour l'encapsulation.",
                    "tags": ["ALD", "Al₂O₃", "barrière", "WVTR"],
                    "collected_at": "2026-04-29",
                },
                {
                    "score": 3, "category": "news",
                    "title": "Oerlikon unveils next-gen DLC coating line for automotive components",
                    "summary": "Oerlikon Balzers presents a new industrial PVD line optimized for DLC deposition on steel and aluminium automotive parts, reducing friction by up to 30%.",
                    "source": "Google News — Oerlikon",
                    "link": "https://www.oerlikon.com/example3",
                    "justification": "Annonce industrielle avec données de friction mesurées, pertinente pour la veille concurrentielle.",
                    "tags": ["DLC", "Oerlikon", "automotive", "friction"],
                    "collected_at": "2026-04-28",
                },
                {
                    "score": 2, "category": "science",
                    "title": "Color tuning of CrN coatings via bias voltage in DC magnetron sputtering",
                    "summary": "Adjusting substrate bias between -50 V and -200 V shifts CrN coating color from silver to gold tones, opening decorative PVD applications.",
                    "source": "ArXiv – Applied Physics",
                    "link": "https://arxiv.org/abs/example4",
                    "justification": "Effet couleur documenté sur CrN par variation de tension de polarisation, signal à confirmer.",
                    "tags": ["CrN", "couleur", "bias voltage", "PVD décoratif"],
                    "collected_at": "2026-04-27",
                },
            ],
        }

    # Prévisualisation HTML locale
    preview_path = os.path.join(os.path.dirname(__file__), "../data/digest_preview.html")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    html = build_html_email(ai_data)
    with open(preview_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info("👁️  Prévisualisation HTML → %s", preview_path)

    # Envoi réel ou dry_run selon les variables d'environnement
    if GMAIL_USER and GMAIL_PASSWORD:
        status = send_digest(ai_data)
    else:
        logger.info("🧪 GMAIL_USER/GMAIL_PASSWORD absents → dry_run activé")
        status = send_digest(ai_data, dry_run=True)

    print(f"\n{'='*50}")
    print(f"  Succès   : {status['success']}")
    print(f"  Sujet    : {status['subject']}")
    print(f"  Pour     : {status['recipient']}")
    print(f"  HTML     : {status['html_bytes']:,} octets")
    if status.get("error"):
        print(f"  Erreur   : {status['error']}")
    print(f"{'='*50}\n")
    if not status.get("error"):
        print(f"  → Ouvrez data/digest_preview.html dans votre navigateur pour prévisualiser.")
