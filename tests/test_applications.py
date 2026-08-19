from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_harvester.applications import (
    ApplicationError,
    ApplicationStore,
    mark_applied,
    synchronize_evaluation,
)
from job_harvester.batches import BatchStore, start_batch
from job_harvester.career_ops import CareerOpsError, CareerOpsEvaluation, _persist_evaluation
from job_harvester.config import CareerOpsConfig, Filters
from job_harvester.models import Job
from job_harvester.revalidation import RevalidationError
from job_harvester.storage import JobStore
from job_harvester import cli
from job_harvester import application_session


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

    def test_session_snapshot_excludes_ineligible_and_orders_stably(self) -> None:
        automatic = self.add_job("1")
        declined = self.add_job("2")
        applied = self.add_job("3")
        review = self.add_job("4")
        good = self.add_job("5")
        priority = self.add_job("6")
        cv = self.make_cv()
        self.evaluate(automatic, "automatic_skip")
        self.evaluate(declined, "review")
        self.evaluate(applied, "good_candidate", cv=cv)
        self.evaluate(review, "review")
        self.evaluate(good, "good_candidate", cv=cv)
        self.evaluate(priority, "priority_candidate", cv=cv)
        synchronize_evaluation(self.database, self.config, automatic)
        synchronize_evaluation(self.database, self.config, declined)
        synchronize_evaluation(self.database, self.config, review)
        for job_id in (applied, good, priority):
            synchronize_evaluation(
                self.database, self.config, job_id, revalidator=Revalidator()
            )
        with ApplicationStore(self.database) as store:
            store.decline(declined)
            store.mark_applied(applied)
            first = [item.job_id for item in store.list_session()]
            second = [item.job_id for item in store.list_session()]
        self.assertEqual(first, [priority, good, review])
        self.assertEqual(second, first)

    def test_session_open_next_and_quit_do_not_mutate(self) -> None:
        first, second = self.add_job("1"), self.add_job("2")
        self.evaluate(first, "review")
        self.evaluate(second, "review")
        synchronize_evaluation(self.database, self.config, first)
        synchronize_evaluation(self.database, self.config, second)
        actions = iter(("o", "r", "c", "n", "q"))
        opened: list[str] = []
        output = io.StringIO()
        application_session.run_session(
            self.database, self.config,
            input_fn=lambda prompt: next(actions), output=output,
            opener=lambda target: opened.append(str(target)) or True,
        )
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(first).state, "needs_review")
            self.assertEqual(store.get(second).state, "needs_review")
        self.assertEqual(len(opened), 2)
        self.assertIn("CV not ready.", output.getvalue())
        self.assertIn("Deferred: 1", output.getvalue())

    def test_needs_review_apply_prepares_without_marking_applied(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "review")
        synchronize_evaluation(self.database, self.config, job_id)
        actions = iter(("a", "q"))

        def prepare(database, config, selected):
            with ApplicationStore(database) as store:
                store.transition(selected, "ready_to_apply", "test preparation")
                return store.get(selected)

        with patch.object(application_session, "_prepare", side_effect=prepare):
            application_session.run_session(
                self.database, self.config, input_fn=lambda prompt: next(actions),
                output=io.StringIO(), opener=lambda target: True,
            )
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
        self.assertEqual(item.state, "ready_to_apply")
        self.assertIsNone(item.applied_at)

    def test_preparation_failure_keeps_application_unapplied(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "review")
        synchronize_evaluation(self.database, self.config, job_id)
        actions = iter(("a", "q"))
        with patch.object(
            application_session, "_prepare", side_effect=CareerOpsError("temporary")
        ):
            application_session.run_session(
                self.database, self.config, input_fn=lambda prompt: next(actions),
                output=io.StringIO(), opener=lambda target: True,
            )
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
        self.assertNotEqual(item.state, "applied")
        self.assertIsNone(item.applied_at)

    def test_ready_session_requires_confirmation_then_advances(self) -> None:
        selected, following = self.add_job("1"), self.add_job("2")
        cv = self.make_cv()
        self.evaluate(selected, "priority_candidate", cv=cv)
        self.evaluate(following, "review")
        synchronize_evaluation(
            self.database, self.config, selected, revalidator=Revalidator()
        )
        synchronize_evaluation(self.database, self.config, following)
        actions = iter(("a", "y", "q"))

        def apply(database, config, job_id):
            return mark_applied(
                database, config, job_id, revalidator=Revalidator(),
                runner=lambda command, **kwargs: subprocess.CompletedProcess(
                    command, 0, "", ""
                ),
            )

        output = io.StringIO()
        with patch.object(application_session, "mark_applied", side_effect=apply):
            application_session.run_session(
                self.database, self.config, input_fn=lambda prompt: next(actions),
                output=output, opener=lambda target: True,
            )
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(selected).state, "applied")
            self.assertEqual(store.get(following).state, "needs_review")
        self.assertIn(f"#%d" % following, output.getvalue())

    def test_ready_session_can_be_explicitly_declined(self) -> None:
        job_id = self.add_job()
        cv = self.make_cv()
        self.evaluate(job_id, "good_candidate", cv=cv)
        synchronize_evaluation(
            self.database, self.config, job_id, revalidator=Revalidator()
        )
        application_session.run_session(
            self.database, self.config, input_fn=lambda prompt: "s",
            output=io.StringIO(), opener=lambda target: True,
        )
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
            event = store.connection.execute(
                "SELECT from_state, to_state FROM application_events "
                "WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,),
            ).fetchone()
        self.assertEqual((item.state, item.decision), ("declined", "skip"))
        self.assertEqual(event, ("ready_to_apply", "declined"))

    def test_open_cv_invokes_file_opener_only(self) -> None:
        job_id = self.add_job()
        cv_value = self.make_cv()
        self.evaluate(job_id, "good_candidate", cv=cv_value)
        synchronize_evaluation(
            self.database, self.config, job_id, revalidator=Revalidator()
        )
        opener = Mock(return_value=True)
        copier = Mock(return_value=True)
        path_converter = Mock(return_value=r"\\wsl.localhost\Ubuntu\cv.pdf")
        actions = iter(("c", "q"))

        application_session.run_session(
            self.database, self.config, input_fn=lambda prompt: next(actions),
            output=io.StringIO(), opener=opener, copier=copier,
            path_converter=path_converter,
        )

        opener.assert_called_once_with(self.repository / cv_value)
        path_converter.assert_not_called()
        copier.assert_not_called()

    def test_copy_windows_cv_path_uses_converter_and_clipboard_only(self) -> None:
        job_id = self.add_job()
        cv_value = self.make_cv()
        self.evaluate(job_id, "good_candidate", cv=cv_value)
        synchronize_evaluation(
            self.database, self.config, job_id, revalidator=Revalidator()
        )
        opener = Mock(return_value=True)
        copier = Mock(return_value=True)
        converted = r"\\wsl.localhost\Ubuntu\cv.pdf"
        path_converter = Mock(return_value=converted)
        actions = iter(("p", "q"))
        output = io.StringIO()
        with ApplicationStore(self.database) as store:
            events_before = store.connection.execute(
                "SELECT COUNT(*) FROM application_events WHERE job_id=?", (job_id,)
            ).fetchone()[0]
        application_session.run_session(
            self.database, self.config, input_fn=lambda prompt: next(actions),
            output=output, opener=opener, copier=copier,
            path_converter=path_converter,
        )
        with ApplicationStore(self.database) as store:
            item = store.get(job_id)
            events_after = store.connection.execute(
                "SELECT COUNT(*) FROM application_events WHERE job_id=?", (job_id,)
            ).fetchone()[0]
        self.assertEqual(item.state, "ready_to_apply")
        self.assertIsNone(item.applied_at)
        self.assertEqual(events_after, events_before)
        path_converter.assert_called_once_with(self.repository / cv_value)
        copier.assert_called_once_with(converted)
        opener.assert_not_called()
        self.assertIn(
            "CV Windows path copied to clipboard.", output.getvalue()
        )

    def test_copy_windows_cv_path_failures_are_safe_and_non_mutating(self) -> None:
        for converted, copied in ((None, True), (r"C:\\cv.pdf", False)):
            with self.subTest(converted=converted, copied=copied):
                job_id = self.add_job(str(converted))
                cv_value = self.make_cv(f"output/{job_id}.pdf")
                self.evaluate(job_id, "good_candidate", cv=cv_value)
                synchronize_evaluation(
                    self.database, self.config, job_id, revalidator=Revalidator()
                )
                opener = Mock(return_value=True)
                copier = Mock(return_value=copied)
                path_converter = Mock(return_value=converted)
                actions = iter(("p", "q"))
                with ApplicationStore(self.database) as store:
                    events_before = store.connection.execute(
                        "SELECT COUNT(*) FROM application_events WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0]

                output = io.StringIO()
                application_session.run_session(
                    self.database, self.config,
                    input_fn=lambda prompt: next(actions), output=output,
                    opener=opener, copier=copier,
                    path_converter=path_converter,
                )

                with ApplicationStore(self.database) as store:
                    item = store.get(job_id)
                    events_after = store.connection.execute(
                        "SELECT COUNT(*) FROM application_events WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0]
                self.assertEqual(item.state, "ready_to_apply")
                self.assertIsNone(item.applied_at)
                self.assertEqual(events_after, events_before)
                opener.assert_not_called()
                if converted is None:
                    copier.assert_not_called()
                else:
                    copier.assert_called_once_with(converted)
                self.assertIn("Could not copy CV Windows path.", output.getvalue())

    def test_missing_cv_copy_fails_safely(self) -> None:
        job_id = self.add_job()
        self.evaluate(job_id, "review")
        synchronize_evaluation(self.database, self.config, job_id)
        actions = iter(("c", "q"))
        copied: list[str] = []
        output = io.StringIO()
        application_session.run_session(
            self.database, self.config, input_fn=lambda prompt: next(actions),
            output=output, opener=lambda target: True,
            copier=lambda value: copied.append(value) or True,
        )
        with ApplicationStore(self.database) as store:
            self.assertEqual(store.get(job_id).state, "needs_review")
        self.assertEqual(copied, [])
        self.assertIn("CV not ready.", output.getvalue())

    def test_windows_path_uses_wslpath_without_shell(self) -> None:
        cv = self.repository / "output/cv.pdf"
        cv.parent.mkdir(parents=True, exist_ok=True)
        cv.write_bytes(b"%PDF")
        process = subprocess.CompletedProcess(
            ["wslpath"], 0, "\\\\wsl.localhost\\Ubuntu\\home\\cv.pdf\n", ""
        )
        with patch.object(application_session.shutil, "which", return_value="/usr/bin/wslpath"), \
             patch.object(application_session.subprocess, "run", return_value=process) as run:
            converted = application_session.windows_path(cv)
        self.assertEqual(converted, r"\\wsl.localhost\Ubuntu\home\cv.pdf")
        run.assert_called_once_with(
            ["/usr/bin/wslpath", "-w", str(cv.resolve())],
            capture_output=True, text=True, check=False,
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_windows_clipboard_copies_exact_value_without_shell(self) -> None:
        value = r"\\wsl.localhost\Ubuntu\home\cv.pdf"
        process = subprocess.CompletedProcess(["clip.exe"], 0, "", "")
        with patch.object(
            application_session.shutil, "which", return_value="/mnt/c/clip.exe"
        ), patch.object(
            application_session.subprocess, "run", return_value=process
        ) as run:
            copied = application_session.copy_windows_clipboard(value)
        self.assertTrue(copied)
        run.assert_called_once_with(
            ["/mnt/c/clip.exe"], input=value, capture_output=True, text=True,
            check=False,
        )
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
