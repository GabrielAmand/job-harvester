from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
from typing import Callable

from job_harvester.batches import BatchStore
from job_harvester.config import CareerOpsConfig
from job_harvester.models import Job
from job_harvester.revalidation import JobRevalidator
from job_harvester.storage import _datetime, _timestamp


APPLICATION_STATES = (
    "not_started", "needs_review", "ready_to_apply", "applied", "declined",
)


class ApplicationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Application:
    job_id: int
    state: str
    decision: str | None
    company: str
    title: str
    source: str
    url: str
    score: float | None
    category: str
    recommendation: str
    tracker_id: int | None
    report_path: str | None
    cv_pdf_path: str | None
    state_reason: str | None
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class AppliedResult:
    application: Application
    tracker_synchronized: bool
    tracker_error: str | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    job_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'not_started' CHECK (state IN (
        'not_started', 'needs_review', 'ready_to_apply', 'applied', 'declined'
    )),
    decision TEXT CHECK (decision IN ('apply', 'skip')),
    state_reason TEXT,
    decided_at TEXT,
    prepared_at TEXT,
    applied_at TEXT,
    applied_job_url TEXT,
    applied_career_ops_tracker_id INTEGER,
    applied_career_ops_report_path TEXT,
    applied_cv_pdf_path TEXT,
    applied_score REAL,
    applied_category TEXT,
    applied_source TEXT,
    applied_company TEXT,
    applied_title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (to_state IN (
        'not_started', 'needs_review', 'ready_to_apply', 'applied', 'declined'
    )),
    event_at TEXT NOT NULL,
    reason TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS application_events_job_id
    ON application_events(job_id, id);
