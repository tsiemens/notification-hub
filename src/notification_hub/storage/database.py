from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from notification_hub.domain import format_timestamp
from notification_hub.storage.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


class MigrationError(RuntimeError):
    pass


class DatabaseSecurityError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path, *, strict_permissions: bool = True) -> None:
        # Do not resolve here: strict mode must be able to detect a symlink at the
        # configured location rather than silently following it.
        self.path = path.expanduser().absolute()
        self.strict_permissions = strict_permissions

    def initialize(self) -> None:
        self._prepare_path()
        connection = self.connect()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = connection.execute(
                "SELECT coalesce(max(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            if current > LATEST_SCHEMA_VERSION:
                raise MigrationError(
                    f"database schema {current} is newer than supported {LATEST_SCHEMA_VERSION}"
                )
            for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
                applied_at = format_timestamp(datetime.now(UTC))
                for statement in _sql_statements(MIGRATIONS[version - 1]):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, applied_at),
                )
            connection.commit()
        except MigrationError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise MigrationError(f"database migration failed: {exc}") from exc
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def _prepare_path(self) -> None:
        if self.path.is_symlink():
            raise DatabaseSecurityError(f"database path may not be a symlink: {self.path}")
        parent = self.path.parent
        if parent.is_symlink():
            raise DatabaseSecurityError(f"database directory may not be a symlink: {parent}")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.strict_permissions:
            mode = stat.S_IMODE(parent.stat().st_mode)
            if mode & 0o077:
                raise DatabaseSecurityError(
                    f"database directory must not be accessible by group/others: {parent}"
                )
            if self.path.exists() and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise DatabaseSecurityError(
                    f"database must not be accessible by group/others: {self.path}"
                )
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()


def _sql_statements(script: str) -> Iterator[str]:
    """Split a trusted migration script using SQLite's own completeness parser."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    if pending.strip():
        raise MigrationError("migration contains an incomplete SQL statement")
