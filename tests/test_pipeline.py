from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import atom, jev_response

from arxiv_digest.clients import profile_key
from arxiv_digest.http import ServiceError
from arxiv_digest.pipeline import run
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 24, tzinfo=UTC)


class Mailer:
    def __init__(self, messages, fail_recipient=None):
        self.messages = messages
        self.fail_recipient = fail_recipient

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, message):
        self.messages.append(message)
        if message["To"] == self.fail_recipient:
            raise ServiceError("Simulated uncertain delivery")


def transport(config, summary, calls, *, summary_fails=False):
    def handler(request):
        if request.url.host == "export.arxiv.org":
            calls["arxiv"] += 1
            return httpx.Response(200, text=atom(["2610.00001v1", "2610.00002v2", "2610.00003"]))
        if request.url.host == "api.typesafe.ai":
            calls["jev"] += 1
            data = json.loads(request.content)
            id = data["state"]["title"].split()[-1]
            scores = {"2610.00001v1": 0.95, "2610.00002v2": 0.75, "2610.00003": 0.2}
            return httpx.Response(200, json=jev_response(config, interest=scores[id]))
        if request.url.host == "api.deepseek.com":
            calls["summary"] += 1
            if summary_fails:
                return httpx.Response(200, json={"choices": []})
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
        raise AssertionError(f"Unexpected endpoint: {request.url.host}")

    return httpx.MockTransport(handler)


def counts():
    return {"arxiv": 0, "jev": 0, "summary": 0}


def test_selection_summary_budget_successful_send_and_dedup(config, summary):
    config.selection.top_k = 1
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        first = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        second = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert first["selected_unique"] == 1
    assert calls == {"arxiv": 2, "jev": 3, "summary": 1}
    assert len(messages) == 1
    assert second["emails_sent"] == 0
    from pathlib import Path

    report = json.loads((Path(first["output_dir"]) / "digest.json").read_text(encoding="utf-8"))
    assert report["papers"][0]["paper"]["id"] == "2610.00001"
    assert "reader@example.com" not in json.dumps(report)
    with Store(config.state_dir) as store:
        # Overflow below top_k was considered and will not trickle into future digests.
        assert not store.eligible(
            recipient_key("reader@example.com"), profile_key(config), "2610.00002"
        )


def test_dry_run_reuses_cache_but_does_not_mark_sent(config, summary):
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        run(config, client, dry_run=True, now=NOW)
        result = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert result["emails_sent"] == 1
    assert len(messages) == 1
    assert calls == {"arxiv": 2, "jev": 3, "summary": 2}


def test_incomplete_summary_never_sends_and_can_resume(config, summary):
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls, summary_fails=True)) as client:
        with pytest.raises(ServiceError):
            run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert not messages
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert calls["jev"] == 3
    assert len(messages) == 1


def test_uncertain_send_blocks_retry_until_resolved(config, summary):
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        with pytest.raises(ServiceError, match="uncertain"):
            run(
                config,
                client,
                mailer_factory=lambda _: Mailer(messages, "reader@example.com"),
                now=NOW,
            )
        before = dict(calls)
        with pytest.raises(ServiceError, match="Unresolved"):
            run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        assert before == calls
        with Store(config.state_dir) as store:
            delivery = store.pending()[0]["id"]
            store.resolve(delivery, "sent")
        result = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert result["emails_sent"] == 0
    assert len(messages) == 1


def test_manual_retry_keeps_cache_and_allows_send(config, summary):
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        with pytest.raises(ServiceError):
            run(
                config,
                client,
                mailer_factory=lambda _: Mailer(messages, "reader@example.com"),
                now=NOW,
            )
        with Store(config.state_dir) as store:
            store.resolve(store.pending()[0]["id"], "retry")
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert len(messages) == 2
    assert calls["jev"] == 3
    assert calls["summary"] == 2


def test_per_recipient_success_survives_later_failure(config, summary, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "first@example.com,second@example.com")
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        with pytest.raises(ServiceError):
            run(
                config,
                client,
                mailer_factory=lambda _: Mailer(messages, "second@example.com"),
                now=NOW,
            )
        with Store(config.state_dir) as store:
            assert not store.eligible(
                recipient_key("first@example.com"), profile_key(config), "2610.00001"
            )
            pending = store.pending()
            assert len(pending) == 1
            assert pending[0]["recipient_hash"] == recipient_key("second@example.com")
            store.resolve(pending[0]["id"], "retry")
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert [m["To"] for m in messages] == [
        "first@example.com",
        "second@example.com",
        "second@example.com",
    ]
    assert all("Bcc" not in m for m in messages)


def test_profile_change_rescreens_unsent_but_never_resends(config, summary):
    calls, messages = counts(), []
    config.selection.top_k = 1
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
        config.selection.min_score = 0.7
        result = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert result["selected_unique"] == 1
    assert len(messages) == 2
    assert calls["jev"] == 5


def test_no_matches_skips_mail(config, summary):
    config.selection.min_score = 0.99
    calls, messages = counts(), []
    with httpx.Client(transport=transport(config, summary, calls)) as client:
        result = run(config, client, mailer_factory=lambda _: Mailer(messages), now=NOW)
    assert result["emails_sent"] == 0
    assert calls["summary"] == 0
    assert not messages


def test_authentication_failure_does_not_create_uncertain_attempt(config, summary):
    class LoginFailure:
        def __enter__(self):
            raise ServiceError("SMTP authentication failed")

        def __exit__(self, *_):
            pass

    with httpx.Client(transport=transport(config, summary, counts())) as client:
        with pytest.raises(ServiceError, match="authentication"):
            run(config, client, mailer_factory=lambda _: LoginFailure(), now=NOW)
    with Store(config.state_dir) as store:
        assert store.pending() == []
        assert store.eligible(
            recipient_key("reader@example.com"), profile_key(config), "2610.00001"
        )
