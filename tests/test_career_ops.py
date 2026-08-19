from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from job_harvester.batches import BatchError, BatchStore, start_batch
from job_harvester.career_ops import (
    CareerOpsEvaluation,
    CareerOpsError,
    _persist_evaluation,
    evaluate_open_batch,
    external_id,
    result_path,
    prepare_application_artifacts,
)
from job_harvester.applications import ApplicationStore
from job_harvester.config import CareerOpsConfig, Filters
from job_harvester.models import Job
from job_harvester.storage import JobStore


class ActiveRevalidator:
    def is_active(self, job: Job) -> bool:
        return True


def completed_result(
    job_id: int,
    category: str = "good_candidate",
    *,
    tracker_id: int = 7,
    report_path: str = "reports/099-acme.md",
    cv_payload_path: str | None = None,
    cv_html_path: str | None = None,
    cv_pdf_path: str | None = None,
) -> dict[str, object]:
    recommendations = {
        "automatic_skip": "skip",
        "review": "review",
        "good_candidate": "apply",
        "priority_candidate": "prioritize",
    }
    return {
        "schema_version": "1.0",
        "status": "completed",
        "external_id": external_id(job_id),
        "job": {"url": f"https://jobs/{job_id}"},
        "evaluation": {
            "score": 4.1,
            "category": category,
            "recommendation": recommendations[category],
        },
        "artifacts": {
            "report_path": report_path,
            "cv_payload_path": cv_payload_path,
            "cv_html_path": cv_html_path,
            "cv_pdf_path": cv_pdf_path,
        },
        "tracker": {"id": tracker_id, "status": "Ready"},
        "meta": {"evaluated_at": "2026-08-18T18:00:00.000Z"},
        "error": None,
    }


class WritingRunner:
    def __init__(self, documents: dict[int, dict[str, object]], returncode: int = 0):
        self.documents = documents
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        root = Path(cwd)
        self.calls.append((command, root))
        job_id = int(command[command.index("--external-id") + 1].split(":", 1)[1])
        destination = root / command[command.index("--json-out") + 1]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.documents[job_id]), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "", "failure")


class CareerOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "jobs.sqlite3"
        self.repository = self.root / "career-ops"
        self.repository.mkdir()
        (self.repository / "evaluate-job.mjs").write_text("// fake\n")
        self.config = CareerOpsConfig(
            enabled=True,
            repository_path=self.repository,
            node_command="fake-node",
            batch_size=20,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_batch(self, count: int) -> list[int]:
        jobs = [
            Job(
                "greenhouse", str(index), "Acme", f"Platform Engineer {index}",
                "Remote", f"https://jobs/{index}", source_key="acme",
            )
            for index in range(1, count + 1)
        ]
        with JobStore(self.database) as store:
            store.upsert(jobs)
        start_batch(
            self.database,
            Filters(positive_title_keywords=("platform",)),
            limit=count,
            revalidator=ActiveRevalidator(),
        )
        with BatchStore(self.database) as store:
            return [
                row[0] for row in store.connection.execute(
                    "SELECT job_id FROM batch_jobs ORDER BY position"
                ).fetchall()
            ]

    def reviews(self):
        with BatchStore(self.database) as store:
            return store.connection.execute(
                "SELECT job_id, review_state, decision FROM job_reviews ORDER BY job_id"
            ).fetchall()

    def test_external_id_and_deterministic_result_path(self) -> None:
        self.assertEqual(external_id(42), "job-harvester:42")
        self.assertEqual(
            result_path(42), "output/evaluations/job-harvester-42.json"
        )

    def test_command_invocation_and_tracker_report_independence(self) -> None:
        job_id = self.create_batch(1)[0]
        runner = WritingRunner({
            job_id: completed_result(
                job_id, tracker_id=7, report_path="reports/099-acme.md"
            )
        })
        result = evaluate_open_batch(self.database, self.config, runner=runner)
        command, cwd = runner.calls[0]
        self.assertEqual(cwd, self.repository.resolve())
        self.assertEqual(command, [
            "fake-node", str((self.repository / "evaluate-job.mjs").resolve()),
            "--url", "https://jobs/1",
            "--external-id", external_id(job_id),
            "--json-out", result_path(job_id),
        ])
        self.assertEqual(result.evaluations[0].tracker_id, 7)
        self.assertEqual(result.evaluations[0].report_path, "reports/099-acme.md")

    def test_all_completed_categories_map_to_review_decisions(self) -> None:
        job_ids = self.create_batch(4)
        categories = (
            "automatic_skip", "review", "good_candidate", "priority_candidate"
        )
        runner = WritingRunner({
            job_id: completed_result(job_id, category, tracker_id=20 + job_id)
            for job_id, category in zip(job_ids, categories)
        })
        result = evaluate_open_batch(self.database, self.config, runner=runner)
        self.assertEqual([item.category for item in result.evaluations], list(categories))
        self.assertEqual(
            {job_id: (state, decision) for job_id, state, decision in self.reviews()},
            {
                job_ids[0]: ("reviewed", "skip"),
                job_ids[1]: ("reviewed", "needs_review"),
                job_ids[2]: ("reviewed", "good_candidate"),
                job_ids[3]: ("reviewed", "priority_candidate"),
            },
        )

    def test_null_artifacts_are_preserved(self) -> None:
        job_id = self.create_batch(1)[0]
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: completed_result(job_id)}),
        )
        with BatchStore(self.database) as store:
            row = store.connection.execute(
                """
                SELECT career_ops_cv_payload_path, career_ops_cv_html_path,
                       career_ops_cv_pdf_path
                FROM career_ops_evaluations WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
        self.assertEqual(row, (None, None, None))

    def test_artifact_paths_and_evaluation_metadata_are_persisted(self) -> None:
        job_id = self.create_batch(1)[0]
        document = completed_result(
            job_id,
            cv_payload_path="output/cv/007.json",
            cv_html_path="output/cv/007.html",
            cv_pdf_path="output/cv/007.pdf",
        )
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: document}),
        )
        with BatchStore(self.database) as store:
            row = store.connection.execute(
                """
                SELECT career_ops_score, career_ops_category,
                       career_ops_recommendation, career_ops_tracker_id,
                       career_ops_report_path, career_ops_cv_payload_path,
                       career_ops_cv_html_path, career_ops_cv_pdf_path,
                       career_ops_evaluated_at, career_ops_error
                FROM career_ops_evaluations WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
        self.assertEqual(row, (
            4.1, "good_candidate", "apply", 7, "reports/099-acme.md",
            "output/cv/007.json", "output/cv/007.html", "output/cv/007.pdf",
            "2026-08-18T18:00:00.000Z", None,
        ))

    def test_schema_version_failure_is_retryable(self) -> None:
        job_id = self.create_batch(1)[0]
        document = completed_result(job_id)
        document["schema_version"] = "2.0"
        result = evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: document}),
        )
        self.assertEqual(result.failed, 1)
        self.assertIn("schema_version", result.evaluations[0].error)
        self.assertEqual(self.reviews(), [(job_id, "in_review", None)])

    def test_structured_failed_result_and_nonzero_exit_are_persisted(self) -> None:
        job_id = self.create_batch(1)[0]
        failed = {
            "schema_version": "1.0", "status": "failed",
            "external_id": external_id(job_id), "error": "integrity failure",
            "meta": {"evaluated_at": "2026-08-18T19:00:00.000Z"},
        }
        result = evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: failed}, returncode=1),
        )
        self.assertEqual(result.evaluations[0].error, "integrity failure")
        self.assertEqual(
            result.evaluations[0].evaluated_at, "2026-08-18T19:00:00.000Z"
        )
        self.assertEqual(self.reviews(), [(job_id, "in_review", None)])

    def test_nonzero_without_result_is_retryable(self) -> None:
        job_id = self.create_batch(1)[0]

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 9, "", "crashed")

        result = evaluate_open_batch(self.database, self.config, runner=runner)
        self.assertIn("code 9", result.evaluations[0].error)
        self.assertEqual(self.reviews(), [(job_id, "in_review", None)])

    def test_failed_job_retries_and_completed_job_is_idempotently_skipped(self) -> None:
        job_id = self.create_batch(1)[0]
        failed = {
            "schema_version": "1.0", "status": "failed",
            "external_id": external_id(job_id), "error": "temporary",
            "meta": {"evaluated_at": "2026-08-18T19:00:00.000Z"},
        }
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: failed}, returncode=1),
        )
        success = WritingRunner({job_id: completed_result(job_id)})
        retried = evaluate_open_batch(self.database, self.config, runner=success)
        self.assertEqual(len(retried.evaluations), 1)
        self.assertEqual(self.reviews(), [(job_id, "reviewed", "good_candidate")])

        def should_not_run(*args, **kwargs):
            raise AssertionError("completed evaluation was invoked again")

        repeated = evaluate_open_batch(
            self.database, self.config, runner=should_not_run
        )
        self.assertEqual(repeated.evaluations, ())
        self.assertEqual(repeated.already_completed, 1)

    def test_mixed_completed_and_unevaluated_batch_persists(self) -> None:
        job_ids = self.create_batch(3)
        first = WritingRunner({
            job_ids[0]: completed_result(job_ids[0]),
            job_ids[1]: completed_result(job_ids[1], "review"),
        })
        evaluate_open_batch(self.database, self.config, limit=2, runner=first)
        second = WritingRunner({
            job_ids[2]: completed_result(job_ids[2], "automatic_skip")
        })
        result = evaluate_open_batch(self.database, self.config, runner=second)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(result.already_completed, 2)
        with BatchStore(self.database) as store:
            entries = store.entries(store.current().id)
        self.assertTrue(all(entry.review_state == "reviewed" for entry in entries))

    def test_force_reevaluates_completed_job(self) -> None:
        job_id = self.create_batch(1)[0]
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: completed_result(job_id, "review")}),
        )
        forced = WritingRunner({job_id: completed_result(job_id, "priority_candidate")})
        evaluate_open_batch(self.database, self.config, force=True, runner=forced)
        self.assertEqual(len(forced.calls), 1)
        self.assertEqual(self.reviews(), [
            (job_id, "reviewed", "priority_candidate")
        ])

    def test_application_preparation_reuses_force_artifacts_interface(self) -> None:
        job_id = self.create_batch(1)[0]
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: completed_result(job_id, "review")}),
        )
        with ApplicationStore(self.database) as store:
            store.decide(job_id, "apply")
        cv = "output/cv/review.pdf"
        (self.repository / cv).parent.mkdir(parents=True, exist_ok=True)
        (self.repository / cv).write_bytes(b"%PDF fixture")
        runner = WritingRunner({
            job_id: completed_result(job_id, "review", cv_pdf_path=cv)
        })
        with BatchStore(self.database) as store:
            before = store.connection.execute(
                "SELECT career_ops_score, career_ops_category, career_ops_recommendation, "
                "career_ops_tracker_id, career_ops_report_path FROM career_ops_evaluations "
                "WHERE job_id=?", (job_id,),
            ).fetchone()
        prepare_application_artifacts(
            self.database, self.config, job_id, runner=runner,
            revalidator=ActiveRevalidator(),
        )
        self.assertIn("--force-artifacts", runner.calls[0][0])
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
            self.assertEqual(item.state, "ready_to_apply")
            self.assertEqual(item.preparation_status, "completed")
        with BatchStore(self.database) as store:
            after = store.connection.execute(
                "SELECT career_ops_score, career_ops_category, career_ops_recommendation, "
                "career_ops_tracker_id, career_ops_report_path FROM career_ops_evaluations "
                "WHERE job_id=?", (job_id,),
            ).fetchone()
        self.assertEqual(after, before)

    def test_preparation_failure_preserves_evaluation_and_apply_decision(self) -> None:
        job_id = self.create_batch(1)[0]
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: completed_result(job_id, "review")}),
        )
        with ApplicationStore(self.database) as store:
            store.decide(job_id, "apply")
        with BatchStore(self.database) as store:
            before = store.connection.execute(
                "SELECT * FROM career_ops_evaluations WHERE job_id=?", (job_id,),
            ).fetchone()
        failed = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "artifact generation failed"
        )
        with self.assertRaisesRegex(CareerOpsError, "artifact generation failed"):
            prepare_application_artifacts(
                self.database, self.config, job_id, runner=failed,
                revalidator=ActiveRevalidator(),
            )
        with BatchStore(self.database) as store:
            after = store.connection.execute(
                "SELECT * FROM career_ops_evaluations WHERE job_id=?", (job_id,),
            ).fetchone()
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
        self.assertEqual(after, before)
        self.assertEqual(item.decision, "apply")
        self.assertEqual(item.state, "not_started")
        self.assertEqual(item.preparation_status, "failed")
        self.assertIn("artifact generation failed", item.preparation_error)

    def test_preparation_restores_completed_result_after_legacy_corruption(self) -> None:
        job_id = self.create_batch(1)[0]
        evaluate_open_batch(
            self.database, self.config,
            runner=WritingRunner({job_id: completed_result(job_id, "review")}),
        )
        with ApplicationStore(self.database) as store:
            store.decide(job_id, "apply")
        _persist_evaluation(self.database, CareerOpsEvaluation(
            job_id, 1, "Acme", "Platform Engineer", "failed", None, None,
            None, None, f"output/evaluations/job-harvester-{job_id}.json",
            None, None, None, None, "2026-08-19T12:00:00+00:00", "legacy failure",
        ))
        cv = "output/cv/recovered.pdf"
        (self.repository / cv).parent.mkdir(parents=True, exist_ok=True)
        (self.repository / cv).write_bytes(b"%PDF fixture")
        prepare_application_artifacts(
            self.database, self.config, job_id,
            runner=WritingRunner({
                job_id: completed_result(job_id, "review", cv_pdf_path=cv)
            }),
            revalidator=ActiveRevalidator(),
        )
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
        self.assertEqual(item.decision, "apply")
        self.assertEqual(item.state, "ready_to_apply")
        self.assertEqual(item.score, 4.1)
        self.assertEqual(item.tracker_id, 7)

    def test_requires_open_batch_and_valid_repository(self) -> None:
        with self.assertRaisesRegex(BatchError, "open batch"):
            evaluate_open_batch(self.database, self.config, runner=lambda *a, **k: None)
        disabled = CareerOpsConfig(enabled=False)
        with self.assertRaisesRegex(CareerOpsError, "disabled"):
            evaluate_open_batch(self.database, disabled, runner=lambda *a, **k: None)
        missing = CareerOpsConfig(
            enabled=True, repository_path=self.root / "missing"
        )
        with self.assertRaisesRegex(CareerOpsError, "repository not found"):
            evaluate_open_batch(self.database, missing, runner=lambda *a, **k: None)
        empty_repository = self.root / "empty-career-ops"
        empty_repository.mkdir()
        no_script = CareerOpsConfig(
            enabled=True, repository_path=empty_repository
        )
        with self.assertRaisesRegex(CareerOpsError, "evaluator not found"):
            evaluate_open_batch(self.database, no_script, runner=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
