"""Tests pour src/dedup.py (deduplication semantique)."""
from __future__ import annotations

import pytest

from src import dedup


def test_dedup_disabled_returns_unchanged(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", False)
    articles = [
        {"title": "A", "summary": "x"},
        {"title": "B", "summary": "y"},
    ]
    kept, removed = dedup.deduplicate_semantically(articles)
    assert kept == articles
    assert removed == 0


def test_dedup_empty_returns_empty():
    kept, removed = dedup.deduplicate_semantically([])
    assert kept == []
    assert removed == 0


def test_dedup_unique_articles_preserved(monkeypatch):
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    articles = [
        {"title": "Atomic layer deposition for batteries",
         "summary": "ALD coating improves cathode cycle life and capacity"},
        {"title": "Football match summary",
         "summary": "Team A beat Team B in regular season finale"},
        {"title": "Quantum dots for display tech",
         "summary": "QD-based pixel color via ALD encapsulation method"},
    ]
    kept, removed = dedup.deduplicate_semantically(articles, threshold=0.85)
    assert removed == 0
    assert len(kept) == 3


def test_dedup_detects_similar_articles(monkeypatch):
    """Deux articles tres similaires sur le meme paper doivent etre regroupes."""
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    articles = [
        {"title": "ALD coating improves Li-ion battery cycle life by 40%",
         "summary": "Atomic layer deposition of alumina coating increases lithium ion battery cycle life by 40 percent",
         "category": "science",
         "collected_at": "2026-05-20T00:00:00Z",
         "source": "ArXiv"},
        {"title": "ALD coating boosts Li-ion battery cycle life 40%",
         "summary": "Atomic layer deposition of alumina coating extends lithium-ion battery cycle life by 40 percent",
         "category": "science",
         "collected_at": "2026-05-22T00:00:00Z",
         "source": "Crossref"},
    ]
    kept, removed = dedup.deduplicate_semantically(articles, threshold=0.70)
    # Les deux articles parlent du meme paper -> doivent etre dedupliques
    assert removed == 1
    assert len(kept) == 1


def test_dedup_keeps_patent_priority(monkeypatch):
    """Dans un groupe de doublons, l'article 'patent' doit etre garde."""
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    articles = [
        {"title": "TiN sputtering target for hard coatings",
         "summary": "Titanium nitride sputtering target manufactured for hard PVD coatings",
         "category": "science",
         "collected_at": "2026-05-20T00:00:00Z"},
        {"title": "TiN sputtering target patent for hard coatings",
         "summary": "Titanium nitride sputtering target manufactured for hard PVD coatings",
         "category": "patent",
         "collected_at": "2026-05-21T00:00:00Z"},
    ]
    kept, removed = dedup.deduplicate_semantically(articles, threshold=0.70)
    assert removed == 1
    assert kept[0]["category"] == "patent"


def test_pick_best_prefers_longer_summary():
    articles = [
        {"title": "X", "summary": "short", "category": "science"},
        {"title": "X", "summary": "much much longer summary with more details here", "category": "science"},
    ]
    best = dedup._pick_best_in_group(articles)
    assert best == 1


def test_pick_best_empty_handled():
    assert dedup._pick_best_in_group([]) == 0


def test_dedup_handles_empty_summary_articles(monkeypatch):
    """Les articles vides ne doivent pas crasher la dedup."""
    monkeypatch.setattr(dedup, "DEDUP_ENABLED", True)
    articles = [
        {"title": "A", "summary": ""},
        {"title": "B", "summary": ""},
        {"title": "Long valid article",
         "summary": "Atomic layer deposition for next-gen lithium-ion batteries"},
    ]
    kept, removed = dedup.deduplicate_semantically(articles)
    # Les vides sont gardes (trop courts pour dedup), le long aussi
    assert len(kept) >= 2
