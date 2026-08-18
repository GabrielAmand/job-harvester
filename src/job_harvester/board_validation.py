from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from job_harvester.collectors.base import Opener
from job_harvester.registry import Board


@dataclass(frozen=True, slots=True)
class ValidationResult:
    outcome: str
    job_count: int | None = None
    company: str | None = None
    error: str | None = None


class BoardValidator:
    def __init__(self, *, timeout: float = 15.0, opener: Opener = urlopen) -> None:
        self.timeout = timeout
        self.opener = opener

    def validate(self, board: Board) -> ValidationResult:
        slug = quote(board.slug, safe="")
        if board.provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        else:
            url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "job-harvester/0.5"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return ValidationResult("invalid", error="HTTP 404")
            return ValidationResult("temporary", error=f"HTTP {error.code}")
        except (URLError, TimeoutError, OSError) as error:
            return ValidationResult("temporary", error=str(error))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return ValidationResult("temporary", error=f"invalid JSON: {error}")
        return self._interpret(board.provider, payload)

    @staticmethod
    def _interpret(provider: str, payload: object) -> ValidationResult:
        if provider == "greenhouse":
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                return ValidationResult("temporary", error="unexpected response shape")
            jobs: list[Any] = payload["jobs"]
        else:
            if not isinstance(payload, list):
                return ValidationResult("temporary", error="unexpected response shape")
            jobs = payload
        return ValidationResult("valid", job_count=len(jobs))
