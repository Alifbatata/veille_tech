"""Tests pour src/user_profiles.py."""
from __future__ import annotations

import pytest

from src import user_profiles


def test_load_profile_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(user_profiles, "_PROFILES_PATH", str(tmp_path / "p.json"))
    profile = user_profiles.load_user_profile("anyone@example.com")
    assert profile == {}


def test_load_profile_case_insensitive(tmp_path, monkeypatch):
    profiles_path = tmp_path / "p.json"
    profiles_path.write_text(
        '{"profiles": {"Alice@Example.COM": {"boost_keywords": ["DLC"]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(user_profiles, "_PROFILES_PATH", str(profiles_path))
    profile = user_profiles.load_user_profile("alice@example.com")
    assert profile.get("boost_keywords") == ["DLC"]


def test_apply_personalization_empty_profile_no_change():
    sample = [
        {"title": "A", "score": 5},
        {"title": "B", "score": 4},
    ]
    result = user_profiles.apply_user_personalization(sample, {})
    assert len(result) == 2
    # Ordre preserve
    assert result[0]["title"] == "A"


def test_apply_personalization_boost_keyword():
    sample = [
        {"title": "Generic article", "summary": "", "score": 4},
        {"title": "Article with DLC keyword", "summary": "DLC coating tribology", "score": 4},
    ]
    profile = {"boost_keywords": ["DLC"]}
    result = user_profiles.apply_user_personalization(sample, profile)
    # L'article avec DLC doit etre boost et passer en premier
    assert "DLC" in result[0]["title"]
    assert result[0]["personalization_boost"] > 0
    assert result[1]["personalization_boost"] == 0


def test_apply_personalization_penalty_keyword():
    sample = [
        {"title": "Medical biocompatible coating", "summary": "medical applications", "score": 5},
        {"title": "DLC tribology", "summary": "", "score": 4},
    ]
    profile = {"penalty_keywords": ["medical"]}
    result = user_profiles.apply_user_personalization(sample, profile)
    # Medical article doit etre penalise → effective_score plus bas que 5
    medical = next(a for a in result if "Medical" in a["title"])
    assert medical["personalization_boost"] < 0


def test_min_score_override(tmp_path, monkeypatch):
    sample = [
        {"title": "5★", "score": 5},
        {"title": "3★", "score": 3},
        {"title": "1★", "score": 1},
    ]
    profile = {"min_score": 4}
    result = user_profiles.apply_user_personalization(sample, profile)
    assert all(a.get("score", 0) >= 4 for a in result)
    assert len(result) == 1


def test_list_profile_emails(tmp_path, monkeypatch):
    profiles_path = tmp_path / "p.json"
    profiles_path.write_text(
        '{"profiles": {"a@x.com": {}, "b@y.com": {}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(user_profiles, "_PROFILES_PATH", str(profiles_path))
    emails = user_profiles.list_profile_emails()
    assert "a@x.com" in emails
    assert "b@y.com" in emails
