"""
Jinora AI - Quota Guard & Free Tier Rate Limiter
Enforces Google Gemini Free Tier constraints:
- Requests Per Day (RPD) with Pacific Time (America/Los_Angeles) reset tracking
- Accurate attempt tracking for ALL requests (success, 503, 429, timeouts)
- Immediate halt upon daily 429 RPD exhaustion until midnight PT
- Requests Per Minute (RPM) & Tokens Per Minute (TPM) sliding window pacing
- Exponential backoff on HTTP 429 errors
- Daily usage counted per API key (key_fingerprint = sha256(key)[:8]); a new key starts at 0
- Prioritized evaluation queue and low-value ad skipping
"""

from __future__ import annotations

import hashlib
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

# Pacific Time Zone used by Google AI Studio to reset daily limits (midnight PT = 02:00 AM America/Lima)
try:
    PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC_TZ = timezone.utc


NO_KEY_FINGERPRINT = "sinclave"  # not hex, never collides with a real fingerprint
LEGACY_KEY_FINGERPRINT = ""  # rows recorded before per-key tracking, and non-Gemini providers


def compute_key_fingerprint(api_key: Optional[str]) -> str:
    """First 8 hex chars of sha256(api_key). The key itself is never stored or printed."""
    if not api_key or not api_key.strip():
        return NO_KEY_FINGERPRINT
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:8]


