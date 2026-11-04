"""Shared prompt boundary for HTTP/MCP/persistence/runtime.

All cooperative writers lock the same directory inode (flock), then acquire SQLite.
Do not hold this blocking lock over async/network work. Call mutations in a worker.
Reads/writes use dirfds and O_NOFOLLOW for every path component and the leaf.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4
from typing import NoReturn

from fastapi import HTTPException


def reject(code, status=400) -> NoReturn:
    raise HTTPException(status_code=status, detail=code)


def prompt_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        reject("unsafe_prompt_path")
    name = value[8:] if value.startswith("prompts/") else value
    if "/" in name or name in {".", ".."} or not name.endswith(".txt"):
        reject("unsafe_prompt_path")
    return name


def canonical_reference(value):
    return "prompts/" + prompt_name(value) if value else ""


def default_root():
    return Path(os.environ.get("GOOFISH_PROMPTS_DIR", "prompts")).absolute()


def resolve_root(app=None):
    return Path(getattr(getattr(app, "state", None), "mcp_prompts_dir", None) or default_root()).absolute()


@contextmanager
def directory(root=None, *, create=False):
    root = Path(root or default_root()).absolute()
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in root.parts[1:]:
            if component in {".", ".."}:
                reject("unsafe_prompt_path")
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            new_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd
    except FileNotFoundError:
        reject("prompt_not_found", 404)
    except OSError:
        reject("unsafe_prompt_path")
    finally:
        os.close(fd)


@contextmanager
def asset_lock(root=None):
    # Directory flock avoids a removable/replaced lock-file inode and artifacts.
    with directory(root, create=True) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def read_at(fd, filename, *, nonempty=True):
    name = prompt_name(filename)
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                reject("unsafe_prompt_path")
            content = stream.read()
    except FileNotFoundError:
        reject("prompt_not_found", 404)
    except (OSError, UnicodeError):
        reject("unsafe_prompt_path")
    if nonempty and not content.strip():
        reject("empty_prompt")
    return content


def read_prompt(filename, root=None, *, nonempty=True):
    with directory(root) as fd:
        return read_at(fd, filename, nonempty=nonempty)


def content_revision(content):
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_at(fd, filename, content, *, create=False):
    name = prompt_name(filename)
    if not content or not content.strip():
        reject("empty_prompt")
    if create:
        try:
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            reject("prompt_already_exists", 409)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        # Check the existing leaf through its descriptor, then atomically replace it.
        read_at(fd, name, nonempty=False)
        temporary = ".prompt-" + uuid4().hex
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
    os.fsync(fd)


def write_prompt(filename, content, root=None, *, create=False):
    with asset_lock(root) as fd:
        write_at(fd, filename, content, create=create)


def references_in_tasks(tasks, filename):
    name = prompt_name(filename)
    result = []
    for task in tasks:
        for field in ("ai_prompt_base_file", "ai_prompt_criteria_file"):
            value = getattr(task, field)
            if value:
                try:
                    matches = prompt_name(value) == name
                except HTTPException:
                    continue  # Invalid legacy references cannot resolve to this file.
                if matches:
                    result.append({"task_id": task.id, "field": field})
    return result


def mutate_prompt(repository, filename, *, root=None, content=None, delete=False, expected_revision=None, block_references=True):
    """Authoritative revision/reference recheck; never trust a preflight scan."""
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection
    from src.infrastructure.persistence.sqlite_task_repository import _row_to_task
    repository._bootstrap()
    with asset_lock(root) as fd, sqlite_connection(repository.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = read_at(fd, filename, nonempty=False)
        if expected_revision is not None and content_revision(current) != expected_revision:
            reject("revision_conflict", 409)
        tasks = [_row_to_task(row) for row in conn.execute("SELECT * FROM tasks")]
        if block_references and references_in_tasks(tasks, filename):
            reject("prompt_in_use", 409)
        if delete:
            os.unlink(prompt_name(filename), dir_fd=fd)
            os.fsync(fd)
        else:
            write_at(fd, filename, content)
        conn.commit()


def cleanup_unreferenced(repository, filename, root=None):
    # Cleanup must obey the same reference lock as deletion; a concurrent task may
    # have adopted this new asset after it was written but before its owner saved.
    try:
        mutate_prompt(repository, filename, root=root, delete=True)
    except HTTPException as exc:
        if exc.detail not in {"prompt_in_use", "prompt_not_found"}:
            raise


def validate_references(task, previous=None, *, root=None, changed_fields=()):
    """Run inside the writer lock; allow risk-reducing legacy config patches."""
    increased_risk = previous is not None and (
        (task.enabled and not previous.enabled)
        or (task.cron and task.cron != previous.cron)
        or (task.decision_mode == "ai" and previous.decision_mode != "ai")
    )
    for field in ("ai_prompt_base_file", "ai_prompt_criteria_file"):
        value = getattr(task, field)
        if not increased_risk and field not in changed_fields and previous is not None and value == getattr(previous, field):
            continue
        if value:
            setattr(task, field, canonical_reference(value))
            if task.decision_mode == "ai":
                read_prompt(value, root)
        elif (increased_risk or field in changed_fields) and task.decision_mode == "ai":
            reject("prompt_not_found", 404)
