"""Tests pour src/epo_enrichment.py (EPO OPS, sans appel reseau)."""
from __future__ import annotations

import pytest

from src import epo_enrichment


def test_to_epodoc_us():
    assert epo_enrichment._to_epodoc("US10867856") == "US.10867856"


def test_to_epodoc_ep_with_kind():
    assert epo_enrichment._to_epodoc("EP3456789A1") == "EP.3456789.A1"


def test_to_epodoc_wo():
    assert epo_enrichment._to_epodoc("WO2020012345") == "WO.2020012345"


def test_to_epodoc_invalid():
    assert epo_enrichment._to_epodoc("") is None
    assert epo_enrichment._to_epodoc("invalid") is None
    assert epo_enrichment._to_epodoc(None) is None  # type: ignore


def test_enrich_patents_disabled_no_op(monkeypatch):
    monkeypatch.setattr(epo_enrichment, "EPO_ENRICHMENT_ENABLED", False)
    articles = [{"category": "patent", "score": 5, "patent_pub_num": "EP123"}]
    result = epo_enrichment.enrich_patents(articles)
    assert result == 0
    # Aucune mutation ne doit avoir lieu si desactive
    assert "epo_citations_count" not in articles[0]


def test_enrich_patents_empty():
    assert epo_enrichment.enrich_patents([]) == 0


def test_cache_freshness(tmp_path, monkeypatch):
    """Une entree datant de < TTL est consideree fresh, > TTL stale."""
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(epo_enrichment, "_CACHE_TTL_DAYS", 60)

    fresh = {"enriched_at": datetime.now(timezone.utc).isoformat()}
    assert epo_enrichment._cache_is_fresh(fresh) is True

    old = {"enriched_at": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}
    assert epo_enrichment._cache_is_fresh(old) is False

    no_date = {}
    assert epo_enrichment._cache_is_fresh(no_date) is False
