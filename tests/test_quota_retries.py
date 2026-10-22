"""
A1: every attempt sent to Gemini (retries included) goes through QuotaGuard.acquire(), so a long run of
503s can never exceed the daily cap (RPD minus RESERVE_REQUESTS) or the session budget. Mocks only.
"""

from unittest.mock import MagicMock

import pytest

from src.ai.llm_client import GeminiClient
from src.ai.quota import QuotaExhausted, QuotaGuard
from src.ai.schemas import AdCopy, MediaBundle
from src.config import settings

MODEL = "gemini-3.5-flash"


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(settings, "FREE_TIER_MODE", True)
    monkeypatch.setattr(settings, "LLM_MODEL", MODEL)
    monkeypatch.setattr(settings, "GEMINI_RPD", 6)
    monkeypatch.setattr(settings, "GEMINI_RPD_MAIN", None)
    monkeypatch.setattr(settings, "RESERVE_REQUESTS", 2)  # usable per day: 6 - 2 = 4
    monkeypatch.setattr(settings, "GEMINI_RPM", 1000)
    monkeypatch.setattr(settings, "GEMINI_RETRY_DELAYS", [0.001] * 12)  # up to 12 attempts if nothing stops them
    monkeypatch.setattr("src.ai.llm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("src.ai.quota.time.sleep", lambda s: None)


@pytest.fixture
def guard(tmp_path):
    return QuotaGuard(db_path=str(tmp_path / "quota.db"), api_key="fake-key-for-tests")


def _client(guard, error="503 UNAVAILABLE. This model is currently experiencing high demand."):
    client = GeminiClient(api_key="fake-key-for-tests", quota_guard=guard)
    client._client = MagicMock()
    client._client.models.generate_content.side_effect = Exception(error)
    return client


def test_long_503_sequence_never_exceeds_the_daily_cap(guard, limits):
    client = _client(guard)
    with pytest.raises(QuotaExhausted):
        client._call_with_transient_retry(["x"], config={})

    calls = client._client.models.generate_content.call_count
    usage = guard.get_daily_usage(MODEL, key_fingerprint=client.key_fingerprint)
    assert calls == 4 == usage["requests"] == usage["requests_failed"]  # 6 RPD - 2 reserve; never 5, never 12
    assert usage["requests"] <= settings.GEMINI_RPD - settings.RESERVE_REQUESTS

    # Once the cap is reached nothing more is sent to the API
    with pytest.raises(QuotaExhausted):
        client._call_with_transient_retry(["x"], config={})
    assert client._client.models.generate_content.call_count == 4


def test_session_budget_cap_applies_to_retries(guard, limits, monkeypatch):
    """A session cap (settings.GEMINI_RPD_MAIN, as used by scripts/run_phase4_real_tests.py) also stops retries."""
    monkeypatch.setattr(settings, "GEMINI_RPD_MAIN", 5)  # 5 - 2 reserve = 3 attempts
    client = _client(guard)
    with pytest.raises(QuotaExhausted):
        client._call_with_transient_retry(["x"], config={})
    assert client._client.models.generate_content.call_count == 3


def test_attempts_already_used_today_reduce_what_retries_may_spend(guard, limits):
    client = _client(guard)
    for _ in range(3):  # three requests already sent today with this key
        guard.record_attempt(model=MODEL, status="success", key_fingerprint=client.key_fingerprint)
    with pytest.raises(QuotaExhausted):
        client._call_with_transient_retry(["x"], config={})
    assert client._client.models.generate_content.call_count == 1  # only 1 usable request was left


def test_success_after_503_counts_every_attempt_once(guard, limits):
    client = _client(guard)
    ok = MagicMock()
    ok.usage_metadata.prompt_token_count, ok.usage_metadata.candidates_token_count = 10, 5
    client._client.models.generate_content.side_effect = [Exception("503 UNAVAILABLE"), Exception("503 UNAVAILABLE"), ok]
    assert client._call_with_transient_retry(["x"], config={}) is ok
    usage = guard.get_daily_usage(MODEL, key_fingerprint=client.key_fingerprint)
    assert (usage["requests"], usage["requests_success"], usage["requests_failed"]) == (3, 1, 2)


def test_evaluate_checks_the_quota_before_uploading_the_video(guard, limits, tmp_path):
    client = _client(guard)
    guard.mark_exhausted(MODEL, key_fingerprint=client.key_fingerprint)
    video = tmp_path / "video_0.mp4"
    video.write_bytes(b"x" * 100)
    with pytest.raises(QuotaExhausted):
        client.evaluate(MediaBundle(media_path=video, duration_s=10.0), AdCopy(headline="h"))
    client._client.files.upload.assert_not_called()
    client._client.models.generate_content.assert_not_called()


def test_execute_with_retry_also_acquires_before_each_attempt(guard, limits):
    calls = []

    def always_rate_limited():
        calls.append(1)
        raise Exception("HTTP 429: rate limit, try again")

    with pytest.raises(QuotaExhausted):
        guard.execute_with_retry(MODEL, always_rate_limited, backoff_delays=(0.001,) * 10)
    assert len(calls) == 4  # the cap, not the 11 attempts the backoff list would allow
