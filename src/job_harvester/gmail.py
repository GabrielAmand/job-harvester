from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

from job_harvester.config import EmailConfig
from job_harvester.mail import MailError, MailStore, normalize_gmail_message


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _imports() -> tuple[Any, Any, Any, Any]:
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise MailError(
            "Gmail dependencies are unavailable; install job-harvester with its V9 dependencies"
        ) from error
    return Credentials, Request, InstalledAppFlow, RefreshError


def _service(credentials: Any) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as error:
        raise MailError("Google API client is unavailable") from error
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _check_secret(path: Path) -> None:
    if not path.is_file():
        raise MailError(
            f"Gmail OAuth client credentials not found: {path}. "
            "Create a Desktop OAuth client and save its JSON file there."
        )


def _write_token(path: Path, credentials: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def authorize(config: EmailConfig) -> str:
    _check_secret(config.client_secret_path)
    Credentials, _, InstalledAppFlow, _ = _imports()
    del Credentials
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.client_secret_path), [GMAIL_READONLY_SCOPE]
    )
    credentials = flow.run_local_server(port=0)
    _write_token(config.token_path, credentials)
    return mailbox_identity(_service(credentials), config.address)


def load_credentials(config: EmailConfig) -> Any:
    _check_secret(config.client_secret_path)
    if not config.token_path.is_file():
        raise MailError(
            f"Gmail token not found: {config.token_path}. Run `job-harvester mail auth`."
        )
    Credentials, Request, _, RefreshError = _imports()
    try:
        credentials = Credentials.from_authorized_user_file(
            str(config.token_path), [GMAIL_READONLY_SCOPE]
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _write_token(config.token_path, credentials)
        if not credentials.valid:
            raise MailError(
                "Gmail authorization is invalid or revoked; run `job-harvester mail auth` again."
            )
        return credentials
    except MailError:
        raise
    except (OSError, ValueError, json.JSONDecodeError, RefreshError) as error:
        raise MailError(
            "Gmail authorization is invalid or revoked; run `job-harvester mail auth` again."
        ) from error


def mailbox_identity(service: Any, expected_address: str | None) -> str:
    try:
        address = str(service.users().getProfile(userId="me").execute()["emailAddress"])
    except Exception as error:
        raise MailError("Gmail API could not read the mailbox profile") from error
    if expected_address and address.casefold() != expected_address.casefold():
        raise MailError(
            f"authenticated Gmail mailbox does not match configured email.address ({expected_address})"
        )
    return address


def status(config: EmailConfig) -> str:
    return mailbox_identity(_service(load_credentials(config)), config.address)


def sync(database: Path, config: EmailConfig, *, service: Any | None = None) -> tuple[int, int, int]:
    gmail = service or _service(load_credentials(config))
    if service is None:
        mailbox_identity(gmail, config.address)
    with MailStore(database) as store:
        sync_started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cursor = store.cursor()
        if cursor is None:
            after = datetime.now(timezone.utc) - timedelta(days=config.initial_lookback_days)
        else:
            after = datetime.fromtimestamp(max(0, cursor - 60_000) / 1000, tz=timezone.utc)
        query = f"after:{int(after.timestamp())} -in:sent"
        message_ids: list[str] = []
        token: str | None = None
        try:
            while True:
                request = gmail.users().messages().list(
                    userId="me", q=query, pageToken=token, maxResults=100
                )
                response = request.execute()
                message_ids.extend(str(item["id"]) for item in response.get("messages", []))
                token = response.get("nextPageToken")
                if not token:
                    break
            normalized = [
                normalize_gmail_message(
                    gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
                )
                for message_id in message_ids
            ]
        except MailError:
            raise
        except Exception as error:
            raise MailError("Gmail sync failed; synchronization state was not advanced") from error
        # A small overlap protects mail arriving near the API query boundary.
        inserted, matched = store.ingest(
            normalized, successful_cursor_ms=sync_started_ms - 60_000
        )
        return inserted, matched, len(message_ids)
