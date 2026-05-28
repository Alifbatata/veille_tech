"""Tests pour src/feedback.py (boucle feedback utilisateur)."""
from __future__ import annotations

import pytest

from src import feedback


def test_compute_article_id_stable():
    art = {"link": "https://example.com/paper1", "title": "Test"}
    id1 = feedback.compute_article_id(art)
    id2 = feedback.compute_article_id(art)
    assert id1 == id2
    assert len(id1) == 12


def test_compute_article_id_different_urls():
    a = feedback.compute_article_id({"link": "https://example.com/a"})
    b = feedback.compute_article_id({"link": "https://example.com/b"})
    assert a != b


def test_make_mailto_link_basic(monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_RECIPIENT", "user@example.com")
    art = {"link": "https://example.org/x", "title": "Test article"}
    link_up = feedback.make_mailto_link(art, "up")
    link_down = feedback.make_mailto_link(art, "down")
    assert link_up.startswith("mailto:")
    assert "user%40example.com" in link_up
    assert "rating%3Dup" in link_up
    assert "rating%3Ddown" in link_down


def test_make_mailto_link_no_recipient_returns_empty(monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_RECIPIENT", "")
    art = {"link": "https://x.com", "title": "y"}
    assert feedback.make_mailto_link(art, "up") == ""


def test_record_and_load_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_FEEDBACK_PATH", str(tmp_path / "fb.json"))
    monkeypatch.setattr(feedback, "FEEDBACK_ENABLED", True)
    feedback.record_feedback("abc123def456", "up", "manual", {"title": "Cool article", "summary": "X", "score": 5})
    feedback.record_feedback("xyz999", "down", "manual", {"title": "Bad article", "summary": "Y", "score": 3})
    examples = feedback.get_few_shot_examples()
    assert len(examples["up"]) == 1
    assert len(examples["down"]) == 1
    assert examples["up"][0]["title"] == "Cool article"
    assert examples["down"][0]["title"] == "Bad article"


def test_record_idempotent_overwrites_previous_rating(tmp_path, monkeypatch):
    """Si l'utilisateur change d'avis, le nouveau feedback ecrase l'ancien."""
    monkeypatch.setattr(feedback, "_FEEDBACK_PATH", str(tmp_path / "fb.json"))
    monkeypatch.setattr(feedback, "FEEDBACK_ENABLED", True)
    feedback.record_feedback("abc12", "up", "manual", {"title": "T", "summary": "S"})
    feedback.record_feedback("abc12", "down", "manual", {"title": "T", "summary": "S"})
    examples = feedback.get_few_shot_examples()
    assert len(examples["up"]) == 0
    assert len(examples["down"]) == 1


def test_build_few_shot_prompt_empty_when_no_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_FEEDBACK_PATH", str(tmp_path / "fb.json"))
    monkeypatch.setattr(feedback, "FEEDBACK_ENABLED", True)
    section = feedback.build_few_shot_prompt_section()
    assert section == ""


def test_build_few_shot_prompt_with_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_FEEDBACK_PATH", str(tmp_path / "fb.json"))
    monkeypatch.setattr(feedback, "FEEDBACK_ENABLED", True)
    feedback.record_feedback("a1", "up", "manual", {"title": "Excellent paper", "summary": "Top", "score": 5})
    feedback.record_feedback("a2", "down", "manual", {"title": "Bad piece", "summary": "Marketing", "score": 4})
    section = feedback.build_few_shot_prompt_section()
    assert "CALIBRATION" in section
    assert "Excellent paper" in section
    assert "Bad piece" in section


def test_subject_regex_parses_correctly():
    """Le regex doit extraire rating et id depuis le subject."""
    m = feedback._SUBJECT_RE.search("[FEEDBACK] rating=up id=abc123def456")
    assert m is not None
    assert m.group(1) == "up"
    assert m.group(2) == "abc123def456"

    m = feedback._SUBJECT_RE.search("[FEEDBACK] rating=DOWN id=ABCDEF123456")
    assert m is not None
    assert m.group(1).lower() == "down"

    m = feedback._SUBJECT_RE.search("Re: [FEEDBACK] rating=up id=abc123def456 confirmed")
    assert m is not None  # gere les replies
