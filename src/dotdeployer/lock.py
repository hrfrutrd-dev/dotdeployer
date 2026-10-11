"""Cross-platform advisory file locking around a project directory.

Uses ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows; falls back to
an O_EXCL lock file if neither is available.  Stale locks are detected via the
process id stored in the file.
"""

from __future__ import annotations

import errno
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import LockError
from .util import ensure_dir, now_iso

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True  # exists but owned by another user
    except Exception:  # pragma: no cover
        return True
    return True


@dataclass
class LockInfo:
    pid: int
    host: str
    started_at: str
    command: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockInfo":
        return cls(
            pid=int(data.get("pid", 0)),
            host=str(data.get("host", "")),
            started_at=str(data.get("started_at", "")),
            command=str(data.get("command", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"pid": self.pid, "host": self.host, "started_at": self.started_at, "command": self.command}


class ProjectLock:
    """Context manager that serialises mutation commands for one project."""

    def __init__(self, state_dir: Path, *, timeout: float = 30.0, command: str = "") -> None:
        self.path = Path(state_dir) / "state.json.lock"
        self.timeout = float(timeout)
        self.command = command
        self._handle = None
        self._acquired = False
        self.info: Optional[LockInfo] = None

    # -- helpers -----------------------------------------------------------
    def _read_info(self) -> Optional[LockInfo]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return LockInfo.from_dict(data)

    def _write_info(self) -> None:
        import socket

        payload = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=now_iso(),
            command=self.command,
        )
        self.info = payload
        try:
            self.path.write_text(json.dumps(payload.to_dict(), indent=2) + "\n", encoding="utf-8")
        except OSError:  # pragma: no cover - not fatal
            pass

    # -- API ---------------------------------------------------------------
    def acquire(self) -> "ProjectLock":
        ensure_dir(self.path.parent)
        deadline = time.monotonic() + self.timeout
        delay = 0.05
        while True:
            existing = self._read_info()
            if existing is not None and existing.pid != os.getpid() and not _pid_alive(existing.pid):
                # stale lock from a crashed run
                try:
                    self.path.unlink()
                except OSError:
                    pass
            try:
                handle = open(self.path, "a+")  # noqa: SIM115 - kept open for the flock
            except OSError as exc:  # pragma: no cover
                raise LockError(f"cannot open lock file {self.path}: {exc}") from exc
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - windows
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                if time.monotonic() >= deadline:
                    holder = existing or self._read_info()
                    detail = ""
                    if holder:
                        detail = (f" (held by pid {holder.pid} on {holder.host or 'this host'}"
                                  f"{', started ' + holder.started_at if holder.started_at else ''})")
                    raise LockError(
                        f"another dotdeployer run holds the project lock{detail}",
                        hint="wait for it to finish, or delete "
                             f"{self.path} if you are sure it is stale",
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
                continue
            self._handle = handle
            self._acquired = True
            self._write_info()
            return self

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if fcntl is not None and self._handle is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None and self._handle is not None:  # pragma: no cover
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover
            pass
        finally:
            try:
                if self._handle is not None:
                    self._handle.close()
            except OSError:  # pragma: no cover
                pass
            self._handle = None
            self._acquired = False

    def __enter__(self) -> "ProjectLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @property
    def acquired(self) -> bool:
        return self._acquired
