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


@pytest.mark.parametrize(
    "first", ["invalid_json", "empty", "truncated", "503", "malformed_finish_reason"]
)
def test_summary_retries_incomplete_or_transient(config, paper, summary, first):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            if first == "503":
                return httpx.Response(503)
            content = "" if first == "empty" else "not-json"
            reason = "length" if first == "truncated" else "stop"
            if first == "malformed_finish_reason":
                reason = {"unexpected": "format"}
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


def test_summary_cache_invalidates_on_language_or_content(config, paper, monkeypatch):
    key = summary_key(config, paper)
    with monkeypatch.context() as previous:
        previous.setattr("arxiv_digest.clients.SUMMARY_PROMPT_VERSION", 4)
        assert summary_key(config, paper) != key
    config.deepseek.language = "English"
    assert summary_key(config, paper) != key


@pytest.mark.parametrize(
    "change,field,error_type",
    [
        ({"method": ["step one"]}, "method", "string_type"),
        ({"limitations": None}, "limitations", "string_type"),
        ({"tldr": " "}, "tldr", "string_too_short"),
    ],
)
def test_summary_retry_explains_schema_failure_without_changing_source(
    config, paper, summary, change, field, error_type
):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        value = summary.model_dump()
        if len(calls) == 1:
            value.update(change)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(value)},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert DeepSeekClient(config, client).summarize(paper) == summary
    assert len(calls) == 2
    correction = calls[1]["messages"][0]["content"]
    assert error_type in correction and field in correction
    assert "Required JSON schema" in correction
    assert calls[1]["messages"][1] == calls[0]["messages"][1]
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"] == config.deepseek.max_tokens


def test_summary_failure_is_bounded_and_diagnostics_do_not_leak_model_values(
    config, paper, summary, caplog
):
    calls = []
    secret = "untrusted-response-value-must-not-be-logged"
    bad = {**summary.model_dump(), secret: secret, "method": [secret]}

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(bad)},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError, match="schema_validation") as error:
            DeepSeekClient(config, client).summarize(paper)
    assert len(calls) == config.deepseek.attempts
    assert paper.id in str(error.value) and paper.id in caplog.text
    assert "string_type" in caplog.text and "method" in caplog.text
    assert secret not in caplog.text and secret not in str(error.value)
    assert secret not in json.dumps(calls)


def test_truncated_even_parseable_translation_is_regenerated_without_abridging(
    config, paper, summary
):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if len(calls) == 1 else "stop",
                        "message": {"content": summary.model_dump_json()},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert DeepSeekClient(config, client).summarize(paper) == summary
    assert len(calls) == 2
    assert "unfinished_completion" in calls[1]["messages"][0]["content"]
    assert "never shorten or omit source content" in calls[1]["messages"][0]["content"]
    assert "compact enough" not in calls[1]["messages"][0]["content"]


def test_full_translation_exceeds_old_character_cap_without_truncation(config, paper, summary):
    translation = (
        "完整翻译原文中的研究内容、实验数据与限定条件。" * 80
    ) + "\n\n最后一句保留 https://example.org 和 42%。"
    value = {**summary.model_dump(), "tldr": translation}
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DeepSeekClient(config, client).summarize(paper)
    assert len(translation) > 1200
    assert result.tldr == translation
    assert json.loads(calls[0]["messages"][1]["content"]) == {
        "title": paper.title,
        "abstract": paper.abstract,
    }
    assert "160-220" not in calls[0]["messages"][0]["content"]
