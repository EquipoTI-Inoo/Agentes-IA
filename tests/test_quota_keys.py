"""
Tests for per-API-key quota tracking in QuotaGuard (key_fingerprint).
Mocks only: no Gemini calls. Keys below are fake strings.
"""

import hashlib
import sqlite3
from unittest.mock import MagicMock

import pytest

from src.ai.quota import (
    LEGACY_KEY_FINGERPRINT,
    NO_KEY_FINGERPRINT,
    QuotaExhausted,
    QuotaGuard,
    compute_key_fingerprint,
)
from src.config import settings

OLD_KEY = "AI" + "za-FAKE-OLD-KEY-0000000000"  # assembled: no key-shaped literal in the repo
NEW_KEY = "AI" + "za-FAKE-NEW-KEY-1111111111"
MODEL = "gemini-3.5-flash"
DAY = "2026-10-21"


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(settings, "FREE_TIER_MODE", True)
    monkeypatch.setattr(settings, "GEMINI_RPD", 5)
    monkeypatch.setattr(settings, "GEMINI_RPD_MAIN", None)
    monkeypatch.setattr(settings, "GEMINI_RPD_CHEAP", None)
    monkeypatch.setattr(settings, "RESERVE_REQUESTS", 1)
    monkeypatch.setattr(settings, "GEMINI_RPM", 1000)
    monkeypatch.setattr("src.ai.quota.get_current_day_pt", lambda *a, **k: DAY)
    monkeypatch.setattr("src.ai.llm_client.get_current_day_pt", lambda *a, **k: DAY)


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "quota_keys.db"


def _dump_db_text(path) -> str:
    with sqlite3.connect(path) as conn:
        parts = []
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            parts.extend(str(r) for r in conn.execute(f"SELECT * FROM {table}"))
    return "\n".join(parts)


def test_fingerprint_is_8_hex_chars_of_sha256():
    fp = compute_key_fingerprint(OLD_KEY)
    assert fp == hashlib.sha256(OLD_KEY.encode()).hexdigest()[:8]
    assert len(fp) == 8
    assert fp != compute_key_fingerprint(NEW_KEY)
    assert compute_key_fingerprint(f"  {OLD_KEY}\n") == fp
    assert compute_key_fingerprint(None) == NO_KEY_FINGERPRINT
    assert compute_key_fingerprint("") == NO_KEY_FINGERPRINT


