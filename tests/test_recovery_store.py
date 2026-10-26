"""State recovery tests against real SQLite transactions and legacy files."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arxiv_digest.http import ServiceError
from arxiv_digest.store import Store

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def queue(store, paper, *, recipient="reader", profile="profile", now=NOW, max_attempts=3):
    store.queue_metadata_retry(
        recipient, profile, paper, delay_hours=24, max_attempts=max_attempts, now=now
    )


def test_cache_ttl_expires_at_boundary_without_deleting_unbounded_cache(tmp_path):
    value = {"project_url": None, "notes": ["暂时无法读取"]}
    with Store(tmp_path) as store:
        store.cache("details", "key", value, now=NOW)
        assert (
            store.cached(
                "details", "key", max_age_hours=24, now=NOW + timedelta(hours=24, microseconds=-1)
            )
            == value
        )
        assert (
            store.cached("details", "key", max_age_hours=24, now=NOW + timedelta(hours=24)) is None
        )
        assert store.cached("details", "key") == value
        assert store.cached("details", "missing", max_age_hours=24, now=NOW) is None


def test_cache_ttl_compares_instants_across_timezones(tmp_path):
    local = NOW.astimezone(ZoneInfo("Asia/Shanghai"))
    with Store(tmp_path) as store:
        store.cache("details", "key", {"ok": True}, now=local)
        assert store.cached("details", "key", max_age_hours=1, now=NOW) == {"ok": True}
        assert (
            store.cached("details", "key", max_age_hours=1, now=local + timedelta(hours=1)) is None
        )


def test_review_checkpoints_are_recipient_scoped_source_scoped_and_monotonic(tmp_path):
    with Store(tmp_path) as store:
        store.mark_reviewed("reader", "profile", ["p1"], source="cs.RO", announcement="2026-10-23")
        store.mark_reviewed(
            "reader",
            "other-profile",
            [],
            checkpoint_updates=[("cs.RO", "2026-10-22"), ("cs.CV", "2026-10-21")],
        )
        assert store.checkpoint("reader", "cs.RO") == "2026-10-23"
        assert store.checkpoint("reader", "cs.CV") == "2026-10-21"
        assert store.checkpoint("other-reader", "cs.RO") is None
        assert store.checkpoint("reader", "cs.AI") is None
        assert not store.eligible("reader", "profile", "p1", now=NOW)
        assert store.eligible("reader", "other-profile", "p1", now=NOW)
    with Store(tmp_path) as store:
        assert store.checkpoint("reader", "cs.RO") == "2026-10-23"


def test_mark_reviewed_and_all_checkpoint_updates_rollback_together(tmp_path):
    with Store(tmp_path) as store:
        store.db.execute(
            "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints "
            "WHEN NEW.source='failing-source' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        store.db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            store.mark_reviewed(
                "reader",
                "profile",
                ["p1"],
                checkpoint_updates=[("cs.RO", "2026-10-23"), ("failing-source", "2026-10-22")],
            )
        assert store.checkpoint("reader", "cs.RO") is None
        assert store.checkpoint("reader", "failing-source") is None
        assert store.eligible("reader", "profile", "p1", now=NOW)


def test_prepare_and_retry_do_not_advance_checkpoint_or_mark_papers(tmp_path):
    with Store(tmp_path) as store:
        store.mark_reviewed("reader", "profile", [], source="cs.RO", announcement="2026-10-20")
        delivery = store.prepare(
            "reader",
            "profile",
            ["p1", "p2"],
            ["p1"],
            source="cs.RO",
            announcement="2026-10-23",
            checkpoint_updates=[("cs.CV", "2026-10-22")],
        )
        assert store.checkpoint("reader", "cs.RO") == "2026-10-20"
        assert store.checkpoint("reader", "cs.CV") is None
        assert store.eligible("reader", "profile", "p1", now=NOW)
        with pytest.raises(ServiceError, match="Unresolved"):
            store.prepare("reader", "profile", [], [])
        store.resolve(delivery, "retry")
        assert not store.pending()
        assert store.checkpoint("reader", "cs.RO") == "2026-10-20"
        assert store.checkpoint("reader", "cs.CV") is None
        assert store.eligible("reader", "profile", "p1", now=NOW)


def test_sent_resolution_commits_history_retries_and_multiple_checkpoints(tmp_path, paper):
    value = paper.to_dict()
    with Store(tmp_path) as store:
        queue(store, value)
        queue(store, value, profile="other-profile")
        queue(store, value, recipient="other-reader")
        delivery = store.prepare(
            "reader",
            "profile",
            [paper.id, "overflow"],
            [paper.id],
            source="legacy-source",
            announcement="2026-10-21",
            checkpoint_updates=[("cs.RO", "2026-10-23"), ("cs.CV", "2026-10-22")],
        )
    with Store(tmp_path) as store:
        assert store.pending()[0]["id"] == delivery
        store.resolve(delivery, "sent")
        assert not store.pending()
        assert store.checkpoint("reader", "legacy-source") == "2026-10-21"
        assert store.checkpoint("reader", "cs.RO") == "2026-10-23"
        assert store.checkpoint("reader", "cs.CV") == "2026-10-22"
        assert not store.eligible("reader", "new-profile", paper.id, now=NOW)
        assert not store.eligible("reader", "profile", "overflow", now=NOW)
        assert store.eligible("reader", "new-profile", "overflow", now=NOW)
        later = NOW + timedelta(days=3)
        assert store.due_metadata("reader", "profile", now=later, limit=10) == []
        assert store.due_metadata("reader", "other-profile", now=later, limit=10) == []
        assert store.due_metadata("other-reader", "profile", now=later, limit=10) == [value]


def test_failed_sent_resolution_rolls_back_all_delivery_effects(tmp_path, paper):
    with Store(tmp_path) as store:
        queue(store, paper.to_dict())
        delivery = store.prepare(
            "reader",
            "profile",
            [paper.id],
            [paper.id],
            checkpoint_updates=[("cs.RO", "2026-10-23"), ("cs.CV", "2026-10-22")],
        )
        store.db.execute(
            "CREATE TRIGGER fail_sent BEFORE UPDATE OF status ON outbox "
            "WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        store.db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            store.resolve(delivery, "sent")
        assert store.pending()[0]["id"] == delivery
        assert store.eligible("reader", "profile", paper.id, now=NOW)
        assert store.checkpoint("reader", "cs.RO") is None
        assert store.checkpoint("reader", "cs.CV") is None
        assert store.due_metadata("reader", "profile", now=NOW + timedelta(days=1), limit=10) == [
            paper.to_dict()
        ]
        store.db.execute("DROP TRIGGER fail_sent")
        store.db.commit()
        store.resolve(delivery, "sent")
        assert store.checkpoint("reader", "cs.RO") == "2026-10-23"
        assert not store.eligible("reader", "new-profile", paper.id, now=NOW)


def test_due_retry_overrides_reviewed_only_at_deadline_and_clear_restores_review(tmp_path, paper):
    with Store(tmp_path) as store:
        store.mark_reviewed("reader", "profile", [paper.id])
        queue(store, paper.to_dict())
        before = NOW + timedelta(hours=24, microseconds=-1)
        due = NOW + timedelta(hours=24)
        assert not store.eligible("reader", "profile", paper.id, now=before)
        assert store.due_metadata("reader", "profile", now=before, limit=10) == []
        assert store.eligible("reader", "profile", paper.id, now=due)
        assert store.due_metadata("reader", "profile", now=due, limit=10) == [paper.to_dict()]
        store.clear_metadata_retry("reader", "profile", paper.id)
        assert store.due_metadata("reader", "profile", now=due, limit=10) == []
        assert not store.eligible("reader", "profile", paper.id, now=due)


def test_retry_queue_is_idempotent_until_due_and_stops_at_attempt_limit(tmp_path, paper):
    value = paper.to_dict()
    with Store(tmp_path) as store:
        store.mark_reviewed("reader", "profile", [paper.id])
        queue(store, value)
        for hours in (0, 1, 12, 23):
            queue(store, value, now=NOW + timedelta(hours=hours))
        assert store.due_metadata("reader", "profile", now=NOW + timedelta(hours=24), limit=10) == [
            value
        ]
        queue(store, value, now=NOW + timedelta(hours=24))  # second completed lookup
        assert (
            store.due_metadata("reader", "profile", now=NOW + timedelta(hours=47), limit=10) == []
        )
        assert store.due_metadata("reader", "profile", now=NOW + timedelta(hours=48), limit=10) == [
            value
        ]
        queue(store, value, now=NOW + timedelta(hours=48))  # third: exhausted
        for days in (3, 10, 20):
            queue(store, value, now=NOW + timedelta(days=days))
            assert (
                store.due_metadata("reader", "profile", now=NOW + timedelta(days=days), limit=10)
                == []
            )
            assert not store.eligible("reader", "profile", paper.id, now=NOW + timedelta(days=days))


def test_retry_due_order_limit_and_scope_are_stable(tmp_path, paper):
    with Store(tmp_path) as store:
        values = [{**paper.to_dict(), "id": id} for id in ("p3", "p1", "p2")]
        for value in values:
            queue(store, value)
        queue(store, {**paper.to_dict(), "id": "other-profile"}, profile="other")
        queue(store, {**paper.to_dict(), "id": "other-reader"}, recipient="other")
        queue(store, {**paper.to_dict(), "id": "not-yet"}, now=NOW + timedelta(hours=1))
        due = NOW + timedelta(days=1)
        assert [p["id"] for p in store.due_metadata("reader", "profile", now=due, limit=2)] == [
            "p1",
            "p2",
        ]
        assert [p["id"] for p in store.due_metadata("reader", "profile", now=due, limit=10)] == [
            "p1",
            "p2",
            "p3",
        ]


def test_sent_always_wins_even_if_stale_retry_row_survives(tmp_path, paper):
    with Store(tmp_path) as store:
        queue(store, paper.to_dict())
        # Simulate a stale row from old state or an interrupted external restore.
        with store.db:
            store.db.execute("INSERT INTO sent VALUES (?, ?)", ("reader", paper.id))
        queue(store, paper.to_dict(), profile="new-profile")
        later = NOW + timedelta(days=2)
        assert not store.eligible("reader", "profile", paper.id, now=later)
        assert not store.eligible("reader", "new-profile", paper.id, now=later)
        assert store.due_metadata("reader", "profile", now=later, limit=10) == []
        assert store.due_metadata("reader", "new-profile", now=later, limit=10) == []


def write_legacy_v1(directory):
    connection = sqlite3.connect(directory / "digest.sqlite3")
    connection.executescript("""
        CREATE TABLE cache (
            kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
            created TEXT NOT NULL, PRIMARY KEY(kind, key)
        );
        CREATE TABLE reviewed (
            recipient TEXT NOT NULL, profile TEXT NOT NULL, paper TEXT NOT NULL,
            PRIMARY KEY(recipient, profile, paper)
        );
        CREATE TABLE sent (
            recipient TEXT NOT NULL, paper TEXT NOT NULL, PRIMARY KEY(recipient, paper)
        );
        CREATE TABLE outbox (
            id TEXT PRIMARY KEY, recipient TEXT NOT NULL, profile TEXT NOT NULL,
            candidates TEXT NOT NULL, selected TEXT NOT NULL,
            status TEXT NOT NULL, created TEXT NOT NULL
        );
        PRAGMA user_version = 1;
    """)
    with connection:
        connection.execute(
            "INSERT INTO cache VALUES (?, ?, ?, ?)",
            ("decision", "legacy-key", json.dumps({"score": 0.9}), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO reviewed VALUES (?, ?, ?)",
            ("reader", "profile", "reviewed-before-upgrade"),
        )
        connection.execute("INSERT INTO sent VALUES (?, ?)", ("reader", "sent-before-upgrade"))
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "old-pending",
                "reader",
                "profile",
                json.dumps(["old-paper", "overflow"]),
                json.dumps(["old-paper"]),
                "pending",
                NOW.isoformat(),
            ),
        )
    connection.close()


def test_real_v1_database_migrates_to_v2_without_losing_pending_or_history(tmp_path):
    write_legacy_v1(tmp_path)
    for attempt in range(2):  # migration and a second normal reopen are both safe
        with Store(tmp_path) as store:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4
            assert store.cached("decision", "legacy-key") == {"score": 0.9}
            assert not store.eligible("reader", "new-profile", "sent-before-upgrade", now=NOW)
            assert not store.eligible("reader", "profile", "reviewed-before-upgrade", now=NOW)
            assert store.pending()[0]["id"] == "old-pending"
            values = store.db.execute(
                "SELECT source, announcement, checkpoint_updates FROM outbox WHERE id='old-pending'"
            ).fetchone()
            assert values == ("", "", "[]")
            assert store.checkpoint("reader", "cs.RO") is None
            if attempt == 1:
                store.resolve("old-pending", "sent")
                assert not store.pending()
                assert not store.eligible("reader", "new-profile", "old-paper", now=NOW)
                # Old pending records have no reliable announcement date; do not infer one.
                assert store.checkpoint("reader", "cs.RO") is None
