import io
from pathlib import Path
import unittest

from job_harvester.collectors.greenhouse import CollectionError, GreenhouseCollector


class Response(io.BytesIO):
    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def fixture_bytes() -> bytes:
    return (Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_bytes()


class GreenhouseTests(unittest.TestCase):
    def test_collects_and_normalizes_greenhouse_jobs(self) -> None:
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> Response:
            seen["url"] = request.full_url  # type: ignore[attr-defined]
            seen["timeout"] = timeout
            return Response(fixture_bytes())

        jobs = GreenhouseCollector("Acme", "acme", opener=opener).collect()
        self.assertEqual(seen, {
            "url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            "timeout": 15.0,
        })
        self.assertEqual([(job.external_id, job.title) for job in jobs], [
            ("123", "Platform Engineer"), ("124", "Data Engineer")
        ])
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].location, "Paris, France")
        self.assertIsNone(jobs[0].published_at)
        self.assertIsNone(jobs[0].remote_status)
        self.assertEqual(jobs[1].location, "")

    def test_rejects_bad_responses(self) -> None:
        cases = [
            (b"{}", "jobs array"),
            (b'{"jobs": [null]}', "must be an object"),
            (b'{"jobs": [{"id": 1, "title": "Engineer"}]}', "absolute_url"),
            (b"not-json", "invalid JSON"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                collector = GreenhouseCollector(
                    "Acme", "acme", opener=lambda *a, _payload=payload, **k: Response(_payload)
                )
                with self.assertRaisesRegex(CollectionError, message):
                    collector.collect()

    def test_escapes_board_token(self) -> None:
        collector = GreenhouseCollector("Acme", "not/a token")
        self.assertTrue(collector.url.endswith("/not%2Fa%20token/jobs"))
