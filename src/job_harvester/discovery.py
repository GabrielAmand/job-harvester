import json
from pathlib import Path
import re
from urllib.parse import urlparse


URL_PATTERN = re.compile(
    r"(?:https?://)?(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|"
    r"jobs\.lever\.co)/[^\s\"'<>]+",
    re.IGNORECASE,
)


class DiscoveryError(ValueError):
    """Raised when a discovery input cannot be read or parsed."""


def board_from_url(value: str) -> tuple[str, str] | None:
    candidate = value.strip().rstrip('.,;:!?)]}"')
    if not re.match(r"https?://", candidate, re.IGNORECASE):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    slug = parts[0].casefold()
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return "greenhouse", slug
    if host == "jobs.lever.co":
        return "lever", slug
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for value_item in value for item in _strings(value_item)]
    if isinstance(value, dict):
        return [item for value_item in value.values() for item in _strings(value_item)]
    return []


def discover_boards(path: str | Path) -> list[tuple[str, str]]:
    input_path = Path(path)
    text = input_path.read_text(encoding="utf-8")
    if input_path.suffix.casefold() == ".json":
        try:
            candidates = _strings(json.loads(text))
        except json.JSONDecodeError as error:
            raise DiscoveryError(f"invalid discovery JSON: {error}") from error
    else:
        candidates = [text]
    discovered: set[tuple[str, str]] = set()
    for candidate in candidates:
        for match in URL_PATTERN.finditer(candidate):
            board = board_from_url(match.group(0))
            if board is not None:
                discovered.add(board)
    return sorted(discovered)
