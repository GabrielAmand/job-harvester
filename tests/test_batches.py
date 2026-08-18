from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from job_harvester.batches import BatchError, BatchStore, start_batch
from job_harvester.config import Filters
from job_harvester.models import Job
from job_harvester.revalidation import RevalidationError
from job_harvester.storage import JobStore


def make_job(identity: str, mode: str = "unknown", *, source: str = "greenhouse",
             published: datetime | None = None) -> Job:
    return Job(
        source, identity, "Acme", f"Platform Engineer {identity}", "Paris",
        f"https://jobs/{identity}", work_mode=mode, published_at=published,
        collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_key="acme" if source != "france_travail" else None,
    )


class FakeRevalidator:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.seen = []

    def is_active(self, job):
        self.seen.append(job.external_id)
        outcome = self.outcomes.get(job.external_id, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jobs.sqlite3"
        self.filters = Filters(positive_title_keywords=("platform",))

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, jobs):
        with JobStore(self.database) as store:
            store.upsert(jobs)

    def test_limit_and_remote_deterministic_ordering(self) -> None:
        recent = datetime(2026, 2, 1, tzinfo=timezone.utc)
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.seed([
            make_job("onsite", "onsite"), make_job("unknown"),
            make_job("remote-old", "remote", published=old),
            make_job("hybrid", "hybrid"),
            make_job("remote-new", "remote", published=recent),
        ])
        checker = FakeRevalidator()
        result = start_batch(self.database, self.filters, limit=3, revalidator=checker)
        self.assertEqual(checker.seen, ["remote-new", "remote-old", "hybrid"])
        self.assertEqual([e.job.external_id for e in result.entries], checker.seen)

    def test_expired_jobs_are_replaced_and_reviewed_jobs_do_not_return(self) -> None:
        self.seed([make_job(str(i), "remote") for i in range(4)])
        result = start_batch(
            self.database, self.filters, limit=2,
            revalidator=FakeRevalidator({"0": False}),
        )
        self.assertEqual(result.expired, 1)
        self.assertEqual(len(result.entries), 2)
        with BatchStore(self.database) as store:
            for entry in result.entries:
                store.review(entry.job.source, entry.job.external_id, "skip")
            store.complete()
        second = start_batch(self.database, self.filters, limit=5, revalidator=FakeRevalidator())
        self.assertEqual([e.job.external_id for e in second.entries], ["3"])

    def test_temporary_failure_is_fully_atomic(self) -> None:
        self.seed([make_job("0"), make_job("1")])
        checker = FakeRevalidator({"0": False, "1": RevalidationError("offline")})
        with self.assertRaises(RevalidationError):
            start_batch(self.database, self.filters, limit=2, revalidator=checker)
        with BatchStore(self.database) as store:
            self.assertIsNone(store.current())
            states = store.connection.execute("SELECT COUNT(*) FROM job_reviews").fetchone()[0]
        self.assertEqual(states, 0)

    def test_open_batch_persists_and_prevents_a_second_batch(self) -> None:
        self.seed([make_job("1")])
        first = start_batch(self.database, self.filters, limit=1, revalidator=FakeRevalidator())
        with BatchStore(self.database) as store:
            self.assertEqual(store.current().id, first.batch.id)
        with self.assertRaisesRegex(BatchError, "open batch"):
            start_batch(self.database, self.filters, limit=1, revalidator=FakeRevalidator())

    def test_completion_requires_review_and_abandon_returns_pending(self) -> None:
        self.seed([make_job("1")])
        result = start_batch(self.database, self.filters, limit=1, revalidator=FakeRevalidator())
        with BatchStore(self.database) as store:
            with self.assertRaisesRegex(BatchError, "unreviewed"):
                store.complete()
            store.abandon()
        again = start_batch(self.database, self.filters, limit=1, revalidator=FakeRevalidator())
        self.assertEqual(again.entries[0].job.external_id, result.entries[0].job.external_id)

    def test_migrates_a_v5_jobs_database_additively(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
            "external_id TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL, "
            "location TEXT NOT NULL, remote_status TEXT, work_mode TEXT NOT NULL, "
            "remote_scope TEXT NOT NULL, published_at TEXT, url TEXT NOT NULL, "
            "collected_at TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(source, external_id))"
        )
        connection.commit(); connection.close()
        with BatchStore(self.database) as store:
            tables = {r[0] for r in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({"jobs", "job_reviews", "batches", "batch_jobs"} <= tables)
        with JobStore(self.database) as store:
            columns = {r[1] for r in store.connection.execute("PRAGMA table_info(jobs)")}
        self.assertIn("source_key", columns)
