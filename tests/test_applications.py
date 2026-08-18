from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import sqlite3
import subprocess
import tempfile
import unittest

from job_harvester.applications import (
    ApplicationError,
    ApplicationStore,
    mark_applied,
    synchronize_evaluation,
)
from job_harvester.batches import BatchStore, start_batch
from job_harvester.career_ops import CareerOpsEvaluation, _persist_evaluation
from job_harvester.config import CareerOpsConfig, Filters
from job_harvester.models import Job
from job_harvester.revalidation import RevalidationError
from job_harvester.storage import JobStore
from job_harvester import cli


class Revalidator:
    def __init__(self, active: bool = True, error: Exception | None = None) -> None:
        self.active = active
        self.error = error
        self.calls = 0

    def is_active(self, job: Job) -> bool:
        self.calls += 1
        if self.error:
            raise self.error
        return self.active


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "jobs.sqlite3"
        self.repository = self.root / "career-ops"
        self.repository.mkdir()
        (self.repository / "evaluate-job.mjs").write_text("// fake\n")
        (self.repository / "set-status.mjs").write_text("// fake\n")
        self.config = CareerOpsConfig(enabled=True, repository_path=self.repository)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            "[career_ops]\n"
            f'enabled = true\nrepository_path = "{self.repository}"\n'
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_job(self, external_id: str = "1") -> int:
        with JobStore(self.database) as store:
            store.upsert([Job(
                "greenhouse", external_id, "Acme", "Platform Engineer", "Remote",
                f"https://boards.greenhouse.io/acme/jobs/{external_id}", source_key="acme",
            )])
        with BatchStore(self.database) as store:
            return int(store.connection.execute(
                "SELECT id FROM jobs WHERE external_id=?", (external_id,)
            ).fetchone()[0])

    def evaluate(
        self,
        job_id: int,
        category: str,
        *,
        cv: str | None = None,
        recommendation: str | None = None,
    ) -> None:
        recommendations = {
            "automatic_skip": "skip", "review": "review",
            "good_candidate": "apply", "priority_candidate": "prioritize",
        }
        _persist_evaluation(self.database, CareerOpsEvaluation(
            job_id, 1, "Acme", "Platform Engineer", "completed", 4.3,
            category, recommendation or recommendations[category], 17,
            f"output/evaluations/job-harvester-{job_id}.json", "reports/017-acme.md",
            None, None, cv, "2026-08-18T12:00:00+00:00", None,
        ))

    def make_cv(self, name: str = "output/acme.pdf") -> str:
        path = self.repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF fixture")
        return name

    def test_skip_is_not_queued_and_review_needs_explicit_decision(self) -> None:
        skipped, review = self.add_job("1"), self.add_job("2")
        self.evaluate(skipped, "automatic_skip")
        self.evaluate(review, "review")
        synchronize_evaluation(self.database, self.config, skipped)
        synchronize_evaluation(self.database, self.config, review)
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(skipped).state, "not_started")
            self.assertEqual(store.get(review).state, "needs_review")
            self.assertEqual([item.job_id for item in store.list_active()], [review])

    def test_needs_review_user_skip_is_declined_and_leaves_queue(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "review")
        synchronize_evaluation(self.database, self.config, job_id)
        with ApplicationStore(self.database) as store:
            store.decide(job_id, "skip")
            self.assertEqual(store.get(job_id).state, "declined")
            self.assertEqual(store.list_active(), [])
            store.reopen(job_id)
            self.assertEqual(store.get(job_id).state, "needs_review")

    def test_needs_review_user_apply_can_become_ready_with_artifact(self) -> None:
        job_id = self.add_job()
        cv = self.make_cv()
        self.evaluate(job_id, "review", cv=cv)
        synchronize_evaluation(self.database, self.config, job_id)
        with ApplicationStore(self.database) as store:
            store.decide(job_id, "apply")
        checker = Revalidator()
        item = synchronize_evaluation(
            self.database, self.config, job_id, revalidator=checker
        )
        self.assertEqual(item.state, "ready_to_apply")
        self.assertEqual(checker.calls, 1)

    def test_good_and_priority_require_real_cv_and_revalidation(self) -> None:
        good, priority = self.add_job("1"), self.add_job("2")
        cv = self.make_cv()
        self.evaluate(good, "good_candidate", cv=cv)
        self.evaluate(priority, "priority_candidate", cv=cv)
        for job_id in (good, priority):
            item = synchronize_evaluation(
                self.database, self.config, job_id, revalidator=Revalidator()
            )
            self.assertEqual(item.state, "ready_to_apply")
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.list_active()[0].category, "priority_candidate")

    def test_missing_cv_requires_preparation_without_revalidating(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "good_candidate", cv="output/missing.pdf")
        checker = Revalidator()
        item = synchronize_evaluation(
            self.database, self.config, job_id, revalidator=checker
        )
        self.assertEqual(item.state, "not_started")
        self.assertIn("missing", item.state_reason)
        self.assertEqual(checker.calls, 0)

    def test_expired_and_temporary_revalidation_are_distinct(self) -> None:
        expired = self.add_job("1")
        temporary = self.add_job("2")
        cv = self.make_cv()
        self.evaluate(expired, "good_candidate", cv=cv)
        self.evaluate(temporary, "good_candidate", cv=cv)
        with self.assertRaisesRegex(ApplicationError, "expired"):
            synchronize_evaluation(
                self.database, self.config, expired, revalidator=Revalidator(False)
            )
        with self.assertRaises(RevalidationError):
            synchronize_evaluation(
                self.database, self.config, temporary,
                revalidator=Revalidator(error=RevalidationError("temporary")),
            )
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(expired).state, "not_started")
            self.assertEqual(store.get(temporary).state, "not_started")
            review_state = store.connection.execute(
                "SELECT review_state FROM job_reviews WHERE job_id=?", (expired,)
            ).fetchone()[0]
        self.assertEqual(review_state, "expired")

    def test_only_explicit_mark_applied_snapshots_metadata_and_syncs_tracker(self) -> None:
        job_id = self.add_job()
        cv = self.make_cv()
        self.evaluate(job_id, "priority_candidate", cv=cv)
        synchronize_evaluation(
            self.database, self.config, job_id, revalidator=Revalidator()
        )
        with ApplicationStore(self.database) as store:
            self.assertIsNone(store.get(job_id).applied_at)
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

        result = mark_applied(
            self.database, self.config, job_id,
            revalidator=Revalidator(), runner=runner,
        )
        self.assertEqual(result.application.state, "applied")
        self.assertIsNotNone(result.application.applied_at)
        self.assertTrue(result.tracker_synchronized)
        self.assertIn("--row", calls[0])
        with ApplicationStore(self.database) as store:
            snapshot = store.connection.execute(
                """
                SELECT applied_job_url, applied_career_ops_tracker_id,
                       applied_career_ops_report_path, applied_cv_pdf_path,
                       applied_score, applied_category, applied_source,
                       applied_company, applied_title FROM applications WHERE job_id=?
                """, (job_id,),
            ).fetchone()
        self.assertEqual(snapshot, (
            "https://boards.greenhouse.io/acme/jobs/1", 17, "reports/017-acme.md",
            cv, 4.3, "priority_candidate", "greenhouse", "Acme", "Platform Engineer",
        ))

    def test_batch_completion_does_not_wait_for_application(self) -> None:
        job_id = self.add_job()
        start_batch(
            self.database, Filters(positive_title_keywords=("platform",)), limit=1,
            revalidator=Revalidator(),
        )
        self.evaluate(job_id, "review")
        synchronize_evaluation(self.database, self.config, job_id)
        with BatchStore(self.database) as store:
            completed = store.complete()
        self.assertEqual(completed.status, "completed")
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(job_id).state, "needs_review")

    def test_v7_database_migrates_additively(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "review")
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='applications'"
            ).fetchone())
        with ApplicationStore(self.database) as store:
            names = {row[0] for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            migrated = store.get(job_id)
        self.assertIn("applications", names)
        self.assertIn("application_events", names)
        self.assertIn("career_ops_evaluations", names)
        self.assertEqual(migrated.state, "needs_review")

    def test_application_list_cli_is_grouped_and_terminal_friendly(self) -> None:
        review, priority = self.add_job("1"), self.add_job("2")
        cv = self.make_cv()
        self.evaluate(review, "review")
        self.evaluate(priority, "priority_candidate", cv=cv)
        synchronize_evaluation(self.database, self.config, review)
        synchronize_evaluation(
            self.database, self.config, priority, revalidator=Revalidator()
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main([
                "application", "--database", str(self.database),
                "--config", str(self.config_path), "list",
            ])
        self.assertEqual(result, 0)
        self.assertIn("PRIORITY\n", output.getvalue())
        self.assertIn("NEEDS REVIEW\n", output.getvalue())
        self.assertIn("output/acme.pdf", output.getvalue())


if __name__ == "__main__":
    unittest.main()