def get_current_day_pt(current_dt: Optional[datetime] = None) -> str:
    """Return the current date string (YYYY-MM-DD) in America/Los_Angeles."""
    if current_dt:
        if current_dt.tzinfo is None:
            dt = current_dt.replace(tzinfo=PACIFIC_TZ)
        else:
            dt = current_dt.astimezone(PACIFIC_TZ)
        return dt.strftime("%Y-%m-%d")
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

    def __init__(self, db_path: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self._api_key = api_key
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

    @property
    def key_fingerprint(self) -> str:
        """Fingerprint of the Gemini key in use (explicit key, else GEMINI_API_KEY from settings)."""
        return compute_key_fingerprint(self._api_key if self._api_key is not None else settings.GEMINI_API_KEY)

    def _resolve_fp(self, key_fingerprint: Optional[str], provider: str = "gemini") -> str:
        if key_fingerprint is not None:
            return key_fingerprint
        return self.key_fingerprint if provider == "gemini" else LEGACY_KEY_FINGERPRINT

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _USAGE_DDL = """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_pt TEXT NOT NULL,
            model TEXT NOT NULL,
            key_fingerprint TEXT NOT NULL DEFAULT '',
            provider TEXT DEFAULT 'gemini',
            status TEXT DEFAULT 'active',
            costo_estimado_usd REAL DEFAULT 0.0,
            requests INTEGER NOT NULL DEFAULT 0,
            requests_success INTEGER NOT NULL DEFAULT 0,
            requests_failed INTEGER NOT NULL DEFAULT 0,
            is_exhausted BOOLEAN DEFAULT 0,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(day_pt, model, key_fingerprint)
        );
    """

    def _backup_db_file(self, label: str) -> None:
        if not self.db_path.exists():
            return
        backup_dir = self.db_path.parent / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        src = sqlite3.connect(str(self.db_path))
        dst = sqlite3.connect(str(backup_dir / f"{self.db_path.stem}_before_{label}_{stamp}.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def _migrate_usage_to_key_fingerprint(self, conn: sqlite3.Connection) -> None:
        """
        Rebuilds llm_usage with UNIQUE(day_pt, model, key_fingerprint); SQLite cannot alter a
        UNIQUE constraint in place. Existing rows are kept with key_fingerprint = '' (legacy).
        """
        self._backup_db_file("key_fingerprint")
        cols = [
            "id", "day_pt", "model", "provider", "status", "costo_estimado_usd", "requests",
            "requests_success", "requests_failed", "is_exhausted", "tokens_in", "tokens_out", "created_at",
        ]
        col_list = ", ".join(cols)
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        try:
            conn.execute("DROP TABLE IF EXISTS llm_usage_new")
            conn.execute(self._USAGE_DDL.format(name="llm_usage_new"))
            conn.execute(
                f"INSERT INTO llm_usage_new ({col_list}, key_fingerprint) "
                f"SELECT {col_list}, '' FROM llm_usage"
            )
            conn.execute("DROP TABLE llm_usage")
            conn.execute("ALTER TABLE llm_usage_new RENAME TO llm_usage")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logger.info("llm_usage migrada a cuota por clave (key_fingerprint); registros previos conservados.")

    def _init_db(self) -> None:
        """Create llm_usage and llm_attempts tables if they do not exist and ensure columns."""
        with self._get_connection() as conn:
            conn.execute(self._USAGE_DDL.format(name="llm_usage"))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_pt TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT DEFAULT 'gemini',
                    status TEXT NOT NULL,
                    error_message TEXT,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    costo_estimado_usd REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(llm_usage)")
            cols = [row["name"] for row in cur.fetchall()]
            if "provider" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN provider TEXT DEFAULT 'gemini'")
            if "costo_estimado_usd" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN costo_estimado_usd REAL DEFAULT 0.0")
            if "status" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN status TEXT DEFAULT 'active'")
            if "requests_success" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN requests_success INTEGER DEFAULT 0")
            if "requests_failed" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN requests_failed INTEGER DEFAULT 0")
            if "is_exhausted" not in cols:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN is_exhausted BOOLEAN DEFAULT 0")
            if "key_fingerprint" not in cols:
                self._migrate_usage_to_key_fingerprint(conn)

            cur.execute("PRAGMA table_info(llm_attempts)")
            if "key_fingerprint" not in [row["name"] for row in cur.fetchall()]:
                conn.execute("ALTER TABLE llm_attempts ADD COLUMN key_fingerprint TEXT DEFAULT ''")
            conn.commit()

    def get_daily_usage(
        self, model: str, day_pt: Optional[str] = None, key_fingerprint: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch current day usage metrics for a specific model and API key."""
        day = day_pt or get_current_day_pt()
        fp = self._resolve_fp(key_fingerprint)
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT requests, requests_success, requests_failed, tokens_in, tokens_out, is_exhausted, status
                FROM llm_usage
                WHERE day_pt = ? AND model = ? AND key_fingerprint = ?
                """,
                (day, model, fp),
            )
            row = cur.fetchone()
            if row:
                col_keys = row.keys()
                return {
                    "requests": int(row["requests"]),
                    "requests_success": int(row["requests_success"] if "requests_success" in col_keys and row["requests_success"] is not None else row["requests"]),
                    "requests_failed": int(row["requests_failed"] if "requests_failed" in col_keys and row["requests_failed"] is not None else 0),
                    "tokens_in": int(row["tokens_in"]),
                    "tokens_out": int(row["tokens_out"]),
                    "is_exhausted": bool(row["is_exhausted"] if "is_exhausted" in col_keys and row["is_exhausted"] is not None else False),
                    "status": str(row["status"] if "status" in col_keys and row["status"] else "active"),
                }
        return {
            "requests": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "is_exhausted": False,
            "status": "active",
        }

    def mark_exhausted(
        self, model: str, day_pt: Optional[str] = None, key_fingerprint: Optional[str] = None
    ) -> None:
        """Mark model quota as exhausted for the given day (PT) and API key only."""
        day = day_pt or get_current_day_pt()
        fp = self._resolve_fp(key_fingerprint)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage (day_pt, model, key_fingerprint, is_exhausted, status, requests)
                VALUES (?, ?, ?, 1, '429_rpd', 0)
                ON CONFLICT(day_pt, model, key_fingerprint) DO UPDATE SET
                    is_exhausted = 1,
                    status = '429_rpd';
                """,
                (day, model, fp),
            )
            conn.commit()
        logger.warning(
            f"🛑 Quota marked as EXHAUSTED for model {model}, key {fp} on {day} (PT). Blocked until midnight PT."
        )

    def is_exhausted(
        self, model: str, day_pt: Optional[str] = None, key_fingerprint: Optional[str] = None
    ) -> bool:
        """Check if quota is explicitly marked as exhausted for today and this API key."""
        usage = self.get_daily_usage(model, day_pt, key_fingerprint)
        return bool(usage.get("is_exhausted", False))

    def get_available_requests(
        self, model: str, day_pt: Optional[str] = None, key_fingerprint: Optional[str] = None
    ) -> int:
        """Calculate remaining usable requests for today considering reserve requests and exhaustion flag."""
        if not settings.FREE_TIER_MODE:
            return 999999

        if self.is_exhausted(model, day_pt, key_fingerprint):
            return 0

        raw_limit = settings.get_rpd_for_model(model)
        effective_limit = max(0, raw_limit - settings.RESERVE_REQUESTS)
        usage = self.get_daily_usage(model, day_pt, key_fingerprint)
        remaining = effective_limit - usage["requests"]
        return max(0, remaining)

    def can_acquire(self, model: str, requests_needed: int = 1, key_fingerprint: Optional[str] = None) -> bool:
        """Check if quota allows acquiring additional requests."""
        if self.is_exhausted(model, key_fingerprint=key_fingerprint):
            return False
        return self.get_available_requests(model, key_fingerprint=key_fingerprint) >= requests_needed

    def get_key_usage_today(self, key_fingerprint: Optional[str] = None, day_pt: Optional[str] = None) -> int:
        """Total Gemini requests recorded today for one API key, across models."""
        day = day_pt or get_current_day_pt()
        fp = self._resolve_fp(key_fingerprint)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(requests), 0) AS total FROM llm_usage WHERE day_pt = ? AND key_fingerprint = ?",
                (day, fp),
            ).fetchone()
        return int(row["total"])

    def acquire(self, model: str, est_tokens: int = 0, key_fingerprint: Optional[str] = None) -> None:
        """
        Reserve an API request slot.
        - Enforces RPD: raises QuotaExhausted if daily cap reached or marked exhausted.
        - Enforces RPM/TPM: sleeps if rate limits are momentarily saturated.
        """
        if not settings.FREE_TIER_MODE:
            return

        day_pt = get_current_day_pt()
        raw_limit = settings.get_rpd_for_model(model)
        effective_limit = max(0, raw_limit - settings.RESERVE_REQUESTS)
        fp = self._resolve_fp(key_fingerprint)

        if self.is_exhausted(model, day_pt, fp):
            raise QuotaExhausted(
                model=model,
                day_pt=day_pt,
                requests_used=raw_limit,
                limit=effective_limit,
                reason="429 RESOURCE_EXHAUSTED: Límite diario RPD agotado. Cola detenida hasta medianoche PT.",
            )

        usage = self.get_daily_usage(model, day_pt, fp)

        if usage["requests"] >= effective_limit:
            logger.warning(
                f"🚨 QuotaGuard: Daily limit reached for {model} (key {fp}) on {day_pt}. "
                f"Used: {usage['requests']}/{effective_limit} (Reserve: {settings.RESERVE_REQUESTS})"
            )
            raise QuotaExhausted(
                model=model,
                day_pt=day_pt,
                requests_used=usage["requests"],
                limit=effective_limit,
                reason=f"Reached daily usable limit ({effective_limit} requests)",
            )

        # Sliding window for RPM (Requests Per Minute)
        now = time.time()
        self._request_timestamps = [t for t in self._request_timestamps if now - t < 60.0]
        if len(self._request_timestamps) >= settings.GEMINI_RPM:
            oldest_request_time = self._request_timestamps[0]
            sleep_time = max(0.1, 60.0 - (now - oldest_request_time) + 0.5)
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

    def record_attempt(
        self,
        model: str,
        provider: str = "gemini",
        status: str = "success",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        error_message: Optional[str] = None,
        day_pt: Optional[str] = None,
        key_fingerprint: Optional[str] = None,
    ) -> None:
        """
        Record every single API request attempt sent over the wire (successful or failed).
        Inserts row into llm_attempts and updates cumulative llm_usage for that API key.
        """
        day = day_pt or get_current_day_pt()
        fp = self._resolve_fp(key_fingerprint, provider)
        now_utc = datetime.now(timezone.utc).isoformat()
        is_success = (status.lower() == "success")

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_attempts (day_pt, timestamp_utc, model, provider, status, error_message, tokens_in, tokens_out, costo_estimado_usd, key_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (day, now_utc, model, provider, status, error_message, tokens_in, tokens_out, cost_usd, fp),
            )

            is_exhausted_flag = 1 if status in ("429_rpd", "rpd_exhausted") else 0
            conn.execute(
                """
                INSERT INTO llm_usage (
                    day_pt, model, key_fingerprint, provider, status, costo_estimado_usd,
                    requests, requests_success, requests_failed, is_exhausted, tokens_in, tokens_out
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(day_pt, model, key_fingerprint) DO UPDATE SET
                    requests = requests + 1,
                    requests_success = requests_success + excluded.requests_success,
                    requests_failed = requests_failed + excluded.requests_failed,
                    tokens_in = tokens_in + excluded.tokens_in,
                    tokens_out = tokens_out + excluded.tokens_out,
                    costo_estimado_usd = costo_estimado_usd + excluded.costo_estimado_usd,
                    status = excluded.status,
                    is_exhausted = CASE WHEN excluded.is_exhausted = 1 THEN 1 ELSE llm_usage.is_exhausted END,
                    provider = excluded.provider;
                """,
                (
                    day, model, fp, provider, status, cost_usd,
                    1 if is_success else 0,
                    0 if is_success else 1,
                    is_exhausted_flag,
                    tokens_in, tokens_out,
                ),
            )
            conn.commit()

        logger.info(
            f"📊 LLM Attempt recorded [{provider} | {model} | key {fp or '-'} | {day}]: status={status}, +1 req "
            f"({'SUCCESS' if is_success else 'FAIL'}), +{tokens_in} tok_in, +{tokens_out} tok_out"
        )

    def record_usage(
        self,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        requests: int = 1,
        day_pt: Optional[str] = None,
        provider: str = "gemini",
        cost_usd: float = 0.0,
        status: str = "success",
        error_message: Optional[str] = None,
        key_fingerprint: Optional[str] = None,
    ) -> None:
        """Atomically record token, request and cost consumption (calls record_attempt)."""
        req_count = max(1, requests)
        for _ in range(req_count):
            self.record_attempt(
                model=model,
                provider=provider,
                status=status,
                tokens_in=tokens_in // req_count,
                tokens_out=tokens_out // req_count,
                cost_usd=cost_usd / req_count,
                error_message=error_message,
                day_pt=day_pt,
                key_fingerprint=key_fingerprint,
            )

    def get_total_spend(self, provider: Optional[str] = None) -> float:
        """Calculate total cumulative spend in USD (e.g. for Claude budget tracking)."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            if provider:
                cur.execute("SELECT SUM(costo_estimado_usd) as total FROM llm_usage WHERE provider = ?", (provider,))
            else:
                cur.execute("SELECT SUM(costo_estimado_usd) as total FROM llm_usage")
            row = cur.fetchone()
            if row and row["total"] is not None:
                return float(row["total"])
        return 0.0

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
                res = func(*args, **kwargs)
                self.record_attempt(model=model, provider="gemini", status="success")
                return res
            except Exception as exc:
                last_exception = exc
                err_str = str(exc).lower()

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
                    or "generaterequestsperday" in err_str
                    or "limit: 20" in err_str
                )

                status_code = "429_rpd" if is_daily_quota else ("429" if is_rate_limit else "error")
                self.record_attempt(model=model, provider="gemini", status=status_code, error_message=err_str[:200])

                if is_rate_limit:
                    if is_daily_quota:
                        self.mark_exhausted(model)
                        day_pt = get_current_day_pt()
                        usage = self.get_daily_usage(model, day_pt)
                        raise QuotaExhausted(
                            model=model,
                            day_pt=day_pt,
                            requests_used=usage["requests"],
                            limit=settings.get_rpd_for_model(model),
                            reason=f"Google API returned quota error: {exc}",
                        ) from exc

                    if attempt == total_attempts:
                        break

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
    1. Active ads (is_active == True) strictly precede inactive ads (inactives have lowest priority)
    2. priority == 1 (LatAm WhatsApp & CRM core competitors)
    3. market_score (descending)
    4. days_active / recency (descending)
    """
    def _sort_key(ad: Dict[str, Any]) -> Tuple[int, int, float, int]:
        is_active = bool(ad.get("is_active", True))
        active_rank = 0 if is_active else 1

        priority = int(ad.get("priority", 2))
        is_p1 = 0 if priority == 1 else 1

        market_score = float(ad.get("market_score", 0.0) or 0.0)
        days_active = int(ad.get("days_active", 0) or 0)

        return (active_rank, is_p1, -market_score, -days_active)

    return sorted(ads, key=_sort_key)


def should_evaluate_ad(
    ad: Dict[str, Any],
    competitor_eval_count: int = 0,
    is_top_market_score: bool = False,
) -> Tuple[bool, str]:
    """
    Heuristic filter (no LLM tokens spent):
    - Rejects active ads with < 3 active days (unproven tests).
    - Inactive ads: data only; only eligible for evaluation if days_active > 60.
    - Skips ads from competitors with >= 10 evaluated ads in the last 90 days,
      unless market_score is in the top 20% for that competitor.
    """
    is_active = bool(ad.get("is_active", True))
    days = int(ad.get("days_active", 0) or 0)

    if not is_active:
        if days <= 60:
            return False, "skipped_data_only: inactive ad <= 60 days (data only, no LLM)"

    if is_active and days < 3:
        return False, "skipped_low_value: days_active < 3"

    if competitor_eval_count >= 10 and not is_top_market_score:
        return False, "skipped_low_value: competitor format saturated (>=10 evaluated)"

    return True, "eligible"


# Global singleton instance
quota_guard = QuotaGuard()


def get_quota_guard() -> QuotaGuard:
    """Return the global QuotaGuard singleton instance."""
    return quota_guard
