from __future__ import annotations

import base64
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses
import html
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import urlparse

from job_harvester.applications import ApplicationStore
from job_harvester.storage import _datetime, _timestamp


CLASSIFICATIONS = (
    "acknowledgement", "rejection", "recruiter_reply", "interview_request",
    "assessment_request", "offer_or_next_step", "other", "unknown",
)
ATTENTION_CLASSES = {
    "rejection", "recruiter_reply", "interview_request", "assessment_request",
    "offer_or_next_step", "unknown",
}


class MailError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    provider_message_id: str
    provider_thread_id: str
    internal_date_ms: int
    received_at: datetime
    sender: str
    recipients: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: int
    provider_message_id: str
    provider_thread_id: str
    received_at: datetime
    sender: str
    recipients: str
    subject: str
    body: str
    matched_job_id: int | None
    matched_application_id: int | None
    company: str | None
    title: str | None
    classification: str
    match_confidence: str | None
    match_reason: str | None
    processing_state: str
    requires_attention: bool


SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    provider_thread_id TEXT NOT NULL,
    internal_date_ms INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    matched_job_id INTEGER,
    matched_application_id INTEGER,
    classification TEXT NOT NULL,
    match_confidence TEXT,
    match_reason TEXT,
    processing_state TEXT NOT NULL,
    requires_attention INTEGER NOT NULL DEFAULT 0,
    manually_linked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, provider_message_id),
    FOREIGN KEY(matched_job_id) REFERENCES jobs(id),
    FOREIGN KEY(matched_application_id) REFERENCES applications(job_id)
);
CREATE INDEX IF NOT EXISTS mail_messages_thread
    ON mail_messages(provider, provider_thread_id);
CREATE INDEX IF NOT EXISTS mail_messages_attention
    ON mail_messages(requires_attention, received_at);
