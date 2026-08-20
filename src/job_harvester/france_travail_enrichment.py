from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from job_harvester.job_import import (
    MAX_PAGE_BYTES, JobImportError, _PageParser, _public_opener, _text,
    canonicalize_url,
)
from job_harvester.models import Job


ENRICHMENT_VERSION = 1


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label)))
            self._href = None
            self._label = []


@dataclass(frozen=True, slots=True)
class RemoteEvidence:
    remote_days_per_week: int | None = None
    onsite_days_per_week: int | None = None
    intensity: str = "unknown"


def extract_remote_frequency(text: str) -> RemoteEvidence:
    normalized = re.sub(r"\s+", " ", text.casefold().replace("’", "'")).strip()
    full_pattern = r"(?:100\s*%\s*(?:de\s+)?t[eé]l[eé]travail|full(?:y)?[- ]remote|t[eé]l[eé]travail\s+(?:[àa]\s+)?100\s*%)"
    full = re.search(full_pattern, normalized)
    negated_full = re.search(r"(?:pas\s+de|non|not)\s+(?:poste\s+en\s+)?" + full_pattern, normalized)
    if full and not negated_full:
        return RemoteEvidence(5, 0, "full")
    remote_patterns = (
        r"(?:jusqu[' ]?[àa]\s+)?([1-5])\s+jours?\s+(?:de\s+|en\s+)?t[eé]l[eé]travail(?:\s+par\s+semaine)?",
        r"([1-5])\s+jours?\s*/\s*semaine\s+(?:en\s+)?t[eé]l[eé]travail",
        r"([1-5])\s+remote\s+days?\s+per\s+week",
        r"remote\s+([1-5])\s+days?\s+(?:a|per)\s+week",
    )
    onsite_patterns = (
        r"([1-5])\s+jours?\s+(?:sur\s+site|sur site)(?:\s+par\s+semaine)?",
        r"pr[eé]sence\s+(?:sur\s+site\s+)?([1-5])\s+jours?\s+par\s+semaine",
        r"([1-5])\s+days?\s+onsite(?:\s+per\s+week)?",
    )
    remote = next((int(m.group(1)) for p in remote_patterns if (m := re.search(p, normalized))), None)
    onsite = next((int(m.group(1)) for p in onsite_patterns if (m := re.search(p, normalized))), None)
    effective_remote = remote if remote is not None else (5 - onsite if onsite is not None else None)
    intensity = {5: "full", 4: "mostly_remote", 3: "balanced", 2: "limited", 1: "occasional", 0: "onsite"}.get(effective_remote, "unknown")
    return RemoteEvidence(remote, onsite, intensity)


class FranceTravailEnricher:
    def __init__(self, *, opener: Callable | None = None, timeout: float = 15.0) -> None:
        self.opener = opener or _public_opener
        self.timeout = timeout

    def enrich(self, job: Job) -> Job:
        if job.source != "france_travail":
            return job
        source_url = canonicalize_url(job.source_url or job.url)
        application_url = job.application_url or self._resolve_application_url(source_url)
        evidence = RemoteEvidence()
        enriched = False
        if application_url:
            try:
                html, application_url = self._fetch_html(application_url)
                evidence = self._evidence(html)
                enriched = True
            except (JobImportError, HTTPError, URLError, TimeoutError, OSError):
                pass
        work_mode, full_remote = job.work_mode, job.full_remote
        if evidence.intensity == "full":
            work_mode, full_remote = "remote", True
        elif evidence.intensity in {"mostly_remote", "balanced", "limited", "occasional"}:
            work_mode, full_remote = "hybrid", False
        elif evidence.intensity == "onsite":
            work_mode, full_remote = "onsite", False
        return replace(
            job, source_url=source_url, application_url=application_url,
            remote_days_per_week=evidence.remote_days_per_week,
            onsite_days_per_week=evidence.onsite_days_per_week,
            remote_intensity=evidence.intensity, work_mode=work_mode,
            full_remote=full_remote,
            remote_enriched_at=datetime.now(timezone.utc) if enriched else None,
            remote_enrichment_version=ENRICHMENT_VERSION if enriched else None,
        )

    def _resolve_application_url(self, source_url: str) -> str | None:
        try:
            html, final = self._fetch_html(source_url)
            parser = _LinkParser(); parser.feed(html)
            action = next((urljoin(final, href) for href, label in parser.links
                           if "postuler" in label.casefold() or "postuler" in href.casefold()), None)
            if not action:
                return None
            action = canonicalize_url(action)
            if not self._is_france_travail(action):
                return action
            contact_html, contact_final = self._fetch_html(action, ajax=True)
            if not self._is_france_travail(contact_final):
                return contact_final
            contact = _LinkParser(); contact.feed(contact_html)
            for href, label in contact.links:
                candidate = canonicalize_url(urljoin(contact_final, href))
                relevant = any(word in f"{href} {label}".casefold()
                               for word in ("offre", "recruteur", "postul", "apply", "career", "job"))
                if relevant and not self._is_france_travail(candidate):
                    return candidate
        except (JobImportError, HTTPError, URLError, TimeoutError, OSError):
            return None
        return None

    @staticmethod
    def _is_france_travail(url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host.endswith("francetravail.fr") or host.endswith("francetravail.io")

    def _fetch_html(self, url: str, *, ajax: bool = False) -> tuple[str, str]:
        safe = canonicalize_url(url)
        headers = {"Accept": "application/json,text/html,application/xhtml+xml",
                   "User-Agent": "job-harvester/0.10.2"}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        response = self.opener(Request(safe, headers=headers), timeout=self.timeout)
        with response:
            final = canonicalize_url(getattr(response, "geturl", lambda: safe)())
            content_type = response.headers.get("Content-Type", "")
            if content_type and not any(kind in content_type.casefold() for kind in ("html", "text/plain", "json")):
                raise JobImportError("application URL did not return HTML")
            data = response.read(MAX_PAGE_BYTES + 1)
            if len(data) > MAX_PAGE_BYTES:
                raise JobImportError("application page exceeds the 2 MB limit")
            charset = response.headers.get_content_charset() or "utf-8"
        try:
            decoded = data.decode(charset, errors="replace")
        except LookupError:
            decoded = data.decode("utf-8", errors="replace")
        if "json" in content_type.casefold():
            try:
                payload = json.loads(decoded)
            except json.JSONDecodeError as error:
                raise JobImportError("application endpoint returned invalid JSON") from error
            fragments: list[str] = []
            def collect(value: object) -> None:
                if isinstance(value, str) and "href" in value.casefold():
                    fragments.append(value)
                elif isinstance(value, list):
                    for child in value: collect(child)
                elif isinstance(value, dict):
                    for child in value.values(): collect(child)
            collect(payload)
            decoded = " ".join(fragments)
        return decoded, final

    @staticmethod
    def _evidence(html: str) -> RemoteEvidence:
        parser = _PageParser(); parser.feed(html)
        visible = _text(" ".join(parser.visible)) or ""
        structured = " ".join(parser.json_ld)
        return extract_remote_frequency(f"{visible} {structured}")
