from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .http import ServiceError


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
        if version not in {0, 1}:
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
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL, profile TEXT NOT NULL,
                candidates TEXT NOT NULL, selected TEXT NOT NULL,
                status TEXT NOT NULL, created TEXT NOT NULL
            );
            PRAGMA user_version = 1;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def cached(self, kind: str, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload FROM cache WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, kind: str, key: str, payload: dict):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                (kind, key, json.dumps(payload, ensure_ascii=False), datetime.now(UTC).isoformat()),
            )

    def eligible(self, recipient: str, profile: str, paper: str) -> bool:
        return not self.db.execute(
            "SELECT 1 FROM sent WHERE recipient=? AND paper=? "
            "UNION ALL SELECT 1 FROM reviewed WHERE recipient=? AND profile=? AND paper=?",
            (recipient, paper, recipient, profile, paper),
        ).fetchone()

    def mark_reviewed(self, recipient: str, profile: str, ids: list[str]):
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO reviewed VALUES (?, ?, ?)",
                [(recipient, profile, paper) for paper in ids],
            )

    def pending(self, recipient: str | None = None) -> list[dict]:
        sql = "SELECT id, recipient, created FROM outbox WHERE status='pending'"
        rows = self.db.execute(
            sql + (" AND recipient=?" if recipient else ""), (recipient,) if recipient else ()
        ).fetchall()
        return [dict(zip(("id", "recipient_hash", "created"), row, strict=True)) for row in rows]

    def prepare(
        self, recipient: str, profile: str, candidates: list[str], selected: list[str]
    ) -> str:
        if self.pending(recipient):
            raise ServiceError("Unresolved email attempt; use pending and resolve commands")
        delivery_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO outbox VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    delivery_id,
                    recipient,
                    profile,
                    json.dumps(candidates),
                    json.dumps(selected),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return delivery_id

    def resolve(self, delivery_id: str, outcome: str):
        row = self.db.execute(
            "SELECT recipient, profile, candidates, selected, status FROM outbox WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if not row or row[4] != "pending":
            raise ServiceError("No pending delivery with that ID")
        recipient, profile, candidates, selected, _ = row
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
                self.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (delivery_id,))
            elif outcome == "retry":
                self.db.execute("DELETE FROM outbox WHERE id=?", (delivery_id,))
            else:
                raise ValueError("outcome must be sent or retry")

    def prune(self, days: int):
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.db:
            self.db.execute("DELETE FROM cache WHERE created < ?", (cutoff,))
            self.db.execute("DELETE FROM outbox WHERE status='sent' AND created < ?", (cutoff,))
