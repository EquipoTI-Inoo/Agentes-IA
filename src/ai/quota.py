"""
Jinora AI - Quota Guard & Free Tier Rate Limiter
Enforces Google Gemini Free Tier constraints:
- Requests Per Day (RPD) with Pacific Time reset tracking
- Requests Per Minute (RPM) & Tokens Per Minute (TPM) sliding window pacing
- Exponential backoff on HTTP 429 errors
- Prioritized evaluation queue and low-value ad skipping
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from zoneinfo import ZoneInfo

from src.config import settings

logger = logging.getLogger("Jinora.QuotaGuard")
T = TypeVar("T")

# Pacific Time Zone used by Google AI Studio to reset daily limits (midnight PT)
try:
    PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC_TZ = timezone.utc


def get_current_day_pt() -> str:
    """Return the current date string (YYYY-MM-DD) in Pacific Time."""
    return datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")


class QuotaExhausted(Exception):
    """Raised when the daily LLM request quota has been exhausted."""

    def __init__(
        self,
        model: str,
        day_pt: str,
        requests_used: int,
        limit: int,
        reason: str = "Daily limit reached",
    ) -> None:
        self.model = model
        self.day_pt = day_pt
        self.requests_used = requests_used
        self.limit = limit
        self.reason = reason
        super().__init__(
            f"QuotaExhausted for model '{model}' on {day_pt} (PT): "
            f"{requests_used}/{limit} requests used. Reason: {reason}"
        )


class QuotaGuard:
    """Guards Gemini API calls against RPM, TPM, and daily RPD quotas."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path:
            self.db_path = Path(db_path)
        else:
            db_url = settings.DATABASE_URL
            if db_url.startswith("sqlite:///"):
                self.db_path = Path(db_url.replace("sqlite:///", ""))
            else:
                self.db_path = Path("data/meta_ads.db")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # In-memory sliding windows for RPM / TPM
        self._request_timestamps: List[float] = []
        self._token_timestamps: List[Tuple[float, int]] = []

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create llm_usage table if it does not exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_pt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    tokens_in INTEGER NOT NULL DEFAULT 0,
                    tokens_out INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(day_pt, model)
                );
                """
            )
            conn.commit()

    def get_daily_usage(self, model: str, day_pt: Optional[str] = None) -> Dict[str, int]:
        """Fetch current day usage metrics for a specific model."""
        day = day_pt or get_current_day_pt()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT requests, tokens_in, tokens_out FROM llm_usage WHERE day_pt = ? AND model = ?",
                (day, model),
            )
            row = cur.fetchone()
            if row:
                return {
                    "requests": int(row["requests"]),
                    "tokens_in": int(row["tokens_in"]),
                    "tokens_out": int(row["tokens_out"]),
                }
        return {"requests": 0, "tokens_in": 0, "tokens_out": 0}

    def get_available_requests(self, model: str, day_pt: Optional[str] = None) -> int:
        """Calculate remaining usable requests for today considering reserve requests."""
        if not settings.FREE_TIER_MODE:
            return 999999

        raw_limit = settings.get_rpd_for_model(model)
        effective_limit = max(0, raw_limit - settings.RESERVE_REQUESTS)
        usage = self.get_daily_usage(model, day_pt)
        remaining = effective_limit - usage["requests"]
        return max(0, remaining)

    def can_acquire(self, model: str, requests_needed: int = 1) -> bool:
        """Check if quota allows acquiring additional requests."""
        return self.get_available_requests(model) >= requests_needed

    def acquire(self, model: str, est_tokens: int = 0) -> None:
        """
        Reserve an API request slot.
        - Enforces RPD: raises QuotaExhausted if daily cap reached.
        - Enforces RPM/TPM: sleeps if rate limits are momentarily saturated.
        """
        if not settings.FREE_TIER_MODE:
            return

        day_pt = get_current_day_pt()
        raw_limit = settings.get_rpd_for_model(model)
        effective_limit = max(0, raw_limit - settings.RESERVE_REQUESTS)
        usage = self.get_daily_usage(model, day_pt)

        if usage["requests"] >= effective_limit:
            logger.warning(
                f"🚨 Quota diaria agotada para {model}: {usage['requests']}/{effective_limit} "
                f"(Límite bruto: {raw_limit}, Reserva: {settings.RESERVE_REQUESTS})"
            )
            raise QuotaExhausted(
                model=model,
                day_pt=day_pt,
                requests_used=usage["requests"],
                limit=effective_limit,
                reason="Daily RPD limit reached after deducting reserve",
            )

        # Sliding window for RPM (Requests Per Minute)
        now = time.time()
        self._request_timestamps = [t for t in self._request_timestamps if now - t < 60.0]
        if len(self._request_timestamps) >= settings.GEMINI_RPM:
            oldest = self._request_timestamps[0]
            sleep_time = max(0.1, 60.0 - (now - oldest) + 0.5)
            logger.info(f"⏳ RPM limit reached ({settings.GEMINI_RPM}/min). Pacing for {sleep_time:.1f}s...")
            time.sleep(sleep_time)
            now = time.time()
            self._request_timestamps = [t for t in self._request_timestamps if now - t < 60.0]

        # Sliding window for TPM (Tokens Per Minute)
        if est_tokens > 0:
            self._token_timestamps = [item for item in self._token_timestamps if now - item[0] < 60.0]
            current_tokens = sum(item[1] for item in self._token_timestamps)
            if current_tokens + est_tokens > settings.GEMINI_TPM and self._token_timestamps:
                oldest_token_time = self._token_timestamps[0][0]
                sleep_time = max(0.1, 60.0 - (now - oldest_token_time) + 0.5)
                logger.info(f"⏳ TPM limit reached ({settings.GEMINI_TPM}/min). Pacing for {sleep_time:.1f}s...")
                time.sleep(sleep_time)

        # Register current acquisition
        now = time.time()
        self._request_timestamps.append(now)
        if est_tokens > 0:
            self._token_timestamps.append((now, est_tokens))

    def record_usage(
        self,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        requests: int = 1,
        day_pt: Optional[str] = None,
    ) -> None:
        """Atomically record actual token and request consumption."""
        day = day_pt or get_current_day_pt()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage (day_pt, model, requests, tokens_in, tokens_out)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day_pt, model) DO UPDATE SET
                    requests = requests + excluded.requests,
                    tokens_in = tokens_in + excluded.tokens_in,
                    tokens_out = tokens_out + excluded.tokens_out;
                """,
                (day, model, requests, tokens_in, tokens_out),
            )
            conn.commit()

        logger.info(
            f"📊 LLM Usage recorded [{model} | {day}]: +{requests} req, +{tokens_in} tok_in, +{tokens_out} tok_out"
        )

    def execute_with_retry(
        self,
        model: str,
        func: Callable[..., T],
        *args: Any,
        est_tokens: int = 0,
        backoff_delays: Tuple[float, ...] = (30.0, 60.0, 120.0),
        **kwargs: Any,
    ) -> T:
        """
        Executes a callable with QuotaGuard acquisition and 429 exponential backoff.
        Raises QuotaExhausted if 429 persists or indicates daily quota exhaustion.
        """
        self.acquire(model=model, est_tokens=est_tokens)

        total_attempts = len(backoff_delays) + 1
        last_exception: Optional[Exception] = None

        for attempt in range(1, total_attempts + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exception = exc
                err_str = str(exc).lower()

                # Detect 429 or ResourceExhausted
                is_rate_limit = (
                    "429" in err_str
                    or "resource_exhausted" in err_str
                    or "rate limit" in err_str
                    or "quota" in err_str
                )
                is_daily_quota = (
                    "quota exceeded" in err_str
                    or "daily" in err_str
                    or "per day" in err_str
                )

                if is_rate_limit:
                    if is_daily_quota or attempt == total_attempts:
                        day_pt = get_current_day_pt()
                        usage = self.get_daily_usage(model, day_pt)
                        raise QuotaExhausted(
                            model=model,
                            day_pt=day_pt,
                            requests_used=usage["requests"],
                            limit=settings.get_rpd_for_model(model),
                            reason=f"Google API returned quota error: {exc}",
                        ) from exc

                    delay = backoff_delays[attempt - 1]
                    logger.warning(
                        f"⚠️ HTTP 429 / Rate Limit detectado (Intento {attempt}/{total_attempts}). "
                        f"Reintentando en {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    raise exc

        if last_exception:
            raise last_exception
        raise RuntimeError("Unreachable code in execute_with_retry")


# --- Pipeline Prioritization & Value Filtering Helpers (Fase 0.1.3 & 0.1.4) ---

def sort_ads_for_evaluation(ads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sorts candidate ads by strategic business priority:
    1. priority == 1 (LatAm WhatsApp & CRM core competitors)
    2. is_active == True (currently running in market)
    3. market_score (descending)
    4. days_active / recency (descending)
    """
    def _sort_key(ad: Dict[str, Any]) -> Tuple[int, int, float, int]:
        priority = int(ad.get("priority", 2))
        is_p1 = 0 if priority == 1 else 1

        is_active = 0 if ad.get("is_active", False) else 1
        market_score = float(ad.get("market_score", 0.0) or 0.0)
        days_active = int(ad.get("days_active", 0) or 0)

        return (is_p1, is_active, -market_score, -days_active)

    return sorted(ads, key=_sort_key)


def should_evaluate_ad(
    ad: Dict[str, Any],
    competitor_eval_count: int = 0,
    is_top_market_score: bool = False,
) -> Tuple[bool, str]:
    """
    Heuristic filter (no LLM tokens spent):
    - Rejects ads with < 3 active days (unproven tests).
    - Skips ads from competitors with >= 10 evaluated ads in the last 90 days,
      unless market_score is in the top 20% for that competitor.
    """
    days = int(ad.get("days_active", 0) or 0)
    if days < 3:
        return False, "skipped_low_value: days_active < 3"

    if competitor_eval_count >= 10 and not is_top_market_score:
        return False, "skipped_low_value: competitor format saturated (>=10 evaluated)"

    return True, "eligible"


# Global singleton instance
quota_guard = QuotaGuard()
