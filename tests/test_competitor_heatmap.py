"""Tests pour src/competitor_heatmap.py."""
from __future__ import annotations

import pytest

from src import competitor_heatmap


def test_count_mentions_basic():
    articles = [
        {"title": "Aixtron files ALD patent", "summary": "Aixtron AG", "category": "patent"},
        {"title": "Industry trends 2026", "summary": "Veeco and Applied Materials...", "category": "news"},
        {"title": "Football", "summary": "Sports", "category": "news"},
    ]
    counts = competitor_heatmap.count_mentions_in_articles(articles, ["Aixtron", "Veeco", "Picosun"])
    assert counts["Aixtron"] == 1
    assert counts["Veeco"] == 1
    assert counts["Picosun"] == 0


def test_count_mentions_case_insensitive():
    articles = [{"title": "AIXTRON announces", "summary": "aixtron technical update", "category": "news"}]
    counts = competitor_heatmap.count_mentions_in_articles(articles, ["Aixtron"])
    assert counts["Aixtron"] == 1


def test_count_mentions_uses_patent_assignee():
    articles = [{
        "title": "Generic patent title",
        "summary": "Some content",
        "category": "patent",
        "patent_assignee": "Aixtron AG",
    }]
    counts = competitor_heatmap.count_mentions_in_articles(articles, ["Aixtron"])
    assert counts["Aixtron"] == 1


def test_compute_heatmap_empty():
    result = competitor_heatmap.compute_heatmap([], ["Aixtron"])
    assert result["current_run"] == {}
    assert result["anomalies"] == []


def test_compute_heatmap_basic(tmp_path, monkeypatch):
    monkeypatch.setattr(competitor_heatmap, "_HISTORY_PATH", str(tmp_path / "h.json"))
    articles = [
        {"title": "Aixtron patent", "summary": "Aixtron AG ALD", "category": "patent",
         "patent_assignee": "Aixtron AG"},
        {"title": "Veeco news", "summary": "Veeco Inc reports", "category": "news"},
    ]
    result = competitor_heatmap.compute_heatmap(articles, ["Aixtron", "Veeco", "Picosun"])
    assert "Aixtron" in result["current_run"]
    assert "Veeco" in result["current_run"]
    # Pas d'historique : pas d'anomalies au premier run
    assert isinstance(result["anomalies"], list)
    assert result["current_run"]["Aixtron"]["patents"] == 1


def test_compute_heatmap_detects_anomaly(tmp_path, monkeypatch):
    """Si X est mentionne 5x ce run vs moyenne 0 historique, anomalie up."""
    monkeypatch.setattr(competitor_heatmap, "_HISTORY_PATH", str(tmp_path / "h.json"))
    monkeypatch.setattr(competitor_heatmap, "HEATMAP_MIN_MENTIONS", 2)
    monkeypatch.setattr(competitor_heatmap, "HEATMAP_ANOMALY_RATIO", 2.0)

    # Run 1, 2, 3 : zero mention
    for _ in range(3):
        competitor_heatmap.compute_heatmap(
            [{"title": "Generic article", "summary": "no competitor", "category": "news"}],
            ["Aixtron"],
        )

    # Run 4 : 3 mentions Aixtron → anomalie attendue
    articles_anom = [
        {"title": "Aixtron grows", "summary": "Aixtron new product", "category": "news"},
        {"title": "Aixtron patent", "summary": "Aixtron AG patent", "category": "patent",
         "patent_assignee": "Aixtron AG"},
        {"title": "Aixtron Q4", "summary": "Aixtron earnings", "category": "news"},
    ]
    result = competitor_heatmap.compute_heatmap(articles_anom, ["Aixtron"])
    assert len(result["anomalies"]) == 1
    assert result["anomalies"][0]["name"] == "Aixtron"
    assert result["anomalies"][0]["direction"] == "up"
