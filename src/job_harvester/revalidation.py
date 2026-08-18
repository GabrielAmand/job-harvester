import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from job_harvester.collectors.base import Opener
from job_harvester.collectors.france_travail import (
    SCOPE,
    TOKEN_URL,
    credentials_from_env,
)
from job_harvester.models import Job


FRANCE_TRAVAIL_DETAIL_URL = (
    "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/"
)


class RevalidationError(RuntimeError):
    """Raised when an offer's availability cannot be determined safely."""


class JobRevalidator:
    def __init__(self, *, timeout: float = 15.0, opener: Opener = urlopen) -> None:
        self.timeout = timeout
        self.opener = opener
        self._france_travail_token: str | None = None

    def is_active(self, job: Job) -> bool:
        if job.source == "manual":
            return self._get_manual(job)
        if job.source == "greenhouse":
            if not job.source_key:
                raise RevalidationError(
                    "Greenhouse job is missing its board token; collect it again first"
                )
            url = (
                "https://boards-api.greenhouse.io/v1/boards/"
                f"{quote(job.source_key, safe='')}/jobs/{quote(job.external_id, safe='')}"
            )
            return self._get(url, f"Greenhouse job {job.external_id}")
        if job.source == "lever":
            if not job.source_key:
                raise RevalidationError(
                    "Lever job is missing its company slug; collect it again first"
                )
            url = (
                "https://api.lever.co/v0/postings/"
                f"{quote(job.source_key, safe='')}/{quote(job.external_id, safe='')}"
            )
            return self._get(url, f"Lever job {job.external_id}")
        if job.source == "france_travail":
            token = self._france_travail_token or self._authenticate_france_travail()
            self._france_travail_token = token
            return self._get(
                FRANCE_TRAVAIL_DETAIL_URL + quote(job.external_id, safe=""),
                f"France Travail offer {job.external_id}",
                authorization=f"Bearer {token}",
            )
        raise RevalidationError(f"unsupported job source: {job.source}")

    def _get_manual(self, job: Job) -> bool:
        request = Request(
            job.url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "job-harvester/0.10.1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                # A successful fetch is deliberately sufficient. Ambiguous page copy
                # must never expire a manually imported opportunity.
                response.read(1024)
                return True
        except HTTPError as error:
            if error.code in {404, 410}:
                return False
            raise RevalidationError(
                f"manual job {job.external_id} returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise RevalidationError(
                f"manual job {job.external_id} failed: {error}"
            ) from error

    def _authenticate_france_travail(self) -> str:
        try:
            client_id, client_secret = credentials_from_env()
        except Exception as error:
            raise RevalidationError(str(error)) from error
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
                "User-Agent": "job-harvester/0.6",
            },
        )
        try:
            payload = self._request_json(request, "France Travail authentication")
        except HTTPError as error:
            raise RevalidationError(
                f"France Travail authentication returned HTTP {error.code}"
            ) from error
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RevalidationError(
                "France Travail authentication returned no access token"
            )
        return token

    def _get(self, url: str, label: str, *, authorization: str | None = None) -> bool:
        headers = {"Accept": "application/json", "User-Agent": "job-harvester/0.6"}
        if authorization:
            headers["Authorization"] = authorization
        request = Request(url, headers=headers)
        try:
            payload = self._request_json(request, label)
        except HTTPError as error:
            if error.code in {404, 410}:
                return False
            raise RevalidationError(f"{label} returned HTTP {error.code}") from error
        if not isinstance(payload, dict):
            raise RevalidationError(f"{label} response must be an object")
        return True

    def _request_json(self, request: Request, label: str) -> object:
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError) as error:
            raise RevalidationError(f"{label} failed: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RevalidationError(f"{label} returned invalid JSON") from error
