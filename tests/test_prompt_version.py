"""Tests pour src/prompt_version.py."""
from __future__ import annotations

import pytest

from src import prompt_version


def test_compute_version_stable():
    p = "You are an AI judge."
    assert prompt_version.compute_prompt_version(p) == prompt_version.compute_prompt_version(p)


def test_compute_version_short_hex():
    v = prompt_version.compute_prompt_version("test")
    assert len(v) == 8
    assert all(c in "0123456789abcdef" for c in v)


def test_compute_version_changes_on_change():
    v1 = prompt_version.compute_prompt_version("Prompt A")
    v2 = prompt_version.compute_prompt_version("Prompt B")
    assert v1 != v2


def test_register_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_version, "_VERSIONS_PATH", str(tmp_path / "v.json"))
    v1 = prompt_version.register_prompt("Test prompt", "test")
    v2 = prompt_version.register_prompt("Test prompt", "test")
    assert v1 == v2


def test_register_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_version, "_VERSIONS_PATH", str(tmp_path / "v.json"))
    prompt_version.register_prompt("Prompt A", "alpha")
    prompt_version.register_prompt("Prompt B", "beta")
    from src.io_utils import safe_read_json
    data = safe_read_json(str(tmp_path / "v.json"), default={})
    assert "versions" in data
    assert len(data["versions"]) == 2


def test_compute_version_empty():
    # Doit pas crasher sur empty
    v = prompt_version.compute_prompt_version("")
    assert len(v) == 8
    v2 = prompt_version.compute_prompt_version(None)  # type: ignore
    assert len(v2) == 8
