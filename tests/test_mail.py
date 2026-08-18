from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_harvester.applications import ApplicationStore
from job_harvester.config import EmailConfig
from job_harvester.gmail import GMAIL_READONLY_SCOPE, load_credentials, sync
from job_harvester.mail import MailError, MailStore, NormalizedMessage, classify
from job_harvester.models import Job
from job_harvester.storage import JobStore


def message(identity: str, thread: str, subject: str, body: str, *, sender: str = "Recruiter <jobs@acme.com>", timestamp: int = 1_800_000_000_000) -> NormalizedMessage:
    return NormalizedMessage(
        identity, thread, timestamp, datetime.fromtimestamp(timestamp / 1000, timezone.utc),
        sender, "candidate@example.com", subject, body,
    )


class MailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "jobs.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def applied(self, company: str = "Acme", title: str = "Platform Engineer", external: str = "1") -> int:
        with JobStore(self.database) as store:
            store.upsert([Job("greenhouse", external, company, title, "Remote", f"https://jobs.{company.casefold()}.com/{external}")])
            job_id = int(store.connection.execute("SELECT id FROM jobs WHERE external_id=?", (external,)).fetchone()[0])
        with ApplicationStore(self.database) as store:
            now = "2026-08-18T00:00:00+00:00"
            store.connection.execute(
                "INSERT INTO applications(job_id,state,created_at,updated_at) VALUES (?,'applied',?,?)",
                (job_id, now, now),
            )
            store.connection.commit()
        return job_id

    def test_english_and_french_classification(self) -> None:
        cases = (
            ("Thanks", "We received your application", "acknowledgement"),
            ("Candidature", "Votre candidature est bien reçue", "acknowledgement"),
            ("Update", "We will not be moving forward", "rejection"),
            ("Candidature", "Nous ne donnerons pas suite", "rejection"),
            ("Interview", "Please schedule an interview", "interview_request"),
            ("Entretien", "Quelles sont vos disponibilités ?", "interview_request"),
            ("Next", "Please complete the coding assessment", "assessment_request"),
            ("Bonjour", "Votre profil est intéressant", "unknown"),
        )
        for subject, body, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify(subject, body), expected)

    def test_duplicate_is_idempotent_and_thread_id_stable(self) -> None:
        job_id = self.applied()
        item = message("m1", "thread-constant", "Application", "We received your application at Acme")
        with MailStore(self.database) as store:
            self.assertEqual(store.ingest([item]), (1, 1))
            cursor = store.cursor()
            self.assertEqual(store.ingest([item]), (0, 0))
            self.assertEqual(store.cursor(), cursor)
            saved = store.list()[0]
            self.assertEqual(saved.provider_thread_id, "thread-constant")
            self.assertEqual(saved.matched_job_id, job_id)

    def test_existing_thread_wins_and_creates_application_event_without_state_change(self) -> None:
        job_id = self.applied()
        with MailStore(self.database) as store:
            store.ingest([message("m1", "t1", "Application Acme", "We received your application")])
            store.ingest([message("m2", "t1", "Re: Application", "Can we schedule an interview?", sender="Jane <jane@agency.net>")])
            second = store.list()[0]
            self.assertEqual(second.matched_job_id, job_id)
            self.assertEqual(second.match_reason, "existing Gmail thread association")
            state = store.connection.execute("SELECT state FROM applications WHERE job_id=?", (job_id,)).fetchone()[0]
            events = store.connection.execute("SELECT event_type FROM application_events WHERE mail_message_id IS NOT NULL ORDER BY id").fetchall()
        self.assertEqual(state, "applied")
        self.assertEqual(events, [("mail.acknowledgement",), ("mail.interview_request",)])

    def test_ambiguous_company_match_stays_unmatched_and_does_not_mutate_workflow(self) -> None:
        first = self.applied(external="1")
        second = self.applied(external="2")
        with MailStore(self.database) as store:
            store.ingest([message("m1", "t", "Acme Platform Engineer", "Application update")])
            saved = store.list()[0]
            self.assertIsNone(saved.matched_job_id)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM application_events WHERE mail_message_id IS NOT NULL").fetchone()[0], 0)
            states = store.connection.execute("SELECT job_id,state FROM applications ORDER BY job_id").fetchall()
        self.assertEqual(states, [(first, "applied"), (second, "applied")])

    def test_manual_link_and_future_thread_inheritance(self) -> None:
        job_id = self.applied()
        with MailStore(self.database) as store:
            store.ingest([message("m1", "manual-thread", "Hello", "Unrelated", sender="x@example.net")])
            mail_id = store.list()[0].id
            store.link(mail_id, job_id)
            self.assertEqual(store.get(mail_id).match_confidence, "manual")
            store.ingest([message("m2", "manual-thread", "Re: Hello", "Following up", sender="x@example.net")])
            self.assertEqual(store.list()[0].matched_job_id, job_id)
            self.assertEqual(store.list()[0].classification, "recruiter_reply")

    def test_sync_failure_does_not_advance_cursor(self) -> None:
        class BrokenMessages:
            def list(self, **kwargs):
                class Request:
                    def execute(self):
                        raise RuntimeError("private remote detail")
                return Request()
        class Users:
            def messages(self): return BrokenMessages()
        class Service:
            def users(self): return Users()
        with self.assertRaisesRegex(MailError, "not advanced"):
            sync(self.database, EmailConfig(), service=Service())
        with MailStore(self.database) as store:
            self.assertIsNone(store.cursor())

    def test_mocked_gmail_sync_is_incremental_and_idempotent(self) -> None:
        self.applied()
        encoded = base64.urlsafe_b64encode(
            b"We received your application at Acme"
        ).decode().rstrip("=")
        raw = {
            "id": "gmail-1", "threadId": "gmail-thread-1",
            "internalDate": "1800000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "jobs@acme.com"},
                    {"name": "To", "value": "candidate@example.com"},
                    {"name": "Subject", "value": "Application received"},
                ],
                "body": {"data": encoded},
            },
        }
        calls: list[tuple[str, dict]] = []
        class Request:
            def __init__(self, value): self.value = value
            def execute(self): return self.value
        class Messages:
            def list(self, **kwargs):
                calls.append(("list", kwargs))
                return Request({"messages": [{"id": "gmail-1"}]})
            def get(self, **kwargs):
                calls.append(("get", kwargs))
                return Request(raw)
        messages_api = Messages()
        class Users:
            def messages(self): return messages_api
        class Service:
            def users(self): return Users()
        service = Service()
        self.assertEqual(sync(self.database, EmailConfig(), service=service), (1, 1, 1))
        self.assertEqual(sync(self.database, EmailConfig(), service=service), (0, 0, 1))
        self.assertTrue(all("-in:sent" in kwargs["q"] for name, kwargs in calls if name == "list"))
        self.assertFalse(any(name not in {"list", "get"} for name, _ in calls))

    def test_missing_credentials_and_invalid_token_errors_do_not_expose_secret(self) -> None:
        config = EmailConfig(client_secret_path=self.root / "missing.json", token_path=self.root / "token.json")
        with self.assertRaisesRegex(MailError, "credentials not found"):
            load_credentials(config)
        config.client_secret_path.write_text("{}")
        config.token_path.write_text('{"refresh_token":"TOP-SECRET"}')
        fake_credentials = type("Credentials", (), {"from_authorized_user_file": staticmethod(lambda *a, **k: (_ for _ in ()).throw(ValueError("TOP-SECRET")))})
        with patch("job_harvester.gmail._imports", return_value=(fake_credentials, object, object, Exception)):
            with self.assertRaises(MailError) as raised:
                load_credentials(config)
        self.assertNotIn("TOP-SECRET", str(raised.exception))

    def test_scope_is_read_only(self) -> None:
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
