"""Tests pour src/auto_tuner.py (auto-purge, auto-promote v2, tiers)."""
from __future__ import annotations

import pytest

from src import auto_tuner


class TestAggressiveNormalize:
    def test_strip_quotes(self):
        assert auto_tuner._aggressive_normalize('"physical vapor deposition"') == "physical vapor deposition"

    def test_lowercase_collapse_whitespace(self):
        assert auto_tuner._aggressive_normalize("  EPFL  ") == "epfl"
        assert auto_tuner._aggressive_normalize("Atomic   Layer\tDeposition") == "atomic layer deposition"

    def test_handle_none_empty(self):
        assert auto_tuner._aggressive_normalize(None) == ""
        assert auto_tuner._aggressive_normalize("") == ""


class TestClassifyActor:
    def test_lab_keywords_to_research_org(self):
        assert auto_tuner._classify_actor("Stanford University", ["openalex"]) == "research_org"
        assert auto_tuner._classify_actor("EPFL", ["openalex"]) == "research_org"
        assert auto_tuner._classify_actor("Fraunhofer IWS", ["openalex"]) == "research_org"

    def test_company_suffixes_to_company(self):
        assert auto_tuner._classify_actor("Aixtron AG", ["patents"]) == "company"
        assert auto_tuner._classify_actor("Applied Materials Inc.", ["patents"]) == "company"

    def test_source_fallback(self):
        # OpenAlex seul → research_org par default
        assert auto_tuner._classify_actor("UnknownEntity", ["openalex"]) == "research_org"
        # Patents seul → company par default
        assert auto_tuner._classify_actor("UnknownEntity", ["patents"]) == "company"


class TestIsTargetSterile:
    def test_sterile_with_sufficient_history(self):
        fixtures = {
            "test|||openalex": {
                "query": "test target",
                "hits_total": 0,
                "runs_total": 10,
                "consecutive_zeros": 10,
            },
        }
        index = auto_tuner._index_queries_by_target(fixtures)
        assert auto_tuner._is_target_sterile("test target", index) is True

    def test_too_young_not_sterile(self):
        fixtures = {
            "test|||openalex": {
                "query": "test target",
                "hits_total": 0,
                "runs_total": 3,
                "consecutive_zeros": 3,
            },
        }
        index = auto_tuner._index_queries_by_target(fixtures)
        assert auto_tuner._is_target_sterile("test target", index) is False

    def test_productive_not_sterile(self):
        fixtures = {
            "test|||openalex": {
                "query": "test target",
                "hits_total": 50,
                "runs_total": 10,
                "consecutive_zeros": 0,
            },
        }
        index = auto_tuner._index_queries_by_target(fixtures)
        assert auto_tuner._is_target_sterile("test target", index) is False

    def test_one_source_productive_protects_target(self):
        """Cas critique : une cible productive sur Crossref mais sterile sur
        OpenAlex ne doit PAS etre purgee."""
        fixtures = {
            "test|||openalex": {
                "query": "test target",
                "hits_total": 0,
                "runs_total": 10,
                "consecutive_zeros": 10,
            },
            "test|||crossref": {
                "query": "test target",
                "hits_total": 5,
                "runs_total": 10,
                "consecutive_zeros": 0,
            },
        }
        index = auto_tuner._index_queries_by_target(fixtures)
        assert auto_tuner._is_target_sterile("test target", index) is False


class TestComputeMaxResults:
    def test_returns_base_for_unknown_query(self):
        result = auto_tuner.compute_max_results("xxx_not_in_history", "openalex", 25)
        assert result == 25

    def test_disabled_returns_base(self, monkeypatch):
        monkeypatch.setattr(auto_tuner, "AUTO_EXPAND_ENABLED", False)
        auto_tuner.invalidate_tier_cache()
        result = auto_tuner.compute_max_results("anything", "openalex", 25)
        assert result == 25
