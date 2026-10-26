import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from arxiv_digest.enrichment import Enricher, classify_details
from arxiv_digest.http import ServiceError
from arxiv_digest.models import Affiliation, PaperDetails
from arxiv_digest.operations import (
    actions_summary,
    inspect_metadata,
    latest_health,
    notify_failure,
    persist_outbox,
)
from arxiv_digest.store import Store

RUN_URL = "https://github.com/example/digest/actions/runs/123"


def unexpected(*_args, **_kwargs):
    raise AssertionError("Operation must not access the network or send unexpected mail")


def complete_details(paper):
    value = PaperDetails(
        project_url="https://project.example/",
        project_source=paper.url,
        affiliations=[
            Affiliation(
                institution="Example University", authors=paper.authors, source=paper.pdf_url
            )
        ],
        sources=[paper.url, paper.pdf_url],
    )
    return classify_details(value, paper, [], checked=True)


def model_keys_removed(monkeypatch):
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("ids", [[], ["2610.00001"] * 6])
def test_inspect_rejects_invalid_paper_count_before_network(config, monkeypatch, ids):
    model_keys_removed(monkeypatch)
    monkeypatch.setattr("arxiv_digest.operations.fetch_by_ids", unexpected)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="1 to 5"):
            inspect_metadata(config, client, ids)
    assert not config.output_dir.exists()


def test_inspect_requires_only_deepseek_never_sends_and_reuses_cache(config, paper, monkeypatch):
    model_keys_removed(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-inspect-key")
    for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("arxiv_digest.operations.fetch_by_ids", lambda *_args: [paper])
    monkeypatch.setattr("arxiv_digest.mailer.SMTPMailer.__enter__", unexpected)
    calls = []

    def enrich(_self, actual):
        calls.append(actual.id)
        return complete_details(actual)

    monkeypatch.setattr(Enricher, "enrich", enrich)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        first = inspect_metadata(config, client, [paper.id])
        cached = inspect_metadata(config, client, [paper.id])
        refreshed = inspect_metadata(config, client, [paper.id], refresh=True)
    assert calls == [paper.id, paper.id]
    for result, expected_cached in ((first, False), (cached, True), (refreshed, False)):
        directory = Path(result["output_dir"])
        record = json.loads((directory / "metadata.json").read_text("utf-8"))
        health = json.loads((directory / "health.json").read_text("utf-8"))
        usage = json.loads((directory / "usage.json").read_text("utf-8"))
        assert record["papers"][0]["cached"] is expected_cached
        assert record["papers"][0]["completeness"]["institutions_found"] is True
        assert result["emails_sent"] == record["emails_sent"] == 0
        assert health["status"] == "success"
        assert health["stats"]["inspected"] == 1
        assert usage["totals"]["jev_calls"] == usage["totals"]["deepseek_calls"] == 0
        assert "test-only-inspect-key" not in json.dumps(record)
    with Store(config.state_dir) as store:
        assert store.db.execute("SELECT count(*) FROM sent").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM reviewed").fetchone()[0] == 0
        assert store.pending() == []


def test_inspect_rechecks_expired_incomplete_metadata(config, paper, monkeypatch):
    monkeypatch.setattr("arxiv_digest.operations.fetch_by_ids", lambda *_args: [paper])
    stale = PaperDetails(project_status="not_found", sources=[paper.url])
    key = Enricher(config, None, None).key(paper)
    with Store(config.state_dir) as store:
        store.cache("details", key, stale.model_dump(), now=datetime.now(UTC) - timedelta(hours=25))
    calls = []

    def enrich(_self, actual):
        calls.append(actual.id)
        return complete_details(actual)

    monkeypatch.setattr(Enricher, "enrich", enrich)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = inspect_metadata(config, client, [paper.id])
    record = json.loads((Path(result["output_dir"]) / "metadata.json").read_text("utf-8"))
    assert calls == [paper.id]
    assert record["papers"][0]["cached"] is False
    assert record["papers"][0]["details"]["project_status"] == "found"


def test_inspect_failure_keeps_health_and_usage_without_raw_exception_data(
    config, paper, monkeypatch
):
    secret = "private-value-that-must-not-appear"
    monkeypatch.setattr("arxiv_digest.operations.fetch_by_ids", lambda *_args: [paper])

    def fail(_self, _paper):
        raise RuntimeError(secret)

    monkeypatch.setattr(Enricher, "enrich", fail)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(RuntimeError):
            inspect_metadata(config, client, [paper.id])
    health = latest_health(config)
    assert health["status"] == "failed"
    assert health["error"] == "RuntimeError"
    assert secret not in json.dumps(health)
    assert len(list(config.output_dir.glob("*/usage.json"))) == 1
    assert not list(config.output_dir.glob("*/metadata.json"))


def write_health(config, health):
    directory = config.output_dir / "test-run"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "health.json").write_text(json.dumps(health), "utf-8")


def test_actions_summary_appends_only_health_metrics_and_no_credentials(
    config, monkeypatch, tmp_path
):
    path = tmp_path / "github-summary.md"
    path.write_text("Existing summary\n", "utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-credential-do-not-display")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-credential-do-not-display")
    health = {
        "status": "failed",
        "stats": {"fetched": 69, "selected_unique": 8, "emails_sent": 0, "metadata_missing": 2},
        "usage": {"usage_complete": False, "totals": {"jev_calls": 69, "deepseek_calls": 12}},
        "error": "Provider unavailable\ntry later",
        "recovery_required": True,
    }
    write_health(config, health)
    assert actions_summary(config) == health
    text = path.read_text("utf-8")
    assert text.startswith("Existing summary\n")
    assert "arXiv 候选 | 69" in text
    assert "尚未提取署名机构 | 2" in text
    assert "Provider unavailable try later" in text
    assert "预算仍保留预估值" in text
    assert "未自动推进检查点" in text
    assert "credential-do-not-display" not in text
    assert "SMTP_PASSWORD" not in text


