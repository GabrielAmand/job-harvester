from dataclasses import dataclass
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Job:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    work_mode: str = "unknown"
    remote_scope: str = "unknown"
    published_at: datetime | None = None
    collected_at: datetime | None = None

    def discovered_at(self) -> datetime:
        return self.collected_at or utc_now()


@dataclass(frozen=True, slots=True)
class StoredJob:
    job: Job
    state: str
