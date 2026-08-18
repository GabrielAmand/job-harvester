from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from job_harvester.collectors.base import Opener
from job_harvester.collectors.france_travail import FranceTravailCollector
from job_harvester.collectors.greenhouse import GreenhouseCollector
from job_harvester.collectors.lever import LeverCollector
from job_harvester.models import Job
from job_harvester.revalidation import FRANCE_TRAVAIL_DETAIL_URL, JobRevalidator
from job_harvester.storage import JobStore, _timestamp
from job_harvester.work_mode import classify_work_mode

MAX_PAGE_BYTES = 2_000_000
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class JobImportError(ValueError):
    pass


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        canonicalize_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_opener(request: Request, *, timeout: float):
    return build_opener(_SafeRedirectHandler()).open(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class ImportResult:
    job_id: int
    job: Job
    existed: bool
    state: str


def canonicalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise JobImportError(f"invalid job URL: {error}") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise JobImportError("job URL must be an absolute http(s) URL")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise JobImportError("local job URLs are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise JobImportError("private or local job URLs are not allowed")
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in TRACKING_PARAMETERS
        ),
        doseq=True,
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), authority, path, query, ""))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.visible: list[str] = []
        self._capture: str | None = None
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta.setdefault(key.casefold(), values["content"].strip())
        if tag == "script" and "ld+json" in values.get("type", "").casefold():
            self._capture = "json"
        elif tag == "title":
            self._capture = "title"
        elif tag in {"h1", "h2"}:
            self._capture = "heading"
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden = max(0, self._hidden - 1)
        if (tag == "script" and self._capture == "json") or (
            tag == "title" and self._capture == "title"
        ) or (tag in {"h1", "h2"} and self._capture == "heading"):
            self._capture = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._capture == "json":
            self.json_ld.append(data)
        elif self._capture == "title":
            self.title_parts.append(text)
        elif self._capture == "heading":
            self.headings.append(text)
        if not self._hidden:
            self.visible.append(text)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()
    return result or None


def _job_posting(documents: list[str]) -> dict[str, Any]:
    def walk(value: object) -> dict[str, Any] | None:
        if isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                return value
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    for document in documents:
        try:
            found = walk(json.loads(document))
        except (json.JSONDecodeError, TypeError):
            continue
        if found:
            return found
    return {}


def _organization(value: object) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _location(value: object) -> str:
    items = value if isinstance(value, list) else [value]
    locations: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if isinstance(address, str):
            part = _text(address)
        elif isinstance(address, dict):
            part = ", ".join(
                str(address[key]).strip()
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(key)
            )
        else:
            part = None
        if part and part not in locations:
            locations.append(part)
    return " / ".join(locations)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