def test_status_without_previous_reports_is_explicit(config, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    health = actions_summary(config)
    assert health["status"] == "unavailable"
    assert "No operation health report" in health["error"]


class CapturingMailer:
    def __init__(self, messages, *, fail_at=None):
        self.messages, self.fail_at = messages, fail_at

    def __enter__(self):
        if self.fail_at == "login":
            raise ServiceError("SMTP login failed")
        return self

    def send(self, message):
        if self.fail_at == "send":
            raise ServiceError("SMTP alert delivery uncertain")
        self.messages.append(message)

    def __exit__(self, *_args):
        pass


def test_failure_notification_disabled_needs_no_keys_or_mailer(config, monkeypatch):
    model_keys_removed(monkeypatch)
    for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    config.notifications.email_on_failure = False
    assert notify_failure(config, "not-even-a-url", mailer_factory=unexpected) == 0


def test_failure_notification_uses_smtp_only_and_reaches_each_recipient(config, monkeypatch):
    model_keys_removed(monkeypatch)
    config.notifications.email_on_failure = True
    monkeypatch.setenv("EMAIL_TO", "first@example.com,second@example.com")
    monkeypatch.setattr("arxiv_digest.operations.DeepSeekClient", unexpected)
    monkeypatch.setattr("arxiv_digest.operations.fetch_by_ids", unexpected)
    write_health(config, {"status": "failed", "error": "API budget exhausted"})
    messages = []
    count = notify_failure(config, RUN_URL, mailer_factory=lambda _: CapturingMailer(messages))
    assert count == len(messages) == 2
    assert [str(message["To"]) for message in messages] == [
        "first@example.com",
        "second@example.com",
    ]
    for message in messages:
        text = message.get_body(preferencelist=("plain",)).get_content()
        assert RUN_URL in text and "API budget exhausted" in text
        assert "运行异常" in str(message["Subject"])
        assert "fake-smtp" not in text
        assert message["Bcc"] is None


@pytest.mark.parametrize("stage", ["login", "send"])
def test_failure_notification_smtp_failure_is_visible(config, monkeypatch, stage):
    model_keys_removed(monkeypatch)
    config.notifications.email_on_failure = True
    with pytest.raises(ServiceError, match="SMTP"):
        notify_failure(config, RUN_URL, mailer_factory=lambda _: CapturingMailer([], fail_at=stage))


@pytest.mark.parametrize(
    "run_url", ["http://github.com/run", "javascript:alert(1)", "https://u:p@github.com/run"]
)
def test_failure_notification_rejects_non_https_or_credential_url(config, run_url):
    config.notifications.email_on_failure = True
    with pytest.raises(ValueError, match="public HTTPS"):
        notify_failure(config, run_url, mailer_factory=unexpected)


@pytest.mark.parametrize("missing", ["script", "token"])
def test_missing_outbox_persistence_prerequisite_blocks_email(tmp_path, monkeypatch, missing):
    if missing != "script":
        path = tmp_path / ".github/scripts/state.py"
        path.parent.mkdir(parents=True)
        path.write_text("# trusted state helper", "utf-8")
    if missing != "token":
        monkeypatch.setenv("GH_TOKEN", "test-only-token")
    else:
        monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("arxiv_digest.operations.subprocess.run", unexpected)
    with pytest.raises(ServiceError, match="delivery is blocked") as raised:
        persist_outbox(tmp_path)
    assert raised.value.fatal


@pytest.mark.parametrize(
    "outcome", [1, OSError("sensitive message"), subprocess.TimeoutExpired("cmd", 60)]
)
def test_outbox_persistence_failure_blocks_email_without_process_output(
    tmp_path, monkeypatch, outcome
):
    path = tmp_path / ".github/scripts/state.py"
    path.parent.mkdir(parents=True)
    path.write_text("# trusted state helper", "utf-8")
    monkeypatch.setenv("GH_TOKEN", "test-only-token")

    def run(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            returncode=outcome, stdout=b"sensitive output", stderr=b"sensitive error"
        )

    monkeypatch.setattr("arxiv_digest.operations.subprocess.run", run)
    with pytest.raises(ServiceError, match="delivery is blocked") as raised:
        persist_outbox(tmp_path)
    assert raised.value.fatal
    assert "sensitive" not in str(raised.value)
    assert "test-only-token" not in str(raised.value)


def test_outbox_persistence_calls_trusted_helper_before_delivery(tmp_path, monkeypatch):
    path = tmp_path / ".github/scripts/state.py"
    path.parent.mkdir(parents=True)
    path.write_text("# trusted state helper", "utf-8")
    monkeypatch.setenv("GH_TOKEN", "test-only-token")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("arxiv_digest.operations.subprocess.run", run)
    assert persist_outbox(tmp_path) is None
    assert calls == [
        (
            [sys.executable, str(path), "save"],
            {"cwd": tmp_path, "capture_output": True, "timeout": 60, "check": False},
        )
    ]
