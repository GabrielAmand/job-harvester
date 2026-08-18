from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from job_harvester.config import Filters
from job_harvester.filters import is_relevant, seniority_category
from job_harvester.models import Job
from job_harvester.revalidation import JobRevalidator
from job_harvester.storage import JobStore, _datetime, _timestamp


REVIEW_STATES = ("pending", "in_review", "reviewed", "expired")
DECISIONS = ("interesting", "skip", "undecided")


class BatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Batch:
    id: int
    status: str
    requested_limit: int
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BatchEntry:
    position: int
    job: Job
    review_state: str
    decision: str | None


@dataclass(frozen=True, slots=True)
class BatchStartResult:
    batch: Batch
    pending_candidates: int
    revalidated: int
    expired: int
    entries: tuple[BatchEntry, ...]


SCHEMA = """
CREATE TABLE IF NOT EXISTS job_reviews (
    job_id INTEGER PRIMARY KEY,
    review_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_state IN ('pending', 'in_review', 'reviewed', 'expired')),
    decision TEXT CHECK (decision IN ('interesting', 'skip', 'undecided')),
    reviewed_at TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'abandoned')),
    requested_limit INTEGER NOT NULL CHECK (requested_limit > 0),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_batch
    ON batches(status) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, job_id),
    UNIQUE (batch_id, position),
    FOREIGN KEY (batch_id) REFERENCES batches(id),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""


class BatchStore(AbstractContextManager["BatchStore"]):
    def __init__(self, path: str | Path) -> None:
        # Ensure jobs and its additive V6 provenance migration exist first.
        with JobStore(path):
            pass
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def current(self) -> Batch | None:
        row = self.connection.execute(
            "SELECT id, status, requested_limit, created_at, completed_at "
            "FROM batches WHERE status = 'open'"
        ).fetchone()
        return _batch(row) if row else None

    def list(self) -> list[Batch]:
        rows = self.connection.execute(
            "SELECT id, status, requested_limit, created_at, completed_at "
            "FROM batches ORDER BY id DESC"
        ).fetchall()
        return [_batch(row) for row in rows]

    def entries(self, batch_id: int) -> list[BatchEntry]:
        rows = self.connection.execute(
            """
            SELECT bj.position, j.source, j.external_id, j.company, j.title,
                   j.location, j.url, j.work_mode, j.remote_scope,
                   j.published_at, j.collected_at, j.source_key, j.full_remote,
                   j.remote_eligibility,
                   COALESCE(r.review_state, 'pending'), r.decision
            FROM batch_jobs bj
            JOIN jobs j ON j.id = bj.job_id
            LEFT JOIN job_reviews r ON r.job_id = j.id
            WHERE bj.batch_id = ? ORDER BY bj.position
            """,
            (batch_id,),
        ).fetchall()
        return [
            BatchEntry(
                position=row[0],
                job=Job(
                    source=row[1], external_id=row[2], company=row[3], title=row[4],
                    location=row[5], url=row[6], work_mode=row[7],
                    remote_scope=row[8], published_at=_datetime(row[9]),
                    collected_at=_datetime(row[10]), source_key=row[11],
                    full_remote=bool(row[12]),
                    remote_eligibility=row[13],
                ),
                review_state=row[14], decision=row[15],
            )
            for row in rows
        ]

    def eligible_identities(self) -> set[tuple[str, str]]:
        rows = self.connection.execute(
            """
            SELECT j.source, j.external_id FROM jobs j
            LEFT JOIN job_reviews r ON r.job_id = j.id
            WHERE COALESCE(r.review_state, 'pending') = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id
                  WHERE bj.job_id = j.id AND b.status = 'open'
              )
            """
        ).fetchall()
        return {(row[0], row[1]) for row in rows}

    def create(
        self,
        jobs: list[Job],
        expired: list[Job],
        *,
        requested_limit: int,
    ) -> Batch:
        now = _timestamp(datetime.now(timezone.utc))
        try:
            with self.connection:
                if self.connection.execute(
                    "SELECT 1 FROM batches WHERE status = 'open'"
                ).fetchone():
                    raise BatchError("an open batch already exists; resume or finish it")
                for job in expired:
                    job_id = self._job_id(job)
                    self.connection.execute(
                        """
                        INSERT INTO job_reviews(job_id, review_state)
                        VALUES (?, 'expired')
                        ON CONFLICT(job_id) DO UPDATE SET
                            review_state = 'expired', decision = NULL, reviewed_at = NULL
                        """,
                        (job_id,),
                    )
                cursor = self.connection.execute(
                    "INSERT INTO batches(status, requested_limit, created_at) "
                    "VALUES ('open', ?, ?)",
                    (requested_limit, now),
                )
                batch_id = cursor.lastrowid
                assert batch_id is not None
                for position, job in enumerate(jobs, 1):
                    job_id = self._job_id(job)
                    self.connection.execute(
                        """
                        INSERT INTO job_reviews(job_id, review_state)
                        VALUES (?, 'in_review')
                        ON CONFLICT(job_id) DO UPDATE SET review_state = 'in_review'
                        """,
                        (job_id,),
                    )
                    self.connection.execute(
                        "INSERT INTO batch_jobs(batch_id, job_id, position, added_at) "
                        "VALUES (?, ?, ?, ?)",
                        (batch_id, job_id, position, now),
                    )
        except sqlite3.IntegrityError as error:
            raise BatchError(f"could not create batch: {error}") from error
        batch = self.current()
        assert batch is not None
        return batch

    def review(self, source: str, external_id: str, decision: str) -> None:
        if decision not in DECISIONS:
            raise BatchError(f"unsupported review decision: {decision}")
        batch = self.current()
        if batch is None:
            raise BatchError("there is no open batch")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            row = self.connection.execute(
                """
                SELECT j.id FROM jobs j JOIN batch_jobs bj ON bj.job_id = j.id
                WHERE bj.batch_id = ? AND j.source = ? AND j.external_id = ?
                """,
                (batch.id, source, external_id),
            ).fetchone()
            if row is None:
                raise BatchError(f"job is not in the open batch: {source}/{external_id}")
            self.connection.execute(
                "UPDATE job_reviews SET review_state='reviewed', decision=?, reviewed_at=? "
                "WHERE job_id=?",
                (decision, now, row[0]),
            )

    def complete(self) -> Batch:
        batch = self.current()
        if batch is None:
            raise BatchError("there is no open batch")
        remaining = self.connection.execute(
            """
            SELECT COUNT(*) FROM batch_jobs bj
            LEFT JOIN job_reviews r ON r.job_id = bj.job_id
            WHERE bj.batch_id = ? AND COALESCE(r.review_state, 'pending') != 'reviewed'
            """,
            (batch.id,),
        ).fetchone()[0]
        if remaining:
            raise BatchError(f"batch has {remaining} unreviewed job(s)")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "UPDATE batches SET status='completed', completed_at=? WHERE id=?",
                (now, batch.id),
            )
        return self.list()[0]

    def abandon(self) -> Batch:
        batch = self.current()
        if batch is None:
            raise BatchError("there is no open batch")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                """
                UPDATE job_reviews SET review_state='pending'
                WHERE review_state='in_review' AND job_id IN (
                    SELECT job_id FROM batch_jobs WHERE batch_id=?
                )
                """,
                (batch.id,),
            )
            self.connection.execute(
                "UPDATE batches SET status='abandoned', completed_at=? WHERE id=?",
                (now, batch.id),
            )
        return self.list()[0]

    def _job_id(self, job: Job) -> int:
        row = self.connection.execute(
            "SELECT id FROM jobs WHERE source=? AND external_id=?",
            (job.source, job.external_id),
        ).fetchone()
        if row is None:
            raise BatchError(f"stored job not found: {job.source}/{job.external_id}")
        return row[0]


def start_batch(
    path: str | Path,
    filters: Filters,
    *,
    limit: int = 20,
    revalidator: JobRevalidator | None = None,
) -> BatchStartResult:
    if limit <= 0:
        raise BatchError("batch limit must be greater than zero")
    with BatchStore(path) as batches:
        if batches.current() is not None:
            raise BatchError("an open batch already exists; resume or finish it")
        eligible = batches.eligible_identities()
    with JobStore(path) as jobs:
        candidates = [
            record.job for record in jobs.list_jobs()
            if (record.job.source, record.job.external_id) in eligible
            and is_relevant(record.job, filters)
        ]
    candidates.sort(key=_candidate_key)
    checker = revalidator or JobRevalidator()
    selected: list[Job] = []
    expired: list[Job] = []
    checked = 0
    for job in candidates:
        if len(selected) >= limit:
            break
        active = checker.is_active(job)
        checked += 1
        (selected if active else expired).append(job)
    # All review/expiry changes are committed together only after every attempted
    # revalidation succeeded conclusively.
    with BatchStore(path) as batches:
        batch = batches.create(selected, expired, requested_limit=limit)
        entries = tuple(batches.entries(batch.id))
    return BatchStartResult(
        batch=batch,
        pending_candidates=len(candidates),
        revalidated=checked,
        expired=len(expired),
        entries=entries,
    )


def _candidate_key(job: Job) -> tuple[object, ...]:
    category = _priority_category(job)
    seniority = {"normal": 0, "senior": 1, "strong": 2}[
        seniority_category(job.title)
    ]
    published = job.published_at.timestamp() if job.published_at else 0.0
    collected = job.discovered_at().timestamp()
    return (
        category[0],
        seniority,
        job.published_at is None,
        -published,
        -collected,
        job.source,
        job.company.casefold(),
        job.title.casefold(),
        job.external_id,
    )


def _priority_category(job: Job) -> tuple[int, str]:
    scope = job.remote_scope if job.remote_scope in {
        "france", "europe", "worldwide", "unknown", "restricted"
    } else "unknown"
    if job.work_mode == "remote":
        if job.full_remote and scope != "restricted":
            ranks = {"france": 1, "europe": 2, "worldwide": 3, "unknown": 4}
            return ranks[scope], f"full_remote_{scope}"
        ranks = {
            "france": 5, "europe": 6, "worldwide": 7,
            "unknown": 8, "restricted": 9,
        }
        return ranks[scope], f"remote_{scope}"
    if job.work_mode == "hybrid":
        return 10, "hybrid"
    if job.work_mode == "onsite":
        return 12, "onsite"
    return 11, "unknown"


def _batch(row: tuple[object, ...]) -> Batch:
    return Batch(
        id=int(row[0]),
        status=str(row[1]),
        requested_limit=int(row[2]),
        created_at=_datetime(str(row[3])),
        completed_at=_datetime(row[4] if isinstance(row[4], str) else None),
    )
