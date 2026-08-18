from email.message import Message
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from job_harvester.batches import start_batch
from job_harvester.config import Filters
from job_harvester.job_import import JobImporter, JobImportError, canonicalize_url
from job_harvester.models import Job
from job_harvester.revalidation import JobRevalidator, RevalidationError
from job_harvester.storage import JobStore


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, content_type: str = "text/html; charset=utf-8") -> None:
        super().__init__(body)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class JobImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jobs.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def importer(self, html: str, *, final: str = "https://example.com/job/123") -> JobImporter:
        return JobImporter(opener=lambda *a, **k: Response(html.encode(), final))

    def test_json_ld_job_posting_and_remote_normalization(self) -> None:
        posting = {
            "@context": "https://schema.org", "@type": "JobPosting",
            "title": "Platform Engineer", "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"address": {"addressLocality": "Paris", "addressCountry": "FR"}},
            "jobLocationType": "TELECOMMUTE", "datePosted": "2026-08-01",
            "description": "A fully remote role, available throughout Europe.",
        }
        html = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        result = self.importer(html).import_url("https://EXAMPLE.com/job/123#apply", self.database)
        self.assertFalse(result.existed)
        self.assertEqual((result.job.title, result.job.company), ("Platform Engineer", "Acme"))
        self.assertEqual(result.job.work_mode, "remote")
        self.assertIsNotNone(result.job.description)
        with sqlite3.connect(self.database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            self.assertNotIn("raw_html", columns)
            self.assertNotIn("<script", connection.execute("SELECT description FROM jobs").fetchone()[0])

    def test_metadata_fallback_and_missing_optional_fields(self) -> None:
        html = '<meta property="og:title" content="DevOps Engineer"><meta property="og:site_name" content="Small Co">'
        job = self.importer(html).import_url("https://example.com/job/123", self.database).job
        self.assertEqual((job.title, job.company, job.location), ("DevOps Engineer", "Small Co", ""))
        self.assertIsNone(job.published_at)

    def test_minimal_interactive_fallback_and_noninteractive_failure(self) -> None:
        prompts = iter(("Engineer", "Acme"))
        result = self.importer("<html></html>").import_url(
            "https://example.com/job/123", self.database, prompt=lambda _: next(prompts)
        )
        self.assertEqual((result.job.title, result.job.company), ("Engineer", "Acme"))
        other = Path(self.temp.name) / "other.sqlite3"
        with self.assertRaisesRegex(JobImportError, "rerun interactively"):
            self.importer("<html></html>").import_url("https://example.com/job/456", other)

    def test_supported_greenhouse_and_lever_use_api_normalizers(self) -> None:
        greenhouse = {"id": 123, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/123", "location": {"name": "Paris"}}
        lever = {"id": "abcd1234-abcd-1234-abcd-123456789abc", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/acme/abcd1234-abcd-1234-abcd-123456789abc", "categories": {"location": "Remote"}}
        payloads = iter((greenhouse, lever))
        seen = []
        def opener(request, **kwargs):
            seen.append(request.full_url)
            return Response(json.dumps(next(payloads)).encode(), request.full_url, "application/json")
        importer = JobImporter(opener=opener)
        gh = importer.import_url("https://boards.greenhouse.io/acme/jobs/123", self.database)
        lv = importer.import_url("https://jobs.lever.co/acme/abcd1234-abcd-1234-abcd-123456789abc", self.database)
        self.assertEqual((gh.job.source, lv.job.source), ("greenhouse", "lever"))
        self.assertIn("boards-api.greenhouse.io", seen[0])
        self.assertIn("api.lever.co", seen[1])

    def test_france_travail_url_uses_supported_detail_api(self) -> None:
        payload = {
            "id": "123ABCD", "intitule": "Ingénieur DevOps",
            "entreprise": {"nom": "Acme France"},
            "lieuTravail": {"libelle": "Paris"},
            "description": "Automatisation des plateformes.",
        }
        seen = []
        def opener(request, **kwargs):
            seen.append((request.full_url, request.headers.get("Authorization")))
            return Response(json.dumps(payload).encode(), request.full_url, "application/json")
        with patch(
            "job_harvester.job_import.JobRevalidator._authenticate_france_travail",
            return_value="token",
        ):
            result = JobImporter(opener=opener).import_url(
                "https://candidat.francetravail.fr/offres/recherche/detail/123ABCD",
                self.database,
            )
        self.assertEqual(result.job.source, "france_travail")
        self.assertIn("/offres/123ABCD", seen[0][0])
        self.assertEqual(seen[0][1], "Bearer token")

    def test_canonical_url_and_existing_collected_job_deduplicate(self) -> None:
        with JobStore(self.database) as store:
            store.upsert([Job("greenhouse", "123", "Acme", "Engineer", "", "https://example.com/job/123?utm_source=x")])
        result = self.importer("unused").import_url("https://example.com/job/123#apply", self.database)
        self.assertTrue(result.existed)
        self.assertEqual(result.job.source, "greenhouse")
        with JobStore(self.database) as store:
            self.assertEqual(len(store.list_jobs()), 1)

    def test_redirect_and_repeat_import(self) -> None:
        html = '<meta property="og:title" content="Engineer"><meta property="og:site_name" content="Acme">'
        importer = self.importer(html, final="https://jobs.example.com/opening")
        first = importer.import_url("https://example.com/go", self.database)
        second = importer.import_url("https://example.com/go", self.database)
        self.assertFalse(first.existed)
        self.assertTrue(second.existed)
        self.assertEqual(first.job_id, second.job_id)

    def test_http_expiry_and_temporary_failure(self) -> None:
        for code in (404, 410):
            importer = JobImporter(opener=lambda request, **k: (_ for _ in ()).throw(HTTPError(request.full_url, code, "gone", {}, None)))
            with self.assertRaisesRegex(JobImportError, "expired"):
                importer.import_url(f"https://example.com/{code}", self.database)
        importer = JobImporter(opener=lambda *a, **k: (_ for _ in ()).throw(URLError("offline")))
        with self.assertRaisesRegex(JobImportError, "retry later"):
            importer.import_url("https://example.com/temporary", self.database)

    def test_manual_job_enters_batch_and_revalidation_is_conservative(self) -> None:
        html = '<meta property="og:title" content="Platform Engineer"><meta property="og:site_name" content="Acme">'
        imported = self.importer(html).import_url("https://example.com/job/123", self.database)
        filters = Filters(positive_title_keywords=("platform",))
        revalidator = JobRevalidator(opener=lambda request, **k: Response(b"still here", request.full_url))
        batch = start_batch(self.database, filters, limit=1, revalidator=revalidator)
        self.assertEqual(batch.entries[0].job.external_id, imported.job.external_id)
        failing = JobRevalidator(opener=lambda *a, **k: (_ for _ in ()).throw(URLError("offline")))
        with self.assertRaises(RevalidationError):
            failing.is_active(imported.job)

    def test_url_validation(self) -> None:
        self.assertEqual(canonicalize_url("HTTPS://Example.com:443/a/?utm_source=x#b"), "https://example.com/a")
        for value in ("file:///tmp/job", "http://localhost/job", "http://127.0.0.1/job"):
            with self.assertRaises(JobImportError):
                canonicalize_url(value)
