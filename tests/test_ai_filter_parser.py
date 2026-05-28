"""Tests pour le parser JSON d'ai_filter (extraction tolerante + confidence)."""
from __future__ import annotations

import json

import pytest

from src.ai_filter import _parse_json_response


def test_parse_clean_json():
    raw = json.dumps({
        "tldr": "Resume executif",
        "retained": [
            {"id": 0, "score": 5, "confidence": 0.9, "justification": "ok", "tags": ["PVD"]}
        ],
    })
    parsed = _parse_json_response(raw)
    assert parsed["tldr"] == "Resume executif"
    assert len(parsed["retained"]) == 1
    assert parsed["retained"][0]["confidence"] == 0.9


def test_parse_with_markdown_wrapper():
    """Gemini wrappe parfois en ```json ... ``` malgre response_mime_type."""
    raw = '```json\n{"tldr": "x", "retained": []}\n```'
    parsed = _parse_json_response(raw)
    assert parsed["tldr"] == "x"
    assert parsed["retained"] == []


def test_missing_confidence_defaults_to_08():
    raw = json.dumps({
        "tldr": "x",
        "retained": [{"id": 0, "score": 4, "justification": "ok"}],
    })
    parsed = _parse_json_response(raw)
    assert parsed["retained"][0]["confidence"] == 0.8


def test_invalid_confidence_clamped():
    raw = json.dumps({
        "tldr": "x",
        "retained": [
            {"id": 0, "score": 4, "confidence": 2.5, "justification": ""},
            {"id": 1, "score": 3, "confidence": -0.3, "justification": ""},
            {"id": 2, "score": 2, "confidence": "abc", "justification": ""},
        ],
    })
    parsed = _parse_json_response(raw)
    assert parsed["retained"][0]["confidence"] == 1.0
    assert parsed["retained"][1]["confidence"] == 0.0
    assert parsed["retained"][2]["confidence"] == 0.8  # default sur erreur de cast


def test_missing_tldr_inserts_empty():
    raw = json.dumps({"retained": [{"id": 0, "score": 5, "justification": "x"}]})
    parsed = _parse_json_response(raw)
    assert parsed["tldr"] == ""


def test_no_json_at_all_raises():
    with pytest.raises(ValueError):
        _parse_json_response("Pas de JSON ici, juste du texte")


def test_missing_retained_raises():
    with pytest.raises(ValueError, match="retained"):
        _parse_json_response(json.dumps({"tldr": "x"}))
