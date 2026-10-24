import json

import httpx
import pytest
from conftest import jev_response

from arxiv_digest.clients import DeepSeekClient, JevClient, parse_decision, summary_key
from arxiv_digest.http import ServiceError


@pytest.mark.parametrize("value", [True, "0.9", None, float("nan"), float("inf"), -0.01, 1.1])
def test_invalid_probabilities(config, value):
    data = jev_response(config)
    data["answers"]["interest_agents"]["noul"] = value
    with pytest.raises(ServiceError):
        parse_decision(data, config)


def test_missing_answer_never_becomes_zero(config):
    data = jev_response(config)
    del data["answers"]["interest_agents"]
    with pytest.raises(ServiceError):
        parse_decision(data, config)


def test_weighted_scoring_and_exclusion(config):
    data = jev_response(config, interest=0.8, exclusion=0.9)
    decision = parse_decision(data, config)
    assert decision.score == pytest.approx(0.8)
    assert decision.excluded
    config.selection.combine = "noisy_or"
    assert parse_decision(data, config).score == pytest.approx(1 - 0.2 * 0.28 * 0.28)


def test_jev_wire_format_and_no_author_bias(config, paper):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=jev_response(config))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        JevClient(config, client).judge(paper)
    body = captured[0]
    assert "authors" not in body["state"]
    assert len(body["questions"]) == len(config.interests) + len(config.exclusions)
    assert body["questions"]["interest_agents"]["type"] == "noul"


@pytest.mark.parametrize("status,expected_calls", [(401, 1), (402, 1), (429, 3), (503, 3)])
def test_jev_retry_policy(config, paper, status, expected_calls):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"secret": "must not appear in error"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError) as error:
            JevClient(config, client).judge(paper)
    assert len(calls) == expected_calls
    assert "must not appear" not in str(error.value)


@pytest.mark.parametrize("first", ["invalid_json", "empty", "truncated", "503"])
def test_summary_retries_incomplete_or_transient(config, paper, summary, first):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            if first == "503":
                return httpx.Response(503)
            content = "" if first == "empty" else "not-json"
            reason = "length" if first == "truncated" else "stop"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": reason,
                            "message": {"content": content},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": summary.model_dump_json()},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DeepSeekClient(config, client).summarize(paper)
    assert result == summary
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["thinking"] == {"type": "disabled"}


def test_summary_fatal_auth_does_not_retry(config, paper):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError):
            DeepSeekClient(config, client).summarize(paper)
    assert len(calls) == 1


def test_summary_cache_invalidates_on_language_or_content(config, paper):
    key = summary_key(config, paper)
    config.deepseek.language = "English"
    assert summary_key(config, paper) != key
