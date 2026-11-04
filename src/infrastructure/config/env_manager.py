"""Shared, atomic .env transactions for REST, MCP and local writers."""
import fcntl
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional

from dotenv import dotenv_values


_PLAIN_ENV_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_./:-]+$")
_LOCKS_GUARD = threading.Lock()
_LOCKS = {}


@dataclass(frozen=True, repr=False)
class EnvSnapshot:
    """An immutable effective configuration; never include it in logs/replies."""
    values: Mapping[str, str]

    def get_value(self, key, default=None):
        return self.values.get(key, default)

    def snapshot(self):
        return self


class EnvManager:
    def __init__(self, env_file: str = ".env"):
        self.env_file = Path(env_file)
        self._ensure_env_file_exists()

    @contextmanager
    def transaction(self):
        """One reentrant, per-path thread + process lock across read/check/write.

        The sidecar inode is stable across atomic replacement; never unlink it.
        Nested managers for the same path share depth within the current thread.
        """
        path = self.env_file.resolve()
        with _LOCKS_GUARD:
            lock, state = _LOCKS.setdefault(str(path), (threading.RLock(), threading.local()))
        with lock:
            depth = getattr(state, "depth", 0)
            if depth == 0:
                fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                state.fd = fd
            state.depth = depth + 1
            try:
                yield self
            finally:
                state.depth -= 1
                if state.depth == 0:
                    try:
                        fcntl.flock(state.fd, fcntl.LOCK_UN)
                    finally:
                        os.close(state.fd)
                        del state.fd

    def _ensure_env_file_exists(self):
        with self.transaction():
            if not self.env_file.exists():
                fd = os.open(self.env_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)

    def read_env(self) -> Dict[str, str]:
        with self.transaction():
            if not self.env_file.exists():
                return {}
            loaded = dotenv_values(self.env_file, encoding="utf-8", interpolate=False)
            return {key: value for key, value in loaded.items() if key and value is not None}

    def snapshot(self) -> EnvSnapshot:
        with self.transaction():
            values = dict(os.environ)
            values.update(self.read_env())
            return EnvSnapshot(MappingProxyType(values))

    def get_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.snapshot().get_value(key, default)

    def update_values(self, updates: Dict[str, str]) -> bool:
        return self.apply_changes(updates=updates)

    def apply_changes(self, updates: Dict[str, str], deletions: List[str] | None = None) -> bool:
        try:
            with self.transaction():
                existing_vars = self.read_env()
                existing_vars.update(updates)
                for key in deletions or []:
                    existing_vars.pop(key, None)
                return self._write_env(existing_vars)
        except Exception:
            # Paths, config values and exception strings may contain credentials.
            return False

    def set_value(self, key: str, value: str) -> bool:
        return self.update_values({key: value})

    def delete_keys(self, keys: List[str]) -> bool:
        return self.apply_changes({}, deletions=keys)

    def _write_env(self, env_vars: Dict[str, str]) -> bool:
        """Private full replacement; callers merging values must hold transaction()."""
        temporary = None
        try:
            with self.transaction():
                fd, temporary = tempfile.mkstemp(prefix=".env-write-", dir=self.env_file.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    for key, value in env_vars.items():
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                            raise ValueError("invalid_env_key")
                        stream.write(f"{key}={self._serialize_value(value)}\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.env_file)
                temporary = None
            return True
        except Exception:
            return False
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _serialize_value(self, value: str) -> str:
        text = str(value)
        if text == "":
            return ""
        if _PLAIN_ENV_VALUE_PATTERN.fullmatch(text):
            return text
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'


env_manager = EnvManager()
