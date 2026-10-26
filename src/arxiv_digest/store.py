from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .http import ServiceError
from .models import Paper


def recipient_key(address: str) -> str:
    return hashlib.sha256(address.strip().casefold().encode()).hexdigest()


class Store:
    """Transactional local state; copy digest.sqlite3 between Actions runs.

    Recipient addresses and credentials are never stored. A pending outbox entry is
    intentionally not retried automatically: SMTP may have accepted that message.
    """

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "digest.sqlite3")
        self.db.execute("PRAGMA busy_timeout = 10000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, 3, 4}:
            self.db.close()
            raise ServiceError("State schema is newer than this app; upgrade before running")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                created TEXT NOT NULL, PRIMARY KEY(kind, key)
            );
            CREATE TABLE IF NOT EXISTS reviewed (
                recipient TEXT NOT NULL, profile TEXT NOT NULL, paper TEXT NOT NULL,
                PRIMARY KEY(recipient, profile, paper)
            );
            CREATE TABLE IF NOT EXISTS sent (
                recipient TEXT NOT NULL, paper TEXT NOT NULL,
                PRIMARY KEY(recipient, paper)
            );
            CREATE TABLE IF NOT EXISTS sent_versions (
                recipient TEXT NOT NULL, paper TEXT NOT NULL, version TEXT NOT NULL,
                PRIMARY KEY(recipient, paper, version)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL, profile TEXT NOT NULL,
                candidates TEXT NOT NULL, selected TEXT NOT NULL,
                status TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                recipient TEXT NOT NULL, source TEXT NOT NULL, announcement TEXT NOT NULL,
                PRIMARY KEY(recipient, source)
            );
            CREATE TABLE IF NOT EXISTS metadata_retry (
                recipient TEXT NOT NULL, profile TEXT NOT NULL, paper TEXT NOT NULL,
                payload TEXT NOT NULL, attempts INTEGER NOT NULL, next_retry TEXT,
                updated TEXT NOT NULL, PRIMARY KEY(recipient, profile, paper)
            );
            CREATE TABLE IF NOT EXISTS assessment_retry (
                recipient TEXT NOT NULL, profile TEXT NOT NULL, paper TEXT NOT NULL,
                payload TEXT NOT NULL, next_retry TEXT NOT NULL, updated TEXT NOT NULL,
                PRIMARY KEY(recipient, profile, paper)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        for column in ("source", "announcement"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if "checkpoint_updates" not in columns:
            self.db.execute(
                "ALTER TABLE outbox ADD COLUMN checkpoint_updates TEXT NOT NULL DEFAULT '[]'"
            )
        if "sent_at" not in columns:
            self.db.execute("ALTER TABLE outbox ADD COLUMN sent_at TEXT NOT NULL DEFAULT ''")
        if "selected_versions" not in columns:
            self.db.execute(
                "ALTER TABLE outbox ADD COLUMN selected_versions TEXT NOT NULL DEFAULT '[]'"
            )
        self.db.execute("PRAGMA user_version = 4")
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def cached(
        self, kind: str, key: str, *, max_age_hours: int | None = None, now: datetime | None = None
    ) -> dict | None:
        row = self.db.execute(
            "SELECT payload, created FROM cache WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        if row and max_age_hours is not None:
            cutoff = (now or datetime.now(UTC)) - timedelta(hours=max_age_hours)
            if datetime.fromisoformat(row[1]) <= cutoff:
                return None
        return json.loads(row[0]) if row else None

    def cache(self, kind: str, key: str, payload: dict, *, now: datetime | None = None):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                (
                    kind,
                    key,
                    json.dumps(payload, ensure_ascii=False),
                    (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
                ),
            )

    def eligible(
        self, recipient: str, profile: str, paper: str, *, now: datetime | None = None
    ) -> bool:
        if self.was_sent(recipient, paper):
            return False
        due = self.db.execute(
            "SELECT 1 FROM metadata_retry WHERE recipient=? AND profile=? AND paper=? "
            "AND next_retry IS NOT NULL AND next_retry<=?",
            (recipient, profile, paper, (now or datetime.now(UTC)).astimezone(UTC).isoformat()),
        ).fetchone()
        if due:
            return True
        if self.db.execute(
            "SELECT 1 FROM assessment_retry WHERE recipient=? AND profile=? AND paper=? "
            "AND next_retry<=?",
            (recipient, profile, paper, (now or datetime.now(UTC)).astimezone(UTC).isoformat()),
        ).fetchone():
            return True
        return not self.db.execute(
            "SELECT 1 FROM reviewed WHERE recipient=? AND profile=? AND paper=?",
            (recipient, profile, paper),
        ).fetchone()

    def was_sent(self, recipient: str, tracking_id: str) -> bool:
        if "@" in tracking_id:
            paper, version = tracking_id.rsplit("@", 1)
            return bool(
                self.db.execute(
                    "SELECT 1 FROM sent_versions WHERE recipient=? AND paper=? AND version=?",
                    (recipient, paper, version),
                ).fetchone()
            )
        return bool(
            self.db.execute(
                "SELECT 1 FROM sent WHERE recipient=? AND paper=?", (recipient, tracking_id)
            ).fetchone()
        )

    def checkpoint(self, recipient: str, source: str) -> str | None:
        row = self.db.execute(
            "SELECT announcement FROM checkpoints WHERE recipient=? AND source=?",
            (recipient, source),
        ).fetchone()
        return row[0] if row else None

    def reset_checkpoint(self, recipient: str, source: str):
        with self.db:
            self.db.execute(
                "DELETE FROM checkpoints WHERE recipient=? AND source=?", (recipient, source)
            )

    def _advance_checkpoint(self, recipient: str, source: str, announcement: str):
        if source and announcement:
            self.db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?) "
                "ON CONFLICT(recipient, source) DO UPDATE SET "
                "announcement=max(checkpoints.announcement, excluded.announcement)",
                (recipient, source, announcement),
            )

    def mark_reviewed(
        self,
        recipient: str,
        profile: str,
        ids: list[str],
        *,
        source="",
        announcement="",
        checkpoint_updates: list[tuple[str, str]] | None = None,
    ):
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO reviewed VALUES (?, ?, ?)",
                [(recipient, profile, paper) for paper in ids],
            )
            self._advance_checkpoint(recipient, source, announcement)
            for source_key, date in checkpoint_updates or []:
                self._advance_checkpoint(recipient, source_key, date)

    def queue_metadata_retry(
        self,
        recipient: str,
        profile: str,
        paper: dict,
        *,
        delay_hours: int,
        max_attempts: int,
        now: datetime,
    ):
        tracking_id = Paper(**paper).tracking_id
        if self.was_sent(recipient, tracking_id):
            return
        row = self.db.execute(
            "SELECT attempts, next_retry FROM metadata_retry "
            "WHERE recipient=? AND profile=? AND paper=?",
            (recipient, profile, tracking_id),
        ).fetchone()
        timestamp = now.astimezone(UTC).isoformat()
        if row and (row[1] is None or row[1] > timestamp):
            # Replaying a failed delivery with the same cached metadata is not
            # another extraction attempt and must not consume the retry budget.
            return
        attempts = row[0] + 1 if row else 1
        next_retry = (
            (now.astimezone(UTC) + timedelta(hours=delay_hours)).isoformat()
            if attempts < max_attempts
            else None
        )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata_retry VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    recipient,
                    profile,
                    tracking_id,
                    json.dumps(paper, ensure_ascii=False),
                    attempts,
                    next_retry,
                    now.astimezone(UTC).isoformat(),
                ),
            )

    def due_metadata(
        self, recipient: str, profile: str, *, now: datetime, limit: int
    ) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM metadata_retry r WHERE recipient=? AND profile=? "
            "AND next_retry IS NOT NULL AND next_retry<=? "
            "AND NOT EXISTS (SELECT 1 FROM sent s "
            "WHERE s.recipient=r.recipient AND s.paper=r.paper) "
            "AND NOT EXISTS (SELECT 1 FROM sent_versions v "
            "WHERE v.recipient=r.recipient AND v.paper || '@' || v.version=r.paper) "
            "ORDER BY next_retry, paper LIMIT ?",
            (recipient, profile, now.astimezone(UTC).isoformat(), limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def clear_metadata_retry(self, recipient: str, profile: str | None, paper: str):
        with self.db:
            if profile is None:
                self.db.execute(
                    "DELETE FROM metadata_retry WHERE recipient=? AND paper=?", (recipient, paper)
                )
            else:
                self.db.execute(
                    "DELETE FROM metadata_retry WHERE recipient=? AND profile=? AND paper=?",
                    (recipient, profile, paper),
                )

    def queue_assessment_retry(
        self, recipient: str, profile: str, paper: dict, *, retry_at: datetime, now: datetime
    ):
        tracking_id = Paper(**paper).tracking_id
        if self.was_sent(recipient, tracking_id):
            return
        timestamp = now.astimezone(UTC).isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO assessment_retry VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(recipient, profile, paper) DO UPDATE SET "
                "payload=excluded.payload, next_retry=excluded.next_retry, "
                "updated=excluded.updated "
                "WHERE assessment_retry.next_retry<=?",
                (
                    recipient,
                    profile,
                    tracking_id,
                    json.dumps(paper, ensure_ascii=False),
                    retry_at.astimezone(UTC).isoformat(),
                    timestamp,
                    timestamp,
                ),
            )

    def due_assessments(self, recipient: str, profile: str, *, now: datetime, limit: int):
        rows = self.db.execute(
            "SELECT payload FROM assessment_retry r WHERE recipient=? AND profile=? "
            "AND next_retry<=? AND NOT EXISTS "
            "(SELECT 1 FROM sent s WHERE s.recipient=r.recipient AND s.paper=r.paper) "
            "AND NOT EXISTS (SELECT 1 FROM sent_versions v "
            "WHERE v.recipient=r.recipient AND v.paper || '@' || v.version=r.paper) "
            "ORDER BY next_retry, paper LIMIT ?",
            (recipient, profile, now.astimezone(UTC).isoformat(), limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def clear_assessment_retry(self, recipient: str, profile: str, paper: str):
        with self.db:
            self.db.execute(
                "DELETE FROM assessment_retry WHERE recipient=? AND profile=? AND paper=?",
                (recipient, profile, paper),
            )

    def discard_prepared(self, recipient: str):
        """A prepared entry cannot have reached SMTP; pending entries remain untouched."""
        with self.db:
            self.db.execute(
                "DELETE FROM outbox WHERE recipient=? AND status='prepared'", (recipient,)
            )

    def begin_attempt(self, delivery_id: str):
        with self.db:
            cursor = self.db.execute(
                "UPDATE outbox SET status='pending' WHERE id=? AND status='prepared'",
                (delivery_id,),
            )
            if cursor.rowcount != 1:
                raise ServiceError("Delivery is not prepared")

    def cancel_before_send(self, delivery_id: str):
        """Caller must know SMTP send has NEVER been invoked for this delivery."""
        with self.db:
            self.db.execute(
                "DELETE FROM outbox WHERE id=? AND status IN ('prepared', 'pending')",
                (delivery_id,),
            )

    def pending(self, recipient: str | None = None) -> list[dict]:
        sql = "SELECT id, recipient, created FROM outbox WHERE status='pending'"
        rows = self.db.execute(
            sql + (" AND recipient=?" if recipient else ""), (recipient,) if recipient else ()
        ).fetchall()
        return [dict(zip(("id", "recipient_hash", "created"), row, strict=True)) for row in rows]

    def delivered_today(self, recipient: str, *, now: datetime) -> bool:
        """Count acknowledged digests, including empty ones; SMTP tests do not count.

        Older state has no acknowledgement timestamp, so use its preparation time.
        Compare in the caller's configured timezone, never the runner's timezone.
        """
        rows = self.db.execute(
            "SELECT coalesce(nullif(sent_at, ''), created) FROM outbox "
            "WHERE recipient=? AND status='sent' AND profile!='delivery-test-v1'",
            (recipient,),
        )
        return any(
            datetime.fromisoformat(row[0]).astimezone(now.tzinfo).date() == now.date()
            for row in rows
        )

    def prepare(
        self,
        recipient: str,
        profile: str,
        candidates: list[str],
        selected: list[str],
        *,
        source="",
        announcement="",
        checkpoint_updates: list[tuple[str, str]] | None = None,
        staged: bool = False,
        selected_versions: list[tuple[str, str]] | None = None,
    ) -> str:
        if self.pending(recipient):
            raise ServiceError("Unresolved email attempt; use pending and resolve commands")
        if self.db.execute(
            "SELECT 1 FROM outbox WHERE recipient=? AND status='prepared'", (recipient,)
        ).fetchone():
            raise ServiceError("A prepared delivery already exists")
        delivery_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO outbox "
                "(id, recipient, profile, candidates, selected, status, created, "
                "source, announcement, checkpoint_updates, selected_versions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    recipient,
                    profile,
                    json.dumps(candidates),
                    json.dumps(selected),
                    "prepared" if staged else "pending",
                    datetime.now(UTC).isoformat(),
                    source,
                    announcement,
                    json.dumps(checkpoint_updates or []),
                    json.dumps(selected_versions or []),
                ),
            )
        return delivery_id

    def resolve(self, delivery_id: str, outcome: str, *, now: datetime | None = None):
        row = self.db.execute(
            "SELECT recipient, profile, candidates, selected, status, source, announcement, "
            "checkpoint_updates, selected_versions "
            "FROM outbox WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if not row or row[4] != "pending":
            raise ServiceError("No pending delivery with that ID")
        recipient, profile, candidates, selected, _, source, announcement, updates, versions = row
        with self.db:
            if outcome == "sent":
                self.db.executemany(
                    "INSERT OR IGNORE INTO reviewed VALUES (?, ?, ?)",
                    [(recipient, profile, p) for p in json.loads(candidates)],
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO sent VALUES (?, ?)",
                    [(recipient, p) for p in json.loads(selected)],
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO sent_versions VALUES (?, ?, ?)",
                    [(recipient, p, version) for p, version in json.loads(versions)],
                )
                completed = json.loads(selected) + [
                    p + "@" + version for p, version in json.loads(versions)
                ]
                self.db.executemany(
                    "DELETE FROM metadata_retry WHERE recipient=? AND paper=?",
                    [(recipient, p) for p in completed],
                )
                self.db.executemany(
                    "DELETE FROM assessment_retry WHERE recipient=? AND paper=?",
                    [(recipient, p) for p in completed],
                )
                self._advance_checkpoint(recipient, source, announcement)
                for source_key, date in json.loads(updates):
                    self._advance_checkpoint(recipient, source_key, date)
                self.db.execute(
                    "UPDATE outbox SET status='sent', sent_at=? WHERE id=?",
                    ((now or datetime.now(UTC)).astimezone(UTC).isoformat(), delivery_id),
                )
            elif outcome == "retry":
                self.db.execute("DELETE FROM outbox WHERE id=?", (delivery_id,))
            else:
                raise ValueError("outcome must be sent or retry")

    def prune(self, days: int, *, now: datetime | None = None):
        cutoff = ((now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=days)).isoformat()
        with self.db:
            self.db.execute("DELETE FROM cache WHERE created < ?", (cutoff,))
            self.db.execute("DELETE FROM outbox WHERE status='sent' AND created < ?", (cutoff,))
            self.db.execute("DELETE FROM metadata_retry WHERE updated < ?", (cutoff,))
            self.db.execute("DELETE FROM assessment_retry WHERE updated < ?", (cutoff,))
