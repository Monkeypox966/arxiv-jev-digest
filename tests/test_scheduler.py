import importlib.util
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import ROOT

from arxiv_digest.store import Store, recipient_key

NOW = datetime(2026, 10, 29, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def scheduler(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "external_scheduler", ROOT / "deploy/scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class API:
    def __init__(self, runs=None, error=None):
        self.history, self.error, self.calls = runs or [], error, []

    def runs(self):
        self.calls.append("runs")
        return self.history

    def dispatch(self, marker):
        self.calls.append(("dispatch", marker))
        if self.error:
            raise self.error


def config(scheduler, tmp_path):
    return scheduler.Settings("owner/repository", "fake-test-token", directory=tmp_path)


def test_default_10am_guard_skips_all_network_before_due(scheduler, tmp_path):
    settings, api = config(scheduler, tmp_path), API()
    assert settings.after == "10:00"
    result = scheduler.tick(settings, api, {}, now=NOW.replace(hour=9, minute=59))
    assert result["status"] == "not_due" and api.calls == []
    assert scheduler.tick(settings, api, {}, now=NOW)["status"] == "dispatched"


def test_each_hour_dispatches_even_after_a_successful_run(scheduler, tmp_path):
    settings, state, api = config(scheduler, tmp_path), {}, API()
    first = scheduler.tick(settings, api, state, now=NOW)
    api.history = [
        {
            "id": 7,
            "status": "completed",
            "conclusion": "success",
            "display_title": first["marker"],
        }
    ]
    second = scheduler.tick(settings, api, state, now=NOW + timedelta(hours=1))
    assert first["status"] == second["status"] == "dispatched"
    assert first["marker"] != second["marker"]
    assert len([call for call in api.calls if isinstance(call, tuple)]) == 2


def test_post_timeout_persists_intent_and_waits_for_run_index(scheduler, tmp_path):
    settings, state = config(scheduler, tmp_path), {}
    with pytest.raises(TimeoutError):
        scheduler.tick(settings, API(error=TimeoutError()), state, now=NOW)
    recovered = json.loads((tmp_path / "state.json").read_text("utf-8"))
    api = API()
    result = scheduler.tick(settings, api, recovered, now=NOW + timedelta(minutes=10))
    assert result["status"] == "awaiting_run_index" and api.calls == ["runs"]


def test_active_workflow_blocks_duplicate_dispatch(scheduler, tmp_path):
    active = {
        "id": 7,
        "status": "in_progress",
        "created_at": NOW.isoformat(),
        "html_url": "https://github.com/owner/repository/actions/runs/7",
    }
    api = API(runs=[active])
    assert scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW)["status"] == "running"
    assert api.calls == ["runs"]
    with pytest.raises(scheduler.SchedulerError, match="stuck"):
        scheduler.tick(config(scheduler, tmp_path), api, {}, now=NOW + timedelta(hours=2))


def test_dispatch_always_uses_daily_mode_and_correlation_id(scheduler, tmp_path, monkeypatch):
    api = scheduler.GitHub(config(scheduler, tmp_path))
    captured = []
    monkeypatch.setattr(api, "call", lambda path, **kw: captured.append((path, kw)))
    api.dispatch("vps-probe")
    assert captured == [
        (
            "actions/workflows/daily.yml/dispatches",
            {"payload": {"ref": "main", "inputs": {"mode": "daily", "scheduler_id": "vps-probe"}}},
        )
    ]


def test_force_probe_before_due_still_uses_the_same_trigger(scheduler, tmp_path):
    api = API()
    result = scheduler.tick(
        config(scheduler, tmp_path), api, {}, now=NOW.replace(hour=9), force_dispatch=True
    )
    assert result["status"] == "dispatched" and api.calls[-1][0] == "dispatch"


def test_aggregate_snapshot_excludes_tests_and_preserves_partial_delivery(config):
    first, second = recipient_key("first@example.com"), recipient_key("second@example.com")
    with Store(config.state_dir) as store:
        store.resolve(store.prepare(first, "normal", [], []), "sent", now=NOW)
        store.resolve(store.prepare(second, "delivery-test-v1", [], []), "sent", now=NOW)
        partial = store.delivery_status([first, second, second], now=NOW)
        assert partial == {
            "schema_version": 1,
            "date": "2026-10-29",
            "timezone": "Asia/Shanghai",
            "recipient_count": 2,
            "acknowledged_count": 1,
            "pending_count": 0,
            "delivered": False,
        }
        store.resolve(store.prepare(second, "normal", [], []), "sent", now=NOW)
        assert store.delivery_status([first, second], now=NOW)["delivered"]
        assert not store.delivery_status([first, second], now=NOW + timedelta(days=1))["delivered"]
        assert not store.delivery_status([], now=NOW)["delivered"]
        assert "example.com" not in json.dumps(partial) and first not in json.dumps(partial)
