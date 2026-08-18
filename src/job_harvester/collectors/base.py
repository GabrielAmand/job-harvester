from typing import Any, Callable, Protocol

from job_harvester.models import Job


class CollectionError(RuntimeError):
    """Raised when a source cannot be fetched or parsed."""


Opener = Callable[..., Any]


class Collector(Protocol):
    def collect(self) -> list[Job]: ...
