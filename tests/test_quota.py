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
    # Expect: Active ads first (P1 then P2), followed by Inactive ads at lowest priority
    # 1. P1 Active: "3"
    # 2. P2 Active: "1"
    # 3. P1 Inactive: "2"
    # 4. P2 Inactive: "4"
    assert order == ["3", "1", "2", "4"]


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


def test_record_attempt_and_audit(temp_guard):
    model = "gemini-3.5-flash"
    # Record 1 success and 2 failures (503 and 429)
    temp_guard.record_attempt(model=model, status="success", tokens_in=100, tokens_out=50)
    temp_guard.record_attempt(model=model, status="503", error_message="Service Unavailable")
    temp_guard.record_attempt(model=model, status="429", error_message="ResourceExhausted")

    usage = temp_guard.get_daily_usage(model)
    assert usage["requests"] == 3
    assert usage["requests_success"] == 1
    assert usage["requests_failed"] == 2

    # Check audit table records
    with sqlite3.connect(temp_guard.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, model FROM llm_attempts WHERE model = ? ORDER BY id", (model,))
        rows = cursor.fetchall()
        assert len(rows) == 3
        assert rows[0] == ("success", model)
        assert rows[1] == ("503", model)
        assert rows[2] == ("429", model)


def test_mark_exhausted_and_stop_queue(temp_guard):
    model = "gemini-3.5-flash"
    assert temp_guard.is_exhausted(model) is False

    # Mark quota exhausted on 429 RPD
    temp_guard.mark_exhausted(model=model)
    assert temp_guard.is_exhausted(model) is True

    # QuotaGuard acquire should raise QuotaExhausted immediately
    with pytest.raises(QuotaExhausted) as exc:
        temp_guard.acquire(model=model)
    assert "RPD agotado" in str(exc.value)


def test_midnight_pt_rollover(temp_guard):
    model = "gemini-3.5-flash"
    day1 = "2026-10-21"
    day2 = "2026-10-22"

    # Record usage on day 1
    temp_guard.record_attempt(model=model, status="success", day_pt=day1)
    temp_guard.mark_exhausted(model=model, day_pt=day1)

    assert temp_guard.get_daily_usage(model=model, day_pt=day1)["requests"] == 1
    assert temp_guard.is_exhausted(model=model, day_pt=day1) is True

    # On day 2 (next day in PT), quota is fresh and not exhausted
    assert temp_guard.get_daily_usage(model=model, day_pt=day2)["requests"] == 0
    assert temp_guard.is_exhausted(model=model, day_pt=day2) is False

