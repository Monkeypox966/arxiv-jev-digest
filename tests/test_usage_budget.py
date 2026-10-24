import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from conftest import jev_response

from arxiv_digest.clients import DeepSeekClient, JevClient
from arxiv_digest.http import ServiceError
from arxiv_digest.usage import BudgetExceeded, UsageTracker, input_reservation


def tracker(**changes):
    settings = {
        "max_jev_calls": 10000,
        "max_deepseek_calls": 200,
        "max_input_tokens": 2000000,
        "max_output_tokens": 300000,
        **changes,
    }
    return UsageTracker(SimpleNamespace(**settings))


def completion(summary, usage=None, *, content=None):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": summary.model_dump_json() if content is None else content},
            }
        ],
        **({"usage": usage} if usage is not None else {}),
    }


def test_usage_reconciles_reservations_and_reports_cache_without_double_counting():
    usage = tracker()
    token = usage.reserve("deepseek", "metadata", input_tokens=1000, output_tokens=500)
    usage.finish(
        token,
        {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        },
    )
    usage.assert_within_limits()
    result = usage.snapshot()
    bucket = result["by_service"]["deepseek"]["metadata"]
    assert bucket["attempts"] == bucket["responses"] == 1
    assert bucket["input_tokens"] == bucket["budget_input_tokens"] == 100
    assert bucket["output_tokens"] == bucket["budget_output_tokens"] == 25
    assert bucket["cache_hit_input_tokens"] == 80
    assert bucket["cache_miss_input_tokens"] == 20
    assert result["usage_complete"]
    assert result["schema_version"] == 1
    json.dumps(result)


def test_missing_usage_keeps_conservative_budget_allowance_and_marks_unknown():
    usage = tracker()
    token = usage.reserve("jev", "screening", input_tokens=1000, output_tokens=500)
    usage.finish(token, responded=False)
    result = usage.snapshot()
    totals = result["totals"]
    assert totals["attempts"] == 1 and totals["responses"] == 0
    assert totals["input_tokens"] == totals["output_tokens"] == 0  # reported values only
    assert totals["budget_input_tokens"] == 1000
    assert totals["budget_output_tokens"] == 500
    assert totals["unknown_input_attempts"] == totals["unknown_output_attempts"] == 1
    assert not result["usage_complete"]


@pytest.mark.parametrize("invalid", [True, -1, "3", None, float("nan")])
def test_invalid_provider_usage_is_unknown_not_silently_treated_as_free(invalid):
    usage = tracker()
    token = usage.reserve("deepseek", "summary", input_tokens=100, output_tokens=50)
    usage.finish(token, {"prompt_tokens": invalid, "completion_tokens": invalid})
    assert not usage.snapshot()["usage_complete"]
    assert usage.snapshot()["totals"]["budget_input_tokens"] == 100


def test_openai_style_cache_usage_and_zero_tokens_are_known():
    usage = tracker()
    token = usage.reserve("deepseek", "metadata", input_tokens=100, output_tokens=50)
    usage.finish(
        token,
        {
            "input_tokens": 10,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 7},
        },
    )
    result = usage.snapshot()
    assert result["usage_complete"]
    assert result["totals"]["output_tokens"] == 0
    assert result["totals"]["cache_hit_input_tokens"] == 7


@pytest.mark.parametrize(
    "limits,amounts",
    [
        ({"max_input_tokens": 9}, (10, 1)),
        ({"max_output_tokens": 1}, (10, 2)),
    ],
)
def test_preflight_token_reservation_denies_call_before_attempt(limits, amounts):
    usage = tracker(**limits)
    with pytest.raises(BudgetExceeded) as error:
        usage.reserve("deepseek", "summary", input_tokens=amounts[0], output_tokens=amounts[1])
    assert error.value.fatal and not error.value.retryable
    assert usage.snapshot()["totals"]["attempts"] == 0
    assert usage.snapshot()["denied_attempts"] == 1
    with pytest.raises(BudgetExceeded):
        usage.assert_within_limits()


