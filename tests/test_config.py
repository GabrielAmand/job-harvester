from pathlib import Path
import tempfile
import unittest

from job_harvester.config import ConfigError, FranceTravailSource, LeverSource, load_config


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
        self.assertEqual(config.filters.positive_title_keywords, ())

    def test_loads_filters(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "acme"\n'
            '[filters]\npositive_title_keywords = ["cloud", "SRE"]\n'
            'negative_title_keywords = ["director"]\nlocation_keywords = ["Paris"]\n'
            'remote_policy = "prefer"\nallow_hybrid = true\nallow_onsite = false\n'
            'exclude_incompatible_remote = false\n'
            'allow_strong_seniority = true\n'
            'excluded_title_phrases = ["custom manager"]\n'
        )
        filters = load_config(path).filters
        self.assertEqual(filters.positive_title_keywords, ("cloud", "SRE"))
        self.assertEqual(filters.negative_title_keywords, ("director",))
        self.assertEqual(filters.location_keywords, ("Paris",))
        self.assertEqual(filters.remote_policy, "prefer")
        self.assertTrue(filters.allow_hybrid)
        self.assertFalse(filters.allow_onsite)
        self.assertFalse(filters.exclude_incompatible_remote)
        self.assertTrue(filters.allow_strong_seniority)
        self.assertEqual(filters.excluded_title_phrases, ("custom manager",))

    def test_loads_mixed_greenhouse_and_lever_sources(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "acme"\n'
            '[[sources]]\ntype = "lever"\ncompany = "Other"\ncompany_slug = "other"\n'
        )
        config = load_config(path)
        self.assertEqual(len(config.sources), 2)
        self.assertIsInstance(config.sources[1], LeverSource)
        self.assertEqual(config.sources[1].company_slug, "other")  # type: ignore[union-attr]

    def test_loads_career_ops_configuration(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[career_ops]\nenabled = true\nrepository_path = "/tmp/career-ops"\n'
            'node_command = "node22"\nbatch_size = 5\n'
        )
        career_ops = load_config(path).career_ops
        self.assertTrue(career_ops.enabled)
        self.assertEqual(career_ops.repository_path, Path("/tmp/career-ops"))
        self.assertEqual(career_ops.node_command, "node22")
        self.assertEqual(career_ops.batch_size, 5)

    def test_loads_email_configuration(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[email]\nprovider = "gmail"\naddress = "jobs@example.com"\n'
            'client_secret_path = "~/gmail-client.json"\n'
            'token_path = "~/gmail-token.json"\ninitial_lookback_days = 30\n'
        )
        email = load_config(path).email
        self.assertEqual(email.address, "jobs@example.com")
        self.assertEqual(email.client_secret_path, Path("~/gmail-client.json").expanduser())
        self.assertEqual(email.initial_lookback_days, 30)

    def test_loads_france_travail_search_terms(self) -> None:
        path = self.directory / "config.toml"
        path.write_text(
            '[[sources]]\ntype = "france_travail"\n'
            'search_terms = ["DevOps", "Ingénieur systèmes"]\n'
        )
        source = load_config(path).sources[0]
        self.assertIsInstance(source, FranceTravailSource)
        self.assertEqual(source.search_terms, ("DevOps", "Ingénieur systèmes"))  # type: ignore[union-attr]

    def test_rejects_invalid_configuration(self) -> None:
        cases = [
            ('[[sources]]\ntype = "lever"\ncompany = "Acme"\n', "company_slug"),
            ('[[sources]]\ntype = "france_travail"\n', "search_terms"),
            ('[[sources]]\ntype = "other"\ncompany = "Acme"\n', "unsupported"),
            ('[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\n', "board_token"),
            (
                '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "a"\n'
                '[filters]\npositive_title_keywords = "cloud"\n',
                "array of strings",
            ),
            (
                '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "a"\n'
                '[filters]\nremote_policy = "mostly"\n',
                "remote_policy",
            ),
            (
                '[[sources]]\ntype = "greenhouse"\ncompany = "Acme"\nboard_token = "a"\n'
                '[filters]\nallow_hybrid = "yes"\n',
                "boolean",
            ),
            (
                '[career_ops]\nenabled = true\n',
                "repository_path",
            ),
            (
                '[career_ops]\nbatch_size = 0\n',
                "positive integer",
            ),
            ('[email]\nprovider = "imap"\n', "provider"),
            ('[email]\naddress = "not-an-address"\n', "valid email"),
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

    def test_allows_registry_only_configuration(self) -> None:
        path = self.directory / "config.toml"
        path.write_text('[filters]\npositive_title_keywords = ["platform"]\n')
        config = load_config(path)
        self.assertEqual(config.sources, ())
