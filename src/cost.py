"""LLM cost summary per run (Phase 7, B7): Gemini = requests, Claude = USD against CLAUDE_BUDGET_USD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.ai.quota import QuotaGuard, get_quota_guard
from src.config import settings


@dataclass
class CostSnapshot:
    gemini_requests: int
    gemini_success: int
    gemini_failed: int
    claude_usd: float
    key_fingerprint: str

    @classmethod
    def take(cls, guard: Optional[QuotaGuard] = None) -> "CostSnapshot":
        guard = guard or get_quota_guard()
        fp = guard.key_fingerprint
        usages = [guard.get_daily_usage(m, key_fingerprint=fp) for m in {settings.LLM_MODEL, settings.LLM_MODEL_CHEAP}]
        return cls(
            gemini_requests=sum(u["requests"] for u in usages),
            gemini_success=sum(u.get("requests_success", 0) for u in usages),
            gemini_failed=sum(u.get("requests_failed", 0) for u in usages),
            claude_usd=guard.get_total_spend(provider="claude"),
            key_fingerprint=fp,
        )


def cost_delta(before: CostSnapshot, after: CostSnapshot) -> Dict[str, Any]:
    """What this run cost (after - before) plus the running totals against the limits."""
    return {
        "gemini_requests": after.gemini_requests - before.gemini_requests,
        "gemini_success": after.gemini_success - before.gemini_success,
        "gemini_failed": after.gemini_failed - before.gemini_failed,
        "gemini_requests_today": after.gemini_requests,
        "gemini_rpd": settings.GEMINI_RPD,
        "key_fingerprint": after.key_fingerprint,
        "claude_usd": round(after.claude_usd - before.claude_usd, 6),
        "claude_usd_total": round(after.claude_usd, 6),
        "claude_budget_usd": settings.CLAUDE_BUDGET_USD,
    }


def format_cost_summary(cost: Dict[str, Any]) -> str:
    return (
        "💰 Costo LLM de esta corrida — "
        f"Gemini: {cost['gemini_requests']} solicitudes (ok {cost['gemini_success']}, fallos {cost['gemini_failed']}; "
        f"hoy {cost['gemini_requests_today']}/{cost['gemini_rpd']}, clave {cost['key_fingerprint']}) | "
        f"Claude: ${cost['claude_usd']:.4f} (acumulado ${cost['claude_usd_total']:.4f} de ${cost['claude_budget_usd']:.2f})"
    )
