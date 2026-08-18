import unittest

from job_harvester.work_mode import (
    classify_france_travail_work_mode,
    classify_lever_work_mode,
    classify_work_mode,
)


def classify(
    *, title: str = "Engineer", location: str = "", content: str = "",
    metadata: object = None, offices: object = None,
) -> tuple[str, str]:
    return classify_work_mode({
        "title": title,
        "location": {"name": location},
        "content": content,
        "metadata": metadata,
        "offices": offices,
    })


class WorkModeTests(unittest.TestCase):
    def test_remote_english_and_scope(self) -> None:
        cases = [
            ("Fully Remote — Worldwide", ("remote", "worldwide")),
            ("Work from home — Remote, France", ("remote", "france")),
            ("WFH — Remote Europe", ("remote", "europe")),
            ("Remote - US only", ("remote", "restricted")),
        ]
        for location, expected in cases:
            with self.subTest(location=location):
                self.assertEqual(classify(location=location), expected)

    def test_remote_french_wording_is_accent_insensitive(self) -> None:
        self.assertEqual(
            classify(content="<p>Poste 100% télétravail, basé en France.</p>"),
            ("remote", "france"),
        )
        self.assertEqual(classify(location="À distance"), ("remote", "unknown"))

    def test_hybrid_and_mixed_location(self) -> None:
        self.assertEqual(classify(location="Paris — Hybride"), ("hybrid", "unknown"))
        self.assertEqual(classify(location="Paris or Remote"), ("hybrid", "unknown"))
        self.assertEqual(
            classify(location="Remote-Friendly, United States"),
            ("hybrid", "restricted"),
        )

    def test_onsite_and_unknown(self) -> None:
        self.assertEqual(classify(location="On-site, Paris"), ("onsite", "unknown"))
        self.assertEqual(classify(location="Paris, France"), ("unknown", "unknown"))

    def test_vague_technical_text_does_not_classify(self) -> None:
        self.assertEqual(
            classify(content="Build distributed systems and hybrid cloud platforms."),
            ("unknown", "unknown"),
        )

    def test_metadata_has_priority_over_lower_priority_evidence(self) -> None:
        self.assertEqual(
            classify(
                location="Remote",
                metadata=[{"name": "Workplace type", "value": "On-site"}],
            ),
            ("onsite", "unknown"),
        )
        self.assertEqual(
            classify(
                location="Sydney, Australia",
                metadata=[{"name": "Location Type", "value": "Remote"}],
            ),
            ("remote", "restricted"),
        )

    def test_generic_worldwide_prose_does_not_set_scope(self) -> None:
        self.assertEqual(
            classify(location="Remote", content="We serve customers worldwide."),
            ("remote", "unknown"),
        )

    def test_office_and_explicit_description_are_fallbacks(self) -> None:
        self.assertEqual(
            classify(offices=[{"name": "Remote - Europe"}]),
            ("remote", "europe"),
        )
        self.assertEqual(
            classify(content="This is a home-based position."),
            ("remote", "unknown"),
        )

    def test_lever_structured_workplace_type_has_priority(self) -> None:
        self.assertEqual(
            classify_lever_work_mode({
                "text": "On-site Engineer",
                "categories": {"location": "Remote - US"},
                "workplaceType": "remote",
                "descriptionPlain": "Work in the office.",
            }),
            ("remote", "restricted"),
        )
        self.assertEqual(
            classify_lever_work_mode({
                "text": "Engineer",
                "categories": {"location": "Remote"},
                "workplaceType": "remote",
            }),
            ("remote", "unknown"),
        )
        self.assertEqual(
            classify_lever_work_mode({
                "text": "Engineer",
                "categories": {"location": "Remote"},
                "country": "US",
                "workplaceType": "remote",
            }),
            ("remote", "restricted"),
        )

    def test_lever_unspecified_falls_back_conservatively(self) -> None:
        self.assertEqual(
            classify_lever_work_mode({
                "text": "Engineer",
                "categories": {"location": "Paris"},
                "workplaceType": "unspecified",
                "descriptionPlain": "This is a hybrid role.",
            }),
            ("hybrid", "unknown"),
        )

    def test_france_travail_structured_telework_has_priority(self) -> None:
        self.assertEqual(
            classify_france_travail_work_mode({
                "intitule": "On-site Engineer",
                "description": "Poste sans télétravail.",
                "teletravail": {"libelle": "Télétravail total"},
                "lieuTravail": {"libelle": "75 - Paris", "codePostal": "75001"},
            }),
            ("remote", "france"),
        )
        self.assertEqual(
            classify_france_travail_work_mode({
                "intitule": "Remote Engineer",
                "description": "Poste entièrement à distance.",
                "teletravail": "Télétravail possible selon accord",
                "lieuTravail": {"libelle": "75 - Paris", "codePostal": "75001"},
            }),
            ("unknown", "unknown"),
        )
        self.assertEqual(
            classify_france_travail_work_mode({
                "intitule": "Engineer",
                "description": "Poste entièrement à distance.",
                "teletravail": "Non précisé",
                "lieuTravail": {"libelle": "France"},
            }),
            ("remote", "france"),
        )
