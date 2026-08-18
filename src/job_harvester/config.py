from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True, slots=True)
class GreenhouseSource:
    company: str
    board_token: str
    type: str = "greenhouse"


@dataclass(frozen=True, slots=True)
class LeverSource:
    company: str
    company_slug: str
    type: str = "lever"


@dataclass(frozen=True, slots=True)
class FranceTravailSource:
    search_terms: tuple[str, ...]
    type: str = "france_travail"


@dataclass(frozen=True, slots=True)
class Filters:
    positive_title_keywords: tuple[str, ...] = ()
    negative_title_keywords: tuple[str, ...] = ()
    location_keywords: tuple[str, ...] = ()
    remote_policy: str = "any"
    allow_hybrid: bool = True
    allow_onsite: bool = True
    exclude_incompatible_remote: bool = True
    allow_strong_seniority: bool = False
    excluded_title_phrases: tuple[str, ...] = (
        "program manager",
        "product manager",
        "product management",
        "project manager",
        "engineering manager",
        "director",
        "head of",
        "vice president",
        "vp",
    )


@dataclass(frozen=True, slots=True)
class Config:
    sources: tuple[GreenhouseSource | LeverSource | FranceTravailSource, ...]
    filters: Filters


def _nonempty_string(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"sources[{index}].{field} must be a non-empty string")
    return value.strip()


def _keyword_list(raw: object, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"filters.{field} must be an array of strings")
    keywords: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"filters.{field}[{index}] must be a non-empty string")
        keywords.append(value.strip())
    return tuple(keywords)


def _source_string_list(raw: object, field: str, source_index: int) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"sources[{source_index}].{field} must be a non-empty array of strings"
        )
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"sources[{source_index}].{field}[{index}] must be a non-empty string"
            )
        values.append(value.strip())
    return tuple(values)


def _boolean(raw: object, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"filters.{field} must be a boolean")
    return raw


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {config_path}") from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not read configuration {config_path}: {error}") from error

    raw_sources = document.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("sources must be an array of tables")

    sources: list[GreenhouseSource | LeverSource | FranceTravailSource] = []
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise ConfigError(f"sources[{index}] must be a table")
        source_type = _nonempty_string(raw.get("type"), "type", index)
        if source_type == "greenhouse":
            sources.append(
                GreenhouseSource(
                    company=_nonempty_string(raw.get("company"), "company", index),
                    board_token=_nonempty_string(raw.get("board_token"), "board_token", index),
                )
            )
        elif source_type == "lever":
            sources.append(
                LeverSource(
                    company=_nonempty_string(raw.get("company"), "company", index),
                    company_slug=_nonempty_string(
                        raw.get("company_slug"), "company_slug", index
                    ),
                )
            )
        elif source_type == "france_travail":
            sources.append(
                FranceTravailSource(
                    search_terms=_source_string_list(
                        raw.get("search_terms"), "search_terms", index
                    )
                )
            )
        else:
            raise ConfigError(f"sources[{index}].type is unsupported: {source_type}")
    raw_filters = document.get("filters", {})
    if not isinstance(raw_filters, dict):
        raise ConfigError("filters must be a table")
    remote_policy = raw_filters.get("remote_policy", "any")
    if remote_policy not in {"any", "prefer", "require"}:
        raise ConfigError('filters.remote_policy must be "any", "prefer", or "require"')
    filters = Filters(
        positive_title_keywords=_keyword_list(
            raw_filters.get("positive_title_keywords"), "positive_title_keywords"
        ),
        negative_title_keywords=_keyword_list(
            raw_filters.get("negative_title_keywords"), "negative_title_keywords"
        ),
        location_keywords=_keyword_list(
            raw_filters.get("location_keywords"), "location_keywords"
        ),
        remote_policy=remote_policy,
        allow_hybrid=_boolean(raw_filters.get("allow_hybrid"), "allow_hybrid", True),
        allow_onsite=_boolean(raw_filters.get("allow_onsite"), "allow_onsite", True),
        exclude_incompatible_remote=_boolean(
            raw_filters.get("exclude_incompatible_remote"),
            "exclude_incompatible_remote",
            True,
        ),
        allow_strong_seniority=_boolean(
            raw_filters.get("allow_strong_seniority"),
            "allow_strong_seniority",
            False,
        ),
        excluded_title_phrases=(
            _keyword_list(
                raw_filters.get("excluded_title_phrases"), "excluded_title_phrases"
            )
            if "excluded_title_phrases" in raw_filters
            else Filters().excluded_title_phrases
        ),
    )
    return Config(tuple(sources), filters)
