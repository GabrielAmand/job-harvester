from job_harvester.config import Filters
from job_harvester.models import Job


def is_relevant(job: Job, filters: Filters) -> bool:
    title = job.title.casefold()
    location = job.location.casefold()
    positive = tuple(keyword.casefold() for keyword in filters.positive_title_keywords)
    negative = tuple(keyword.casefold() for keyword in filters.negative_title_keywords)
    locations = tuple(keyword.casefold() for keyword in filters.location_keywords)

    return (
        bool(positive)
        and any(keyword in title for keyword in positive)
        and not any(keyword in title for keyword in negative)
        and (not locations or any(keyword in location for keyword in locations))
    )
