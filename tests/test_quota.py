"""
Tests for QuotaGuard, Daily Limits, and Evaluation Queue Logic
"""

import sqlite3
import pytest
from src.ai.quota import (
    QuotaExhausted,
    QuotaGuard,
    get_current_day_pt,
    should_evaluate_ad,
    sort_ads_for_evaluation,
)
from src.config import settings


@pytest.fixture
def temp_guard(tmp_path):
    db_file = tmp_path / "test_meta_ads.db"
    guard = QuotaGuard(db_path=str(db_file))
    return guard


def test_get_current_day_pt():
    day_pt = get_current_day_pt()
    assert len(day_pt) == 10
    parts = day_pt.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4  # Year


def test_record_and_get_usage(temp_guard):
    model = "gemini-2.5-flash"
    temp_guard.record_usage(model=model, tokens_in=1000, tokens_out=250, requests=1)

    usage = temp_guard.get_daily_usage(model)
    assert usage["requests"] == 1
    assert usage["tokens_in"] == 1000
    assert usage["tokens_out"] == 250

    # Record more usage
    temp_guard.record_usage(model=model, tokens_in=500, tokens_out=100, requests=2)
    updated = temp_guard.get_daily_usage(model)
    assert updated["requests"] == 3
    assert updated["tokens_in"] == 1500
    assert updated["tokens_out"] == 350


def test_quota_exhausted_raised(temp_guard, monkeypatch):
    model = "gemini-2.5-flash"
    # Set limit to 3 requests with 0 reserve for strict test
    monkeypatch.setattr(settings, "GEMINI_RPD", 3)
    monkeypatch.setattr(settings, "RESERVE_REQUESTS", 0)
    monkeypatch.setattr(settings, "FREE_TIER_MODE", True)

    assert temp_guard.get_available_requests(model) == 3

    # Acquire 3 requests
    temp_guard.acquire(model=model)
    temp_guard.record_usage(model=model, requests=1)

    temp_guard.acquire(model=model)
    temp_guard.record_usage(model=model, requests=1)

    temp_guard.acquire(model=model)
    temp_guard.record_usage(model=model, requests=1)

    assert temp_guard.get_available_requests(model) == 0

    # 4th acquire must raise QuotaExhausted
    with pytest.raises(QuotaExhausted) as exc_info:
        temp_guard.acquire(model=model)
    assert exc_info.value.model == model
    assert exc_info.value.requests_used == 3


def test_execute_with_retry_on_429(temp_guard, monkeypatch):
    model = "gemini-2.5-flash"
    attempts = [0]

    def mock_flaky_api():
        attempts[0] += 1
        if attempts[0] < 3:
            raise Exception("HTTP 429: ResourceExhausted rate limit")
        return {"status": "success"}

    # Use tiny delays for quick test execution
    result = temp_guard.execute_with_retry(
        model=model,
        func=mock_flaky_api,
        backoff_delays=(0.01, 0.02),
    )
    assert result == {"status": "success"}
    assert attempts[0] == 3


def test_sort_ads_for_evaluation():
    sample_ads = [
        {"ad_id": "1", "priority": 2, "is_active": True, "market_score": 90.0, "days_active": 40},
        {"ad_id": "2", "priority": 1, "is_active": False, "market_score": 60.0, "days_active": 10},
        {"ad_id": "3", "priority": 1, "is_active": True, "market_score": 85.0, "days_active": 20},
        {"ad_id": "4", "priority": 2, "is_active": False, "market_score": 95.0, "days_active": 100},
    ]

    sorted_ads = sort_ads_for_evaluation(sample_ads)
    order = [a["ad_id"] for a in sorted_ads]
    # Expect:
    # 1. P1 Active: "3"
    # 2. P1 Inactive: "2"
    # 3. P2 Active: "1"
    # 4. P2 Inactive: "4"
    assert order == ["3", "2", "1", "4"]


def test_should_evaluate_ad_heuristics():
    # Ad with < 3 days should be skipped
    fresh_ad = {"ad_id": "new", "days_active": 2}
    eligible, reason = should_evaluate_ad(fresh_ad)
    assert eligible is False
    assert "days_active < 3" in reason

    # Ad with >= 3 days should be evaluated
    good_ad = {"ad_id": "proven", "days_active": 15}
    eligible, reason = should_evaluate_ad(good_ad)
    assert eligible is True

    # Saturated competitor format without top market score
    saturated_ad = {"ad_id": "sat", "days_active": 25}
    eligible, reason = should_evaluate_ad(
        saturated_ad, competitor_eval_count=12, is_top_market_score=False
    )
    assert eligible is False
    assert "saturated" in reason
