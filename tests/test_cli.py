from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_harvester import cli
from job_harvester.collectors.greenhouse import CollectionError
from job_harvester.models import Job
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
            "Found 1 jobs; 1 new; 0 updated.\nFound 1 jobs; 0 new; 0 updated.\n",
        )

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
