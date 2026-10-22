"""
Unit tests for Video Evaluator (Phase 4).
Tests creative_score calculation strictly by code, DB caching, and analysis.json generation.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.ai.schemas import DimensionScore, EvaluationResult, Evidence
from src.database import AnalysisRecord, SessionLocal
from src.evaluator import evaluate_video_ad


@pytest.fixture
def mock_evaluation_result():
    ev = Evidence(timestamp="00:01", source="visual", quote="Demostración")
    return EvaluationResult(
        transcript=[],
        has_voiceover=False,
        hook=DimensionScore(score=18, anchor_level="13-19", reasoning="Buen gancho", evidence=[ev]),
        mechanism=DimensionScore(score=15, anchor_level="13-19", reasoning="Muestra pantalla", evidence=[ev]),
        narrative=DimensionScore(score=12, anchor_level="7-12", reasoning="Ritmo irregular", evidence=[ev]),
        offer=DimensionScore(score=20, anchor_level="20-25", reasoning="Prueba gratis clara", evidence=[ev]),
        hook_description="Pantalla de inicio",
        value_proposition="Gestión de mensajes en un solo buzón",
        feature_focus=["mensajes_masivos_campanas"],
        ai_mention="ninguna",
        format="demo_pantalla",
        language="es",
        what_to_model="Claridad del CTA",
        what_to_beat="Mejorar audio",
        cosmiq_adaptation_idea="Demo directa",
        confidence=0.9,
    )


def test_evaluate_video_ad_sums_scores_strictly_by_code(tmp_path, monkeypatch, mock_evaluation_result):
    # Setup dummy video file
    ad_dir = tmp_path / "123456"
    ad_dir.mkdir(parents=True, exist_ok=True)
    video_file = ad_dir / "video_0.mp4"
    video_file.write_bytes(b"dummy video data " * 100)

    # Mock GeminiClient
    mock_client = MagicMock()
    mock_client.evaluate.return_value = mock_evaluation_result

    ad_dict = {
        "ad_id": "123456",
        "video_path": str(video_file),
        "headline": "Vende más",
        "body_text": "Automatiza tus chats",
    }

    result = evaluate_video_ad(ad_dict, client=mock_client, force=True)

    # Expected sum: 18 + 15 + 12 + 20 = 65
    assert result["creative_score"] == 65
    assert result["total_score"] == 65
    assert result["cached"] is False

    # Check analysis.json was created
    analysis_file = ad_dir / "analysis.json"
    assert analysis_file.exists()
    data = json.loads(analysis_file.read_text(encoding="utf-8"))
    assert data["creative_score"] == 65
    assert data["value_proposition"] == "Gestión de mensajes en un solo buzón"


def test_evaluate_video_ad_cache_hit(tmp_path, mock_evaluation_result):
    ad_dir = tmp_path / "cached_ad"
    ad_dir.mkdir(parents=True, exist_ok=True)
    video_file = ad_dir / "video_0.mp4"
    video_file.write_bytes(b"dummy data " * 50)

    mock_client = MagicMock()
    mock_client.evaluate.return_value = mock_evaluation_result

    ad_dict = {
        "ad_id": "cached_ad_999",
        "video_path": str(video_file),
    }

    # First run (computes and caches)
    res1 = evaluate_video_ad(ad_dict, client=mock_client, force=True)
    assert res1["cached"] is False
    assert mock_client.evaluate.call_count == 1

    # Second run without force (must hit cache, call_count stays 1)
    res2 = evaluate_video_ad(ad_dict, client=mock_client, force=False)
    assert res2["cached"] is True
    assert res2["creative_score"] == 65
    assert mock_client.evaluate.call_count == 1
