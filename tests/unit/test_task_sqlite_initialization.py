"""Startup WAL transition can return SQLITE_BUSY without invoking its handler."""
import sqlite3

import pytest

from src.infrastructure.persistence.sqlite_connection import _apply_pragmas


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_wal_transition_retries_transient_lock(tmp_path, code):
    class ContendedConnection(sqlite3.Connection):
        attempts = 0

        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode=WAL":
                self.attempts += 1
                if self.attempts == 1:
                    error = sqlite3.OperationalError("fixture WAL transition contention")
                    error.sqlite_errorcode = code
                    raise error
            return super().execute(statement, *args)

    conn = sqlite3.connect(tmp_path / "fixture.sqlite", factory=ContendedConnection)
    try:
        _apply_pragmas(conn)
        assert conn.attempts == 2
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_wal_initialization_does_not_retry_io_failure(tmp_path):
    class BrokenConnection(sqlite3.Connection):
        attempts = 0

        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode=WAL":
                self.attempts += 1
                error = sqlite3.OperationalError("fixture I/O failure")
                error.sqlite_errorcode = sqlite3.SQLITE_IOERR
                raise error
            return super().execute(statement, *args)

    conn = sqlite3.connect(tmp_path / "fixture.sqlite", factory=BrokenConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="I/O failure"):
            _apply_pragmas(conn)
        assert conn.attempts == 1
    finally:
        conn.close()