"""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ApplicationStore(AbstractContextManager["ApplicationStore"]):
    def __init__(self, path: str | Path) -> None:
        # BatchStore applies every additive migration through V7 first.
        with BatchStore(path):
            pass
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)
        self._backfill_v7_evaluations()
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def _backfill_v7_evaluations(self) -> None:
        """Create workflow rows for completed evaluations imported before V8."""
        now = _timestamp(datetime.now(timezone.utc))
        missing = self.connection.execute(
            """
            SELECT e.job_id,
                   CASE e.career_ops_category
                     WHEN 'review' THEN 'needs_review'
                     ELSE 'not_started'
                   END
            FROM career_ops_evaluations e
            LEFT JOIN applications a ON a.job_id=e.job_id
            WHERE e.career_ops_status='completed' AND a.job_id IS NULL
              AND e.career_ops_category IN (
                'automatic_skip', 'review', 'good_candidate', 'priority_candidate'
              )
            """
        ).fetchall()
        for job_id, state in missing:
            self.connection.execute(
                "INSERT INTO applications(job_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, state, now, now),
            )
            self.connection.execute(
                "INSERT INTO application_events(job_id, from_state, to_state, event_at, reason) "
                "VALUES (?, NULL, ?, ?, 'V7 evaluation migrated')",
                (job_id, state, now),
            )

    def get(self, job_id: int) -> Application:
        row = self.connection.execute(_APPLICATION_QUERY + " WHERE j.id = ?", (job_id,)).fetchone()
        if row is None:
            raise ApplicationError(f"evaluated job not found: {job_id}")
        return _application(row)

    def list_active(self) -> list[Application]:
        rows = self.connection.execute(
            _APPLICATION_QUERY
            + " WHERE (a.state IN ('needs_review', 'ready_to_apply') "
              "OR (a.state='not_started' AND e.career_ops_category != 'automatic_skip')) "
              "ORDER BY CASE e.career_ops_category WHEN 'priority_candidate' THEN 0 "
              "WHEN 'good_candidate' THEN 1 ELSE 2 END, e.career_ops_score DESC, j.id"
        ).fetchall()
        return [_application(row) for row in rows]

    def list_session(self) -> list[Application]:
        """Return a stable snapshot of applications requiring human action."""
        rows = self.connection.execute(
            _APPLICATION_QUERY
            + " WHERE a.state IN ('needs_review', 'ready_to_apply') "
              "AND NOT EXISTS (SELECT 1 FROM job_reviews r "
              "WHERE r.job_id=j.id AND r.review_state='expired') "
              "ORDER BY CASE "
              "WHEN a.state='ready_to_apply' AND e.career_ops_category='priority_candidate' THEN 0 "
              "WHEN a.state='ready_to_apply' AND e.career_ops_category='good_candidate' THEN 1 "
              "WHEN a.state='ready_to_apply' THEN 2 ELSE 3 END, "
              "e.career_ops_score DESC, e.career_ops_evaluated_at, j.id"
        ).fetchall()
        return [_application(row) for row in rows]

    def is_expired(self, job_id: int) -> bool:
        row = self.connection.execute(
            "SELECT review_state FROM job_reviews WHERE job_id=?", (job_id,)
        ).fetchone()
        return row is not None and row[0] == "expired"

    def initialize_evaluation(self, job_id: int, category: str) -> str:
        target = {
            "automatic_skip": "not_started",
            "review": "needs_review",
            "good_candidate": "not_started",
            "priority_candidate": "not_started",
        }.get(category)
        if target is None:
            raise ApplicationError(f"unsupported Career-Ops category: {category}")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            row = self.connection.execute(
                "SELECT state FROM applications WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO applications(job_id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (job_id, target, now, now),
                )
                self._event(job_id, None, target, "Career-Ops evaluation mapped")
                return target
            current = str(row[0])
            if current in {"applied", "declined"}:
                return current
            if current != target:
                self._transition(job_id, target, "Career-Ops evaluation mapped")
            return target

    def transition(self, job_id: int, state: str, reason: str | None = None) -> None:
        if state not in APPLICATION_STATES:
            raise ApplicationError(f"unsupported application state: {state}")
        with self.connection:
            self._transition(job_id, state, reason)

    def decide(self, job_id: int, decision: str) -> None:
        if decision not in {"apply", "skip"}:
            raise ApplicationError(f"unsupported application decision: {decision}")
        application = self.get(job_id)
        if application.state != "needs_review":
            raise ApplicationError(
                f"job {job_id} is {application.state}, not needs_review"
            )
        if decision == "skip":
            self.decline(job_id)
            return
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "UPDATE applications SET decision=?, decided_at=?, updated_at=? WHERE job_id=?",
                (decision, now, now, job_id),
            )
            self._transition(job_id, "not_started", "user decided apply")

    def decline(self, job_id: int) -> None:
        application = self.get(job_id)
        if application.state not in {"needs_review", "ready_to_apply"}:
            raise ApplicationError(
                f"job {job_id} is {application.state}, not eligible for decline"
            )
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "UPDATE applications SET decision='skip', decided_at=?, updated_at=? "
                "WHERE job_id=?",
                (now, now, job_id),
            )
            self._transition(job_id, "declined", "user decided skip")

    def reopen(self, job_id: int) -> None:
        application = self.get(job_id)
        if application.state != "declined":
            raise ApplicationError(f"job {job_id} is {application.state}, not declined")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "UPDATE applications SET decision=NULL, decided_at=NULL, updated_at=? "
                "WHERE job_id=?",
                (now, job_id),
            )
            self._transition(job_id, "needs_review", "user reopened application decision")

    def mark_expired(self, job_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE job_reviews SET review_state='expired' WHERE job_id=?", (job_id,)
            )
            self._transition(job_id, "not_started", "job is expired")

    def mark_applied(self, job_id: int) -> Application:
        application = self.get(job_id)
        if application.state != "ready_to_apply":
            raise ApplicationError(
                f"job {job_id} is {application.state}, not ready_to_apply"
            )
        now = _timestamp(datetime.now(timezone.utc))
        row = self.connection.execute(
            """
            SELECT j.url, e.career_ops_tracker_id, e.career_ops_report_path,
                   e.career_ops_cv_pdf_path, e.career_ops_score,
                   e.career_ops_category, j.source, j.company, j.title
            FROM jobs j JOIN career_ops_evaluations e ON e.job_id=j.id
            WHERE j.id=? AND e.career_ops_status='completed'
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ApplicationError(f"completed Career-Ops evaluation not found: {job_id}")
        with self.connection:
            self.connection.execute(
                """
                UPDATE applications SET state='applied', state_reason=?, applied_at=?,
                    applied_job_url=?, applied_career_ops_tracker_id=?,
                    applied_career_ops_report_path=?, applied_cv_pdf_path=?,
                    applied_score=?, applied_category=?, applied_source=?,
                    applied_company=?, applied_title=?, updated_at=? WHERE job_id=?
                """,
                ("user confirmed submission", now, *row, now, job_id),
            )
            self._event(job_id, "ready_to_apply", "applied", "user confirmed submission")
        return self.get(job_id)

    def job(self, job_id: int) -> Job:
        row = self.connection.execute(
            """
            SELECT source, external_id, company, title, location, url, work_mode,
                   remote_scope, published_at, collected_at, source_key,
                   full_remote, remote_eligibility FROM jobs WHERE id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ApplicationError(f"job not found: {job_id}")
        return Job(
            source=row[0], external_id=row[1], company=row[2], title=row[3],
            location=row[4], url=row[5], work_mode=row[6], remote_scope=row[7],
            published_at=_datetime(row[8]), collected_at=_datetime(row[9]),
            source_key=row[10], full_remote=bool(row[11]), remote_eligibility=row[12],
        )

    def _transition(self, job_id: int, target: str, reason: str | None) -> None:
        row = self.connection.execute(
            "SELECT state FROM applications WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise ApplicationError(f"application workflow not found: {job_id}")
        current = str(row[0])
        now = _timestamp(datetime.now(timezone.utc))
        prepared_at = now if target == "ready_to_apply" else None
        self.connection.execute(
            """
            UPDATE applications SET state=?, state_reason=?,
                prepared_at=COALESCE(?, prepared_at), updated_at=? WHERE job_id=?
            """,
            (target, reason, prepared_at, now, job_id),
        )
        if current != target:
            self._event(job_id, current, target, reason)

    def _event(
        self, job_id: int, from_state: str | None, to_state: str, reason: str | None
    ) -> None:
        self.connection.execute(
            "INSERT INTO application_events(job_id, from_state, to_state, event_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, from_state, to_state, _timestamp(datetime.now(timezone.utc)), reason),
        )


_APPLICATION_QUERY = """
SELECT j.id, a.state, a.decision, j.company, j.title, j.source, j.url,
       e.career_ops_score, e.career_ops_category, e.career_ops_recommendation,
       e.career_ops_tracker_id,
       e.career_ops_report_path, e.career_ops_cv_pdf_path, a.state_reason,
       a.applied_at
FROM applications a JOIN jobs j ON j.id=a.job_id
JOIN career_ops_evaluations e ON e.job_id=a.job_id
"""


def _application(row: tuple[object, ...]) -> Application:
    return Application(
        job_id=int(row[0]), state=str(row[1]),
        decision=str(row[2]) if row[2] is not None else None,
        company=str(row[3]), title=str(row[4]), source=str(row[5]), url=str(row[6]),
        score=float(row[7]) if row[7] is not None else None, category=str(row[8]),
        recommendation=str(row[9]),
        tracker_id=int(row[10]) if row[10] is not None else None,
        report_path=str(row[11]) if row[11] is not None else None,
        cv_pdf_path=str(row[12]) if row[12] is not None else None,
        state_reason=str(row[13]) if row[13] is not None else None,
        applied_at=_datetime(str(row[14])) if row[14] is not None else None,
    )


def synchronize_evaluation(
    database: str | Path,
    config: CareerOpsConfig,
    job_id: int,
    *,
    revalidator: JobRevalidator | None = None,
) -> Application:
    with ApplicationStore(database) as store:
        row = store.connection.execute(
            """
            SELECT career_ops_status, career_ops_category, career_ops_recommendation,
                   career_ops_cv_pdf_path FROM career_ops_evaluations WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row[0] != "completed":
            raise ApplicationError(f"completed Career-Ops evaluation not found: {job_id}")
        category, recommendation, cv_path = str(row[1]), str(row[2]), row[3]
        state = store.initialize_evaluation(job_id, category)
        if state in {"applied", "declined"} or category == "automatic_skip":
            return store.get(job_id)
        explicit_apply = store.get(job_id).decision == "apply"
        recommendation_ok = (
            (category == "good_candidate" and recommendation == "apply")
            or (category == "priority_candidate" and recommendation == "prioritize")
            or (category == "review" and explicit_apply)
        )
        if not recommendation_ok:
            return store.get(job_id)
        if not isinstance(cv_path, str) or not cv_path:
            store.transition(job_id, "not_started", "Career-Ops CV PDF preparation required")
            return store.get(job_id)
        if config.repository_path is None or not (config.repository_path.resolve() / cv_path).is_file():
            store.transition(job_id, "not_started", "Career-Ops CV PDF is missing")
            return store.get(job_id)
        job = store.job(job_id)
        if not (revalidator or JobRevalidator()).is_active(job):
            store.mark_expired(job_id)
            raise ApplicationError(f"job {job_id} is expired")
        store.transition(job_id, "ready_to_apply", "artifacts ready and job revalidated")
        return store.get(job_id)


def mark_applied(
    database: str | Path,
    config: CareerOpsConfig,
    job_id: int,
    *,
    revalidator: JobRevalidator | None = None,
    runner: Runner = subprocess.run,
) -> AppliedResult:
    with ApplicationStore(database) as store:
        current = store.get(job_id)
        if current.state != "ready_to_apply":
            raise ApplicationError(
                f"job {job_id} is {current.state}, not ready_to_apply"
            )
        if (
            current.cv_pdf_path is None
            or config.repository_path is None
            or not (config.repository_path.resolve() / current.cv_pdf_path).is_file()
        ):
            store.transition(job_id, "not_started", "Career-Ops CV PDF is missing")
            raise ApplicationError(f"job {job_id} CV PDF is missing; prepare it again")
        job = store.job(job_id)
        if not (revalidator or JobRevalidator()).is_active(job):
            store.mark_expired(job_id)
            raise ApplicationError(f"job {job_id} is expired")
        application = store.mark_applied(job_id)
    synchronized = False
    error = None
    if config.enabled and config.repository_path is not None and application.tracker_id is not None:
        script = config.repository_path.resolve() / "set-status.mjs"
        if script.is_file():
            command = [
                config.node_command, str(script), "--row", str(application.tracker_id),
                "Applied", "--on", application.applied_at.date().isoformat(), "--json",
            ]
            try:
                process = runner(command, cwd=config.repository_path.resolve(), capture_output=True, text=True, check=False)
                synchronized = process.returncode == 0
                if not synchronized:
                    error = (process.stderr or process.stdout or f"exit {process.returncode}").strip()
            except OSError as exc:
                error = str(exc)
        else:
            error = f"Career-Ops tracker updater not found: {script}"
    else:
        error = "Career-Ops tracker synchronization is not configured"
    return AppliedResult(application, synchronized, error)
