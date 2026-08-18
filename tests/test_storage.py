from datetime import datetime, timezone
from pathlib import Path
import sqlite3
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
            self.assertEqual(store.upsert([make_job()]).new, 1)
            result = store.upsert(
            [
                make_job(
                    title="Senior Engineer",
                    collected_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                )
            ]
            )
            self.assertEqual((result.new, result.updated), (0, 1))
            row = store.connection.execute(
                "SELECT title, collected_at, state FROM jobs WHERE external_id = '123'"
            ).fetchone()
        self.assertEqual(
            row, ("Senior Engineer", "2026-01-01T00:00:00+00:00", "updated")
        )

    def test_identical_source_data_is_seen_and_meaningful_change_is_updated(self) -> None:
        first_discovery = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later_collection = datetime(2026, 2, 1, tzinfo=timezone.utc)
        with JobStore(self.database) as store:
            store.upsert([make_job(collected_at=first_discovery)])

            identical = store.upsert([make_job(collected_at=later_collection)])
            identical_record = store.list_jobs()[0]
            self.assertEqual((identical.new, identical.updated), (0, 0))
            self.assertEqual(identical_record.state, "seen")
            self.assertEqual(identical_record.job.collected_at, first_discovery)

            changed = store.upsert([
                make_job(location="Lyon", collected_at=later_collection)
            ])
            changed_record = store.list_jobs()[0]
            self.assertEqual((changed.new, changed.updated), (0, 1))
            self.assertEqual(changed_record.state, "updated")
            self.assertEqual(changed_record.job.collected_at, first_discovery)

    def test_identity_includes_source(self) -> None:
        with JobStore(self.database) as store:
            self.assertEqual(
                store.upsert([make_job(), make_job(source="another")]).new, 2
            )

    def test_greenhouse_and_lever_ids_coexist(self) -> None:
        with JobStore(self.database) as store:
            result = store.upsert([make_job(), make_job(source="lever")])
            sources = {record.job.source for record in store.list_jobs()}
        self.assertEqual(result.new, 2)
        self.assertEqual(sources, {"greenhouse", "lever"})

    def test_all_three_sources_with_the_same_id_coexist(self) -> None:
        with JobStore(self.database) as store:
            result = store.upsert([
                make_job(),
                make_job(source="lever"),
                make_job(source="france_travail"),
            ])
            sources = {record.job.source for record in store.list_jobs()}
        self.assertEqual(result.new, 3)
        self.assertEqual(sources, {"greenhouse", "lever", "france_travail"})

    def test_rows_missing_from_later_collection_are_retained(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([make_job(), make_job(external_id="456")])
            store.upsert([make_job()])
            count = store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, 2)

    def test_new_seen_and_updated_describe_latest_collection(self) -> None:
        with JobStore(self.database) as store:
            first = store.upsert([make_job(), make_job(external_id="456")])
            self.assertEqual((first.new, first.updated), (2, 0))
            self.assertEqual([row.state for row in store.list_jobs(new_only=True)], ["new", "new"])

            second = store.upsert([
                make_job(),
                make_job(external_id="456", location="Lyon"),
                make_job(external_id="789"),
            ])
            states = {
                row.job.external_id: row.state for row in store.list_jobs()
            }
            self.assertEqual((second.new, second.updated), (1, 1))
            self.assertEqual(states, {"123": "seen", "456": "updated", "789": "new"})
            self.assertEqual(
                [row.job.external_id for row in store.list_jobs(new_only=True)], ["789"]
            )

    def test_migrates_v1_rows_to_seen_without_changing_collected_at(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                company TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL,
                remote_status TEXT, published_at TEXT, url TEXT NOT NULL,
                collected_at TEXT NOT NULL, UNIQUE (source, external_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs
            (source, external_id, company, title, location, url, collected_at)
            VALUES ('greenhouse', 'old', 'Acme', 'Cloud Engineer', 'Paris',
                    'https://job/old', '2026-01-01T00:00:00+00:00')
            """
        )
        connection.commit()
        connection.close()

        with JobStore(self.database) as store:
            record = store.list_jobs()[0]
        self.assertEqual(record.state, "seen")
        self.assertEqual(
            record.job.collected_at, datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(record.job.work_mode, "unknown")
        self.assertEqual(record.job.remote_scope, "unknown")

    def test_work_mode_and_scope_changes_are_updates(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([make_job()])
            result = store.upsert([
                make_job(work_mode="remote", remote_scope="france")
            ])
            record = store.list_jobs()[0]
        self.assertEqual(result.updated, 1)
        self.assertEqual((record.job.work_mode, record.job.remote_scope), ("remote", "france"))
