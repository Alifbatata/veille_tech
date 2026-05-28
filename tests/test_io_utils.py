"""Tests pour src/io_utils.py (atomic writes + safe reads)."""
from __future__ import annotations

import json
import os

import pytest

from src.io_utils import atomic_write_json, safe_read_json


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(str(target), {"a": 1, "b": [2, 3]})
    assert target.exists()
    with open(target, "r", encoding="utf-8") as f:
        assert json.load(f) == {"a": 1, "b": [2, 3]}


def test_atomic_write_overwrites_atomically(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(str(target), {"v": 1})
    atomic_write_json(str(target), {"v": 2})
    with open(target, "r", encoding="utf-8") as f:
        assert json.load(f) == {"v": 2}
    # Pas de fichier .tmp residuel
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == [], f"Fichiers tmp residuels : {tmps}"


def test_atomic_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deeper" / "data.json"
    atomic_write_json(str(target), {"ok": True})
    assert target.exists()


def test_safe_read_missing_returns_default():
    result = safe_read_json("/path/that/does/not/exist.json", default={"empty": True})
    assert result == {"empty": True}


def test_safe_read_corrupt_returns_default(tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    result = safe_read_json(str(bad), default=[])
    assert result == []


def test_safe_read_valid(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"x": 42}', encoding="utf-8")
    result = safe_read_json(str(good), default=None)
    assert result == {"x": 42}


def test_atomic_write_unicode(tmp_path):
    target = tmp_path / "unicode.json"
    payload = {"name": "Bühler Leybold", "topic": "Métasurfaces ⚛", "list": ["École", "PVD/ALD"]}
    atomic_write_json(str(target), payload)
    with open(target, "r", encoding="utf-8") as f:
        assert json.load(f) == payload
