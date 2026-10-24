from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
USER_AGENT = "arxiv-jev-digest/0.2 (+https://arxiv.org/help/api)"


class ServiceError(RuntimeError):
    """Safe error without request headers, response bodies, or private email addresses."""

    def __init__(self, message: str, *, retryable=False, delay: float | None = None, fatal=False):
        super().__init__(message)
        self.retryable = retryable
        self.delay = delay
        self.fatal = fatal


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = max(0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay:
            time.sleep(delay)


def retry_delay(value: str | None, attempt: int) -> float:
    try:
        delay = float(value) if value else 2**attempt
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 2**attempt
    return max(0, min(delay, 60))


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    service: str,
    limiter: RateLimiter,
    attempts: int = 3,
    before_attempt: Callable | None = None,
    after_attempt: Callable | None = None,
    **kwargs,
) -> httpx.Response:
    for attempt in range(attempts):
        limiter.wait()
        reservation = before_attempt() if before_attempt else None
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError:
            if after_attempt:
                after_attempt(reservation, None)
            if attempt + 1 == attempts:
                raise ServiceError(
                    f"{service}: network request failed after {attempts} attempts", retryable=True
                ) from None
            time.sleep(retry_delay(None, attempt))
            continue
        if after_attempt:
            after_attempt(reservation, response)
        if response.is_success:
            return response
        if response.status_code not in RETRYABLE or attempt + 1 == attempts:
            raise ServiceError(
                f"{service}: HTTP {response.status_code}",
                retryable=response.status_code in RETRYABLE,
                delay=retry_delay(response.headers.get("retry-after"), attempt),
                fatal=service in {"Jev", "DeepSeek"} and response.status_code in {401, 402, 403},
            )
        time.sleep(retry_delay(response.headers.get("retry-after"), attempt))
    raise AssertionError("unreachable")
