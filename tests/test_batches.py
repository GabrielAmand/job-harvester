from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from job_harvester.batches import BatchError, BatchStore, start_batch
from job_harvester.config import Filters
from job_harvester.models import Job
from job_harvester.revalidation import CandidateRevalidationError, RevalidationError
from job_harvester.storage import JobStore


def make_job(identity: str, mode: str = "unknown", *, source: str = "greenhouse",
             published: datetime | None = None, scope: str = "unknown",
             full_remote: bool = False, title: str | None = None,
             eligibility: str = "unknown") -> Job:
    return Job(
        source, identity, "Acme", title or f"Platform Engineer {identity}", "Paris",
        f"https://jobs/{identity}", work_mode=mode, remote_scope=scope,
        full_remote=full_remote, published_at=published,
        collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_key="acme" if source != "france_travail" else None,
        remote_eligibility=eligibility,
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

    def test_full_remote_and_scope_priority_categories(self) -> None:
        jobs = [
            make_job("onsite", "onsite"), make_job("unknown"),
            make_job("hybrid", "hybrid"),
            make_job("remote-restricted", "remote", scope="restricted"),
            make_job("remote-unknown", "remote"),
            make_job("remote-worldwide", "remote", scope="worldwide"),
            make_job("remote-europe", "remote", scope="europe"),
            make_job("remote-france", "remote", scope="france"),
            make_job("full-unknown", "remote", full_remote=True),
            make_job("full-worldwide", "remote", scope="worldwide", full_remote=True),
            make_job("full-europe", "remote", scope="europe", full_remote=True),
            make_job("full-france", "remote", scope="france", full_remote=True),
        ]
        self.seed(reversed(jobs))
        checker = FakeRevalidator()
        start_batch(self.database, self.filters, limit=12, revalidator=checker)
        self.assertEqual(checker.seen, [
            "full-france", "full-europe", "full-worldwide", "full-unknown",
            "remote-france", "remote-europe", "remote-worldwide",
            "remote-unknown", "remote-restricted", "hybrid", "unknown", "onsite",
        ])

    def test_same_category_keeps_recency_and_stable_tie_breakers(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.seed([
            make_job("b", "remote", scope="france", published=old),
            make_job("c", "remote", scope="france", published=new),
            make_job("a", "remote", scope="france", published=old),
        ])
        checker = FakeRevalidator()
        start_batch(self.database, self.filters, limit=3, revalidator=checker)
        self.assertEqual(checker.seen, ["c", "a", "b"])

    def test_remote_category_precedes_seniority(self) -> None:
        filters = Filters(
            positive_title_keywords=("platform",), allow_strong_seniority=True
        )
        self.seed([
            make_job("strong", "remote", scope="france", full_remote=True,
                     title="Principal Platform Engineer"),
            make_job("senior", "remote", scope="france", full_remote=True,
                     title="Senior Platform Engineer"),
            make_job("normal", "onsite", title="Platform Engineer"),
        ])
        checker = FakeRevalidator()
        start_batch(self.database, filters, limit=3, revalidator=checker)
        self.assertEqual(checker.seen, ["senior", "strong", "normal"])

    def test_seniority_orders_jobs_within_the_same_remote_category(self) -> None:
        filters = Filters(
            positive_title_keywords=("platform",), allow_strong_seniority=True
        )
        self.seed([
            make_job("strong", "remote", scope="france",
                     title="Principal Platform Engineer"),
            make_job("senior", "remote", scope="france",
                     title="Senior Platform Engineer"),
            make_job("normal", "remote", scope="france",
                     title="Platform Engineer"),
        ])
        checker = FakeRevalidator()
        start_batch(self.database, filters, limit=3, revalidator=checker)
        self.assertEqual(checker.seen, ["normal", "senior", "strong"])

    def test_strong_seniority_does_not_fill_normal_batches_by_default(self) -> None:
        self.seed([
            make_job("strong", "remote", title="Staff Platform Engineer"),
            make_job("senior", "remote", title="Senior Platform Engineer"),
        ])
        checker = FakeRevalidator()
        result = start_batch(self.database, self.filters, limit=2, revalidator=checker)
        self.assertEqual(checker.seen, ["senior"])
        self.assertEqual([entry.job.external_id for entry in result.entries], ["senior"])

    def test_disallowed_onsite_jobs_are_removed_before_ordering(self) -> None:
        self.seed([
            make_job("onsite", "onsite", title="Platform Engineer"),
            make_job("remote", "remote", title="Senior Platform Engineer"),
        ])
        filters = Filters(
            positive_title_keywords=("platform",),
            remote_policy="prefer",
            allow_hybrid=True,
            allow_onsite=False,
        )
        checker = FakeRevalidator()
        result = start_batch(self.database, filters, limit=2, revalidator=checker)
        self.assertEqual(checker.seen, ["remote"])
        self.assertEqual([entry.job.external_id for entry in result.entries], ["remote"])

    def test_incompatible_remote_and_out_of_scope_jobs_never_enter_batch(self) -> None:
        self.seed([
            make_job("good", "remote", title="Platform Engineer"),
            make_job("us", "remote", scope="restricted",
                     title="Platform Engineer", eligibility="incompatible"),
            make_job("tpm", "remote", title="Technical Program Manager, Platform"),
            make_job("us-tpm", "remote", scope="restricted",
                     title="Technical Program Manager, Infrastructure",
                     eligibility="incompatible"),
        ])
        checker = FakeRevalidator()
        result = start_batch(self.database, self.filters, limit=10, revalidator=checker)
        self.assertEqual(checker.seen, ["good"])
        self.assertEqual(result.pending_candidates, 1)

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

    def test_temporary_failure_is_deferred_and_batch_remains_atomic(self) -> None:
        self.seed([make_job("0"), make_job("1"), make_job("2")])
        checker = FakeRevalidator({"0": CandidateRevalidationError("offline")})
        result = start_batch(self.database, self.filters, limit=2, revalidator=checker)
        self.assertEqual(checker.seen, ["0", "0", "1", "2"])
        self.assertEqual([entry.job.external_id for entry in result.entries], ["1", "2"])
        self.assertEqual([(job.external_id, reason) for job, reason in result.deferred],
                         [("0", "offline")])
        with BatchStore(self.database) as store:
            state = store.connection.execute(
                "SELECT r.review_state FROM jobs j LEFT JOIN job_reviews r ON r.job_id=j.id "
                "WHERE j.external_id='0'"
            ).fetchone()[0]
        self.assertIsNone(state)

    def test_repeated_malformed_candidate_does_not_block_later_candidates(self) -> None:
        self.seed([make_job("0", source="france_travail"), make_job("1"), make_job("2")])
        malformed = CandidateRevalidationError(
            "France Travail offer 0 returned invalid JSON"
        )
        result = start_batch(
            self.database, self.filters, limit=2,
            revalidator=FakeRevalidator({"0": malformed}),
        )
        self.assertEqual(len(result.entries), 2)
        self.assertEqual([entry.job.external_id for entry in result.entries], ["1", "2"])
        self.assertEqual(result.expired, 0)
        with BatchStore(self.database) as store:
            row = store.connection.execute(
                "SELECT r.review_state FROM jobs j LEFT JOIN job_reviews r ON r.job_id=j.id "
                "WHERE j.external_id='0'"
            ).fetchone()
        self.assertIsNone(row[0])

    def test_systemic_revalidation_failure_still_aborts_atomically(self) -> None:
        self.seed([make_job("0")])
        with self.assertRaisesRegex(RevalidationError, "authentication"):
            start_batch(
                self.database, self.filters, limit=1,
                revalidator=FakeRevalidator({
                    "0": RevalidationError("authentication failed")
                }),
            )
        with BatchStore(self.database) as store:
            self.assertIsNone(store.current())
            count = store.connection.execute(
                "SELECT COUNT(*) FROM job_reviews"
            ).fetchone()[0]
        self.assertEqual(count, 0)

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
        self.assertIn("full_remote", columns)

    def test_migrates_v6_review_decisions_additively(self) -> None:
        self.seed([make_job("legacy")])
        connection = sqlite3.connect(self.database)
        job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]
        connection.execute(
            """
            CREATE TABLE job_reviews (
                job_id INTEGER PRIMARY KEY,
                review_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_state IN ('pending', 'in_review', 'reviewed', 'expired')),
                decision TEXT CHECK (decision IN ('interesting', 'skip', 'undecided')),
                reviewed_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO job_reviews VALUES (?, 'reviewed', 'interesting', NULL)",
            (job_id,),
        )
        connection.commit()
        connection.close()
        with BatchStore(self.database) as store:
            schema = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='job_reviews'"
            ).fetchone()[0]
            row = store.connection.execute("SELECT * FROM job_reviews").fetchone()
        self.assertIn("priority_candidate", schema)
        self.assertEqual(row[:3], (job_id, "reviewed", "interesting"))
