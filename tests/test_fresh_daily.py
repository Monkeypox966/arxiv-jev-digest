from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import atom
from test_new_announcements import new_page
from test_pipeline import Mailer

from arxiv_digest.models import Decision
from arxiv_digest.pipeline import run
from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 29, 11, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def subscription(config):
    config.arxiv.source = "new"
    config.arxiv.categories = ["cs.RO"]
    config.enrichment.enabled = config.ranking.enabled = False
    config.email.send_empty = True  # Legacy true must not re-enable empty emails.
    return config


@pytest.mark.parametrize("now", [NOW, NOW.replace(day=31), NOW.replace(month=11, day=1)])
def test_old_batch_waits_without_api_models_smtp_or_delivery_history(subscription, now):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "arxiv.org"
        return httpx.Response(
            200,
            text=new_page(
                [("new", ["2610.00001"], 1)], day=(now.date() - timedelta(days=1)).isoformat()
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run(
            subscription,
            client,
            now=now,
            daily_after=time(10),
            mailer_factory=lambda _: pytest.fail("SMTP must not open"),
        )
    assert len(calls) == 1
    assert result["skip_reason"] == "waiting_for_arxiv_update"
    assert result["usage"]["totals"]["attempts"] == result["emails_sent"] == 0
    with Store(subscription.state_dir) as store:
        for table in ["sent", "reviewed", "outbox", "checkpoints"]:
            assert store.db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_late_update_after_legacy_empty_mail_sends_once_then_skips_before_fetch(
    subscription, summary, monkeypatch
):
    phase, calls, messages = ["2026-10-28"], [], []
    with Store(subscription.state_dir) as store:
        old = store.prepare(
            recipient_key("reader@example.com"), "old-profile", [], [], announcement="2026-10-29"
        )
        store.resolve(old, "sent", now=NOW)

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "arxiv.org":
            return httpx.Response(200, text=new_page([("new", ["2610.00001"], 1)], day=phase[0]))
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, text=atom(["2610.00001v1"]).replace("cs.AI", "cs.RO"))

    monkeypatch.setattr(
        "arxiv_digest.clients.JevClient.judge",
        lambda *_: Decision(score=0.95, interests={}, exclusions={}, excluded=False, model="test"),
    )
    monkeypatch.setattr("arxiv_digest.clients.DeepSeekClient.summarize", lambda *_: summary)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run(
            subscription,
            client,
            now=NOW,
            daily_after=time(10),
            mailer_factory=lambda _: Mailer(messages),
        )
        assert first["skip_reason"] == "waiting_for_arxiv_update" and not messages
        phase[0] = "2026-10-29"
        second = run(
            subscription,
            client,
            now=NOW,
            daily_after=time(10),
            mailer_factory=lambda _: Mailer(messages),
        )
        before = list(calls)
        third = run(
            subscription,
            client,
            now=NOW,
            daily_after=time(10),
            mailer_factory=lambda _: pytest.fail("Already delivered"),
        )
    assert second["emails_sent"] == len(messages) == 1
    assert third["skip_reason"] == "already_delivered_today" and calls == before
    with Store(subscription.state_dir) as store:
        assert store.delivered_today(
            recipient_key("reader@example.com"), now=NOW, minimum_announcement="2026-10-29"
        )
        assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_fresh_zero_entry_batch_retains_date_but_never_sends_empty_mail(subscription):
    def handler(request):
        assert request.url.host == "arxiv.org"
        return httpx.Response(200, text=new_page([], day="2026-10-29"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run(
            subscription,
            client,
            now=NOW,
            daily_after=time(10),
            mailer_factory=lambda _: pytest.fail("No empty email"),
        )
    assert result["skip_reason"] == "no_matching_papers"
    assert result["announcement_dates"] == ["2026-10-29"]
    assert result["emails_sent"] == result["usage"]["totals"]["attempts"] == 0


def test_one_stale_category_blocks_the_whole_daily_batch(subscription):
    subscription.arxiv.categories = ["cs.RO", "cs.AI"]

    def handler(request):
        assert request.url.host == "arxiv.org"
        day = "2026-10-29" if "cs.RO" in request.url.path else "2026-10-28"
        return httpx.Response(200, text=new_page([], day=day))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run(
            subscription,
            client,
            now=NOW,
            daily_after=time(10),
            mailer_factory=lambda _: pytest.fail("No partial batch"),
        )
    assert result["skip_reason"] == "waiting_for_arxiv_update" and result["emails_sent"] == 0
