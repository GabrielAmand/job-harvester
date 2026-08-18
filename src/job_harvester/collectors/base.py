from typing import Protocol

from job_harvester.models import Job


class Collector(Protocol):
    def collect(self) -> list[Job]: ...
