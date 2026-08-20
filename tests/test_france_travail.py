from datetime import datetime, timezone
import io
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from job_harvester.collectors.base import CollectionError
from job_harvester.collectors.france_travail import (
    FranceTravailCollector,
    credentials_from_env,
)


class Headers(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key, default)


class Response(io.BytesIO):
    def __init__(self, body: bytes, *, content_range: str | None = None, status: int = 200):
        super().__init__(body)
        self.headers = Headers()
        if content_range:
            self.headers["Content-Range"] = content_range
        self.status = status

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def fixture_bytes() -> bytes:
    return (Path(__file__).parent / "fixtures" / "france_travail_offers.json").read_bytes()


class FranceTravailTests(unittest.TestCase):
    def test_authenticates_searches_normalizes_and_deduplicates_terms(self) -> None:
        requests: list[object] = []

        def opener(request: object, *, timeout: float) -> Response:
            requests.append(request)
            if request.full_url.startswith("https://entreprise.francetravail.fr"):  # type: ignore[attr-defined]
                return Response(b'{"access_token":"test-token","expires_in":1499}')
            return Response(fixture_bytes(), content_range="offres 0-1/2", status=206)

        jobs = FranceTravailCollector(
            ("DevOps", "Linux"),
            client_id="client-id",
            client_secret="client-secret",
            opener=opener,
        ).collect()

        self.assertEqual(len(requests), 3)
        auth = requests[0]
        auth_body = parse_qs(auth.data.decode("ascii"))  # type: ignore[attr-defined]
        self.assertEqual(auth_body["grant_type"], ["client_credentials"])
        self.assertEqual(auth_body["scope"], ["api_offresdemploiv2 o2dsoffre"])
        search_query = parse_qs(urlparse(requests[1].full_url).query)  # type: ignore[attr-defined]
        self.assertEqual(search_query["motsCles"], ["DevOps"])
        self.assertEqual(search_query["range"], ["0-149"])
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer test-token")  # type: ignore[attr-defined]

        self.assertEqual(len(jobs), 2)
        first, second = jobs
        self.assertEqual(first.source, "france_travail")
        self.assertEqual(first.external_id, "201ABCD")
        self.assertEqual(first.company, "Exemple SAS")
        self.assertEqual(first.location, "75 - Paris")
        self.assertEqual(first.published_at, datetime(2026, 8, 18, 8, 15, 42, tzinfo=timezone.utc))
        self.assertEqual((first.work_mode, first.remote_scope), ("hybrid", "france"))
        self.assertEqual(first.source_url,
            "https://candidat.francetravail.fr/offres/recherche/detail/201ABCD")
        self.assertEqual(second.company, "Entreprise non précisée")
        self.assertEqual((second.work_mode, second.remote_scope), ("onsite", "unknown"))
        self.assertEqual(
            second.url,
            "https://candidat.francetravail.fr/offres/recherche/detail/202EFGH",
        )

    def test_follows_content_range_pagination(self) -> None:
        offer = json.loads(fixture_bytes())["resultats"][0]
        pages = [
            Response(json.dumps({"resultats": [offer]}).encode(), content_range="offres 0-0/2", status=206),
            Response(json.dumps({"resultats": [{**offer, "id": "second"}]}).encode(), content_range="offres 1-1/2"),
        ]
        seen_ranges: list[str] = []

        def opener(request: object, *, timeout: float) -> Response:
            if request.full_url.startswith("https://entreprise.francetravail.fr"):  # type: ignore[attr-defined]
                return Response(b'{"access_token":"token"}')
            seen_ranges.append(parse_qs(urlparse(request.full_url).query)["range"][0])  # type: ignore[attr-defined]
            return pages.pop(0)

        jobs = FranceTravailCollector(
            ("DevOps",), client_id="id", client_secret="secret", opener=opener
        ).collect()
        self.assertEqual(seen_ranges, ["0-149", "1-150"])
        self.assertEqual([job.external_id for job in jobs], ["201ABCD", "second"])

    def test_accepts_no_content_as_an_empty_search(self) -> None:
        responses = iter([
            Response(b'{"access_token":"token"}'),
            Response(b"", content_range="*/0", status=204),
        ])
        collector = FranceTravailCollector(
            ("No matches",),
            client_id="id",
            client_secret="secret",
            opener=lambda *args, **kwargs: next(responses),
        )
        self.assertEqual(collector.collect(), [])

    def test_missing_credentials_are_reported_without_values(self) -> None:
        with self.assertRaisesRegex(CollectionError, "FRANCE_TRAVAIL_CLIENT_ID"):
            credentials_from_env({})
        with self.assertRaisesRegex(CollectionError, "FRANCE_TRAVAIL_CLIENT_SECRET"):
            credentials_from_env({"FRANCE_TRAVAIL_CLIENT_ID": "id"})
        with self.assertRaisesRegex(CollectionError, "provided together"):
            FranceTravailCollector(("DevOps",), client_id="id").collect()

    def test_rejects_invalid_responses(self) -> None:
        cases = [
            (b"{}", "resultats array"),
            (b'{"resultats":[null]}', "must be an object"),
            (b'{"resultats":[{"id":"1"}]}', "intitule"),
            (b"not-json", "invalid JSON"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                responses = iter([Response(b'{"access_token":"token"}'), Response(payload)])
                collector = FranceTravailCollector(
                    ("DevOps",),
                    client_id="id",
                    client_secret="secret",
                    opener=lambda *args, **kwargs: next(responses),
                )
                with self.assertRaisesRegex(CollectionError, message):
                    collector.collect()
