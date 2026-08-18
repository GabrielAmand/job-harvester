from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from job_harvester.registry import BoardRegistry, RegistryError
from job_harvester.storage import JobStore


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jobs.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persists_manual_board_and_prevents_duplicates(self) -> None:
        with BoardRegistry(self.database) as registry:
            self.assertTrue(
                registry.add(
                    "Greenhouse", "Acme", company="Acme Inc.", provenance="manual"
                )
            )
            self.assertFalse(registry.add("greenhouse", "acme"))
        with BoardRegistry(self.database) as registry:
            boards = registry.list()
        self.assertEqual(len(boards), 1)
        board = boards[0]
        self.assertEqual((board.provider, board.slug), ("greenhouse", "acme"))
        self.assertEqual(board.company, "Acme Inc.")
        self.assertTrue(board.enabled)
        self.assertEqual(board.validation_status, "unknown")
        self.assertIsInstance(board.discovered_at, datetime)

    def test_enable_disable_and_temporary_validation_preserves_status(self) -> None:
        with BoardRegistry(self.database) as registry:
            registry.add("lever", "acme")
            registry.set_enabled("lever", "acme", False)
            registry.record_validation("lever", "acme", "valid")
            board = registry.list()[0]
            self.assertFalse(board.enabled)
            self.assertEqual(board.validation_status, "valid")
            self.assertIsNotNone(board.last_checked_at)
            registry.record_validation("lever", "acme", None)
            self.assertEqual(registry.list()[0].validation_status, "valid")
            registry.set_enabled("lever", "acme", True)
            self.assertTrue(registry.list(enabled_only=True, valid_only=True)[0].enabled)

    def test_missing_board_and_invalid_provider_are_rejected(self) -> None:
        with BoardRegistry(self.database) as registry:
            with self.assertRaisesRegex(RegistryError, "unsupported"):
                registry.add("other", "acme")
            with self.assertRaisesRegex(RegistryError, "not found"):
                registry.set_enabled("lever", "missing", False)

    def test_registry_table_is_additive_to_existing_job_database(self) -> None:
        with JobStore(self.database) as store:
            self.assertEqual(store.list_jobs(), [])
        with BoardRegistry(self.database) as registry:
            registry.add("lever", "acme")
        with JobStore(self.database) as store:
            self.assertEqual(store.list_jobs(), [])
        with BoardRegistry(self.database) as registry:
            self.assertEqual(registry.list()[0].slug, "acme")
