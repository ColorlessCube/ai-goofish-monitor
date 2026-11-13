"""Durable at-most-once-per-hour reservations for ASS source-fault notices.

This is separate from the website circuit breaker. Reserve before submitting a
notification: a lost acknowledgement must not result in an immediate retry.
Only a digest of the task identity and an attempt time are persisted.
"""
import hashlib
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import time


SOURCE_NOTICE_INTERVAL_SECONDS = 3600
MAX_SOURCE_NOTICE_TASKS = 128


def reserve_source_notice(path: str | Path, task_key: str, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    if not math.isfinite(current):
        return False
    path = Path(path)
    identity = hashlib.sha256(task_key.encode('utf-8')).hexdigest()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(str(path), timeout=2)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS attempts '
                               '(task_key TEXT PRIMARY KEY, attempted_at REAL NOT NULL)')
            connection.execute('DELETE FROM attempts WHERE attempted_at <= ?',
                               (current - SOURCE_NOTICE_INTERVAL_SECONDS,))
            if connection.execute('SELECT 1 FROM attempts WHERE task_key = ?',
                                  (identity,)).fetchone():
                return False
            count = connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            if count >= MAX_SOURCE_NOTICE_TASKS:
                return False
            connection.execute('INSERT INTO attempts(task_key, attempted_at) VALUES (?, ?)',
                               (identity, current))
        return True
    except (OSError, sqlite3.Error):
        # Fail closed rather than flooding or exporting a path/exception body.
        return False
