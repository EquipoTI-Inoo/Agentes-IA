"""
Unit tests for Video Evaluator (Phase 4).
Tests creative_score calculation strictly by code, DB caching, and analysis.json generation.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.ai.schemas import DimensionScore, EvaluationResult, Evidence
from src.config import settings
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


def test_extract_retry_after_header_and_body():
    from src.ai.llm_client import extract_retry_after

    # Test Response with Retry-After header
    class MockResp:
        headers = {"Retry-After": "45"}

    class MockErrWithResp(Exception):
        response = MockResp()

    assert extract_retry_after(MockErrWithResp()) == 45.0

    # Test Google RPC string with retryDelay: '12.5s'
    rpc_err = Exception("429 ResourceExhausted: Please wait. {'retryDelay': '12.5s'}")
    assert extract_retry_after(rpc_err) == 12.5

    # Test no delay
    plain_err = Exception("500 Internal Server Error")
    assert extract_retry_after(plain_err) is None


def test_evaluation_failure_leaves_pending_evaluation(tmp_path):
    from src.database import AdRecord, SessionLocal

    ad_id = "ad_fail_test_123"
    ad_dir = tmp_path / ad_id
    ad_dir.mkdir(parents=True, exist_ok=True)
    video_file = ad_dir / "video_0.mp4"
    video_file.write_bytes(b"dummy" * 50)

    # Insert test AdRecord into DB
    db = SessionLocal()
    try:
        existing = db.query(AdRecord).filter(AdRecord.id == ad_id).first()
        if not existing:
            rec = AdRecord(id=ad_id, page_name="TestCo", video_path=str(video_file), cognitive_score=None)
            db.add(rec)
            db.commit()
    finally:
        db.close()

    mock_client = MagicMock()
    mock_client.evaluate.side_effect = RuntimeError("503 Service Unavailable temporarily")

    ad_dict = {"ad_id": ad_id, "video_path": str(video_file)}

    with pytest.raises(RuntimeError) as exc:
        evaluate_video_ad(ad_dict, client=mock_client, force=True)
    assert "503" in str(exc.value)

    # Verify that AdRecord in DB has cognitive_score = None and score_breakdown with pending_evaluation
    db = SessionLocal()
    try:
        updated = db.query(AdRecord).filter(AdRecord.id == ad_id).first()
        assert updated.cognitive_score is None
        breakdown = json.loads(updated.score_breakdown)
        assert breakdown["status"] == "pending_evaluation"
        assert "503 Service Unavailable" in breakdown["last_error"]
        assert "retry_after" in breakdown
    finally:
        db.close()


def test_gemini_client_retry_sequence(monkeypatch):
    from src.ai.llm_client import GeminiClient
    from src.ai.schemas import MediaBundle, AdCopy

    client = GeminiClient(api_key="mock_key")
    client.quota_guard = MagicMock()

    call_count = [0]
    def mock_generate_content(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise Exception("503 Service Unavailable from upstream")
        resp = MagicMock()
        resp.usage_metadata.prompt_token_count = 100
        resp.usage_metadata.candidates_token_count = 50
        resp.text = json.dumps({
            "transcript": [],
            "has_voiceover": False,
            "hook": {"score": 15, "anchor_level": "13-19", "reasoning": "Bien", "evidence": [{"timestamp": "00:01", "source": "visual", "quote": "demo"}]},
            "mechanism": {"score": 15, "anchor_level": "13-19", "reasoning": "Bien", "evidence": [{"timestamp": "00:01", "source": "visual", "quote": "demo"}]},
            "narrative": {"score": 15, "anchor_level": "13-19", "reasoning": "Bien", "evidence": [{"timestamp": "00:01", "source": "visual", "quote": "demo"}]},
            "offer": {"score": 15, "anchor_level": "13-19", "reasoning": "Bien", "evidence": [{"timestamp": "00:01", "source": "visual", "quote": "demo"}]},
            "hook_description": "Gancho",
            "value_proposition": "Propuesta",
            "feature_focus": ["crm"],
            "ai_mention": "ninguna",
            "format": "demo_pantalla",
            "language": "es",
            "what_to_model": "Modelo",
            "what_to_beat": "Beat",
            "cosmiq_adaptation_idea": "Idea",
            "confidence": 0.9,
        })
        return resp

    client._client = MagicMock()
    client._client.models.generate_content = mock_generate_content

    # Override delays with tiny delays for speed
    monkeypatch.setattr(settings, "GEMINI_RETRY_DELAYS", [0.01, 0.01, 0.01, 0.01])

    # Call _call_with_transient_retry directly
    resp = client._call_with_transient_retry(cnts=["test"], config={})
    assert resp is not None
    assert call_count[0] == 3
    # Check that quota_guard.record_attempt was called 3 times (2 fails + 1 success)
    assert client.quota_guard.record_attempt.call_count == 3

