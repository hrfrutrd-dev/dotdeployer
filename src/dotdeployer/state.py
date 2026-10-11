"""Persistent state: what is deployed, when, and what it replaced.

The state file (``.dotdeployer/state.json``) is the memory of the project.  It
lets dotdeployer detect drift (files edited by hand after deployment), prune
files that are no longer managed, list backups, and print a useful status.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import StateError
from .util import atomic_write, is_relative_to, json_dump, now_iso, sha256_file, sha256_text

STATE_VERSION = 1


@dataclass
class EntryState:
    """What dotdeployer knows about a single deployed target."""

    name: str
    target: str
    source: Optional[str] = None
    mode: str = "symlink"
    fingerprint: str = ""
    deployed_at: str = ""
    backup: Optional[str] = None
    owner: Optional[str] = None
    group: Optional[str] = None
    permissions: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    profile: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "source": self.source,
            "mode": self.mode,
            "fingerprint": self.fingerprint,
            "deployed_at": self.deployed_at,
            "backup": self.backup,
            "owner": self.owner,
            "group": self.group,
            "permissions": self.permissions,
            "tags": list(self.tags),
            "profile": self.profile,
        }

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "EntryState":
        return cls(
            name=name,
            target=str(data.get("target", "")),
            source=data.get("source"),
            mode=str(data.get("mode", "symlink")),
            fingerprint=str(data.get("fingerprint", "")),
            deployed_at=str(data.get("deployed_at", "")),
            backup=data.get("backup"),
            owner=data.get("owner"),
            group=data.get("group"),
            permissions=data.get("permissions"),
            tags=list(data.get("tags") or []),
            profile=str(data.get("profile", "")),
        )


class State:
    """A tiny JSON document with atomic writes and history."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: Dict[str, EntryState] = {}
        self.history: List[Dict[str, Any]] = []
        self.created_at: str = now_iso()
        self.updated_at: str = ""
        self.meta: Dict[str, Any] = {}

    # -- io ----------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "State":
        state = cls(path)
        if not path.exists():
            return state
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(
                f"state file {path} is unreadable: {exc}",
                hint="move it aside to start fresh (existing deployments stay untouched)",
            ) from exc
        if not isinstance(raw, dict):
            raise StateError(f"state file {path} must contain a JSON object")
        version = raw.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise StateError(
                f"state file version {version} is not supported (expected {STATE_VERSION})",
                hint="delete the state file and re-run 'dotdeployer apply'",
            )
        state.created_at = raw.get("created_at", state.created_at)
        state.updated_at = raw.get("updated_at", "")
        state.meta = dict(raw.get("meta") or {})
        entries = raw.get("entries") or {}
        if isinstance(entries, dict):
            state.entries = {name: EntryState.from_dict(name, data) for name, data in entries.items()}
        state.history = list(raw.get("history") or [])
        return state

    def save(self) -> None:
        self.updated_at = now_iso()
        payload = {
            "version": STATE_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
            "entries": {name: entry.to_dict() for name, entry in sorted(self.entries.items())},
            "history": self.history[-200:],
        }
        atomic_write(self.path, (json_dump(payload) + "\n").encode("utf-8"))

    # -- mutations ---------------------------------------------------------
    def record(self, entry_state: EntryState) -> None:
        self.entries[entry_state.name] = entry_state

    def forget(self, name: str) -> None:
        self.entries.pop(name, None)

    def get(self, name: str) -> Optional[EntryState]:
        return self.entries.get(name)

    def add_history(self, record: Dict[str, Any]) -> None:
        record.setdefault("at", now_iso())
        self.history.append(record)

    def prune_history(self, keep: int = 200) -> None:
        self.history = self.history[-keep:]

    # -- queries -----------------------------------------------------------
    def targets(self) -> Dict[str, EntryState]:
        return {entry.target: entry for entry in self.entries.values()}

    def managed_targets(self) -> List[str]:
        return sorted(entry.target for entry in self.entries.values())

    def orphaned(self, known_names: Iterable[str], known_targets: Iterable[str]) -> List[EntryState]:
        """Previously deployed entries that are no longer part of the config."""

        names = set(known_names)
        targets = {str(Path(t)) for t in known_targets}
        out = []
        for entry in self.entries.values():
            if entry.name in names:
                continue
            if str(Path(entry.target)) in targets:
                continue
            out.append(entry)
        return sorted(out, key=lambda e: e.target)

    def stats(self) -> Dict[str, Any]:
        modes: Dict[str, int] = {}
        for entry in self.entries.values():
            modes[entry.mode] = modes.get(entry.mode, 0) + 1
        return {
            "tracked": len(self.entries),
            "modes": modes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": len(self.history),
            "path": str(self.path),
        }


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #
def fingerprint_symlink(link: Path) -> str:
    try:
        target = os.readlink(str(link))
    except OSError:
        return ""
    return sha256_text(f"symlink:{target}")


def fingerprint_file(path: Path) -> str:
    if path.is_symlink():
        return fingerprint_symlink(path)
    if path.is_dir():
        return fingerprint_tree(path)
    try:
        return sha256_file(path)
    except OSError:
        return ""


def fingerprint_tree(path: Path) -> str:
    """Stable hash of a directory tree (paths + contents + modes)."""

    import hashlib

    digest = hashlib.sha256()
    root = Path(path)
    for current, dirs, files in os.walk(root):
        dirs.sort()
        rel = os.path.relpath(current, root)
        digest.update(f"D:{rel}\n".encode())
        for name in sorted(files):
            full = Path(current) / name
            rel_file = os.path.relpath(full, root)
            if full.is_symlink():
                digest.update(f"L:{rel_file}:{os.readlink(str(full))}\n".encode())
                continue
            try:
                stat = full.stat()
            except OSError:
                continue
            digest.update(f"F:{rel_file}:{stat.st_mode & 0o777}:{stat.st_size}\n".encode())
            try:
                with open(full, "rb") as fh:
                    while True:
                        block = fh.read(1024 * 256)
                        if not block:
                            break
                        digest.update(block)
            except OSError:
                continue
    return "sha256:" + digest.hexdigest()


def fingerprint_text(text: str) -> str:
    return sha256_text(text)


def fingerprint_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def is_within(path: Path, parent: Path) -> bool:
    return is_relative_to(Path(path), Path(parent))