def test_guard_follows_key_in_settings(db_file, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", OLD_KEY)
    guard = QuotaGuard(db_path=str(db_file))
    assert guard.key_fingerprint == compute_key_fingerprint(OLD_KEY)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    assert guard.key_fingerprint == compute_key_fingerprint(NEW_KEY)


def test_key_change_resets_counter_and_exhaustion(db_file, limits, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", OLD_KEY)
    guard = QuotaGuard(db_path=str(db_file))
    for _ in range(4):
        guard.acquire(MODEL)
        guard.record_attempt(model=MODEL, status="success")
    guard.mark_exhausted(MODEL)
    assert guard.get_daily_usage(MODEL)["requests"] == 4
    assert guard.is_exhausted(MODEL)
    with pytest.raises(QuotaExhausted):
        guard.acquire(MODEL)

    # New key in .env: same day, fresh counter, no exhausted flag
    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    assert guard.get_daily_usage(MODEL)["requests"] == 0
    assert guard.is_exhausted(MODEL) is False
    assert guard.can_acquire(MODEL) is True
    guard.acquire(MODEL)
    guard.record_attempt(model=MODEL, status="success")
    assert guard.get_key_usage_today() == 1

    # Old records are preserved
    old_fp = compute_key_fingerprint(OLD_KEY)
    assert guard.get_daily_usage(MODEL, key_fingerprint=old_fp)["requests"] == 4
    assert guard.is_exhausted(MODEL, key_fingerprint=old_fp) is True
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_attempts WHERE key_fingerprint = ?", (old_fp,)
        ).fetchone()[0] == 4


def test_same_key_stays_exhausted(db_file, limits, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", OLD_KEY)
    QuotaGuard(db_path=str(db_file)).mark_exhausted(MODEL)

    # A new process (new guard instance) with the same key is still blocked
    guard = QuotaGuard(db_path=str(db_file))
    assert guard.is_exhausted(MODEL) is True
    assert guard.get_available_requests(MODEL) == 0
    assert guard.can_acquire(MODEL) is False
    with pytest.raises(QuotaExhausted, match="RPD agotado"):
        guard.acquire(MODEL)


def test_429_rpd_on_new_key_marks_only_that_key(db_file, limits, monkeypatch):
    from src.ai.llm_client import GeminiClient

    monkeypatch.setattr(settings, "GEMINI_API_KEY", OLD_KEY)
    guard = QuotaGuard(db_path=str(db_file))
    guard.record_attempt(model=MODEL, status="success")  # old key used once, not exhausted

    client = GeminiClient(api_key=NEW_KEY, quota_guard=guard)
    client._client = MagicMock()
    client._client.models.generate_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED: Quota exceeded for metric generate_content_free_tier_requests, limit: 20 per day"
    )
    with pytest.raises(QuotaExhausted):
        client._call_with_transient_retry(cnts=["x"], config={})

    new_fp = compute_key_fingerprint(NEW_KEY)
    old_fp = compute_key_fingerprint(OLD_KEY)
    assert client.key_fingerprint == new_fp
    assert guard.is_exhausted(MODEL, key_fingerprint=new_fp) is True
    assert guard.is_exhausted(MODEL, key_fingerprint=old_fp) is False
    assert guard.get_daily_usage(MODEL, key_fingerprint=old_fp)["requests"] == 1
    with sqlite3.connect(db_file) as conn:
        statuses = conn.execute(
            "SELECT status, key_fingerprint FROM llm_attempts ORDER BY id"
        ).fetchall()
    assert statuses == [("success", old_fp), ("429_rpd", new_fp)]


def test_reserve_and_per_model_limits_apply_per_key(db_file, limits, monkeypatch):
    cheap = "gemini-3.5-flash-lite"
    monkeypatch.setattr(settings, "LLM_MODEL", MODEL)
    monkeypatch.setattr(settings, "LLM_MODEL_CHEAP", cheap)
    monkeypatch.setattr(settings, "GEMINI_RPD_MAIN", 3)
    monkeypatch.setattr(settings, "GEMINI_RPD_CHEAP", 6)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    guard = QuotaGuard(db_path=str(db_file))

    # Main model: 3 RPD - 1 reserve = 2 usable for this key
    assert guard.get_available_requests(MODEL) == 2
    for _ in range(2):
        guard.acquire(MODEL)
        guard.record_attempt(model=MODEL, status="success")
    with pytest.raises(QuotaExhausted):
        guard.acquire(MODEL)

    # Cheap model keeps its own limit: 6 - 1 = 5
    assert guard.get_available_requests(cheap) == 5


def test_legacy_usage_table_is_migrated_without_losing_rows(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE llm_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, day_pt TEXT NOT NULL,
               model TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
               tokens_out INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(day_pt, model))"""
        )
        conn.execute("INSERT INTO llm_usage (day_pt, model, requests, tokens_in) VALUES (?, ?, 7, 999)", (DAY, MODEL))

    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    guard = QuotaGuard(db_path=str(db))
    assert list((tmp_path / "backup").glob("legacy_before_key_fingerprint_*.db"))

    legacy = guard.get_daily_usage(MODEL, day_pt=DAY, key_fingerprint=LEGACY_KEY_FINGERPRINT)
    assert legacy["requests"] == 7 and legacy["tokens_in"] == 999
    assert guard.get_daily_usage(MODEL, day_pt=DAY)["requests"] == 0

    # Same day and model, different key: separate row allowed by the new UNIQUE constraint
    guard.record_attempt(model=MODEL, status="success", day_pt=DAY)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 2

    # Re-initializing is idempotent
    QuotaGuard(db_path=str(db))
    assert len(list((tmp_path / "backup").glob("*.db"))) == 1


def test_raw_key_is_never_stored(db_file, limits, monkeypatch, caplog):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    guard = QuotaGuard(db_path=str(db_file))
    with caplog.at_level("DEBUG"):
        guard.acquire(MODEL)
        guard.record_attempt(model=MODEL, status="success")
        guard.mark_exhausted(MODEL)
    dump = _dump_db_text(db_file)
    assert compute_key_fingerprint(NEW_KEY) in dump
    assert NEW_KEY not in dump
    assert NEW_KEY not in caplog.text


def test_claude_usage_is_not_keyed_by_gemini_fingerprint(db_file, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", NEW_KEY)
    guard = QuotaGuard(db_path=str(db_file))
    guard.record_usage(model="claude-sonnet-5-5", provider="claude", cost_usd=0.5)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", OLD_KEY)
    # Claude budget keeps counting across Gemini key changes
    assert guard.get_total_spend(provider="claude") == pytest.approx(0.5)
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT key_fingerprint FROM llm_usage").fetchone()[0] == LEGACY_KEY_FINGERPRINT