CREATE TABLE IF NOT EXISTS mail_sync_state (
    provider TEXT PRIMARY KEY,
    last_internal_date_ms INTEGER,
    last_successful_sync_at TEXT
);
"""


def _fold(value: str) -> str:
    return " ".join(
        "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                if not unicodedata.combining(c)).split()
    )


RULES = (
    ("rejection", (
        "will not be moving forward", "not moving forward", "unable to proceed",
        "nous ne donnerons pas suite", "ne pas donner suite", "candidature non retenue",
    )),
    ("assessment_request", (
        "coding assessment", "technical assessment", "technical test", "take-home test",
        "test technique", "test de recrutement", "exercice technique",
    )),
    ("interview_request", (
        "schedule an interview", "interview availability", "interview invitation",
        "entretien", "vos disponibilites", "quelles sont vos disponibilites",
    )),
    ("offer_or_next_step", (
        "job offer", "offer letter", "prochaine etape", "next step in the process",
        "next stage of the process",
    )),
    ("acknowledgement", (
        "we received your application", "application has been received",
        "thank you for applying", "candidature bien recue", "candidature est bien recue",
        "avons bien recu votre candidature",
        "accusons reception de votre candidature",
    )),
)


def classify(
    subject: str, body: str, *, matched_thread: bool = False, sender: str = ""
) -> str:
    text = _fold(f"{subject}\n{body}")
    for name, phrases in RULES:
        if any(phrase in text for phrase in phrases):
            return name
    if matched_thread and body.strip() and not _automated_sender(sender):
        return "recruiter_reply"
    return "unknown"


def _automated_sender(sender: str) -> bool:
    folded = sender.casefold()
    return any(value in folded for value in ("no-reply", "noreply", "do-not-reply"))


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _body_part(payload: dict) -> str:
    candidates: list[tuple[int, str]] = []
    def visit(part: dict) -> None:
        mime = str(part.get("mimeType", ""))
        data = part.get("body", {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
                if mime == "text/html":
                    decoded = re.sub(r"<[^>]+>", " ", html.unescape(decoded))
                candidates.append((0 if mime == "text/plain" else 1, decoded))
            except (ValueError, TypeError):
                pass
        for child in part.get("parts", []):
            visit(child)
    visit(payload)
    return " ".join(sorted(candidates)[0][1].split()) if candidates else ""


def normalize_gmail_message(raw: dict) -> NormalizedMessage:
    try:
        headers = {
            str(item["name"]).casefold(): _decode(str(item.get("value", "")))
            for item in raw["payload"].get("headers", [])
        }
        timestamp = int(raw["internalDate"])
        return NormalizedMessage(
            provider_message_id=str(raw["id"]), provider_thread_id=str(raw["threadId"]),
            internal_date_ms=timestamp,
            received_at=datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc),
            sender=headers.get("from", ""), recipients=headers.get("to", ""),
            subject=headers.get("subject", ""), body=_body_part(raw["payload"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MailError("Gmail returned an invalid message") from error


class MailStore(AbstractContextManager["MailStore"]):
    def __init__(self, path: str | Path) -> None:
        with ApplicationStore(path):
            pass
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(application_events)")}
        for name, definition in (
            ("event_type", "TEXT"), ("mail_message_id", "INTEGER"),
            ("event_metadata", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE application_events ADD COLUMN {name} {definition}")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS application_events_mail_message "
            "ON application_events(mail_message_id) WHERE mail_message_id IS NOT NULL"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def cursor(self) -> int | None:
        row = self.connection.execute(
            "SELECT last_internal_date_ms FROM mail_sync_state WHERE provider='gmail'"
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def ingest(
        self, messages: list[NormalizedMessage], *, successful_cursor_ms: int | None = None
    ) -> tuple[int, int]:
        inserted = 0
        matched = 0
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            for message in messages:
                if self.connection.execute(
                    "SELECT 1 FROM mail_messages WHERE provider='gmail' AND provider_message_id=?",
                    (message.provider_message_id,),
                ).fetchone():
                    continue
                job_id, confidence, reason, inherited = self._match(message)
                classification = classify(
                    message.subject, message.body, matched_thread=inherited,
                    sender=message.sender,
                )
                attention = classification in ATTENTION_CLASSES or job_id is None
                state = "unmatched" if job_id is None else ("attention" if attention else "processed")
                cursor = self.connection.execute(
                    """INSERT INTO mail_messages(
                    provider, provider_message_id, provider_thread_id, internal_date_ms,
                    received_at, sender, recipients, subject, body, matched_job_id,
                    matched_application_id, classification, match_confidence, match_reason,
                    processing_state, requires_attention, created_at, updated_at
                    ) VALUES ('gmail', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (message.provider_message_id, message.provider_thread_id,
                     message.internal_date_ms, _timestamp(message.received_at), message.sender,
                     message.recipients, message.subject, message.body, job_id, job_id,
                     classification, confidence, reason, state, int(attention), now, now),
                )
                inserted += 1
                if job_id is not None:
                    matched += 1
                    self._application_event(job_id, cursor.lastrowid, classification, now)
            maximum = max(
                (m.internal_date_ms for m in messages),
                default=successful_cursor_ms if successful_cursor_ms is not None else self.cursor() or 0,
            )
            if successful_cursor_ms is not None:
                maximum = max(maximum, successful_cursor_ms)
            current = self.cursor() or 0
            self.connection.execute(
                """INSERT INTO mail_sync_state(provider, last_internal_date_ms, last_successful_sync_at)
                VALUES ('gmail', ?, ?) ON CONFLICT(provider) DO UPDATE SET
                last_internal_date_ms=MAX(mail_sync_state.last_internal_date_ms, excluded.last_internal_date_ms),
                last_successful_sync_at=excluded.last_successful_sync_at""",
                (max(maximum, current), now),
            )
        return inserted, matched

    def _match(self, message: NormalizedMessage) -> tuple[int | None, str | None, str | None, bool]:
        thread = self.connection.execute(
            """SELECT matched_job_id, manually_linked FROM mail_messages
            WHERE provider='gmail' AND provider_thread_id=? AND matched_job_id IS NOT NULL
            ORDER BY manually_linked DESC, id DESC LIMIT 1""",
            (message.provider_thread_id,),
        ).fetchone()
        if thread:
            return int(thread[0]), "high", "existing Gmail thread association", True
        candidates = self.connection.execute(
            """SELECT a.job_id, COALESCE(a.applied_company,j.company),
                      COALESCE(a.applied_title,j.title), COALESCE(a.applied_job_url,j.url)
            FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.state='applied'"""
        ).fetchall()
        sender_addresses = [address.casefold() for _, address in getaddresses([message.sender])]
        sender_domain = sender_addresses[0].rsplit("@", 1)[-1] if sender_addresses else ""
        text = _fold(f"{message.subject} {message.body}")
        strong: list[int] = []
        for job_id, company, title, url in candidates:
            company_words = [w for w in re.findall(r"[a-z0-9]+", _fold(str(company))) if len(w) >= 3]
            host = (urlparse(str(url)).hostname or "").casefold()
            company_hit = bool(company_words) and any(w in text for w in company_words)
            domain_hit = bool(sender_domain and (
                sender_domain == host or sender_domain.endswith("." + host)
                or any(w in sender_domain for w in company_words)
            ))
            title_hit = _fold(str(title)) in text
            if (company_hit and domain_hit) or (company_hit and title_hit):
                strong.append(int(job_id))
        unique = sorted(set(strong))
        if len(unique) == 1:
            return unique[0], "high", "unique company/domain or company/title match", False
        return None, None, "ambiguous or insufficient deterministic evidence", False

    def _application_event(self, job_id: int, mail_id: int, classification: str, now: str | None) -> None:
        row = self.connection.execute("SELECT state FROM applications WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return
        state = str(row[0])
        self.connection.execute(
            """INSERT OR IGNORE INTO application_events(
            job_id, from_state, to_state, event_at, reason, event_type, mail_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, state, state, now, f"Gmail: {classification.replace('_', ' ')}",
             f"mail.{classification}", mail_id),
        )

    def list(self, *, attention_only: bool = False) -> list[MailMessage]:
        where = "WHERE m.requires_attention=1" if attention_only else ""
        rows = self.connection.execute(_MAIL_QUERY + f" {where} ORDER BY m.received_at DESC, m.id DESC").fetchall()
        return [_mail(row) for row in rows]

    def get(self, mail_id: int) -> MailMessage:
        row = self.connection.execute(_MAIL_QUERY + " WHERE m.id=?", (mail_id,)).fetchone()
        if row is None:
            raise MailError(f"stored mail not found: {mail_id}")
        return _mail(row)

    def link(self, mail_id: int, job_id: int) -> None:
        message = self.get(mail_id)
        application = self.connection.execute(
            "SELECT state FROM applications WHERE job_id=?", (job_id,)
        ).fetchone()
        if application is None:
            raise MailError(f"application not found for job: {job_id}")
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                """UPDATE mail_messages SET matched_job_id=?, matched_application_id=?,
                match_confidence='manual', match_reason='manually linked by user',
                manually_linked=1, processing_state='attention', requires_attention=1,
                updated_at=? WHERE id=?""", (job_id, job_id, now, mail_id),
            )
            self.connection.execute("DELETE FROM application_events WHERE mail_message_id=?", (mail_id,))
            self._application_event(job_id, mail_id, message.classification, now)

    def unlink(self, mail_id: int) -> None:
        self.get(mail_id)
        now = _timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute("DELETE FROM application_events WHERE mail_message_id=?", (mail_id,))
            self.connection.execute(
                """UPDATE mail_messages SET matched_job_id=NULL, matched_application_id=NULL,
                match_confidence=NULL, match_reason='manually unlinked by user', manually_linked=0,
                processing_state='unmatched', requires_attention=1, updated_at=? WHERE id=?""",
                (now, mail_id),
            )


_MAIL_QUERY = """
SELECT m.id, m.provider_message_id, m.provider_thread_id, m.received_at, m.sender,
       m.recipients, m.subject, m.body, m.matched_job_id, m.matched_application_id,
       j.company, j.title, m.classification, m.match_confidence, m.match_reason,
       m.processing_state, m.requires_attention
FROM mail_messages m LEFT JOIN jobs j ON j.id=m.matched_job_id
"""


def _mail(row: tuple[object, ...]) -> MailMessage:
    return MailMessage(
        id=int(row[0]), provider_message_id=str(row[1]), provider_thread_id=str(row[2]),
        received_at=_datetime(str(row[3])), sender=str(row[4]), recipients=str(row[5]),
        subject=str(row[6]), body=str(row[7]),
        matched_job_id=int(row[8]) if row[8] is not None else None,
        matched_application_id=int(row[9]) if row[9] is not None else None,
        company=str(row[10]) if row[10] is not None else None,
        title=str(row[11]) if row[11] is not None else None, classification=str(row[12]),
        match_confidence=str(row[13]) if row[13] is not None else None,
        match_reason=str(row[14]) if row[14] is not None else None,
        processing_state=str(row[15]), requires_attention=bool(row[16]),
    )
