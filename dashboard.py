"""Dashboard local Streamlit pour explorer la veille technologique.

Lancement : streamlit run dashboard.py
Installation prealable : pip install streamlit pandas

Tout local, aucune donnee envoyee a un service externe. Lit :
  - data/articles_archive.json   (archive cumulative)
  - data/score_calibration.json  (distribution scores inter-runs)
  - data/query_stats.json        (productivite requetes)
  - data/discovered_actors.json  (acteurs decouverts auto)
  - data/feedback_history.json   (feedback utilisateur)
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Streamlit optionnel : pas d'import si lance hors-Streamlit (echec gracieux)
try:
    import streamlit as st  # type: ignore
except ImportError:
    print("Erreur : streamlit non installe. Lance : pip install streamlit pandas")
    print("Puis : streamlit run dashboard.py")
    sys.exit(1)

try:
    import pandas as pd  # type: ignore
except ImportError:
    pd = None  # type: ignore


DATA_DIR = Path(__file__).parent / "data"


@st.cache_data(ttl=60)
def load_json(path: str) -> dict | list | None:
    full = DATA_DIR / path
    if not full.exists():
        return None
    try:
        with open(full, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def page_overview():
    st.title("📊 Veille Technologique — Dashboard")
    st.caption("Local · aucune donnee envoyee a un service externe")

    archive = load_json("articles_archive.json")
    calib = load_json("score_calibration.json")
    actors = load_json("discovered_actors.json")
    feedback = load_json("feedback_history.json")

    col1, col2, col3, col4 = st.columns(4)
    n_archive = len(archive.get("articles", [])) if isinstance(archive, dict) else 0
    col1.metric("Articles archives", f"{n_archive:,}")

    n_runs = len(calib.get("history", [])) if isinstance(calib, dict) else 0
    col2.metric("Runs traces", n_runs)

    n_actors = len(actors.get("actors", {})) if isinstance(actors, dict) else 0
    col3.metric("Acteurs decouverts", n_actors)

    n_feedback = len(feedback.get("feedbacks", [])) if isinstance(feedback, dict) else 0
    col4.metric("Feedbacks user", n_feedback)

    # Drift warning
    if isinstance(calib, dict) and calib.get("last_drift_warning"):
        st.warning(f"⚠️ Calibration drift : {calib['last_drift_warning']}")

    # Distribution scores du dernier run
    if isinstance(calib, dict) and calib.get("history"):
        latest = calib["history"][-1]
        by_score = latest.get("by_score", {})
        if by_score and pd is not None:
            st.subheader("Distribution scores — dernier run")
            df = pd.DataFrame([
                {"Score": f"{s}★", "Articles": v}
                for s, v in sorted(by_score.items())
            ])
            st.bar_chart(df.set_index("Score"))


def page_distribution_over_time():
    st.title("📈 Distribution des scores sur le temps")
    calib = load_json("score_calibration.json")
    if not isinstance(calib, dict) or not calib.get("history"):
        st.info("Pas de donnees historiques (score_calibration.json absent).")
        return
    if pd is None:
        st.error("pandas requis : pip install pandas")
        return

    rows = []
    for entry in calib["history"]:
        date = entry.get("date", "")[:10]
        by_score = entry.get("by_score", {})
        for s in ["1", "2", "3", "4", "5"]:
            rows.append({"date": date, "score": f"{s}★", "count": by_score.get(s, 0)})
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("Aucune entree.")
        return

    pivot = df.pivot_table(index="date", columns="score", values="count", aggfunc="sum", fill_value=0)
    st.line_chart(pivot)
    st.caption("Si la part de 5★ explose / s'effondre, calibration drift potentielle.")


def page_top_articles():
    st.title("🏆 Top articles archive")
    archive = load_json("articles_archive.json")
    if not isinstance(archive, dict) or not archive.get("articles"):
        st.info("Archive vide ou inaccessible.")
        return
    articles = archive["articles"]
    if pd is None:
        st.error("pandas requis : pip install pandas")
        return

    df = pd.DataFrame([{
        "Score":   a.get("score", "-"),
        "Titre":   (a.get("title") or "")[:120],
        "Source":  (a.get("source") or "")[:40],
        "Date":    (a.get("collected_at") or "")[:10],
        "Confiance": round(float(a.get("confidence", 0)), 2) if a.get("confidence") is not None else "-",
        "Prompt v.": a.get("scoring_prompt_version", "-"),
        "URL":     a.get("link", ""),
    } for a in articles])

    # Filtres
    col1, col2 = st.columns(2)
    min_score = col1.slider("Score minimum", 1, 5, 4)
    search = col2.text_input("Recherche dans titre", "")

    filtered = df[df["Score"] >= min_score]
    if search:
        filtered = filtered[filtered["Titre"].str.contains(search, case=False, na=False)]

    st.caption(f"{len(filtered)} articles sur {len(df)} total")
    st.dataframe(filtered.sort_values("Score", ascending=False), use_container_width=True)


def page_actors():
    st.title("🔍 Acteurs decouverts automatiquement")
    actors = load_json("discovered_actors.json")
    if not isinstance(actors, dict) or not actors.get("actors"):
        st.info("Aucun acteur decouvert (discovered_actors.json absent).")
        return
    if pd is None:
        st.error("pandas requis")
        return

    rows = []
    for norm, e in actors["actors"].items():
        rows.append({
            "Nom":              e.get("name", norm),
            "Occurrences":      e.get("count", 0),
            "Apparu sur N runs": e.get("appearances_runs", 1),
            "Sources":          ", ".join(e.get("sources", [])),
            "Vu en premier":    (e.get("first_seen") or "")[:10],
            "Vu en dernier":    (e.get("last_seen") or "")[:10],
        })
    df = pd.DataFrame(rows).sort_values(["Occurrences", "Apparu sur N runs"], ascending=False)
    st.caption(f"{len(df)} acteurs cumules · seuil auto-promote : count >= 5 ET apparitions >= 2")
    st.dataframe(df, use_container_width=True)


def page_query_stats():
    st.title("🔬 Productivite des requetes")
    stats = load_json("query_stats.json")
    if not isinstance(stats, dict) or not stats.get("queries"):
        st.info("Pas de stats de requetes (query_stats.json absent).")
        return
    if pd is None:
        return

    rows = []
    for key, e in stats["queries"].items():
        rows.append({
            "Requete":           e.get("query", "")[:80],
            "Source":            e.get("source", ""),
            "Hits cumules":      e.get("hits_total", 0),
            "Hits dernier run":  e.get("hits_last_run", 0),
            "Runs":              e.get("runs_total", 0),
            "Zeros consecutifs": e.get("consecutive_zeros", 0),
        })
    df = pd.DataFrame(rows)
    st.caption(f"{len(df)} requetes cumulees · auto-purge : runs >= 8 ET hits == 0 ET zeros >= 8")
    st.dataframe(df.sort_values("Hits cumules", ascending=False), use_container_width=True)


def page_feedback():
    st.title("👍/👎 Historique feedback utilisateur")
    fb = load_json("feedback_history.json")
    if not isinstance(fb, dict) or not fb.get("feedbacks"):
        st.info("Aucun feedback enregistre. Clique sur 👍/👎 dans les emails pour calibrer l'IA.")
        return
    if pd is None:
        return

    rows = []
    for e in fb.get("feedbacks", []):
        rows.append({
            "Date":           (e.get("date") or "")[:10],
            "Rating":         "👍" if e.get("rating") == "up" else "👎",
            "Titre":          (e.get("title") or e.get("article_id") or "?")[:100],
            "Score original": e.get("original_score", "-"),
            "Source":         e.get("source", "?"),
        })
    df = pd.DataFrame(rows).sort_values("Date", ascending=False)
    st.dataframe(df, use_container_width=True)

    up_count = sum(1 for e in fb["feedbacks"] if e.get("rating") == "up")
    down_count = sum(1 for e in fb["feedbacks"] if e.get("rating") == "down")
    col1, col2 = st.columns(2)
    col1.metric("👍 Pertinents", up_count)
    col2.metric("👎 Non pertinents", down_count)


def main():
    st.set_page_config(
        page_title="Veille Tech — Dashboard",
        page_icon="📊",
        layout="wide",
    )

    pages = {
        "📊 Vue d'ensemble":           page_overview,
        "📈 Distribution dans le temps": page_distribution_over_time,
        "🏆 Top articles archive":      page_top_articles,
        "🔍 Acteurs decouverts":        page_actors,
        "🔬 Stats requetes":            page_query_stats,
        "👍 Feedback utilisateur":      page_feedback,
    }
    choice = st.sidebar.radio("Navigation", list(pages.keys()))
    st.sidebar.caption("\n\n_Toutes les donnees sont locales._")
    if st.sidebar.button("🔄 Recharger les donnees"):
        load_json.clear()
    pages[choice]()


if __name__ == "__main__":
    main()