class JobImporter:
    def __init__(self, *, opener: Opener | None = None, timeout: float = 15.0) -> None:
        self.opener = opener or _public_opener
        self.timeout = timeout

    def import_url(
        self,
        value: str,
        database: str | Path,
        *,
        prompt: Callable[[str], str] | None = None,
    ) -> ImportResult:
        canonical = canonicalize_url(value)
        identity = self._supported_identity(canonical)
        with JobStore(database) as store:
            duplicate = self._find_duplicate(store.connection, canonical, identity)
            if duplicate:
                return duplicate
        job, final_url = self._resolve(canonical, identity)
        final_url = canonicalize_url(final_url)
        job = replace(job, url=final_url)
        job = self._complete_identity(job, prompt)
        return self._persist(database, job, final_url)

    @staticmethod
    def _supported_identity(url: str) -> tuple[str, str, str | None] | None:
        parsed = urlsplit(url)
        host, path = parsed.hostname or "", parsed.path
        greenhouse = re.search(r"/(?:jobs/)?(\d+)(?:/|$)", path)
        if "greenhouse.io" in host and greenhouse:
            parts = [part for part in path.split("/") if part]
            token = parts[0] if parts and parts[0] not in {"jobs"} else None
            return "greenhouse", greenhouse.group(1), token
        lever = re.match(r"/([^/]+)/([0-9a-fA-F-]{8,})(?:/|$)", path)
        if host in {"jobs.lever.co", "api.lever.co"} and lever:
            return "lever", lever.group(2), lever.group(1)
        ft = re.search(r"/detail/([A-Za-z0-9]+)(?:/|$)", path)
        if ft is None:
            ft = re.search(r"/offres/([A-Za-z0-9]+)(?:/|$)", path)
        if host.endswith("francetravail.fr") and ft:
            return "france_travail", ft.group(1), None
        return None

    def _resolve(self, url: str, identity: tuple[str, str, str | None] | None) -> tuple[Job, str]:
        if not identity:
            return self._generic(url)
        source, external_id, source_key = identity
        if source == "greenhouse":
            api = ("https://boards-api.greenhouse.io/v1/boards/" +
                   f"{quote(source_key or '', safe='')}/jobs/{quote(external_id, safe='')}")
            payload, _ = self._json(api, "Greenhouse job")
            if not isinstance(payload, dict):
                raise JobImportError("Greenhouse job response must be an object")
            company = _text(payload.get("company_name")) or source_key or ""
            return GreenhouseCollector(company, source_key or "")._normalize_job(payload, 0), url
        if source == "lever":
            api = f"https://api.lever.co/v0/postings/{quote(source_key or '', safe='')}/{quote(external_id, safe='')}"
            payload, _ = self._json(api, "Lever job")
            if not isinstance(payload, dict):
                raise JobImportError("Lever job response must be an object")
            company = _text(payload.get("company")) or source_key or ""
            return LeverCollector(company, source_key or "")._normalize_job(payload, 0), url
        revalidator = JobRevalidator(timeout=self.timeout, opener=self.opener)
        token = revalidator._authenticate_france_travail()
        payload, _ = self._json(
            FRANCE_TRAVAIL_DETAIL_URL + quote(external_id, safe=""),
            "France Travail offer", authorization=f"Bearer {token}",
        )
        if not isinstance(payload, dict):
            raise JobImportError("France Travail offer response must be an object")
        return FranceTravailCollector(())._normalize_job(payload, "manual import"), url

    def _request(self, url: str, *, accept: str, authorization: str | None = None):
        headers = {"Accept": accept, "User-Agent": "job-harvester/0.10.1"}
        if authorization:
            headers["Authorization"] = authorization
        request = Request(url, headers=headers)
        try:
            response = self.opener(request, timeout=self.timeout)
        except HTTPError as error:
            if error.code in {404, 410}:
                raise JobImportError(f"job URL is expired (HTTP {error.code})") from error
            raise JobImportError(f"job fetch returned HTTP {error.code}; retry later") from error
        except (URLError, TimeoutError, OSError) as error:
            raise JobImportError(f"could not fetch job URL; retry later: {error}") from error
        final_url = canonicalize_url(getattr(response, "geturl", lambda: url)())
        return response, final_url

    def _json(self, url: str, label: str, *, authorization: str | None = None) -> tuple[object, str]:
        response, final = self._request(url, accept="application/json", authorization=authorization)
        try:
            with response:
                return json.load(response), final
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise JobImportError(f"{label} returned invalid JSON") from error

    def _generic(self, url: str) -> tuple[Job, str]:
        response, final_url = self._request(url, accept="text/html,application/xhtml+xml")
        try:
            with response:
                content_type = response.headers.get("Content-Type", "")
                if content_type and not any(value in content_type.casefold() for value in ("html", "text/plain")):
                    raise JobImportError("job URL did not return an HTML page")
                data = response.read(MAX_PAGE_BYTES + 1)
        except JobImportError:
            raise
        except OSError as error:
            raise JobImportError(f"could not read job page; retry later: {error}") from error
        if len(data) > MAX_PAGE_BYTES:
            raise JobImportError("job page exceeds the 2 MB import limit")
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            html = data.decode(charset, errors="replace")
        except LookupError:
            html = data.decode("utf-8", errors="replace")
        parser = _PageParser()
        parser.feed(html)
        posting = _job_posting(parser.json_ld)
        title = _text(posting.get("title"))
        company = _organization(posting.get("hiringOrganization"))
        description = _text(posting.get("description"))
        location = _location(posting.get("jobLocation"))
        if not title:
            title = _text(parser.meta.get("og:title") or parser.meta.get("twitter:title"))
        if not company:
            company = _text(
                parser.meta.get("og:site_name") or parser.meta.get("application-name")
            )
        document_title = _text(" ".join(parser.title_parts))
        if not company and document_title:
            # A document title that repeats the already evidenced job title and
            # names a site/organization is a narrow, deterministic fallback.
            for separator in (" – ", " — ", " | "):
                parts = [part.strip() for part in document_title.split(separator) if part.strip()]
                if len(parts) == 2 and title and title.casefold() in parts[0].casefold():
                    company = re.sub(r"^(?:careers?|jobs?|talents?)\s+", "", parts[1], flags=re.I).strip()
                    break
        if not description:
            description = _text(
                parser.meta.get("og:description") or parser.meta.get("description")
            )
        if not title:
            title = _text(parser.headings[0] if parser.headings else document_title)
        visible = _text(" ".join(parser.visible))
        if not company and visible:
            host_labels = (urlsplit(final_url).hostname or "").split(".")
            if len(host_labels) >= 2:
                domain_brand = host_labels[-2]
                match = re.search(rf"\b{re.escape(domain_brand)}\b", visible, re.I)
                if match:
                    company = match.group(0)
        if visible and (not description or len(visible) > len(description)):
            description = visible
        description = description[:100_000] if description else None
        evidence = {
            "title": title or "",
            "location": {"name": location},
            "content": " ".join(
                value for value in (
                    str(posting.get("jobLocationType", "")),
                    str(posting.get("applicantLocationRequirements", "")),
                    description or "",
                ) if value
            ),
        }
        work_mode, scope, full_remote, eligibility = classify_work_mode(evidence)
        return Job(
            source="manual",
            external_id=sha256(final_url.encode()).hexdigest(),
            company=company or "",
            title=title or "",
            location=location,
            url=final_url,
            published_at=_date(posting.get("datePosted")),
            work_mode=work_mode,
            remote_scope=scope,
            full_remote=full_remote,
            remote_eligibility=eligibility,
            description=description,
        ), final_url

    @staticmethod
    def _complete_identity(job: Job, prompt: Callable[[str], str] | None) -> Job:
        values = {"title": job.title.strip(), "company": job.company.strip()}
        for field, label in (("title", "Could not determine job title.\nTitle: "),
                             ("company", "Could not determine company.\nCompany: ")):
            if not values[field] and prompt:
                values[field] = prompt(label).strip()
            if not values[field]:
                raise JobImportError(
                    f"could not determine job {field}; rerun interactively to provide it"
                )
        return replace(job, title=values["title"], company=values["company"])

    @staticmethod
    def _find_duplicate(connection: sqlite3.Connection, canonical: str,
                        identity: tuple[str, str, str | None] | None) -> ImportResult | None:
        row = None
        if identity:
            row = connection.execute(
                "SELECT id FROM jobs WHERE source=? AND external_id=?",
                identity[:2],
            ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT id FROM jobs WHERE canonical_url=? OR url=? ORDER BY id LIMIT 1",
                (canonical, canonical),
            ).fetchone()
        if row is None:
            for job_id, stored_url in connection.execute("SELECT id, url FROM jobs"):
                try:
                    if canonicalize_url(str(stored_url)) == canonical:
                        row = (job_id,)
                        break
                except JobImportError:
                    continue
        return JobImporter._stored_result(connection, int(row[0]), True) if row else None

    @staticmethod
    def _persist(database: str | Path, job: Job, final: str) -> ImportResult:
        with JobStore(database) as store, store.connection:
            duplicate = JobImporter._find_duplicate(store.connection, final,
                                                     (job.source, job.external_id, job.source_key))
            if duplicate:
                return duplicate
            cursor = store.connection.execute(
                """INSERT INTO jobs(source, external_id, company, title, location,
                   work_mode, remote_scope, published_at, url, collected_at, state,
                   source_key, full_remote, remote_eligibility, description, canonical_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)""",
                (job.source, job.external_id, job.company, job.title, job.location,
                 job.work_mode, job.remote_scope, _timestamp(job.published_at), job.url,
                 _timestamp(job.discovered_at()), job.source_key, int(job.full_remote),
                 job.remote_eligibility, job.description, final),
            )
            job_id = int(cursor.lastrowid)
            return ImportResult(job_id, job, False, "new")

    @staticmethod
    def _stored_result(connection: sqlite3.Connection, job_id: int, existed: bool) -> ImportResult:
        row = connection.execute(
            """SELECT source, external_id, company, title, location, url, work_mode,
               remote_scope, published_at, collected_at, source_key, full_remote,
               remote_eligibility, description, state FROM jobs WHERE id=?""", (job_id,)
        ).fetchone()
        assert row is not None
        job = Job(
            source=row[0], external_id=row[1], company=row[2], title=row[3],
            location=row[4], url=row[5], work_mode=row[6], remote_scope=row[7],
            published_at=datetime.fromisoformat(row[8]) if row[8] else None,
            collected_at=datetime.fromisoformat(row[9]) if row[9] else None,
            source_key=row[10], full_remote=bool(row[11]),
            remote_eligibility=row[12], description=row[13],
        )
        return ImportResult(job_id, job, existed, str(row[14]))
