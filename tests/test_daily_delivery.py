from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from arxiv_digest.cli import daily_time
from arxiv_digest.http import ServiceError
from arxiv_digest.pipeline import run
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 26, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def unexpected(*_args, **_kwargs):
    raise AssertionError("Daily guard must run before HTTP, SMTP and paid calls")


def acknowledge(store, address="reader@example.com", *, when=NOW, profile="old-profile"):
    delivery = store.prepare(recipient_key(address), profile, [], [])
    store.resolve(delivery, "sent", now=when)
    return delivery


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [
        (datetime(2026, 10, 25, 16, 5, tzinfo=UTC), True),
        (datetime(2026, 10, 25, 15, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 26, 1, 30, tzinfo=UTC), True),
    ],
)
def test_delivery_uses_acknowledgement_day_in_configured_timezone(config, accepted, expected):
    with Store(config.state_dir) as store:
        delivery = acknowledge(store, when=accepted)
        # Preparing on a different day must not override the actual acknowledgement.
        store.db.execute(
            "UPDATE outbox SET created=? WHERE id=?",
            ((NOW - timedelta(days=2)).isoformat(), delivery),
        )
        assert store.delivered_today(recipient_key("reader@example.com"), now=NOW) is expected
        assert not store.delivered_today(recipient_key("other@example.com"), now=NOW)


def test_legacy_empty_delivery_counts_but_smtp_tests_and_pending_do_not(config):
    with Store(config.state_dir) as store:
        acknowledge(store, profile="delivery-test-v1")
        assert not store.delivered_today(recipient_key("reader@example.com"), now=NOW)
        delivery = store.prepare(recipient_key("reader@example.com"), "normal-profile", [], [])
        assert not store.delivered_today(recipient_key("reader@example.com"), now=NOW)
        store.resolve(delivery, "sent", now=NOW)
        store.db.execute(
            "UPDATE outbox SET sent_at='', created=? WHERE id=?", (NOW.isoformat(), delivery)
        )
        assert store.delivered_today(recipient_key("reader@example.com"), now=NOW)


def test_already_sent_skips_before_models_credentials_and_smtp(config, monkeypatch):
    with Store(config.state_dir) as store:
        acknowledge(store)
    for key in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(key)
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", unexpected)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = run(config, client, now=NOW, daily_after=time(9, 30), mailer_factory=unexpected)
    assert result["skip_reason"] == "already_delivered_today"
    assert result["skipped_recipients"] == 1
    assert result["emails_sent"] == result["usage"]["totals"]["attempts"] == 0
    assert result["start_delay_seconds"] == 3600
    assert list(config.output_dir.glob("*/health.json"))


def test_early_delayed_job_does_not_send_next_days_digest_before_target(config, monkeypatch):
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", unexpected)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = run(
            config,
            client,
            now=NOW.replace(hour=9, minute=29),
            daily_after=time(9, 30),
            mailer_factory=unexpected,
        )
    assert result["skip_reason"] == "before_daily_send_time"
    assert result["emails_sent"] == result["usage"]["totals"]["attempts"] == 0


class Mailer:
    def __init__(self, messages):
        self.messages = messages

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, message):
        self.messages.append(message)


def test_fallback_retries_only_missing_recipients_and_sends_empty_digest(config, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "first@example.com,second@example.com")
    config.email.send_empty = True
    with Store(config.state_dir) as store:
        acknowledge(store, "first@example.com")
        acknowledge(store, "second@example.com", profile="delivery-test-v1")
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", lambda *_args, **_kwargs: [])
    messages, pending_sizes = [], []

    def persist_pending():
        with Store(config.state_dir) as store:
            pending_sizes.append(len(store.pending()))

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = run(
            config,
            client,
            now=NOW,
            daily_after=time(9, 30),
            mailer_factory=lambda _: Mailer(messages),
            outbox_hook=persist_pending,
        )
    assert result["emails_sent"] == result["skipped_recipients"] == 1
    assert result["selected_unique"] == result["usage"]["totals"]["attempts"] == 0
    assert [message["To"] for message in messages] == ["second@example.com"]
    assert pending_sizes == [0, 1]
    assert "0 篇" in messages[0]["Subject"]
    with Store(config.state_dir) as store:
        assert not store.pending()
        assert store.db.execute("SELECT count(*) FROM sent").fetchone()[0] == 0


def test_pending_blocks_scheduled_retries_before_any_network(config, monkeypatch):
    with Store(config.state_dir) as store:
        store.prepare(recipient_key("reader@example.com"), "normal-profile", [], [])
    monkeypatch.setattr("arxiv_digest.pipeline.fetch_papers", unexpected)
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ServiceError, match="Unresolved"):
            run(config, client, now=NOW, daily_after=time(9, 30), mailer_factory=unexpected)


def test_preview_cannot_use_daily_delivery_guard(config):
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="dry run"):
            run(config, client, now=NOW, daily_after=time(9, 30), dry_run=True)


def test_guard_requires_recipients(config, monkeypatch):
    monkeypatch.delenv("EMAIL_TO")
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="recipients"):
            run(config, client, now=NOW, daily_after=time(9, 30))


def test_v2_state_adds_acknowledgement_timestamp_without_losing_history(config):
    with Store(config.state_dir) as store:
        delivery = acknowledge(store)
        store.db.execute("UPDATE outbox SET created=? WHERE id=?", (NOW.isoformat(), delivery))
        store.db.execute("ALTER TABLE outbox DROP COLUMN sent_at")
        store.db.execute("PRAGMA user_version = 2")
        store.db.commit()
    for _ in range(2):
        with Store(config.state_dir) as store:
            assert store.delivered_today(recipient_key("reader@example.com"), now=NOW)
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert not store.pending()


@pytest.mark.parametrize("value", ["25:00", "09:60", "9:30", "09:30:00", "09:30+08:00"])
def test_daily_time_rejects_ambiguous_or_invalid_arguments(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        daily_time(value)


def test_daily_time_accepts_hhmm():
    assert daily_time("09:30") == time(9, 30)
