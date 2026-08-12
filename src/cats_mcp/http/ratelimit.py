"""Rate-limit interpretation and retry policy for the CATS API.

CATS's documented standard limit is 500 requests/hour on a rolling basis;
individual accounts can be raised (this project's account is at 1,500). The
ceiling is therefore *not* knowable from configuration, so it is read from the
response headers on every call rather than assumed.

The previous implementations got two things wrong that matter under a tight
budget: neither honoured `Retry-After`, and the retrying one used a fixed
exponential backoff with no jitter, which synchronises concurrent callers into
retry storms against the same limit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

#: CATS returns these on every response.
HEADER_LIMIT = "X-Rate-Limit-Limit"
HEADER_REMAINING = "X-Rate-Limit-Remaining"
HEADER_RETRY_AFTER = "Retry-After"

#: Conservative default used only until the first response is seen.
DEFAULT_ASSUMED_LIMIT = 500

#: Never sleep longer than this for a single attempt, even if CATS asks for
#: more. A tool call that blocks for minutes is worse than a clear failure the
#: orchestrator can reschedule.
MAX_RETRY_SLEEP_SECONDS = 60.0


@dataclass
class RateLimitState:
    """Most recent view of the account's rate-limit budget."""

    limit: int | None = None
    remaining: int | None = None

    def observe(self, headers) -> None:
        self.limit = _int_or_none(headers.get(HEADER_LIMIT)) or self.limit
        remaining = _int_or_none(headers.get(HEADER_REMAINING))
        if remaining is not None:
            self.remaining = remaining

    @property
    def effective_limit(self) -> int:
        return self.limit or DEFAULT_ASSUMED_LIMIT

    def is_nearly_exhausted(self, threshold: float = 0.1) -> bool:
        """True when less than `threshold` of the budget remains.

        Callers use this to log a warning; the adapter does not block requests
        on its own, because deciding to stop is the orchestrator's call.
        """
        if self.remaining is None:
            return False
        return self.remaining < max(1, int(self.effective_limit * threshold))

    def snapshot(self) -> dict[str, int | None]:
        return {"limit": self.limit, "remaining": self.remaining}


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def retry_delay(
    attempt: int,
    *,
    retry_after: str | None = None,
    base_delay: float = 1.0,
) -> float:
    """Seconds to wait before the next attempt.

    `Retry-After` wins when CATS sends it - it is the server telling us exactly
    when it will accept traffic again, and guessing shorter just wastes budget
    on requests that will be rejected.

    Otherwise: exponential backoff with full jitter. Full jitter (a uniform draw
    over the whole window rather than a fixed value plus noise) is what actually
    de-correlates concurrent clients.
    """
    explicit = _int_or_none(retry_after)
    if explicit is not None and explicit >= 0:
        return min(float(explicit), MAX_RETRY_SLEEP_SECONDS)

    window = min(base_delay * (2**attempt), MAX_RETRY_SLEEP_SECONDS)
    return random.uniform(0.0, window)


def is_retryable_status(status_code: int) -> bool:
    """429 and 5xx are worth retrying; 4xx client errors are not.

    Retrying a 400 or 404 wastes a request against a 500/hour budget and cannot
    succeed - the request itself is wrong.
    """
    return status_code == 429 or 500 <= status_code < 600
