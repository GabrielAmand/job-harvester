from dataclasses import replace
from email.message import Message
import io
import unittest
from urllib.error import URLError

from job_harvester.france_travail_enrichment import (
    FranceTravailEnricher, extract_remote_frequency,
)
from job_harvester.models import Job


class Response(io.BytesIO):
    def __init__(self, body: str, url: str, content_type: str = "text/html; charset=utf-8") -> None:
        super().__init__(body.encode())
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def job() -> Job:
    url = "https://candidat.francetravail.fr/offres/recherche/detail/123ABCD"
    return Job("france_travail", "123ABCD", "Acme", "Platform Engineer", "Paris", url,
               work_mode="remote", source_url=url)


class RemoteFrequencyTests(unittest.TestCase):
    def test_explicit_remote_days(self) -> None:
        self.assertEqual(extract_remote_frequency("2 jours de télétravail par semaine").remote_days_per_week, 2)
        one = extract_remote_frequency("1 jour de télétravail")
        self.assertEqual((one.remote_days_per_week, one.intensity), (1, "occasional"))

    def test_full_remote_and_onsite_days(self) -> None:
        full = extract_remote_frequency("Poste en 100 % télétravail")
        self.assertEqual((full.remote_days_per_week, full.onsite_days_per_week, full.intensity), (5, 0, "full"))
        onsite = extract_remote_frequency("présence 3 jours par semaine")
        self.assertEqual((onsite.onsite_days_per_week, onsite.intensity), (3, "limited"))

    def test_vague_wording_stays_unknown(self) -> None:
        for text in ("télétravail flexible", "regular telework", "hybrid work available",
                     "présence sur site obligatoire, pas de 100% télétravail"):
            with self.subTest(text=text):
                self.assertEqual(extract_remote_frequency(text).intensity, "unknown")


class ResolutionTests(unittest.TestCase):
    def test_postuler_endpoint_resolves_and_redirect_is_stored(self) -> None:
        seen = []
        def opener(request, **kwargs):
            seen.append(request.full_url)
            if len(seen) == 1:
                return Response('<a href="/offres/recherche/detail.detailstandalone.postuler.contact:chargercontact?t:ac=123ABCD">Postuler</a>', request.full_url)
            if len(seen) == 2:
                return Response('<a href="https://apply.example/jobs/123">Continuer</a>', request.full_url)
            return Response("2 jours de télétravail par semaine", "https://ats.example/opening/123")
        enriched = FranceTravailEnricher(opener=opener).enrich(job())
        self.assertEqual(enriched.source_url, job().url)
        self.assertEqual(enriched.application_url, "https://ats.example/opening/123")
        self.assertEqual((enriched.remote_days_per_week, enriched.remote_intensity), (2, "limited"))
        self.assertEqual((enriched.work_mode, enriched.full_remote), ("hybrid", False))

    def test_missing_postuler_and_fetch_failure_are_safe(self) -> None:
        missing = FranceTravailEnricher(opener=lambda request, **k: Response("No action", request.full_url)).enrich(job())
        self.assertIsNone(missing.application_url)
        failing = FranceTravailEnricher(opener=lambda *a, **k: (_ for _ in ()).throw(URLError("offline"))).enrich(job())
        self.assertIsNone(failing.application_url)
        self.assertEqual(failing.work_mode, "remote")

    def test_invalid_external_content_is_safe(self) -> None:
        responses = iter((
            Response('<a href="https://apply.example/job">Postuler</a>', job().url),
            Response("binary", "https://apply.example/job", "application/pdf"),
        ))
        enriched = FranceTravailEnricher(opener=lambda *a, **k: next(responses)).enrich(job())
        self.assertEqual(enriched.application_url, "https://apply.example/job")
        self.assertEqual(enriched.remote_intensity, "unknown")
