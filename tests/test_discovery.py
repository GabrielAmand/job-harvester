from pathlib import Path
import tempfile
import unittest

from job_harvester.discovery import board_from_url, discover_boards


class DiscoveryTests(unittest.TestCase):
    def test_extracts_supported_provider_and_slug_patterns(self) -> None:
        cases = {
            "https://boards.greenhouse.io/Acme/jobs/123": ("greenhouse", "acme"),
            "https://job-boards.greenhouse.io/Other": ("greenhouse", "other"),
            "jobs.lever.co/Example/uuid": ("lever", "example"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(board_from_url(url), expected)
        self.assertIsNone(board_from_url("https://careers.example.com/jobs"))

    def test_imports_text_and_json_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text_path = Path(directory) / "urls.txt"
            text_path.write_text(
                "https://boards.greenhouse.io/acme/jobs/1\n"
                "Search result: jobs.lever.co/other/abc.\n"
                "https://boards.greenhouse.io/ACME\n"
            )
            self.assertEqual(
                discover_boards(text_path),
                [("greenhouse", "acme"), ("lever", "other")],
            )
            json_path = Path(directory) / "urls.json"
            json_path.write_text(
                '{"results":[{"url":"https://job-boards.greenhouse.io/jsonco"}]}'
            )
            self.assertEqual(discover_boards(json_path), [("greenhouse", "jsonco")])
