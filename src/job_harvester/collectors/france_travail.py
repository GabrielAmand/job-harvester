from datetime import datetime
import json
import os
import re
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from job_harvester.collectors.base import CollectionError, Opener
from job_harvester.models import Job
from job_harvester.work_mode import classify_france_travail_work_mode


TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"
PAGE_SIZE = 150
MAX_RESULT_INDEX = 1149


def credentials_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    values = os.environ if environ is None else environ
    client_id = values.get("FRANCE_TRAVAIL_CLIENT_ID", "").strip()
    client_secret = values.get("FRANCE_TRAVAIL_CLIENT_SECRET", "").strip()
    missing = [
        name
        for name, value in (
            ("FRANCE_TRAVAIL_CLIENT_ID", client_id),
            ("FRANCE_TRAVAIL_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise CollectionError(
            "France Travail credentials missing from environment: " + ", ".join(missing)
        )
    return client_id, client_secret


class FranceTravailCollector:
    def __init__(
        self,
        search_terms: tuple[str, ...],
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 15.0,
        opener: Opener = urlopen,
    ) -> None:
        self.search_terms = search_terms
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.opener = opener

    def collect(self) -> list[Job]:
        if self.client_id is None and self.client_secret is None:
            client_id, client_secret = credentials_from_env()
        elif self.client_id is None or self.client_secret is None:
            raise CollectionError(
                "France Travail client_id and client_secret must be provided together"
            )
        else:
            client_id, client_secret = self.client_id, self.client_secret
        token = self._authenticate(client_id, client_secret)
        jobs: dict[str, Job] = {}
        for term in self.search_terms:
            for raw in self._search(term, token):
                job = self._normalize_job(raw, term)
                jobs[job.external_id] = job
        return list(jobs.values())

    def _authenticate(self, client_id: str, client_secret: str) -> str:
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": SCOPE,
            }
        ).encode("ascii")
        request = Request(
            TOKEN_URL,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "job-harvester/0.4",
            },
        )
        payload = self._request_json(request, "France Travail authentication")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CollectionError("France Travail authentication returned no access token")
        return token

    def _search(self, term: str, token: str) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        start = 0
        while start <= MAX_RESULT_INDEX:
            end = min(start + PAGE_SIZE - 1, MAX_RESULT_INDEX)
            url = SEARCH_URL + "?" + urlencode(
                {"motsCles": term, "range": f"{start}-{end}", "sort": "1"}
            )
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "job-harvester/0.4",
                },
            )
            payload, content_range = self._request_search(request, term)
            raw_results = payload.get("resultats") if isinstance(payload, dict) else None
            if payload is None:
                return offers
            if not isinstance(raw_results, list):
                raise CollectionError(
                    f"France Travail search {term!r} response must contain a resultats array"
                )
            for index, raw in enumerate(raw_results):
                if not isinstance(raw, dict):
                    raise CollectionError(
                        f"France Travail search {term!r} result {index} must be an object"
                    )
                offers.append(raw)
            page = re.fullmatch(r"offres\s+(\d+)-(\d+)/(\d+|\*)", content_range or "")
            if not page or not raw_results:
                break
            last = int(page.group(2))
            total = int(page.group(3)) if page.group(3) != "*" else last + 1
            if last >= total - 1 or last >= MAX_RESULT_INDEX:
                break
            start = last + 1
        return offers

    def _request_search(self, request: Request, term: str) -> tuple[object, str | None]:
        try:
            with self.opener(request, timeout=self.timeout) as response:
                content_range = response.headers.get("Content-Range")
                if getattr(response, "status", None) == 204:
                    return None, content_range
                return json.load(response), content_range
        except HTTPError as error:
            raise CollectionError(
                f"France Travail search {term!r} returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CollectionError(
                f"could not fetch France Travail search {term!r}: {error}"
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CollectionError(
                f"France Travail search {term!r} returned invalid JSON"
            ) from error

    def _request_json(self, request: Request, label: str) -> object:
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise CollectionError(f"{label} returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise CollectionError(f"{label} failed: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CollectionError(f"{label} returned invalid JSON") from error

    def _normalize_job(self, raw: dict[str, Any], term: str) -> Job:
        offer_id = raw.get("id")
        title = raw.get("intitule")
        if not isinstance(offer_id, str) or not offer_id.strip():
            raise CollectionError(
                f"France Travail search {term!r} returned an offer without a valid id"
            )
        if not isinstance(title, str) or not title.strip():
            raise CollectionError(
                f"France Travail offer {offer_id!r} has no valid intitule"
            )
        company_data = raw.get("entreprise")
        company = company_data.get("nom") if isinstance(company_data, dict) else None
        if not isinstance(company, str) or not company.strip():
            company = "Entreprise non précisée"
        location_data = raw.get("lieuTravail")
        location = location_data.get("libelle") if isinstance(location_data, dict) else None
        origin = raw.get("origineOffre")
        url = origin.get("urlOrigine") if isinstance(origin, dict) else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            url = f"https://candidat.francetravail.fr/offres/recherche/detail/{offer_id.strip()}"
        published_at = self._published_at(raw.get("dateCreation"), offer_id)
        work_mode, remote_scope, full_remote, remote_eligibility = classify_france_travail_work_mode(raw)
        return Job(
            source="france_travail",
            external_id=offer_id.strip(),
            company=company.strip(),
            title=title.strip(),
            location=location.strip() if isinstance(location, str) else "",
            url=url.strip(),
            published_at=published_at,
            work_mode=work_mode,
            remote_scope=remote_scope,
            full_remote=full_remote,
            remote_eligibility=remote_eligibility,
        )

    @staticmethod
    def _published_at(value: object, offer_id: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise CollectionError(
                f"France Travail offer {offer_id!r} has invalid dateCreation"
            )
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise CollectionError(
                f"France Travail offer {offer_id!r} has invalid dateCreation"
            ) from error
