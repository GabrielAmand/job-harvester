from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


PROVIDERS = {"greenhouse", "lever"}
STATUSES = {"valid", "invalid", "unknown"}

BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('greenhouse', 'lever')),
    slug TEXT NOT NULL COLLATE NOCASE,
    company TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    discovered_at TEXT NOT NULL,
    last_checked_at TEXT,
    validation_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (validation_status IN ('valid', 'invalid', 'unknown')),
    provenance TEXT,
    UNIQUE (provider, slug)
)
"""


class RegistryError(ValueError):
    """Raised when a board registry operation is invalid."""


@dataclass(frozen=True, slots=True)
class Board:
    provider: str
    slug: str
    company: str | None
    enabled: bool
    discovered_at: datetime
    last_checked_at: datetime | None
    validation_status: str
    provenance: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalize(provider: str, slug: str) -> tuple[str, str]:
    provider = provider.strip().casefold()
    slug = slug.strip().strip("/").casefold()
    if provider not in PROVIDERS:
        raise RegistryError(f"unsupported board provider: {provider}")
    if not slug or "/" in slug or any(character.isspace() for character in slug):
        raise RegistryError("board slug must be a non-empty URL path segment")
    return provider, slug


class BoardRegistry(AbstractContextManager["BoardRegistry"]):
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(BOARD_SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def add(
        self,
        provider: str,
        slug: str,
        *,
        company: str | None = None,
        provenance: str | None = None,
    ) -> bool:
        provider, slug = _normalize(provider, slug)
        company = company.strip() if isinstance(company, str) and company.strip() else None
        provenance = (
            provenance.strip()
            if isinstance(provenance, str) and provenance.strip()
            else None
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO boards (
                    provider, slug, company, discovered_at, provenance
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (provider, slug, company, _timestamp(_now()), provenance),
            )
            if not cursor.rowcount and company is not None:
                self.connection.execute(
                    """
                    UPDATE boards SET company = COALESCE(company, ?)
                    WHERE provider = ? AND slug = ?
                    """,
                    (company, provider, slug),
                )
        return bool(cursor.rowcount)

    def list(
        self,
        *,
        enabled_only: bool = False,
        valid_only: bool = False,
    ) -> list[Board]:
        clauses: list[str] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if valid_only:
            clauses.append("validation_status = 'valid'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            """
            SELECT provider, slug, company, enabled, discovered_at,
                   last_checked_at, validation_status, provenance
            FROM boards
            """
            + where
            + " ORDER BY provider, COALESCE(company, slug) COLLATE NOCASE, slug"
        ).fetchall()
        return [
            Board(
                provider=row[0],
                slug=row[1],
                company=row[2],
                enabled=bool(row[3]),
                discovered_at=datetime.fromisoformat(row[4]),
                last_checked_at=(
                    datetime.fromisoformat(row[5]) if row[5] is not None else None
                ),
                validation_status=row[6],
                provenance=row[7],
            )
            for row in rows
        ]

    def get(self, provider: str, slug: str) -> Board | None:
        provider, slug = _normalize(provider, slug)
        return next(
            (
                board
                for board in self.list()
                if board.provider == provider and board.slug.casefold() == slug
            ),
            None,
        )

    def set_enabled(self, provider: str, slug: str, enabled: bool) -> None:
        provider, slug = _normalize(provider, slug)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE boards SET enabled = ? WHERE provider = ? AND slug = ?",
                (int(enabled), provider, slug),
            )
        if not cursor.rowcount:
            raise RegistryError(f"board not found: {provider}/{slug}")

    def record_validation(
        self,
        provider: str,
        slug: str,
        status: str | None,
        *,
        company: str | None = None,
    ) -> None:
        provider, slug = _normalize(provider, slug)
        if status is not None and status not in STATUSES:
            raise RegistryError(f"unsupported validation status: {status}")
        assignments = ["last_checked_at = ?"]
        values: list[object] = [_timestamp(_now())]
        if status is not None:
            assignments.append("validation_status = ?")
            values.append(status)
        if isinstance(company, str) and company.strip():
            assignments.append("company = COALESCE(company, ?)")
            values.append(company.strip())
        values.extend((provider, slug))
        with self.connection:
            cursor = self.connection.execute(
                f"UPDATE boards SET {', '.join(assignments)} "
                "WHERE provider = ? AND slug = ?",
                values,
            )
        if not cursor.rowcount:
            raise RegistryError(f"board not found: {provider}/{slug}")
