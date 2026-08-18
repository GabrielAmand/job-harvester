from dataclasses import dataclass, field
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
class CareerOpsConfig:
    enabled: bool = False
    repository_path: Path | None = None
    node_command: str = "node"
    batch_size: int = 20


@dataclass(frozen=True, slots=True)
class EmailConfig:
    provider: str = "gmail"
    address: str | None = None
    client_secret_path: Path = field(default_factory=lambda: Path(
        "~/.config/job-harvester/google/client_secret.json"
    ).expanduser())
    token_path: Path = field(default_factory=lambda: Path(
        "~/.config/job-harvester/google/token.json"
    ).expanduser())
    initial_lookback_days: int = 90


@dataclass(frozen=True, slots=True)
class Config:
    sources: tuple[GreenhouseSource | LeverSource | FranceTravailSource, ...]
    filters: Filters
    career_ops: CareerOpsConfig = CareerOpsConfig()
    email: EmailConfig = EmailConfig()


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


def _career_ops_config(raw: object) -> CareerOpsConfig:
    if raw is None:
        return CareerOpsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("career_ops must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("career_ops.enabled must be a boolean")
    repository = raw.get("repository_path")
    if repository is not None and (
        not isinstance(repository, str) or not repository.strip()
    ):
        raise ConfigError("career_ops.repository_path must be a non-empty string")
    if enabled and repository is None:
        raise ConfigError("career_ops.repository_path is required when enabled")
    node_command = raw.get("node_command", "node")
    if not isinstance(node_command, str) or not node_command.strip():
        raise ConfigError("career_ops.node_command must be a non-empty string")
    batch_size = raw.get("batch_size", 20)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ConfigError("career_ops.batch_size must be a positive integer")
    return CareerOpsConfig(
        enabled=enabled,
        repository_path=Path(repository).expanduser() if repository is not None else None,
        node_command=node_command.strip(),
        batch_size=batch_size,
    )


def _email_config(raw: object) -> EmailConfig:
    if raw is None:
        return EmailConfig()
    if not isinstance(raw, dict):
        raise ConfigError("email must be a table")
    provider = raw.get("provider", "gmail")
    if provider != "gmail":
        raise ConfigError('email.provider must be "gmail"')
    address = raw.get("address")
    if address is not None and (not isinstance(address, str) or "@" not in address):
        raise ConfigError("email.address must be a valid email address")
    defaults = EmailConfig()
    client_path = raw.get("client_secret_path", str(defaults.client_secret_path))
    token_path = raw.get("token_path", str(defaults.token_path))
    for name, value in (("client_secret_path", client_path), ("token_path", token_path)):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"email.{name} must be a non-empty path")
    lookback = raw.get("initial_lookback_days", 90)
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback <= 0:
        raise ConfigError("email.initial_lookback_days must be a positive integer")
    return EmailConfig(
        provider="gmail", address=address.strip() if address else None,
        client_secret_path=Path(client_path).expanduser(),
        token_path=Path(token_path).expanduser(), initial_lookback_days=lookback,
    )


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
    return Config(
        tuple(sources), filters, _career_ops_config(document.get("career_ops")),
        _email_config(document.get("email")),
    )
