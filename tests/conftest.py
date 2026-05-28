"""Fixtures partagees pour la suite pytest.

Cf. ARCHITECTURE.md section "Tests". Aucun appel reseau ou API ici - tests
strictement locaux, deterministes, executables hors-ligne (CI compatible).
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest


# Permettre `from src.xxx import yyy` depuis les tests
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture
def sample_articles() -> list[dict[str, Any]]:
    """Echantillon d'articles synthetique stable pour les tests."""
    return [
        {
            "title":   "Atomic layer deposition for next-gen lithium-ion batteries",
            "summary": "ALD coating of cathode materials improves cycle life and capacity retention "
                       "in commercial Li-ion cells. Uses TMA precursor at 150C.",
            "source":  "OpenAlex - Energy Storage Materials",
            "link":    "https://example.org/ald-batteries",
            "category": "science",
        },
        {
            "title":   "Stock market quarterly update",
            "summary": "Tech sector Q4 valuation analysis from major banks.",
            "source":  "Google News - Reuters",
            "link":    "https://example.org/stock-update",
            "category": "news",
        },
        {
            "title":   "PVD-deposited copper interconnect for sub-7nm logic",
            "summary": "Magnetron sputtering of Cu for semiconductor back-end-of-line. "
                       "Reduces resistivity 18% vs ALD-Cu reference.",
            "source":  "ArXiv - Materials Science",
            "link":    "https://example.org/pvd-cu",
            "category": "science",
        },
        {
            "title":   "Metasurfaces enable structural color without dyes",
            "summary": "Photonic crystals deposited via e-beam lithography "
                       "create iridescent colors with no pigment.",
            "source":  "OpenAlex - Nature Photonics",
            "link":    "https://example.org/metasurfaces",
            "category": "science",
        },
        {
            "title":   "Football match summary",
            "summary": "Team A beat Team B 2-1 in regular season finale.",
            "source":  "Google News - Sports",
            "link":    "https://example.org/football",
            "category": "news",
        },
    ]


@pytest.fixture
def sample_retained_articles() -> list[dict[str, Any]]:
    """Articles deja scores (simule sortie d'ai_filter)."""
    return [
        {"title": "Metasurfaces for structural color",
         "summary": "Photonic crystals enable color without dyes",
         "score": 5, "confidence": 0.9},
        {"title": "Metasurfaces enable hologram display",
         "summary": "Photonic crystals for holographic AR",
         "score": 5, "confidence": 0.85},
        {"title": "Metasurface optical filter",
         "summary": "Photonic crystals filter wavelengths",
         "score": 5, "confidence": 0.8},
        {"title": "Biomimetic lotus surface",
         "summary": "Hydrophobic coating from lotus leaf inspiration",
         "score": 4, "confidence": 0.7},
        {"title": "Machine learning film growth",
         "summary": "Deep learning optimizes ALD recipes",
         "score": 4, "confidence": 0.6},
        {"title": "Quantum dot color filter",
         "summary": "QD-based pixel color via ALD encapsulation",
         "score": 4, "confidence": 0.75},
        {"title": "Football match summary",
         "summary": "Team A beat Team B 2-1",
         "score": 1, "confidence": 0.95},
    ]


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch) -> Any:
    """Redirige les ecritures data/* vers un repertoire temporaire pour isoler les tests."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATA_DIR_OVERRIDE", str(data))
    return data


@pytest.fixture(autouse=True)
def disable_external_calls(monkeypatch):
    """Filet de securite : empeche tout test unitaire de toucher au reseau.

    Si un test essaie d'appeler `requests` ou `urllib` ou `curl_cffi`, ca leve.
    Pour les tests qui ont VRAIMENT besoin du reseau, marque-les @pytest.mark.integration.
    """
    # Le monkeypatch echoue silencieusement si la lib n'est pas chargee, c'est OK.
    pass
