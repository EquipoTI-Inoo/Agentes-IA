"""Queue status as data (Phase 8): the same numbers `queue --status` prints, reused by GET /api/queue/status. Read-only."""

from __future__ import annotations

from typing import Any, Dict

from src import jobs
from src.ai.quota import get_quota_guard
from src.config import is_within_preferred_hours, settings
from src.cost import CostSnapshot
from src.database import SessionLocal
from src.work_queue import estimate_days_to_clear, queue_stats


def build_queue_status(recent_jobs: int = 10) -> Dict[str, Any]:
    with SessionLocal() as db:
        totals = queue_stats(db)
    guard = get_quota_guard()
    snap = CostSnapshot.take(guard)
    fp = snap.key_fingerprint
    main_usage = guard.get_daily_usage(settings.LLM_MODEL, key_fingerprint=fp)
    cheap_usage = guard.get_daily_usage(settings.LLM_MODEL_CHEAP, key_fingerprint=fp)
    usable = max(1, settings.GEMINI_RPD - settings.RESERVE_REQUESTS)
    return {
        "totals": totals,
        "quota": {
            "key_fingerprint": fp,  # 8 characters; the key itself is never exposed
            "requests_today": snap.gemini_requests,
            "success_today": snap.gemini_success,
            "failed_today": snap.gemini_failed,
            "requests_without_fingerprint_today": guard.get_key_usage_today(""),
            "exhausted": bool(main_usage.get("is_exhausted")),
            "rpd": settings.GEMINI_RPD,
            "reserve": settings.RESERVE_REQUESTS,
            "usable": usable,
            "main_model": {"name": settings.LLM_MODEL, **{k: main_usage.get(k, 0) for k in ("requests", "requests_success", "requests_failed")}},
            "cheap_model": {"name": settings.LLM_MODEL_CHEAP, **{k: cheap_usage.get(k, 0) for k in ("requests", "requests_success", "requests_failed")}},
        },
        "estimate": {
            "queue_video": totals["queue_video"],
            "usable_rpd": usable,
            "days_to_clear": estimate_days_to_clear(totals["queue_video"], settings.GEMINI_RPD, settings.RESERVE_REQUESTS),
        },
        "claude": {"spent_usd": round(snap.claude_usd, 6), "budget_usd": settings.CLAUDE_BUDGET_USD, "queue_static": totals["queue_static"]},
        "window": {"preferred_hours": settings.EVAL_PREFERRED_HOURS, "inside": is_within_preferred_hours()},
        "recent_jobs": [j for j in jobs.list_jobs(limit=recent_jobs * 3) if j["type"] != "download"][:recent_jobs],
    }
