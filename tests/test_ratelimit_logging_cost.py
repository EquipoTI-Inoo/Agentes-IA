"""B6 (Meta rate limiting + backoff) and B7 (rotating log file + per-run LLM cost summary). No network."""

import logging

import pytest

from src.ai.quota import QuotaGuard
from src.collect.ratelimit import MetaBlockedError, MetaRateLimiter, is_block_error
from src.cost import CostSnapshot, cost_delta, format_cost_summary
from src.logging_setup import setup_logging


def limiter(sleeps, **kw):
    # uniform returns the lower bound: pauses are 1.0 s and jitter is 0, so delays are exact
    return MetaRateLimiter(sleep=sleeps.append, uniform=lambda a, b: a, **kw)


def test_random_pause_is_between_one_and_three_seconds():
    seen = []
    lim = MetaRateLimiter(sleep=lambda s: None, uniform=lambda a, b: seen.append((a, b)) or a)
    lim.wait()
    assert seen == [(1.0, 3.0)]
    import random

    real = MetaRateLimiter(sleep=lambda s: None)
    pauses = [real.wait() for _ in range(50)]
    assert all(1.0 <= p <= 3.0 for p in pauses) and len(set(pauses)) > 1


def test_every_query_waits_first_then_runs():
    sleeps, calls = [], []
    lim = limiter(sleeps)
    assert lim.call(lambda x: calls.append(x) or "ok", 5) == "ok"
    assert sleeps == [1.0] and calls == [5] and lim.queries == 1 and lim.blocks == 0


def test_backoff_grows_exponentially_when_meta_blocks_then_succeeds():
    sleeps, attempts = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 4:
            raise Exception("HTTP 429 Too Many Requests")
        return "finally"

    lim = limiter(sleeps, backoff_base=30.0)
    assert lim.call(flaky) == "finally"
    assert sleeps == [1.0, 30.0, 60.0, 120.0]  # random pause, then 30 s, 60 s, 120 s
    assert lim.blocks == 3


def test_backoff_gives_up_with_meta_blocked_error_and_caps_the_delay():
    sleeps = []
    lim = limiter(sleeps, max_retries=3, backoff_base=300.0, backoff_max=400.0)

    def always_blocked():
        raise Exception("You have been temporarily blocked")

    with pytest.raises(MetaBlockedError):
        lim.call(always_blocked)
    assert sleeps == [1.0, 300.0, 400.0, 400.0]  # capped at backoff_max, 3 retries only


def test_non_block_errors_are_not_retried():
    sleeps, calls = [], []

    def broken():
        calls.append(1)
        raise ValueError("bad parameter")

    with pytest.raises(ValueError):
        limiter(sleeps).call(broken)
    assert calls == [1] and sleeps == [1.0]


def test_block_detection():
    from meta_ads_collector.exceptions import RateLimitError, SessionExpiredError

    assert is_block_error(RateLimitError("x")) and is_block_error(SessionExpiredError("x"))
    assert is_block_error(Exception("HTTP 403 Forbidden")) and is_block_error(Exception("captcha required"))
    assert not is_block_error(ValueError("invalid country")) and not is_block_error(Exception("timeout 4030ms"))


def test_scraper_routes_every_query_through_the_limiter(monkeypatch):
    import src.scraper as scraper

    class FakeCollector:
        def __init__(self):
            self.calls = []

        def collect(self, **kw):
            self.calls.append(kw)
            return []

        def close(self):
            pass

    collector = FakeCollector()
    monkeypatch.setattr(scraper, "MetaAdsCollector", lambda: collector)
    sleeps = []
    hunter = scraper.MetaAdsHunter(output_dir="downloads", relevance_filter=object(), limiter=limiter(sleeps))
    hunter.search_ads(query="Kommo", country="pe", limit=3, filter_relevance=False)
    hunter.search_ads(query="x", country="MX", competitor={"name": "Kommo", "page_ids": ["9"]}, filter_relevance=False)
    assert sleeps == [1.0, 1.0] and hunter.limiter.queries == 2  # one pause per query
    assert collector.calls[0]["query"] == "Kommo" and collector.calls[1]["page_ids"] == ["9"]


# ---------------------------------------------------------------- B7

@pytest.fixture
def isolated_root_logger():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield root
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def test_log_file_is_written_and_setup_is_idempotent(tmp_path, isolated_root_logger):
    log_file = setup_logging(tmp_path / "logs", console=False)
    setup_logging(tmp_path / "logs", console=False)
    handlers = [h for h in isolated_root_logger.handlers if getattr(h, "_jinora_file_handler", False)]
    assert len(handlers) == 1 and log_file == tmp_path / "logs" / "jinora.log"
    logging.getLogger("Jinora.Test").info("hola desde el pipeline")
    handlers[0].flush()
    assert "hola desde el pipeline" in log_file.read_text(encoding="utf-8") and "Jinora.Test" in log_file.read_text(encoding="utf-8")


def test_log_file_rotates(tmp_path, isolated_root_logger):
    setup_logging(tmp_path, console=False, max_bytes=400, backup_count=2)
    for i in range(80):
        logging.getLogger("Jinora.Rotate").info(f"linea {i} " + "x" * 30)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["jinora.log", "jinora.log.1", "jinora.log.2"]  # capped at backup_count


def test_cost_summary_counts_gemini_requests_and_claude_usd(tmp_path):
    guard = QuotaGuard(db_path=str(tmp_path / "cost.db"), api_key="fake-key-for-tests")
    before = CostSnapshot.take(guard)
    guard.record_attempt(model="gemini-3.5-flash", status="success", tokens_in=10, tokens_out=5)
    guard.record_attempt(model="gemini-3.5-flash", status="503")
    guard.record_usage(model="claude-sonnet-5-5", provider="claude", tokens_in=1000, tokens_out=200, cost_usd=0.0123)
    guard.record_usage(model="claude-sonnet-5-5", provider="claude", cost_usd=0.02)
    cost = cost_delta(before, CostSnapshot.take(guard))

    assert (cost["gemini_requests"], cost["gemini_success"], cost["gemini_failed"]) == (2, 1, 1)
    assert cost["claude_usd"] == pytest.approx(0.0323) and cost["claude_usd_total"] == pytest.approx(0.0323)
    text = format_cost_summary(cost)
    assert "Gemini: 2 solicitudes (ok 1, fallos 1" in text and cost["key_fingerprint"] in text
    assert "Claude: $0.0323" in text and "de $" in text  # against CLAUDE_BUDGET_USD
