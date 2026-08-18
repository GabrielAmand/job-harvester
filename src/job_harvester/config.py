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
class Config:
    sources: tuple[GreenhouseSource, ...]


def _nonempty_string(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"sources[{index}].{field} must be a non-empty string")
    return value.strip()


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {config_path}") from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not read configuration {config_path}: {error}") from error

    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("configuration must contain at least one [[sources]] entry")

    sources: list[GreenhouseSource] = []
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise ConfigError(f"sources[{index}] must be a table")
        source_type = _nonempty_string(raw.get("type"), "type", index)
        if source_type != "greenhouse":
            raise ConfigError(f"sources[{index}].type is unsupported: {source_type}")
        sources.append(
            GreenhouseSource(
                company=_nonempty_string(raw.get("company"), "company", index),
                board_token=_nonempty_string(raw.get("board_token"), "board_token", index),
            )
        )
    return Config(tuple(sources))
