from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import re
import unicodedata
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(unescape(value))
    text = " ".join(parser.parts)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", text.casefold()).strip()


REMOTE = re.compile(
    r"\b(?:fully remote|full remote|remote[- ]first|work(?:ing)? from home|wfh|"
    r"home[- ]based|distributed|100 ?% (?:remote|teletravail)|teletravail|a distance|remote)\b"
)
DESCRIPTION_REMOTE = re.compile(
    r"\b(?:fully remote|full remote|remote[- ]first|work(?:ing)? from home|wfh|"
    r"home[- ]based|100 ?% (?:remote|teletravail)|teletravail|a distance|"
    r"remote (?:role|position|job|work|team)|work remotely|distributed (?:team|workforce|company))\b"
)
HYBRID = re.compile(r"\b(?:hybrid|hybride|remote[- ]friendly)\b")
ONSITE = re.compile(r"\b(?:on[- ]?site|onsite|sur site|presentiel|office[- ]based)\b")
MIXED = re.compile(r"\b(?:or|ou)\s+(?:remote|a distance|teletravail)\b")
DESCRIPTION_HYBRID = re.compile(
    r"\b(?:hybrid (?:role|position|job|work|workplace|schedule|team)|"
    r"(?:work|working) hybrid|hybride|travail hybride|mode hybride)\b"
)
DESCRIPTION_ONSITE = re.compile(
    r"\b(?:on[- ]?site (?:role|position|job|work)|work on[- ]?site|sur site|"
    r"presentiel|office[- ]based)\b"
)


def _mode(text: str, *, description: bool = False) -> str | None:
    if not text:
        return None
    hybrid_pattern = DESCRIPTION_HYBRID if description else HYBRID
    onsite_pattern = DESCRIPTION_ONSITE if description else ONSITE
    if hybrid_pattern.search(text) or MIXED.search(text):
        return "hybrid"
    remote = bool((DESCRIPTION_REMOTE if description else REMOTE).search(text))
    onsite = bool(onsite_pattern.search(text))
    if remote and onsite:
        return "unknown"
    if remote:
        return "remote"
    if onsite:
        return "onsite"
    return None


def _metadata_text(metadata: object) -> str:
    if not isinstance(metadata, list):
        return ""
    values: list[str] = []
    for item in metadata:
        if not isinstance(item, dict):
            continue
        label = item.get("name", item.get("label"))
        if not isinstance(label, str) or not re.search(
            r"remote|work.?mode|workplace|location.?type|teletravail|hybrid", _plain(label)
        ):
            continue
        for key in ("value", "values"):
            value = item.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(part for part in value if isinstance(part, str))
    return _plain(" ".join(values))


def _office_text(offices: object) -> str:
    if not isinstance(offices, list):
        return ""
    values: list[str] = []
    for office in offices:
        if isinstance(office, dict):
            values.extend(
                value for key in ("name", "location")
                if isinstance((value := office.get(key)), str)
            )
    return _plain(" ".join(values))


def _scope(text: str, location: str, work_mode: str) -> str:
    if re.search(r"\b(?:france only|only in france|based in france|basee? en france|within france|remote\W+france)\b", text):
        return "france"
    if re.search(r"\b(?:europe only|only in europe|within europe|european union|remote\W+europe|eu only)\b", text):
        return "europe"
    if re.search(
        r"\b(?:us only|u\.s\. only|united states only|remote[- ]friendly\W+united states|"
        r"remote\W+(?:us|u\.s\.|united states|uk|canada)|uk only|canada only|apac|emea|"
        r"must (?:be based|reside|live)|eligible (?:in|to work)|within [a-z]+ time zones?|"
        r"[a-z]+ time zones? only|remote in (?!france\b|europe\b))",
        text,
    ):
        return "restricted"
    if re.search(
        r"\b(?:(?:fully |full )?remote\W+worldwide|worldwide remote|global remote|"
        r"work(?:ing)? from anywhere in the world|remote from anywhere in the world)\b",
        text,
    ):
        return "worldwide"
    if work_mode == "remote" and location and not re.fullmatch(
        r"(?:remote|fully remote|full remote|a distance|teletravail)", location
    ):
        return "restricted"
    return "unknown"


def classify_work_mode(raw: dict[str, Any]) -> tuple[str, str]:
    """Classify only explicit Greenhouse job-post evidence, by source priority."""
    location = raw.get("location")
    location_name = location.get("name") if isinstance(location, dict) else ""
    title_location = _plain(
        " ".join(value for value in (raw.get("title"), location_name) if isinstance(value, str))
    )
    offices = _office_text(raw.get("offices"))
    content = _plain(raw.get("content")) if isinstance(raw.get("content"), str) else ""
    evidence = (_metadata_text(raw.get("metadata")), title_location, offices, content)
    work_mode = next(
        (
            result
            for index, text in enumerate(evidence)
            if (result := _mode(text, description=index == 3))
        ),
        "unknown",
    )
    scope = (
        _scope(" ".join(evidence), _plain(location_name), work_mode)
        if work_mode in {"remote", "hybrid"}
        else "unknown"
    )
    return work_mode, scope