def test_reported_usage_exceeding_allowance_blocks_further_calls_and_delivery():
    usage = tracker(max_input_tokens=100)
    token = usage.reserve("jev", "screening", input_tokens=90, output_tokens=2)
    with pytest.raises(BudgetExceeded):
        usage.finish(token, {"input_tokens": 101, "output_tokens": 1})
    assert usage.snapshot()["totals"]["budget_input_tokens"] == 101
    assert usage.snapshot()["totals"]["in_flight"] == 0
    with pytest.raises(BudgetExceeded):
        usage.reserve("jev", "screening", input_tokens=0, output_tokens=0)
    with pytest.raises(BudgetExceeded):
        usage.assert_within_limits()


def test_strict_call_limit_is_thread_safe_under_concurrent_admission():
    usage = tracker(max_jev_calls=17)

    def attempt(_):
        try:
            token = usage.reserve("jev", "screening", input_tokens=10, output_tokens=2)
        except BudgetExceeded:
            return False
        usage.finish(token, {"input_tokens": 3, "output_tokens": 1})
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(attempt, range(100)))
    result = usage.snapshot()
    assert sum(admitted) == result["totals"]["jev_calls"] == 17
    assert result["totals"]["attempts"] == 17
    assert result["totals"]["in_flight"] == 0
    assert result["denied_attempts"] == 83


def test_in_flight_calls_block_delivery_and_cannot_be_settled_twice():
    usage = tracker()
    token = usage.reserve("jev", "screening", input_tokens=10, output_tokens=2)
    with pytest.raises(BudgetExceeded, match="in flight"):
        usage.assert_within_limits()
    usage.finish(token, {"input_tokens": 3, "output_tokens": 1})
    usage.assert_within_limits()
    with pytest.raises(ValueError, match="already settled"):
        usage.finish(token)


def test_jev_counts_all_http_retries_and_stops_before_exceeding_call_budget(config, paper):
    usage = tracker(max_jev_calls=2)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BudgetExceeded):
            JevClient(config, client, usage=usage).judge(paper)
    assert len(calls) == 2
    assert usage.snapshot()["by_service"]["jev"]["screening"]["attempts"] == 2


def test_jev_transport_failures_are_counted_without_exposing_request_data(config, paper):
    usage = tracker(max_jev_calls=1)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("provider detail must stay private", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BudgetExceeded) as error:
            JevClient(config, client, usage=usage).judge(paper)
    assert len(calls) == 1
    assert "provider detail" not in str(error.value)
    assert usage.snapshot()["totals"]["responses"] == 0


def test_jev_success_records_actual_usage(config, paper):
    usage = tracker()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jev_response(config)))
    ) as client:
        JevClient(config, client, usage=usage).judge(paper)
    result = usage.snapshot()
    assert result["totals"]["input_tokens"] == 600
    assert result["totals"]["output_tokens"] == 20
    assert result["usage_complete"]


def test_deepseek_summary_and_metadata_have_separate_attempt_buckets(config, paper, summary):
    usage = tracker()
    payload = completion(summary, {"prompt_tokens": 20, "completion_tokens": 10})
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        model = DeepSeekClient(config, client, usage=usage)
        assert model.summarize(paper) == summary
        model.complete_json("metadata", {"text": "header"}, json.loads)
    result = usage.snapshot()
    assert result["by_service"]["deepseek"]["summary"]["attempts"] == 1
    assert result["by_service"]["deepseek"]["metadata"]["attempts"] == 1
    assert result["totals"]["deepseek_calls"] == 2
    assert result["totals"]["input_tokens"] == 40


def test_semantic_retry_counts_paid_attempt_and_cannot_bypass_deepseek_budget(
    config, paper, summary
):
    usage = tracker(max_deepseek_calls=1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion(summary, content="invalid JSON"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BudgetExceeded):
            DeepSeekClient(config, client, usage=usage).summarize(paper)
    assert len(calls) == 1
    assert usage.snapshot()["totals"]["deepseek_calls"] == 1


@pytest.mark.parametrize("status", [401, 402, 403])
def test_model_auth_and_credit_failures_are_fatal_without_retries(config, paper, status):
    usage = tracker()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        with pytest.raises(ServiceError) as error:
            DeepSeekClient(config, client, usage=usage).summarize(paper)
    assert error.value.fatal and not error.value.retryable
    assert usage.snapshot()["totals"]["deepseek_calls"] == 1


def test_utf8_input_allowance_accounts_for_chinese_and_protocol_overhead():
    body = {"text": "中文摘要"}
    assert input_reservation(body) > len(json.dumps(body, ensure_ascii=False).encode())
