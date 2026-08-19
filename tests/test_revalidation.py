import io
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from job_harvester.models import Job
from job_harvester.revalidation import JobRevalidator, RevalidationError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def job(source: str, *, source_key: str | None = None) -> Job:
    return Job(source, "abc", "Acme", "Engineer", "Paris", "https://job", source_key=source_key)


class RevalidationTests(unittest.TestCase):
    def test_greenhouse_and_lever_use_official_detail_endpoints(self) -> None:
        urls = []
        def opener(request, *, timeout):
            urls.append(request.full_url)
            return Response(b"{}")
        checker = JobRevalidator(opener=opener)
        self.assertTrue(checker.is_active(job("greenhouse", source_key="acme board")))
        self.assertTrue(checker.is_active(job("lever", source_key="acme")))
        self.assertEqual(urls, [
            "https://boards-api.greenhouse.io/v1/boards/acme%20board/jobs/abc",
            "https://api.lever.co/v0/postings/acme/abc",
        ])

    def test_france_travail_authenticates_then_uses_detail_endpoint(self) -> None:
        requests = []
        def opener(request, *, timeout):
            requests.append(request)
            return Response(b'{"access_token":"token"}' if request.data else b"{}")
        with patch.dict(os.environ, {
            "FRANCE_TRAVAIL_CLIENT_ID": "id",
            "FRANCE_TRAVAIL_CLIENT_SECRET": "secret",
        }):
            self.assertTrue(JobRevalidator(opener=opener).is_active(job("france_travail")))
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[1].full_url.endswith("/offres/abc"))
        self.assertEqual(requests[1].headers["Authorization"], "Bearer token")

    def test_not_found_is_expired_but_temporary_failure_is_an_error(self) -> None:
        missing = HTTPError("https://job", 404, "gone", {}, None)
        checker = JobRevalidator(opener=lambda *a, **k: (_ for _ in ()).throw(missing))
        self.assertFalse(checker.is_active(job("lever", source_key="acme")))
        checker = JobRevalidator(
            opener=lambda *a, **k: (_ for _ in ()).throw(URLError("offline"))
        )
        with self.assertRaises(RevalidationError):
            checker.is_active(job("lever", source_key="acme"))

    def test_france_travail_invalid_json_is_indeterminate(self) -> None:
        responses = iter([Response(b'{"access_token":"token"}'), Response(b"not-json")])
        checker = JobRevalidator(opener=lambda *a, **k: next(responses))
        with patch.dict(os.environ, {
            "FRANCE_TRAVAIL_CLIENT_ID": "id",
            "FRANCE_TRAVAIL_CLIENT_SECRET": "secret",
        }):
            with self.assertRaisesRegex(RevalidationError, "returned invalid JSON"):
                checker.is_active(job("france_travail"))

    def test_gone_remains_expired_for_both_official_statuses(self) -> None:
        for status in (404, 410):
            with self.subTest(status=status):
                error = HTTPError("https://job", status, "gone", {}, None)
                checker = JobRevalidator(
                    opener=lambda *a, **k: (_ for _ in ()).throw(error)
                )
                self.assertFalse(checker.is_active(job("lever", source_key="acme")))

    def test_missing_board_provenance_is_inconclusive(self) -> None:
        with self.assertRaisesRegex(RevalidationError, "collect it again"):
            JobRevalidator().is_active(job("greenhouse"))
