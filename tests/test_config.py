from pathlib import Path
import tempfile
import unittest

from job_harvester.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_loads_greenhouse_sources(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "acme"\n'
        )
        config = load_config(path)
        self.assertEqual(len(config.sources), 1)
        self.assertEqual(config.sources[0].company, "Acme")
        self.assertEqual(config.sources[0].board_token, "acme")

    def test_rejects_invalid_configuration(self) -> None:
        cases = [
            ("", "at least one"),
            ('[[sources]]\ntype = "lever"\ncompany = "Acme"\n', "unsupported"),
            ('[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\n', "board_token"),
        ]
        for content, message in cases:
            with self.subTest(content=content):
                path = self.directory / "config.toml"
                path.write_text(content)
                with self.assertRaisesRegex(ConfigError, message):
                    load_config(path)

    def test_reports_missing_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "not found"):
            load_config(self.directory / "missing.toml")
