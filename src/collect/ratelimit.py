"""
Polite rate limiting towards Meta Ad Library (Phase 7, B6).
- A random 1-3 s pause before every query.
- Exponential backoff (with jitter) when Meta blocks or throttles; MetaBlockedError once retries run out, so the
  caller can stop discovering instead of hammering a blocked session.
Sleep and randomness are injectable so tests run instantly and deterministically.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable

logger = logging.getLogger("Jinora.RateLimit")

_BLOCK_TEXT = re.compile(r"\b(429|403)\b|rate.?limit|too many requests|blocked|captcha|temporarily|checkpoint|login required", re.I)


class MetaBlockedError(Exception):
    """Meta kept blocking/throttling after all backoff retries."""


def is_block_error(exc: BaseException) -> bool:
    """True for the collector's rate-limit/session errors or any error text that looks like a block."""
    try:
        from meta_ads_collector.exceptions import RateLimitError, SessionExpiredError

        if isinstance(exc, (RateLimitError, SessionExpiredError)):
            return True
    except Exception:  # collector not importable (never in production)
        pass
    return bool(_BLOCK_TEXT.search(f"{type(exc).__name__} {exc}"))


class MetaRateLimiter:
    def __init__(
        self,
        min_pause: float = 1.0,
        max_pause: float = 3.0,
        max_retries: int = 4,
        backoff_base: float = 30.0,
        backoff_max: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.min_pause, self.max_pause = min_pause, max_pause
        self.max_retries, self.backoff_base, self.backoff_max = max_retries, backoff_base, backoff_max
        self._sleep, self._uniform = sleep, uniform
        self.queries = 0
        self.blocks = 0

    def wait(self) -> float:
        """Random pause between queries; returns the seconds slept."""
        pause = self._uniform(self.min_pause, self.max_pause)
        self._sleep(pause)
        return pause

    def backoff_delay(self, attempt: int) -> float:
        """Delay before retry number `attempt` (1-based): base * 2^(attempt-1) plus up to 10% jitter, capped."""
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        return delay + self._uniform(0, delay * 0.1)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Runs one Meta query: random pause first, then fn; on a block waits and retries with backoff."""
        self.wait()
        for attempt in range(1, self.max_retries + 2):
            self.queries += 1
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if not is_block_error(exc):
                    raise
                self.blocks += 1
                if attempt > self.max_retries:
                    raise MetaBlockedError(f"Meta sigue bloqueando tras {self.max_retries} reintentos: {exc}") from exc
                delay = self.backoff_delay(attempt)
                logger.warning(f"Meta bloqueó/limitó la consulta ({type(exc).__name__}). Reintento {attempt}/{self.max_retries} en {delay:.0f}s")
                self._sleep(delay)
        raise MetaBlockedError("unreachable")  # pragma: no cover

