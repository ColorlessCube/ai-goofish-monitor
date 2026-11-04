"""Durable at-most-once mutation receipts, not an automatic retry queue.

Only hashes of arguments are stored. Interrupted writes stay unknown and are
never reclaimed, because a lost response does not prove a lost side effect.
"""
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def state_dir() -> Path:
    path = Path(os.environ.get("GOOFISH_MCP_STATE_DIR", "data/mcp"))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError("mcp_state_symlink_denied")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def secure_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise RuntimeError("mcp_state_permissions_invalid")
    finally:
        os.close(fd)


@contextmanager
def mutation_lock():
    path = state_dir() / "mutation.lock"
    secure_file(path)
    with path.open("r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def connection():
    path = state_dir() / "invocations.sqlite3"
    secure_file(path)
    db = sqlite3.connect(path, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS invocations (
            invocation_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
            request_id TEXT NOT NULL, tool TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            response_json TEXT, UNIQUE(subject,request_id))""")
        db.commit()
        yield db
    finally:
        db.close()


def fingerprint(tool: str, arguments: dict) -> str:
    raw = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def claim(subject: str, request_id: str, tool: str, arguments: dict):
    digest = fingerprint(tool, arguments)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM invocations WHERE subject=? AND request_id=?", (subject, request_id)).fetchone()
        if row:
            db.commit()
            if row["fingerprint"] != digest:
                return "conflict", dict(row)
            return "existing", dict(row)
        now = time.time()
        invocation_id = str(uuid4())
        db.execute("INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)", (invocation_id, subject, request_id, tool, digest, "running", now, now, None))
        db.commit()
        return "new", {"invocation_id": invocation_id, "request_id": request_id, "status": "running"}


def finish(invocation_id: str, status: str, response: dict):
    with connection() as db:
        db.execute("UPDATE invocations SET status=?,updated_at=?,response_json=? WHERE invocation_id=? AND status='running'", (status, time.time(), json.dumps(response, ensure_ascii=False, allow_nan=False), invocation_id))
        db.commit()


def get_receipt(subject: str, request_id: str):
    with connection() as db:
        row = db.execute("SELECT * FROM invocations WHERE subject=? AND request_id=?", (subject, request_id)).fetchone()
    if row is None:
        return None
    # A running row may be live or may have survived a process crash. Never say
    # it did not execute, and never allow automatic resubmission under a new ID.
    return {"invocation_id": row["invocation_id"], "request_id": row["request_id"], "tool": row["tool"], "status": "running_or_unknown" if row["status"] == "running" else row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"], "retry_allowed": False, "result": json.loads(row["response_json"]) if row["response_json"] else None}
