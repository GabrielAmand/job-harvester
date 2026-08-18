import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from job_harvester.collectors.base import CollectionError, Opener
from job_harvester.models import Job
from job_harvester.work_mode import classify_work_mode


class GreenhouseCollector:
    def __init__(
        self,
        company: str,
        board_token: str,
        *,
        timeout: float = 15.0,
        opener: Opener = urlopen,
    ) -> None:
        self.company = company
        self.board_token = board_token
        self.timeout = timeout
        self.opener = opener

    @property
    def url(self) -> str:
        token = quote(self.board_token, safe="")
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def collect(self) -> list[Job]:
        request = Request(self.url, headers={"User-Agent": "job-harvester/0.1"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise CollectionError(
                f"Greenhouse board {self.board_token!r} returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CollectionError(
                f"could not fetch Greenhouse board {self.board_token!r}: {error}"
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CollectionError(
                f"Greenhouse board {self.board_token!r} returned invalid JSON"
            ) from error
        return self._normalize(payload)

    def _normalize(self, payload: object) -> list[Job]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise CollectionError("Greenhouse response must contain a jobs array")

        jobs: list[Job] = []
        for index, raw in enumerate(payload["jobs"]):
            if not isinstance(raw, dict):
                raise CollectionError(f"Greenhouse job at index {index} must be an object")
            jobs.append(self._normalize_job(raw, index))
        return jobs

    def _normalize_job(self, raw: dict[str, Any], index: int) -> Job:
        job_id = raw.get("id")
        title = raw.get("title")
        absolute_url = raw.get("absolute_url")
        location = raw.get("location")
        location_name = location.get("name") if isinstance(location, dict) else None
        if not isinstance(job_id, (str, int)) or isinstance(job_id, bool):
            raise CollectionError(f"Greenhouse job at index {index} has no valid id")
        for name, value in (("title", title), ("absolute_url", absolute_url)):
            if not isinstance(value, str) or not value.strip():
                raise CollectionError(
                    f"Greenhouse job at index {index} has no valid {name}"
                )
        work_mode, remote_scope, full_remote, remote_eligibility = classify_work_mode(raw)
        return Job(
            source="greenhouse",
            external_id=str(job_id),
            company=self.company,
            title=title.strip(),
            location=location_name.strip() if isinstance(location_name, str) else "",
            url=absolute_url.strip(),
            work_mode=work_mode,
            remote_scope=remote_scope,
            full_remote=full_remote,
            remote_eligibility=remote_eligibility,
            source_key=self.board_token,
        )
