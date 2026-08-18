from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_harvester import cli
from job_harvester.board_validation import ValidationResult
from job_harvester.collectors.greenhouse import CollectionError
from job_harvester.models import Job
from job_harvester.registry import BoardRegistry
from job_harvester.storage import JobStore


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = self.directory / "config.toml"
        self.database = self.directory / "jobs.sqlite3"
        self.config.write_text(
            '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "acme"\n'
            '[filters]\npositive_title_keywords = ["platform"]\n'
            'negative_title_keywords = ["director"]\n'
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_collect_reports_found_and_new_counts(self) -> None:
        jobs = [Job("greenhouse", "1", "Acme", "Engineer", "Paris", "https://job/1")]
        output = io.StringIO()
        arguments = ["collect", "--config", str(self.config), "--database", str(self.database)]
        with patch.object(cli.GreenhouseCollector, "collect", return_value=jobs):
            with redirect_stdout(output):
                self.assertEqual(cli.main(arguments), 0)
                self.assertEqual(cli.main(arguments), 0)
        self.assertEqual(
            output.getvalue(),
            "Greenhouse: 1 boards; Found 1; 1 new; 0 updated.\n"
            "Total: Found 1; 1 new; 0 updated.\n"
            "Greenhouse: 1 boards; Found 1; 0 new; 0 updated.\n"
            "Total: Found 1; 0 new; 0 updated.\n",
        )

    def test_persistent_batch_cli_workflow(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([
                Job(
                    "greenhouse", "batch-1", "Acme", "Platform Engineer",
                    "Paris", "https://job/1", source_key="acme",
                )
            ])
        output = io.StringIO()
        prefix = ["batch", "--database", str(self.database)]
        with patch("job_harvester.batches.JobRevalidator.is_active", return_value=True), \
             redirect_stdout(output):
            self.assertEqual(cli.main(prefix + [
                "start", "--config", str(self.config), "--limit", "1"
            ]), 0)
            self.assertEqual(cli.main(prefix + ["current"]), 0)
            self.assertEqual(cli.main(prefix + [
                "review", "greenhouse", "batch-1", "--decision", "skip"
            ]), 0)
            self.assertEqual(cli.main(prefix + ["complete"]), 0)
        self.assertIn("batch size: 1", output.getvalue())
        self.assertIn("Completed batch 1", output.getvalue())

    def test_batch_evaluate_cli_arguments(self) -> None:
        args = cli.build_parser().parse_args([
            "batch", "--database", str(self.database), "evaluate",
            "--config", str(self.config), "--limit", "5", "--force",
        ])
        self.assertEqual(args.batch_command, "evaluate")
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.force)

    def test_mail_cli_has_no_send_command(self) -> None:
        args = cli.build_parser().parse_args([
            "mail", "--database", str(self.database), "--config", str(self.config),
            "list", "--attention",
        ])
        self.assertEqual(args.mail_command, "list")
        self.assertTrue(args.attention)
        mail_parser = next(
            action for action in cli.build_parser()._actions
            if getattr(action, "dest", None) == "command"
        ).choices["mail"]
        command_action = next(a for a in mail_parser._actions if a.dest == "mail_command")
        self.assertNotIn("send", command_action.choices)

    def test_collects_greenhouse_and_lever_in_one_atomic_run(self) -> None:
        self.config.write_text(
            self.config.read_text()
            + '[[sources]]\ntype = "lever"\ncompany = "Other"\ncompany_slug = "other"\n'
        )
        greenhouse = [Job("greenhouse", "same", "Acme", "Engineer", "Paris", "https://gh")]
        lever = [Job("lever", "same", "Other", "Engineer", "Remote", "https://lever")]
        output = io.StringIO()
        arguments = ["collect", "--config", str(self.config), "--database", str(self.database)]
        with patch.object(cli.GreenhouseCollector, "collect", return_value=greenhouse), \
             patch.object(cli.LeverCollector, "collect", return_value=lever), \
             redirect_stdout(output):
            self.assertEqual(cli.main(arguments), 0)
        with JobStore(self.database) as store:
            self.assertEqual(len(store.list_jobs()), 2)
        self.assertIn("Greenhouse: 1 boards; Found 1; 1 new; 0 updated.", output.getvalue())
        self.assertIn("Lever: 1 boards; Found 1; 1 new; 0 updated.", output.getvalue())
        self.assertIn("Total: Found 2; 2 new; 0 updated.", output.getvalue())

    def test_collects_all_three_sources_and_reports_france_travail(self) -> None:
        self.config.write_text(
            self.config.read_text()
            + '[[sources]]\ntype = "lever"\ncompany = "Other"\ncompany_slug = "other"\n'
            + '[[sources]]\ntype = "france_travail"\nsearch_terms = ["DevOps"]\n'
        )
        jobs = {
            "greenhouse": [Job("greenhouse", "same", "Acme", "Engineer", "Paris", "https://gh")],
            "lever": [Job("lever", "same", "Other", "Engineer", "Remote", "https://lever")],
            "france_travail": [Job("france_travail", "same", "FT", "DevOps", "Paris", "https://ft")],
        }
        output = io.StringIO()
        arguments = ["collect", "--config", str(self.config), "--database", str(self.database)]
        with patch.object(cli.GreenhouseCollector, "collect", return_value=jobs["greenhouse"]), \
             patch.object(cli.LeverCollector, "collect", return_value=jobs["lever"]), \
             patch.object(cli.FranceTravailCollector, "collect", return_value=jobs["france_travail"]), \
             redirect_stdout(output):
            self.assertEqual(cli.main(arguments), 0)
        with JobStore(self.database) as store:
            self.assertEqual(len(store.list_jobs()), 3)
        self.assertIn("France Travail: Found 1; 1 new; 0 updated.", output.getvalue())
        self.assertIn("Total: Found 3; 3 new; 0 updated.", output.getvalue())

    def test_collect_failure_is_nonzero_and_does_not_create_database(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = cli.main([
                "collect", "--config", str(self.directory / "missing.toml"),
                "--database", str(self.database),
            ])
        self.assertEqual(result, 1)
        self.assertIn("error:", errors.getvalue())
        self.assertFalse(self.database.exists())

    def test_collection_failure_preserves_previous_new_state(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([
                Job("greenhouse", "1", "Acme", "Platform Engineer", "Paris", "https://job/1")
            ])
        errors = io.StringIO()
        with patch.object(
            cli.GreenhouseCollector,
            "collect",
            side_effect=CollectionError("temporary failure"),
        ), redirect_stderr(errors):
            result = cli.main([
                "collect", "--config", str(self.config), "--database", str(self.database)
            ])
        with JobStore(self.database) as store:
            state = store.list_jobs()[0].state
        self.assertEqual(result, 1)
        self.assertEqual(state, "new")

    def test_one_source_failure_prevents_all_source_writes(self) -> None:
        self.config.write_text(
            self.config.read_text()
            + '[[sources]]\ntype = "lever"\ncompany = "Other"\ncompany_slug = "other"\n'
        )
        greenhouse = [Job("greenhouse", "1", "Acme", "Engineer", "Paris", "https://gh")]
        with patch.object(cli.GreenhouseCollector, "collect", return_value=greenhouse), \
             patch.object(cli.LeverCollector, "collect", side_effect=CollectionError("failed")):
            result = cli.main([
                "collect", "--config", str(self.config), "--database", str(self.database)
            ])
        self.assertEqual(result, 1)
        self.assertFalse(self.database.exists())

    def test_france_travail_failure_prevents_other_source_writes(self) -> None:
        self.config.write_text(
            self.config.read_text()
            + '[[sources]]\ntype = "france_travail"\nsearch_terms = ["DevOps"]\n'
        )
        greenhouse = [Job("greenhouse", "1", "Acme", "Engineer", "Paris", "https://gh")]
        with patch.object(cli.GreenhouseCollector, "collect", return_value=greenhouse), \
             patch.object(
                 cli.FranceTravailCollector,
                 "collect",
                 side_effect=CollectionError("authentication failed"),
             ):
            result = cli.main([
                "collect", "--config", str(self.config), "--database", str(self.database)
            ])
        self.assertEqual(result, 1)
        self.assertFalse(self.database.exists())

    def test_list_and_export_only_relevant_new_jobs(self) -> None:
        jobs = [
            Job("greenhouse", "1", "Acme", "Platform Engineer", "Paris", "https://job/1"),
            Job("greenhouse", "2", "Acme", "Director of Platform", "Paris", "https://job/2"),
            Job("greenhouse", "3", "Acme", "Designer", "Paris", "https://job/3"),
        ]
        with JobStore(self.database) as store:
            store.upsert(jobs)

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main([
                "list", "--config", str(self.config), "--database", str(self.database),
                "--new-only",
            ])
        self.assertEqual(result, 0)
        self.assertIn("[new] Acme — Platform Engineer", output.getvalue())
        self.assertNotIn("Director", output.getvalue())
        self.assertIn("1 relevant job(s).", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main([
                "export", "--config", str(self.config), "--database", str(self.database),
                "--new-only",
            ])
        document = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(document), 1)
        self.assertEqual(document[0]["external_id"], "1")
        self.assertEqual(document[0]["state"], "new")
        self.assertIn("collected_at", document[0])
        self.assertEqual(document[0]["work_mode"], "unknown")
        self.assertEqual(document[0]["remote_scope"], "unknown")

    def test_export_can_write_to_file(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([
                Job("greenhouse", "1", "Acme", "Platform Engineer", "Paris", "https://job/1")
            ])
        destination = self.directory / "jobs.json"
        result = cli.main([
            "export", "--config", str(self.config), "--database", str(self.database),
            "--output", str(destination),
        ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(destination.read_text())[0]["title"], "Platform Engineer")

    def test_prefer_policy_orders_work_modes_without_excluding_unknown(self) -> None:
        self.config.write_text(self.config.read_text() + 'remote_policy = "prefer"\n')
        modes = ("onsite", "unknown", "hybrid", "remote")
        with JobStore(self.database) as store:
            store.upsert([
                Job(
                    "greenhouse", str(index), "Acme", f"Platform {mode}", "Paris",
                    f"https://job/{index}", work_mode=mode,
                )
                for index, mode in enumerate(modes)
            ])
        records = cli.relevant_jobs(self.config, self.database, new_only=False)
        self.assertEqual([record.job.work_mode for record in records], [
            "remote", "hybrid", "unknown", "onsite"
        ])

    def test_board_commands_add_list_toggle_discover_and_validate(self) -> None:
        output = io.StringIO()
        base = ["boards", "--database", str(self.database)]
        with redirect_stdout(output):
            self.assertEqual(cli.main(base + [
                "add", "greenhouse", "Acme", "--company", "Acme Inc."
            ]), 0)
            self.assertEqual(cli.main(base + ["disable", "greenhouse", "acme"]), 0)
            self.assertEqual(cli.main(base + ["enable", "greenhouse", "acme"]), 0)
            self.assertEqual(cli.main(base + ["list"]), 0)
        self.assertIn("greenhouse | acme | Acme Inc. | enabled | unknown", output.getvalue())

        candidates = self.directory / "candidates.txt"
        candidates.write_text("https://jobs.lever.co/other/jobs/123\n")
        with redirect_stdout(output):
            self.assertEqual(cli.main(base + ["discover", "--input", str(candidates)]), 0)
        with patch.object(
            cli.BoardValidator,
            "validate",
            return_value=ValidationResult("valid", job_count=0),
        ), redirect_stdout(output):
            self.assertEqual(cli.main(base + ["validate"]), 0)
        with BoardRegistry(self.database) as registry:
            statuses = {
                (board.provider, board.slug): board.validation_status
                for board in registry.list()
            }
        self.assertEqual(statuses, {
            ("greenhouse", "acme"): "valid",
            ("lever", "other"): "valid",
        })

    def test_toml_board_wins_over_duplicate_registry_board(self) -> None:
        with BoardRegistry(self.database) as registry:
            registry.add("greenhouse", "ACME", company="Registry Company")
            registry.record_validation("greenhouse", "acme", "valid")
        job = Job("greenhouse", "1", "Acme", "Engineer", "Paris", "https://job")
        arguments = ["collect", "--config", str(self.config), "--database", str(self.database)]
        with patch.object(cli, "GreenhouseCollector") as collector:
            collector.return_value.collect.return_value = [job]
            self.assertEqual(cli.main(arguments), 0)
        collector.assert_called_once_with("Acme", "acme")
        collector.return_value.collect.assert_called_once_with()

    def test_registry_only_valid_boards_are_collected(self) -> None:
        self.config.write_text('[filters]\npositive_title_keywords = ["platform"]\n')
        with BoardRegistry(self.database) as registry:
            registry.add("lever", "valid", company="Valid Co")
            registry.record_validation("lever", "valid", "valid")
            registry.add("lever", "unknown", company="Unknown Co")
        job = Job("lever", "1", "Valid Co", "Platform", "Remote", "https://job")
        arguments = ["collect", "--config", str(self.config), "--database", str(self.database)]
        with patch.object(cli.LeverCollector, "collect", return_value=[job]) as collect:
            self.assertEqual(cli.main(arguments), 0)
        self.assertEqual(collect.call_count, 1)
