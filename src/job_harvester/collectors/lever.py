import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from job_harvester.collectors.base import CollectionError, Opener
from job_harvester.models import Job
from job_harvester.work_mode import classify_lever_work_mode


class LeverCollector:
    def __init__(
        self,
        company: str,
        company_slug: str,
        *,
        timeout: float = 15.0,
        opener: Opener = urlopen,
    ) -> None:
        self.company = company
        self.company_slug = company_slug
        self.timeout = timeout
        self.opener = opener

    @property
    def url(self) -> str:
        slug = quote(self.company_slug, safe="")
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"

    def collect(self) -> list[Job]:
        request = Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "job-harvester/0.3"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise CollectionError(
                f"Lever board {self.company_slug!r} returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CollectionError(
                f"could not fetch Lever board {self.company_slug!r}: {error}"
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CollectionError(
                f"Lever board {self.company_slug!r} returned invalid JSON"
            ) from error
        return self._normalize(payload)

    def _normalize(self, payload: object) -> list[Job]:
        if not isinstance(payload, list):
            raise CollectionError("Lever response must be a jobs array")
        jobs: list[Job] = []
        for index, raw in enumerate(payload):
            if not isinstance(raw, dict):
                raise CollectionError(f"Lever job at index {index} must be an object")
            jobs.append(self._normalize_job(raw, index))
        return jobs

    def _normalize_job(self, raw: dict[str, Any], index: int) -> Job:
        job_id = raw.get("id")
        title = raw.get("text")
        hosted_url = raw.get("hostedUrl")
        for name, value in (("id", job_id), ("text", title), ("hostedUrl", hosted_url)):
            if not isinstance(value, str) or not value.strip():
                raise CollectionError(f"Lever job at index {index} has no valid {name}")
        categories = raw.get("categories")
        location = categories.get("location") if isinstance(categories, dict) else None
        if not isinstance(location, str):
            all_locations = categories.get("allLocations") if isinstance(categories, dict) else None
            location = " / ".join(
                value for value in all_locations if isinstance(value, str)
            ) if isinstance(all_locations, list) else ""
        work_mode, remote_scope = classify_lever_work_mode(raw)
        return Job(
            source="lever",
            external_id=job_id.strip(),
            company=self.company,
            title=title.strip(),
            location=location.strip(),
            url=hosted_url.strip(),
            work_mode=work_mode,
            remote_scope=remote_scope,
            source_key=self.company_slug,
        )
