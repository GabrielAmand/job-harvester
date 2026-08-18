import re

from job_harvester.config import Filters
from job_harvester.models import Job


def is_relevant(job: Job, filters: Filters) -> bool:
    title = job.title.casefold()
    location = job.location.casefold()
    positive = tuple(keyword.casefold() for keyword in filters.positive_title_keywords)
    negative = tuple(keyword.casefold() for keyword in filters.negative_title_keywords)
    locations = tuple(keyword.casefold() for keyword in filters.location_keywords)
    excluded_phrases = tuple(
        phrase.casefold() for phrase in filters.excluded_title_phrases
    )

    text_relevant = (
        bool(positive)
        and any(keyword in title for keyword in positive)
        and not any(keyword in title for keyword in negative)
        and not any(_title_phrase_matches(title, phrase) for phrase in excluded_phrases)
        and (not locations or any(keyword in location for keyword in locations))
    )
    if not text_relevant:
        return False
    if filters.exclude_incompatible_remote and job.remote_eligibility == "incompatible":
        return False
    if not filters.allow_strong_seniority and seniority_category(job.title) == "strong":
        return False
    if filters.remote_policy == "require":
        return job.work_mode == "remote"
    if job.work_mode == "hybrid":
        return filters.allow_hybrid
    if job.work_mode == "onsite":
        return filters.allow_onsite
    return True


def _title_phrase_matches(title: str, phrase: str) -> bool:
    if phrase == "vp":
        return bool(re.search(r"\bvp\b", title))
    return phrase in title


def seniority_category(title: str) -> str:
    """Return a deterministic title-only priority category."""
    normalized = title.casefold()
    strong_patterns = (
        r"\bprincipal\b",
        r"\bstaff\b",
        r"\bdistinguished\b",
        r"\blead\b",
        r"\benterprise architect\b",
        r"\bchief architect\b",
    )
    if any(re.search(pattern, normalized) for pattern in strong_patterns):
        return "strong"
    if re.search(r"\bsenior\b|\bsr\.?\b", normalized):
        return "senior"
    return "normal"
