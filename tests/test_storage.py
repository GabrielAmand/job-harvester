from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from job_harvester.models import Job
from job_harvester.storage import JobStore


def make_job(**changes: object) -> Job:
    values = {
        "source": "greenhouse",
        "external_id": "123",
        "company": "Acme",
        "title": "Engineer",
        "location": "Paris",
        "url": "https://example.test/jobs/123",
        "collected_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(changes)
    return Job(**values)  # type: ignore[arg-type]


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jobs.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upsert_is_idempotent_and_refreshes_mutable_fields(self) -> None:
        with JobStore(self.database) as store:
            self.assertEqual(store.upsert([make_job()]), 1)
            self.assertEqual(store.upsert(
            [
                make_job(
                    title="Senior Engineer",
                    collected_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                )
            ]
            ), 0)
            row = store.connection.execute(
                "SELECT title, collected_at FROM jobs WHERE external_id = '123'"
            ).fetchone()
        self.assertEqual(row, ("Senior Engineer", "2026-01-01T00:00:00+00:00"))

    def test_identity_includes_source(self) -> None:
        with JobStore(self.database) as store:
            self.assertEqual(store.upsert([make_job(), make_job(source="another")]), 2)

    def test_rows_missing_from_later_collection_are_retained(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([make_job(), make_job(external_id="456")])
            store.upsert([make_job()])
            count = store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, 2)
