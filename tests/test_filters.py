import unittest

from job_harvester.config import Filters
from job_harvester.filters import is_relevant
from job_harvester.models import Job


def job(title: str, location: str = "Paris, France") -> Job:
    return Job("greenhouse", "1", "Acme", title, location, "https://job/1")


class FilterTests(unittest.TestCase):
    def test_positive_filtering_is_case_insensitive(self) -> None:
        filters = Filters(positive_title_keywords=("DevOps", "SITE RELIABILITY"))
        self.assertTrue(is_relevant(job("Senior devops Engineer"), filters))
        self.assertTrue(is_relevant(job("Site Reliability Engineer"), filters))
        self.assertFalse(is_relevant(job("Backend Engineer"), filters))

    def test_negative_filter_overrides_positive_filter(self) -> None:
        filters = Filters(
            positive_title_keywords=("platform",),
            negative_title_keywords=("Director", "staff engineer"),
        )
        self.assertFalse(is_relevant(job("Director of Platform"), filters))
        self.assertTrue(is_relevant(job("Senior Platform Engineer"), filters))

    def test_location_filter_is_optional_and_case_insensitive(self) -> None:
        unrestricted = Filters(positive_title_keywords=("cloud",))
        restricted = Filters(
            positive_title_keywords=("cloud",), location_keywords=("FRANCE", "remote")
        )
        self.assertTrue(is_relevant(job("Cloud Engineer", "Berlin"), unrestricted))
        self.assertTrue(is_relevant(job("Cloud Engineer", "Paris, France"), restricted))
        self.assertFalse(is_relevant(job("Cloud Engineer", "Berlin"), restricted))

    def test_no_positive_keywords_matches_nothing(self) -> None:
        self.assertFalse(is_relevant(job("Cloud Engineer"), Filters()))

    def test_work_mode_policies_and_allow_flags(self) -> None:
        def mode(value: str) -> Job:
            return Job(
                "greenhouse", value, "Acme", "Cloud Engineer", "", "https://job/1",
                work_mode=value,
            )

        any_policy = Filters(positive_title_keywords=("cloud",), allow_onsite=False)
        self.assertTrue(is_relevant(mode("remote"), any_policy))
        self.assertTrue(is_relevant(mode("unknown"), any_policy))
        self.assertTrue(is_relevant(mode("hybrid"), any_policy))
        self.assertFalse(is_relevant(mode("onsite"), any_policy))

        prefer = Filters(
            positive_title_keywords=("cloud",), remote_policy="prefer",
            allow_hybrid=False, allow_onsite=True,
        )
        self.assertTrue(is_relevant(mode("unknown"), prefer))
        self.assertFalse(is_relevant(mode("hybrid"), prefer))
        self.assertTrue(is_relevant(mode("onsite"), prefer))

        required = Filters(
            positive_title_keywords=("cloud",), remote_policy="require",
            allow_hybrid=True, allow_onsite=True,
        )
        self.assertTrue(is_relevant(mode("remote"), required))
        self.assertFalse(is_relevant(mode("unknown"), required))
        self.assertFalse(is_relevant(mode("hybrid"), required))
        self.assertFalse(is_relevant(mode("onsite"), required))
