"""SQLite job store (stdlib only).

UNIQUE(url) gives dedup on re-ingest, status updates are single-row transactions, and `events`
is an append-only audit log of every status change.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .models import ATS, Job, Status, Tier

_STATUSES = ", ".join(f"'{s}'" for s in Status)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    company     TEXT NOT NULL,
    role        TEXT NOT NULL,
    location    TEXT NOT NULL DEFAULT '',
    ats         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ({_STATUSES})),
    flags       TEXT NOT NULL DEFAULT '[]',
    age         TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    reason      TEXT,
    tier        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_ats ON jobs (status, ats);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    message     TEXT,
    at          TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        company=row["company"],
        role=row["role"],
        location=row["location"],
        ats=ATS(row["ats"]),
        status=Status(row["status"]),
        flags=json.loads(row["flags"]),
        age=row["age"],
        source=row["source"],
        reason=row["reason"],
        tier=Tier(row["tier"]) if row["tier"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def upsert_many(self, jobs: Iterable[Job]) -> tuple[int, int]:
        """Insert new jobs; refresh metadata of known URLs without touching status.

        Returns (inserted, updated).
        """
        inserted = updated = 0
        now = _now()
        with self._conn:
            for job in jobs:
                known = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE url = ?", (job.url,)
                ).fetchone()
                self._conn.execute(
                    """
                    INSERT INTO jobs (url, company, role, location, ats, status, flags, age,
                                      source, reason, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (url) DO UPDATE SET
                        company = excluded.company, role = excluded.role,
                        location = excluded.location, ats = excluded.ats,
                        flags = excluded.flags, age = excluded.age,
                        updated_at = excluded.updated_at
                    """,
                    (
                        job.url,
                        job.company,
                        job.role,
                        job.location,
                        job.ats.value,
                        job.status.value,
                        json.dumps(job.flags),
                        job.age,
                        job.source,
                        job.reason,
                        now,
                        now,
                    ),
                )
                if known:
                    updated += 1
                else:
                    inserted += 1
        return inserted, updated

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _to_job(row) if row else None

    def list(
        self, status: Status | None = None, ats: ATS | None = None, limit: int | None = None
    ) -> list[Job]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status.value)
        if ats:
            sql += " AND ats = ?"
            args.append(ats.value)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [_to_job(r) for r in self._conn.execute(sql, args)]

    def set_status(
        self, job_id: int, status: Status, reason: str | None = None, tier: Tier | None = None
    ) -> None:
        """Change status (and reason); `tier` is only set when given."""
        with self._conn:
            row = self._conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"no job with id {job_id}")
            now = _now()
            self._conn.execute(
                "UPDATE jobs SET status = ?, reason = ?, tier = COALESCE(?, tier), updated_at = ?"
                " WHERE id = ?",
                (status.value, reason, tier.value if tier else None, now, job_id),
            )
            self._conn.execute(
                "INSERT INTO events (job_id, from_status, to_status, message, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, row["status"], status.value, reason, now),
            )

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    def counts(self) -> dict[tuple[str, str], int]:
        """{(ats, status): count}"""
        rows = self._conn.execute("SELECT ats, status, COUNT(*) AS n FROM jobs GROUP BY 1, 2")
        return {(r["ats"], r["status"]): r["n"] for r in rows}
