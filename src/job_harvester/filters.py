from job_harvester.config import Filters
from job_harvester.models import Job


def is_relevant(job: Job, filters: Filters) -> bool:
    title = job.title.casefold()
    location = job.location.casefold()
    positive = tuple(keyword.casefold() for keyword in filters.positive_title_keywords)
    negative = tuple(keyword.casefold() for keyword in filters.negative_title_keywords)
    locations = tuple(keyword.casefold() for keyword in filters.location_keywords)

    text_relevant = (
        bool(positive)
        and any(keyword in title for keyword in positive)
        and not any(keyword in title for keyword in negative)
        and (not locations or any(keyword in location for keyword in locations))
    )
    if not text_relevant:
        return False
    if filters.remote_policy == "require":
        return job.work_mode == "remote"
    if job.work_mode == "hybrid":
        return filters.allow_hybrid
    if job.work_mode == "onsite":
        return filters.allow_onsite
    return True
