import io
from pathlib import Path
import unittest

from job_harvester.collectors.greenhouse import CollectionError
from job_harvester.collectors.lever import LeverCollector


class Response(io.BytesIO):
    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def fixture_bytes() -> bytes:
    return (Path(__file__).parent / "fixtures" / "lever_jobs.json").read_bytes()


class LeverTests(unittest.TestCase):
    def test_collects_and_normalizes_lever_jobs(self) -> None:
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> Response:
            seen["url"] = request.full_url  # type: ignore[attr-defined]
            seen["accept"] = request.get_header("Accept")  # type: ignore[attr-defined]
            seen["timeout"] = timeout
            return Response(fixture_bytes())

        jobs = LeverCollector("Acme", "acme", opener=opener).collect()
        self.assertEqual(seen, {
            "url": "https://api.lever.co/v0/postings/acme?mode=json",
            "accept": "application/json",
            "timeout": 15.0,
        })
        self.assertEqual([(job.external_id, job.title) for job in jobs], [
            ("lever-123", "Platform Engineer"),
            ("lever-124", "Site Reliability Engineer"),
        ])
        self.assertEqual(jobs[0].source, "lever")
        self.assertEqual(jobs[0].source_key, "acme")
        self.assertEqual(jobs[0].location, "Remote - US")
        self.assertEqual((jobs[0].work_mode, jobs[0].remote_scope), ("remote", "restricted"))
        self.assertEqual(jobs[0].remote_eligibility, "incompatible")
        self.assertIsNone(jobs[0].published_at)
        self.assertEqual((jobs[1].work_mode, jobs[1].remote_scope), ("hybrid", "unknown"))

    def test_falls_back_to_all_locations(self) -> None:
        payload = b'[{"id":"1","text":"Engineer","categories":{"allLocations":["Paris","Lyon"]},"hostedUrl":"https://job/1"}]'
        collector = LeverCollector("Acme", "acme", opener=lambda *a, **k: Response(payload))
        self.assertEqual(collector.collect()[0].location, "Paris / Lyon")

    def test_rejects_bad_responses(self) -> None:
        cases = [
            (b"{}", "jobs array"),
            (b"[null]", "must be an object"),
            (b'[{"id":"1","text":"Engineer"}]', "hostedUrl"),
            (b"not-json", "invalid JSON"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                collector = LeverCollector(
                    "Acme", "acme", opener=lambda *a, _payload=payload, **k: Response(_payload)
                )
                with self.assertRaisesRegex(CollectionError, message):
                    collector.collect()

    def test_escapes_company_slug(self) -> None:
        self.assertIn("/not%2Fa%20slug?", LeverCollector("Acme", "not/a slug").url)
