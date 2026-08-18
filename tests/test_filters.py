import unittest

from job_harvester.config import Filters
from job_harvester.filters import is_relevant, seniority_category
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

    def test_default_out_of_scope_role_phrases_use_title_only(self) -> None:
        filters = Filters(positive_title_keywords=("manager", "engineer"))
        excluded = (
            "Technical Program Manager", "Product Manager", "Product Management, Cloud",
            "Project Manager",
            "Engineering Manager", "Director of Infrastructure", "Head of Cloud",
            "Vice President of Platform", "VP Infrastructure",
        )
        for title in excluded:
            with self.subTest(title=title):
                self.assertFalse(is_relevant(job(title), filters))
        for title in ("DevOps Engineer", "Cloud Engineer", "Production Engineer"):
            with self.subTest(title=title):
                self.assertTrue(is_relevant(job(title), filters))

    def test_explicit_incompatible_geography_is_rejected_but_unknown_is_not(self) -> None:
        filters = Filters(positive_title_keywords=("cloud",))
        incompatible = Job(
            "greenhouse", "us", "Acme", "Cloud Engineer", "Remote — US only",
            "https://job/us", work_mode="remote", remote_scope="restricted",
            remote_eligibility="incompatible",
        )
        unclear = Job(
            "greenhouse", "unclear", "Acme", "Cloud Engineer", "Remote",
            "https://job/unclear", work_mode="remote", remote_scope="restricted",
        )
        self.assertFalse(is_relevant(incompatible, filters))
        self.assertTrue(is_relevant(unclear, filters))
        self.assertTrue(is_relevant(
            incompatible,
            Filters(
                positive_title_keywords=("cloud",),
                exclude_incompatible_remote=False,
            ),
        ))

    def test_seniority_categories_are_title_only_and_deterministic(self) -> None:
        cases = {
            "DevOps Engineer": "normal",
            "Senior DevOps Engineer": "senior",
            "Principal DevOps Engineer": "strong",
            "Staff Platform Engineer": "strong",
            "Lead DevOps Engineer": "strong",
            "Cloud Architect": "normal",
            "Enterprise Architect": "strong",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(seniority_category(title), expected)

    def test_strong_seniority_is_excluded_by_default_and_can_be_enabled(self) -> None:
        strong = job("Staff Cloud Engineer")
        default = Filters(positive_title_keywords=("cloud",))
        enabled = Filters(
            positive_title_keywords=("cloud",), allow_strong_seniority=True
        )
        self.assertFalse(is_relevant(strong, default))
        self.assertTrue(is_relevant(strong, enabled))
        self.assertTrue(is_relevant(job("Senior Cloud Engineer"), default))
