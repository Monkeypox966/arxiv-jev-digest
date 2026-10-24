"""Thread-safe attempt budgets and usage reporting, without provider price assumptions."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from .http import ServiceError


class BudgetExceeded(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, fatal=True)


@dataclass(frozen=True)
class Reservation:
    id: int
    service: str
    purpose: str
    input_tokens: int
    output_tokens: int


def input_reservation(body: dict) -> int:
    # UTF-8 bytes are deliberately conservative relative to ordinary token
    # counts. Provider-specific hidden prompts cannot be bounded by a local
    # tokenizer, so reports distinguish this allowance from reported usage.
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) + 1024


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first_count(data: dict, *names: str) -> int | None:
    return next((value for name in names if (value := _count(data.get(name))) is not None), None)


def _bucket() -> dict[str, int]:
    return dict.fromkeys(
        (
            "attempts",
            "responses",
            "input_tokens",
            "output_tokens",
            "cache_hit_input_tokens",
            "cache_miss_input_tokens",
            "unknown_input_attempts",
            "unknown_output_attempts",
            "budget_input_tokens",
            "budget_output_tokens",
            "in_flight",
        ),
        0,
    )


class UsageTracker:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._sequence = 0
        self._active: dict[int, Reservation] = {}
        self._buckets: dict[tuple[str, str], dict[str, int]] = {}
        self._service_calls = {"jev": 0, "deepseek": 0}
        self._input = self._output = 0
        self._breached = False
        self._denied = 0

    def reserve(
        self, service: str, purpose: str, *, input_tokens: int, output_tokens: int
    ) -> Reservation:
        if service not in self._service_calls:
            raise ValueError("Unknown budget service")
        if _count(input_tokens) is None or _count(output_tokens) is None:
            raise ValueError("Token reservations must be nonnegative integers")
        with self._lock:
            call_limit = getattr(self.settings, f"max_{service}_calls")
            if (
                self._breached
                or self._service_calls[service] >= call_limit
                or self._input + input_tokens > self.settings.max_input_tokens
                or self._output + output_tokens > self.settings.max_output_tokens
            ):
                self._denied += 1
                self._breached = True
                raise BudgetExceeded(
                    f"API budget exhausted before {service}/{purpose}; "
                    "completed results are cached; increase the budget or resume later"
                )
            self._sequence += 1
            token = Reservation(self._sequence, service, purpose, input_tokens, output_tokens)
            self._active[token.id] = token
            self._service_calls[service] += 1
            self._input += input_tokens
            self._output += output_tokens
            bucket = self._buckets.setdefault((service, purpose), _bucket())
            bucket["attempts"] += 1
            bucket["in_flight"] += 1
            bucket["budget_input_tokens"] += input_tokens
            bucket["budget_output_tokens"] += output_tokens
            return token

    def finish(self, token: Reservation, usage: dict | None = None, *, responded=True) -> None:
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = _first_count(usage, "input_tokens", "prompt_tokens")
        output_tokens = _first_count(usage, "output_tokens", "completion_tokens")
        cached = _first_count(usage, "prompt_cache_hit_tokens", "cache_read_input_tokens")
        if cached is None:
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
            cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
        cache_miss = _first_count(usage, "prompt_cache_miss_tokens")
        with self._lock:
            if self._active.get(token.id) != token:
                raise ValueError("Unknown or already settled token reservation")
            del self._active[token.id]
            bucket = self._buckets[(token.service, token.purpose)]
            bucket["in_flight"] -= 1
            bucket["responses"] += int(responded)
            for name, reported, reserved in (
                ("input", input_tokens, token.input_tokens),
                ("output", output_tokens, token.output_tokens),
            ):
                if reported is None:
                    bucket[f"unknown_{name}_attempts"] += 1
                else:
                    bucket[f"{name}_tokens"] += reported
                    adjustment = reported - reserved
                    bucket[f"budget_{name}_tokens"] += adjustment
                    if name == "input":
                        self._input += adjustment
                    else:
                        self._output += adjustment
            bucket["cache_hit_input_tokens"] += cached or 0
            bucket["cache_miss_input_tokens"] += cache_miss or 0
            if (
                self._input > self.settings.max_input_tokens
                or self._output > self.settings.max_output_tokens
            ):
                self._breached = True
                raise BudgetExceeded(
                    "Provider-reported token usage exceeded the run budget; "
                    "no new API calls or email delivery are allowed"
                )

    def assert_within_limits(self) -> None:
        with self._lock:
            if self._breached:
                raise BudgetExceeded("API budget was exhausted; email delivery is blocked")
            if self._active:
                raise BudgetExceeded("API requests are still in flight; email delivery is blocked")

    def snapshot(self) -> dict:
        with self._lock:
            by_service: dict[str, dict] = {}
            totals = _bucket()
            for (service, purpose), bucket in sorted(self._buckets.items()):
                by_service.setdefault(service, {})[purpose] = dict(bucket)
                for name, value in bucket.items():
                    totals[name] += value
            totals["jev_calls"] = self._service_calls["jev"]
            totals["deepseek_calls"] = self._service_calls["deepseek"]
            return {
                "schema_version": 1,
                "by_service": by_service,
                "totals": totals,
                "limits": {
                    name: getattr(self.settings, name)
                    for name in (
                        "max_jev_calls",
                        "max_deepseek_calls",
                        "max_input_tokens",
                        "max_output_tokens",
                    )
                },
                "budget_exhausted": self._breached,
                "denied_attempts": self._denied,
                "usage_complete": not (
                    totals["unknown_input_attempts"] or totals["unknown_output_attempts"]
                ),
                "token_budget_basis": (
                    "reported usage when available; conservative request byte/output allowances "
                    "otherwise; provider-side hidden tokenization may exceed local allowances"
                ),
            }
