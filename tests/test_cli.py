from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_harvester import cli
from job_harvester.models import Job


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = self.directory / "config.toml"
        self.database = self.directory / "jobs.sqlite3"
        self.config.write_text(
            '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "acme"\n'
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
        self.assertEqual(output.getvalue(), "Found 1 jobs; 1 new.\nFound 1 jobs; 0 new.\n")

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
