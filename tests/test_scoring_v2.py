"""Tests pour src/scoring_v2.py (BM25 pre-rank + MMR + calibration)."""
from __future__ import annotations

import pytest

from src import scoring_v2


class TestTokenize:
    def test_basic(self):
        toks = scoring_v2._tokenize("Hello WORLD!")
        assert toks == ["hello", "world"]

    def test_preserves_short_acronyms(self):
        """Critique : PVD, ALD, CVD sont 3 chars mais font tout le signal."""
        toks = scoring_v2._tokenize("PVD and ALD coating")
        assert "pvd" in toks
        assert "ald" in toks

    def test_empty(self):
        assert scoring_v2._tokenize("") == []
        assert scoring_v2._tokenize(None) == []


class TestBuildTargetProfile:
    def test_includes_all_lists(self):
        profile = scoring_v2.build_target_profile(
            ["Aixtron"], ["PVD"], ["thin film"], ["EPFL"], ["metasurfaces"],
        )
        assert "Aixtron" in profile
        assert "PVD" in profile
        assert "EPFL" in profile
        assert "metasurfaces" in profile

    def test_empty_lists(self):
        profile = scoring_v2.build_target_profile([], [], [], [], [])
        # Le profil de base (PVD/CVD/ALD anchor) doit toujours etre present
        assert "PVD" in profile or "deposition" in profile


class TestDetectRankerKind:
    def test_returns_valid_kind(self):
        # Reset cache pour test pur
        scoring_v2._ranker_kind = None
        kind = scoring_v2._detect_ranker_kind()
        assert kind in ("bm25", "tfidf", "none")


class TestPreRankArticles:
    def test_discriminates_technical_vs_offtopic(self, sample_articles):
        if scoring_v2._detect_ranker_kind() == "none":
            pytest.skip("Pas de ranker dispo")
        profile = scoring_v2.build_target_profile(
            ["Aixtron"], ["PVD", "ALD"], ["magnetron sputtering"], [], [],
        )
        kept, rejected = scoring_v2.pre_rank_articles(sample_articles, profile)
        # Football doit avoir une similarite tres basse vs ALD/PVD
        football = next(a for a in sample_articles if "Football" in a["title"])
        pvd = next(a for a in sample_articles if "PVD" in a["title"])
        assert football["prerank_similarity"] < pvd["prerank_similarity"]

    def test_keeps_at_least_top_fraction(self, sample_articles):
        if scoring_v2._detect_ranker_kind() == "none":
            pytest.skip("Pas de ranker dispo")
        profile = scoring_v2.build_target_profile([], ["PVD"], [], [], [])
        # Avec keep_top_fraction=0.80, on garde au moins 4 sur 5 articles
        kept, _ = scoring_v2.pre_rank_articles(sample_articles, profile)
        assert len(kept) >= int(len(sample_articles) * scoring_v2.PRERANK_KEEP_TOP_FRACTION)

    def test_empty_input_returns_empty(self):
        kept, rejected = scoring_v2.pre_rank_articles([], "anything")
        assert kept == []
        assert rejected == 0


class TestComputeMMRRanking:
    def test_diversifies_redundant_articles(self, sample_retained_articles):
        if scoring_v2._detect_ranker_kind() == "none":
            pytest.skip("Pas de ranker dispo")
        reordered = scoring_v2.compute_mmr_ranking(
            sample_retained_articles, lambda_param=0.3, top_k=6,
        )
        # Avec 3 articles "Metasurfaces" en tete, MMR avec lambda bas
        # ne doit PAS placer 3 metasurfaces consecutivement
        first3 = [a["title"] for a in reordered[:3]]
        meta_count = sum(1 for t in first3 if "Metasurface" in t)
        assert meta_count < 3, f"MMR n'a pas diversifie : {first3}"

    def test_preserves_count(self, sample_retained_articles):
        reordered = scoring_v2.compute_mmr_ranking(sample_retained_articles)
        assert len(reordered) == len(sample_retained_articles)

    def test_single_article_returns_unchanged(self):
        articles = [{"title": "X", "summary": "Y", "score": 5}]
        assert scoring_v2.compute_mmr_ranking(articles) == articles

    def test_empty_returns_empty(self):
        assert scoring_v2.compute_mmr_ranking([]) == []


class TestTrackScoreDistribution:
    def test_correct_counts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scoring_v2, "_SCORE_CALIBRATION_PATH",
                            str(tmp_path / "calib.json"))
        sample = [
            {"score": 5}, {"score": 5}, {"score": 4}, {"score": 4},
            {"score": 3}, {"score": 2}, {"score": 1},
        ]
        result = scoring_v2.track_score_distribution(sample)
        d = result["distribution"]
        assert d["n"] == 7
        assert d["by_score"]["5"] == 2
        assert d["by_score"]["4"] == 2

    def test_drift_warning_high_5stars(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scoring_v2, "_SCORE_CALIBRATION_PATH",
                            str(tmp_path / "calib.json"))
        # Force 3 runs avec >70% en 5★ → trigger drift warning (seuil 50%)
        for _ in range(3):
            scoring_v2.track_score_distribution([
                {"score": 5}, {"score": 5}, {"score": 5}, {"score": 5},
                {"score": 5}, {"score": 5}, {"score": 5}, {"score": 4},
            ])
        # 4e appel deja apres 3 runs : drift_warning doit etre set
        result = scoring_v2.track_score_distribution([{"score": 5} for _ in range(8)])
        assert result["drift_warning"] is not None
        assert "5★" in result["drift_warning"] or "over-scoring" in result["drift_warning"].lower()
