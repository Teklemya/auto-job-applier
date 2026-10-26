"""SQLite job store (stdlib only).

UNIQUE(url) gives dedup on re-ingest, status updates are single-row transactions, and `events`
is an append-only audit log of every status change.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .engines.base import Packet, PageField
from .models import ATS, Fit, FormField, Job, ResolvedAnswer, Status, Tier

_STATUSES = ", ".join(f"'{s}'" for s in Status)
# Not yet in flight: safe to re-screen when the posting's facts change.
_REOPENABLE = (Status.PENDING, Status.MANUAL, Status.SKIPPED)
RETIRED_REASON = "no longer listed (closed)"

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

CREATE TABLE IF NOT EXISTS descriptions (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fit_checks (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    verdict     TEXT,
    score       INTEGER,
    keywords    TEXT NOT NULL DEFAULT '[]',
    reason      TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('llm', 'keyword', 'none')),
    checked_at  TEXT NOT NULL
);

-- Answers the owner gave at review (D3), reused for the same question later (R10).
CREATE TABLE IF NOT EXISTS answer_bank (
    question_key  TEXT PRIMARY KEY,
    question      TEXT NOT NULL,
    value         TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- The resolved form of a staged job (D3): `aja review` re-fills from it without the LLM.
CREATE TABLE IF NOT EXISTS packets (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    entries     TEXT NOT NULL,
    blockers    TEXT NOT NULL,
    saved_at    TEXT NOT NULL
);

-- LLM calls per local day and model: daily budgets (D10) hold across runs.
CREATE TABLE IF NOT EXISTS llm_calls (
    day           TEXT NOT NULL,
    model         TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    rate_limited  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
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
        """Insert new jobs; refresh metadata of known URLs.

        Status is kept, except that a job not yet in flight (PENDING, MANUAL, SKIPPED) whose
        flags or location changed is reopened as NEW so screening sees the new facts. A retired
        posting that is listed again is reopened too. Returns (inserted, updated).
        """
        inserted = updated = 0
        now = _now()
        with self._conn:
            for job in jobs:
                known = self._conn.execute(
                    "SELECT id, status, reason, flags, location FROM jobs WHERE url = ?",
                    (job.url,),
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
                if known is None:
                    inserted += 1
                    continue
                updated += 1
                changed = (
                    known["flags"] != json.dumps(job.flags) or known["location"] != job.location
                )
                relisted = known["reason"] == RETIRED_REASON
                if Status(known["status"]) in _REOPENABLE and (changed or relisted):
                    why = "relisted" if relisted else "flags or location changed: re-screen"
                    self._write_status(known["id"], known["status"], Status.NEW, why, None, now)
        return inserted, updated

    def retire_missing(self, source: str, seen_urls: set[str]) -> int:
        """Mark not-yet-applied jobs from `source` that are no longer listed as SKIPPED.

        Closed SimplifyJobs rows lose their link, so a closed posting simply disappears.
        """
        now = _now()
        retired = 0
        with self._conn:
            rows = self._conn.execute(
                "SELECT id, url, status FROM jobs WHERE source = ? AND status IN (?, ?, ?)",
                (source, Status.NEW.value, Status.PENDING.value, Status.MANUAL.value),
            ).fetchall()
            for row in rows:
                if row["url"] not in seen_urls:
                    self._write_status(
                        row["id"], row["status"], Status.SKIPPED, RETIRED_REASON, None, now
                    )
                    retired += 1
        return retired

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
        if limit is not None:
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
            self._write_status(job_id, row["status"], status, reason, tier, _now())

    def _write_status(
        self,
        job_id: int,
        from_status: str,
        status: Status,
        reason: str | None,
        tier: Tier | None,
        now: str,
    ) -> None:
        """Status update + audit event. Caller owns the transaction."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, reason = ?, tier = COALESCE(?, tier), updated_at = ?"
            " WHERE id = ?",
            (status.value, reason, tier.value if tier else None, now, job_id),
        )
        self._conn.execute(
            "INSERT INTO events (job_id, from_status, to_status, message, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, from_status, status.value, reason, now),
        )

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    def save_description(self, job_id: int, text: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO descriptions (job_id, text, fetched_at) VALUES (?, ?, ?)",
                (job_id, text, _now()),
            )

    def get_description(self, job_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT text FROM descriptions WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["text"] if row else None

    def save_fit(self, job_id: int, fit: Fit) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO fit_checks"
                " (job_id, verdict, score, keywords, reason, source, checked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    fit.verdict,
                    fit.score,
                    json.dumps(fit.matched_keywords),
                    fit.reason,
                    fit.source,
                    _now(),
                ),
            )

    def get_fit(self, job_id: int) -> Fit | None:
        row = self._conn.execute("SELECT * FROM fit_checks WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return Fit(
            row["verdict"], row["score"], json.loads(row["keywords"]), row["reason"], row["source"]
        )

    def save_packet(self, job_id: int, packet: Packet) -> None:
        entries = [
            {
                "dom_id": pf.dom_id,
                "field": asdict(pf.field),
                "value": a.value,
                "source": a.source,
                "confident": a.confident,
                "reason": a.reason,
                "alternatives": a.alternatives,
            }
            for pf, a in zip(packet.fields, packet.answers, strict=True)
        ]
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO packets (job_id, entries, blockers, saved_at)"
                " VALUES (?, ?, ?, ?)",
                (job_id, json.dumps(entries), json.dumps(packet.blockers), _now()),
            )

    def get_packet(self, job_id: int) -> Packet | None:
        row = self._conn.execute("SELECT * FROM packets WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        fields, answers = [], []
        for e in json.loads(row["entries"]):
            field = FormField(**e["field"])
            fields.append(PageField(e["dom_id"], field))
            answers.append(
                ResolvedAnswer(
                    field,
                    e["value"],
                    e["source"],
                    e["confident"],
                    e["reason"],
                    e.get("alternatives", []),
                )
            )
        return Packet(fields, answers, json.loads(row["blockers"]))

    def bank_entries(self) -> list[tuple[str, str]]:
        """[(question_key, value)]"""
        rows = self._conn.execute("SELECT question_key, value FROM answer_bank")
        return [(r["question_key"], r["value"]) for r in rows]

    def save_bank_entry(self, key: str, question: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO answer_bank (question_key, question, value, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (key, question, value, _now()),
            )

    def count_llm_call(self, day: str, model: str, rate_limited: bool = False) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO llm_calls (day, model, calls, rate_limited) VALUES (?, ?, 1, ?)"
                " ON CONFLICT (day, model) DO UPDATE SET calls = calls + 1,"
                " rate_limited = rate_limited + excluded.rate_limited",
                (day, model, int(rate_limited)),
            )

    def llm_usage(self, day: str, model: str) -> tuple[int, int]:
        """(calls, rate-limited calls) made to `model` on `day`."""
        row = self._conn.execute(
            "SELECT calls, rate_limited FROM llm_calls WHERE day = ? AND model = ?", (day, model)
        ).fetchone()
        return (row["calls"], row["rate_limited"]) if row else (0, 0)

    def apply_queue(self, ats: ATS, limit: int | None = None) -> list[Job]:
        """Fit-checked PENDING jobs on `ats`, best first: location tier, then fit score (D9).
        Jobs still waiting for a fit check are left for a later run."""
        sql = (
            "SELECT jobs.* FROM jobs JOIN fit_checks ON fit_checks.job_id = jobs.id"
            " WHERE jobs.status = ? AND jobs.ats = ?"
            " ORDER BY jobs.tier IS NULL, jobs.tier, fit_checks.score IS NULL,"
            " fit_checks.score DESC, jobs.id"
        )
        args: list = [Status.PENDING.value, ats.value]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [_to_job(r) for r in self._conn.execute(sql, args)]

    def count_events(self, message_prefix: str, since: str) -> int:
        """Status changes whose message starts with `message_prefix`, at or after `since`
        (ISO, UTC)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE substr(message, 1, ?) = ? AND at >= ?",
            (len(message_prefix), message_prefix, since),
        ).fetchone()
        return row["n"]

    def new_since(self, since: str) -> dict[str, int]:
        """{ats: count} of jobs first collected at or after `since` that passed screening."""
        rows = self._conn.execute(
            "SELECT ats, COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status NOT IN (?, ?)"
            " GROUP BY ats",
            (since, Status.SKIPPED.value, Status.NEW.value),
        )
        return {r["ats"]: r["n"] for r in rows}

    def counts(self) -> dict[tuple[str, str], int]:
        """{(ats, status): count}"""
        rows = self._conn.execute("SELECT ats, status, COUNT(*) AS n FROM jobs GROUP BY 1, 2")
        return {(r["ats"], r["status"]): r["n"] for r in rows}
