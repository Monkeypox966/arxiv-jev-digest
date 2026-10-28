import importlib.util
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import ROOT

from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def scheduler(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "external_scheduler", ROOT / "deploy/scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def receipt(**changes):
    return {
        "schema_version": 1,
        "date": "2026-10-27",
        "timezone": "Asia/Shanghai",
        "recipient_count": 1,
        "acknowledged_count": 1,
        "pending_count": 0,
        "delivered": True,
        **changes,
    }


class API:
    def __init__(self, snapshot=None, runs=None, error=None):
        self.snapshot, self.history, self.error = snapshot, runs or [], error
        self.calls = []

    def receipt(self):
        self.calls.append("receipt")
        return self.snapshot

    def runs(self):
        self.calls.append("runs")
        return self.history

    def dispatch(self, marker):
        self.calls.append(("dispatch", marker))
        if self.error:
            raise self.error


def config(scheduler, tmp_path):
    return scheduler.Settings("owner/repository", "fake-test-token", directory=tmp_path)


def test_acknowledged_receipt_skips_dispatch_and_pre_due_skips_even_api(scheduler, tmp_path):
    settings, api = config(scheduler, tmp_path), API(receipt())
    assert settings.after == "11:00"
    result = scheduler.tick(settings, api, {}, now=NOW)
    assert result["status"] == "delivered" and api.calls == ["receipt"]
    api.calls.clear()
    result = scheduler.tick(settings, api, {}, now=NOW.replace(hour=10, minute=59))
    assert result["status"] == "not_due" and api.calls == []


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        receipt(date="2026-10-26"),
        receipt(recipient_count=2, acknowledged_count=1, delivered=False),
    ],
)
def test_missing_stale_or_partial_receipt_dispatches_daily(scheduler, tmp_path, snapshot):
    api, state = API(snapshot), {}
    result = scheduler.tick(config(scheduler, tmp_path), api, state, now=NOW)
    assert result["status"] == "dispatched"
    assert api.calls[-1] == ("dispatch", state["last_dispatch"]["marker"])


def test_pending_receipt_never_causes_an_automatic_resend(scheduler, tmp_path):
    api = API(receipt(date="2026-10-26", pending_count=1, delivered=False))
    with pytest.raises(scheduler.SchedulerError, match="pending"):
        scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW)
    assert api.calls == ["receipt", "runs"]


def test_successful_workflow_without_smtp_receipt_is_not_delivery(scheduler, tmp_path):
    api = API(None, [{"id": 7, "status": "completed", "conclusion": "success"}])
    assert scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW)["status"] == "dispatched"


def test_post_timeout_persists_intent_and_waits_for_run_index(scheduler, tmp_path):
    settings, state = config(scheduler, tmp_path), {}
    api = API(error=TimeoutError())
    with pytest.raises(TimeoutError):
        scheduler.tick(settings, api, state, now=NOW)
    recovered = json.loads((tmp_path / "state.json").read_text("utf-8"))
    api = API()
    result = scheduler.tick(settings, api, recovered, now=NOW + timedelta(minutes=10))
    assert result["status"] == "awaiting_run_index"
    assert api.calls == ["receipt", "runs"]


def test_active_workflow_blocks_duplicate_dispatch(scheduler, tmp_path):
    active = {
        "id": 7,
        "status": "in_progress",
        "created_at": NOW.isoformat(),
        "html_url": "https://github.com/owner/repository/actions/runs/7",
    }
    api = API(receipt(pending_count=1, delivered=False), runs=[active])
    assert scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW)["status"] == "running"
    assert api.calls == ["receipt", "runs"]
    with pytest.raises(scheduler.SchedulerError, match="stuck"):
        scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW + timedelta(hours=2))


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "UTC"},
        {"delivered": False},
        {"recipient_count": 0},
        {"acknowledged_count": True},
        {"schema_version": 2},
    ],
)
def test_invalid_receipt_cannot_claim_delivery(scheduler, tmp_path, changes):
    api = API(receipt(**changes))
    with pytest.raises(scheduler.SchedulerError):
        scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW)
    assert api.calls == ["receipt"]


def test_probe_always_uses_daily_mode_and_correlation_id(scheduler, tmp_path, monkeypatch):
    settings = config(scheduler, tmp_path)
    api = scheduler.GitHub(settings)
    captured = []
    monkeypatch.setattr(api, "call", lambda path, **kw: captured.append((path, kw)))
    api.dispatch("vps-probe")
    assert captured == [
        (
            "actions/workflows/daily.yml/dispatches",
            {
                "payload": {
                    "ref": "main",
                    "inputs": {"mode": "daily", "scheduler_id": "vps-probe"},
                }
            },
        )
    ]
    fake = API(receipt())
    assert (
        scheduler.tick(settings, fake, {}, now=NOW, force_dispatch=True)["status"] == "dispatched"
    )


def test_aggregate_snapshot_excludes_tests_and_preserves_partial_delivery(config):
    first, second = recipient_key("first@example.com"), recipient_key("second@example.com")
    with Store(config.state_dir) as store:
        store.resolve(store.prepare(first, "normal", [], []), "sent", now=NOW)
        store.resolve(store.prepare(second, "delivery-test-v1", [], []), "sent", now=NOW)
        partial = store.delivery_status([first, second, second], now=NOW)
        assert partial == receipt(recipient_count=2, delivered=False)
        store.resolve(store.prepare(second, "normal", [], []), "sent", now=NOW)
        assert store.delivery_status([first, second], now=NOW)["delivered"]
        assert not store.delivery_status([first, second], now=NOW + timedelta(days=1))["delivered"]
        assert not store.delivery_status([], now=NOW)["delivered"]
        encoded = json.dumps(partial)
        assert "example.com" not in encoded and first not in encoded
