"""The deployment engine: inspect, back up, mutate, verify, restore.

Every mutation goes through :class:`Deployer`, which keeps three invariants:

1. **Nothing is destroyed silently** - existing targets are moved into a
   timestamped backup set according to the entry's conflict strategy.
2. **Writes are atomic** - files are written to a temporary sibling and then
   renamed, so a crash never leaves a half-written dotfile.
3. **Everything is verified** - after a mutation the target is re-inspected and
   its fingerprint is compared with what was intended.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config, Entry
from .errors import ActionError
from .secrets import SecretResolver
from .state import (
    EntryState,
    State,
    fingerprint_file,
    fingerprint_symlink,
    fingerprint_text,
    fingerprint_tree,
)
from .template import Environment, Template, TemplateError
from .ui import Console, Style
from .util import (
    atomic_write,
    display_path,
    ensure_dir,
    human_size,
    is_windows,
    now_iso,
    read_bytes,
    read_text,
    sha256_bytes,
    stat_mode,
    timestamp_id,
)

# statuses
CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
DRIFT = "drift"
CONFLICT = "conflict"
SKIP = "skip"
REMOVE = "remove"
INFO = "info"
ERROR = "error"
ORPHAN = "orphan"

_SYMLINKABLE = {"symlink", "copy", "template"}


@dataclass
class Change:
    """The outcome (or the plan) for a single entry."""

    entry: Entry
    status: str
    target: Path
    detail: str = ""
    diff: str = ""
    backup: Optional[str] = None
    error: Optional[str] = None
    fingerprint: str = ""
    previous: str = ""
    removed: List[str] = field(default_factory=list)

    @property
    def planned(self) -> bool:
        return self.status in {CREATE, UPDATE, REMOVE, DRIFT, CONFLICT, SKIP}

    @property
    def mutating(self) -> bool:
        return self.status in {CREATE, UPDATE, REMOVE, DRIFT}

    @property
    def ok(self) -> bool:
        return self.status != ERROR

    @property
    def deployable(self) -> bool:
        """True when the entry owns a target on disk (state may track it)."""

        return self.status != ERROR and self.entry.mode != "ignore"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry": self.entry.key,
            "target": str(self.target),
            "target_display": display_path(self.target),
            "status": self.status,
            "mode": self.entry.mode,
            "strategy": self.entry.strategy,
            "tags": list(self.entry.tags),
            "detail": self.detail,
            "backup": self.backup,
            "error": self.error,
            "diff": self.diff,
            "fingerprint": self.fingerprint,
            "previous": self.previous,
        }


# --------------------------------------------------------------------------- #
# backups
# --------------------------------------------------------------------------- #
class BackupStore:
    """Content-addressed-ish backup sets stored under ``.dotdeployer/backups``."""

    def __init__(self, root: Path, *, keep: int = 10) -> None:
        self.root = Path(root)
        self.keep = int(keep)
        self.enabled = True
        self.current_id: Optional[str] = None
        self.current_dir: Optional[Path] = None
        self.last_committed_id: Optional[str] = None
        self.manifest: Dict[str, Any] = {"id": "", "created_at": "", "items": []}

    # -- lifecycle ---------------------------------------------------------
    def begin(self, *, reason: str = "apply") -> str:
        base = f"{timestamp_id()}-{os.getpid()}"
        candidate = base
        suffix = 1
        # several runs can start within the same second; keep every set separate
        while (self.root / candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        self.current_id = candidate
        self.current_dir = self.root / self.current_id
        ensure_dir(self.current_dir)
        self.manifest = {"id": self.current_id, "created_at": now_iso(), "reason": reason, "items": []}
        return self.current_id

    def commit(self) -> Optional[str]:
        """Finalise the current set; returns its id when something was stored."""

        if self.current_dir is None:
            return None
        committed: Optional[str] = None
        if not self.manifest["items"]:
            try:
                self.current_dir.rmdir()
            except OSError:
                pass
        else:
            self._write_manifest(self.current_dir, self.manifest)
            committed = self.manifest.get("id") or self.current_dir.name
            self.last_committed_id = committed
        self.current_dir = None
        self.current_id = None
        return committed

    def abort(self) -> None:
        if self.current_dir is not None and self.current_dir.exists() and not self.manifest["items"]:
            shutil.rmtree(self.current_dir, ignore_errors=True)
        self.current_dir = None
        self.current_id = None

    @staticmethod
    def _write_manifest(directory: Path, manifest: Dict[str, Any]) -> None:
        ensure_dir(directory)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # -- saving ------------------------------------------------------------
    def save(self, target: Path, *, label: str = "") -> Optional[str]:
        """Back up *target*; returns a human readable marker or ``None``."""

        target = Path(target)
        if not self.enabled:
            return None
        if not (target.exists() or target.is_symlink()):
            return None
        if self.current_dir is None:
            self.begin()
        assert self.current_dir is not None

        index = len(self.manifest["items"]) + 1
        safe_label = label or target.name or "item"
        safe_label = safe_label.replace(os.sep, "_").replace("~", "")
        destination = self.current_dir / f"{index:03d}-{safe_label}"

        item: Dict[str, Any] = {
            "target": str(target),
            "stored_as": str(destination),
            "created_at": now_iso(),
        }
        if target.is_symlink():
            item["kind"] = "symlink"
            item["link_target"] = os.readlink(str(target))
            atomic_write(destination, (item["link_target"] + "\n").encode("utf-8"))
        elif target.is_dir():
            item["kind"] = "directory"
            shutil.copytree(target, destination, symlinks=True, dirs_exist_ok=False)
        else:
            item["kind"] = "file"
            ensure_dir(destination.parent)
            shutil.copy2(target, destination, follow_symlinks=False)
            try:
                item["size"] = target.stat().st_size
                item["mode"] = f"{stat_mode(target):04o}"
            except OSError:  # pragma: no cover
                pass

        self.manifest["items"].append(item)
        self._write_manifest(self.current_dir, self.manifest)
        return f"{self.current_id}/{destination.name}"

    # -- reading -----------------------------------------------------------
    def sets(self) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        out: List[Dict[str, Any]] = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            manifest_path = directory / "manifest.json"
            if manifest_path.is_file():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
            else:
                data = {"id": directory.name, "items": [], "created_at": ""}
            data["path"] = str(directory)
            data["count"] = len(data.get("items") or [])
            out.append(data)
        return out

    def total_size(self) -> int:
        total = 0
        if not self.root.exists():
            return 0
        for current, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except OSError:
                    continue
        return total

    def purge(self, *, keep: Optional[int] = None, dry_run: bool = False) -> List[str]:
        keep = self.keep if keep is None else keep
        sets = self.sets()
        removed: List[str] = []
        for item in sets[keep:]:
            removed.append(item["id"])
            if not dry_run:
                shutil.rmtree(item["path"], ignore_errors=True)
        return removed

    def restore(self, backup_id: str, *, dry_run: bool = False, console: Optional[Console] = None) -> List[str]:
        """Restore every item of a backup set, newest first."""

        directory = self.root / backup_id
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise ActionError(f"no backup set named {backup_id!r} in {self.root}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored: List[str] = []
        for item in reversed(manifest.get("items") or []):
            target = Path(item["target"])
            stored = Path(item["stored_as"])
            if not stored.exists():
                continue
            if console:
                console.info(f"restore {console.paint(display_path(target), Style.CYAN)}")
            if dry_run:
                restored.append(str(target))
                continue
            if target.is_symlink() or target.exists():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            ensure_dir(target.parent)
            if item.get("kind") == "directory":
                shutil.copytree(stored, target, symlinks=True)
            elif item.get("kind") == "symlink":
                link_target = (stored.read_text(encoding="utf-8") or "").strip()
                if link_target:
                    os.symlink(link_target, str(target))
            else:
                shutil.copy2(stored, target, follow_symlinks=False)
                if item.get("mode"):
                    os.chmod(target, int(str(item["mode"]), 8))
            restored.append(str(target))
        return restored


# --------------------------------------------------------------------------- #
# deployer
# --------------------------------------------------------------------------- #
class Deployer:
    """Inspects and mutates single entries against the real file system."""

    def __init__(
        self,
        config: Config,
        state: State,
        *,
        backups: Optional[BackupStore] = None,
        console: Optional[Console] = None,
        dry_run: bool = False,
        force: bool = False,
        assume_yes: bool = False,
        no_input: bool = False,
        secrets: Optional[SecretResolver] = None,
        diff_context: Optional[int] = None,
    ) -> None:
        self.config = config
        self.state = state
        self.backups = backups or BackupStore(config.backup_dir, keep=config.settings.get("backup_keep", 10))
        self.console = console or Console()
        self.dry_run = dry_run
        self.force = force
        self.assume_yes = assume_yes
        self.no_input = no_input
        self.secrets = secrets
        self.diff_context = int(
            diff_context if diff_context is not None else config.settings.get("diff_context", 3)
        )
        self.rendered_cache: Dict[str, bytes] = {}
        self.template_envs: Dict[str, Environment] = {}

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def template_env(self, entry: Entry) -> Environment:
        if entry.key in self.template_envs:
            env = self.template_envs[entry.key]
        else:
            extra: Dict[str, Any] = {
                "entry": {
                    "name": entry.key,
                    "target": str(entry.target),
                    "source": entry.source,
                    "mode": entry.mode,
                    "tags": list(entry.tags),
                    "comment": entry.comment,
                },
                "tags": list(entry.tags),
            }
            env = self.config.template_env({**extra, **entry.vars})
            if self.secrets is not None:
                env.secret_resolver = self.secrets.resolve_optional
            self.template_envs[entry.key] = env
        return env

    def render_template_file(self, entry: Entry, source: Path) -> bytes:
        """Render one template source to bytes (cached per entry)."""

        cache_key = f"{entry.key}:{source}"
        if cache_key in self.rendered_cache:
            return self.rendered_cache[cache_key]
        try:
            text = read_text(source)
        except OSError as exc:
            raise ActionError(f"cannot read template {source}: {exc}", path=str(source)) from exc
        try:
            template = Template(text, name=str(source))
            rendered = template.render(self.template_env(entry))
        except TemplateError as exc:
            raise ActionError(
                f"template error in {source}: {exc}",
                path=str(source),
                hint=f"entry {entry.key}; run 'dotdeployer doctor' for details",
            ) from exc
        if entry.strip_trailing_whitespace:
            rendered = "\n".join(line.rstrip() for line in rendered.splitlines()) + (
                "\n" if rendered.endswith("\n") else ""
            )
        if entry.ensure_newline and rendered and not rendered.endswith("\n"):
            rendered += "\n"
        data = rendered.encode("utf-8")
        self.rendered_cache[cache_key] = data
        return data

    def template_tree(self, entry: Entry, source: Path) -> Dict[str, bytes]:
        """Render a directory of templates into ``{relative path: bytes}``."""

        out: Dict[str, bytes] = {}
        for current, dirs, files in os.walk(source):
            dirs.sort()
            for name in sorted(files):
                full = Path(current) / name
                rel = os.path.relpath(full, source)
                if name.endswith((".ddignore", ".tmpl.dd")):
                    continue
                out[rel] = self.render_template_file(entry, full)
        return out

    # ------------------------------------------------------------------ #
    # inspection helpers
    # ------------------------------------------------------------------ #
    def _source_bytes(self, entry: Entry) -> Dict[str, bytes]:
        """The bytes this entry wants to place at the target (file mode only)."""

        if entry.source_path is None:
            return {}
        source = entry.source_path
        if entry.mode == "template":
            if source.is_dir():
                return self.template_tree(entry, source)
            return {"": self.render_template_file(entry, source)}
        if source.is_dir():
            return {}
        try:
            data = read_bytes(source)
        except OSError as exc:
            raise ActionError(f"cannot read source {source}: {exc}", path=str(source)) from exc
        if entry.encrypt:
            data = self._decrypt(entry, data, source)
        if entry.strip_trailing_whitespace or entry.ensure_newline:
            text = data.decode("utf-8", errors="surrogateescape")
            if entry.strip_trailing_whitespace:
                text = "\n".join(line.rstrip() for line in text.splitlines()) + (
                    "\n" if text.endswith("\n") else ""
                )
            if entry.ensure_newline and text and not text.endswith("\n"):
                text += "\n"
            data = text.encode("utf-8", errors="surrogateescape")
        return {"": data}

    def _decrypt(self, entry: Entry, data: bytes, source: Path) -> bytes:
        from .secrets import decrypt_bytes

        try:
            return decrypt_bytes(data, config=self.config)
        except Exception as exc:
            raise ActionError(
                f"cannot decrypt {source}: {exc}",
                path=str(source),
                hint="check [secrets] in your config (gpg/age) or remove 'encrypt = true'",
            ) from exc

    def _desired_fingerprint(self, entry: Entry) -> Tuple[str, str]:
        """Return ``(fingerprint, detail)`` describing the desired target state."""

        if entry.mode == "mkdir":
            return "dir", "directory"
        assert entry.source_path is not None
        source = entry.source_path
        if entry.mode == "symlink":
            link_target = str(source)
            return sha256_bytes(f"symlink:{link_target}".encode()), f"→ {display_path(source)}"
        if entry.mode in {"copy", "template"}:
            if not source.exists() and not source.is_symlink():
                raise ActionError(
                    f"source does not exist: {source}",
                    path=str(source),
                    hint=f"entry {entry.key} points at {entry.source!r}",
                )
            if source.is_dir() and entry.mode == "copy":
                return fingerprint_tree(source), "directory tree"
            payload = self._source_bytes(entry)
            if not payload:
                raise ActionError(f"source missing: {source}", path=str(source))
            if list(payload) == [""]:
                return sha256_bytes(payload[""]), human_size(len(payload[""])).strip()
            digest = fingerprint_text(json.dumps({k: sha256_bytes(v) for k, v in sorted(payload.items())},
                                                 sort_keys=True))
            return digest, f"{len(payload)} file(s)"
        raise ActionError(f"unsupported mode: {entry.mode}")

    def _current_fingerprint(self, target: Path) -> str:
        if target.is_symlink():
            return fingerprint_symlink(target)
        if target.is_dir():
            return fingerprint_tree(target)
        if target.exists():
            return fingerprint_file(target)
        return ""

    def _recorded(self, entry: Entry) -> Optional[EntryState]:
        return self.state.get(entry.key) or None

    # ------------------------------------------------------------------ #
    # inspect
    # ------------------------------------------------------------------ #
    def inspect(self, entry: Entry) -> Change:
        """Compute what would happen without touching anything."""

        change = Change(entry=entry, status=UNCHANGED, target=entry.target)
        if entry.disabled:
            change.status = SKIP
            change.detail = "disabled"
            return change

        if entry.mode == "ignore":
            change.status = SKIP
            change.detail = "ignored"
            return change

        try:
            desired, detail = self._desired_fingerprint(entry)
        except ActionError as exc:
            change.status = ERROR
            change.error = str(exc)
            change.detail = exc.hint or ""
            return change

        change.fingerprint = desired
        change.detail = detail

        target = entry.target
        exists = target.exists() or target.is_symlink()
        previous = self._current_fingerprint(target) if exists else ""
        change.previous = previous

        if not exists:
            change.status = CREATE
            if entry.mode == "symlink" and entry.source_path is not None and not entry.source_path.exists():
                change.status = ERROR
                change.error = f"source does not exist: {entry.source_path}"
            return change

        # ----- existing target -----------------------------------------
        recorded = self._recorded(entry)

        if entry.mode == "mkdir":
            if target.is_dir():
                change.status = UNCHANGED
                change.detail = "directory exists"
            else:
                change.status = CONFLICT
                change.detail = "not a directory"
            return change

        if entry.mode == "symlink":
            if target.is_symlink():
                actual = os.readlink(str(target))
                if actual == str(entry.source_path):
                    change.status = UNCHANGED
                    return change
                if recorded and recorded.fingerprint != previous:
                    change.status = DRIFT
                    change.detail = f"relinked to {actual} since last apply"
                    return change
                change.status = UPDATE
                change.detail = f"relink → {display_path(entry.source_path)}"
                return change
            if recorded and recorded.fingerprint == previous:
                change.status = UPDATE
                change.detail = "replace (was managed, now a symlink)"
                return change
            if recorded:
                change.status = DRIFT
                change.detail = "replaced with a regular file since last apply"
                return change
            change.status = CONFLICT
            change.detail = "existing file/directory"
            return change

        # copy / template
        if target.is_dir() and not target.is_symlink():
            source_is_tree = bool(entry.source_path and entry.source_path.is_dir()) or entry.mode == "template"
            if source_is_tree and entry.mode == "template":
                # compare rendered tree against target tree file by file
                payload = self._source_bytes(entry)
                mismatches = self._tree_mismatches(payload, target)
                if not mismatches:
                    change.status = UNCHANGED
                    change.detail = f"{len(payload)} file(s)"
                    return change
                change.status = CONFLICT if not recorded else UPDATE
                change.detail = f"{len(mismatches)} file(s) differ"
                if len(mismatches) == 1:
                    change.diff = self._diff_paths(
                        target / mismatches[0][0], payload[mismatches[0][0]], mismatches[0][0]
                    )
                return change
            if source_is_tree:
                if entry.mode == "copy" and entry.source_path and fingerprint_tree(entry.source_path) == previous:
                    change.status = UNCHANGED
                    return change
                change.status = CONFLICT if not recorded else UPDATE
                change.detail = "directory tree differs"
                return change

        if previous == desired:
            change.status = UNCHANGED
            return change

        if target.is_symlink():
            change.status = UPDATE if recorded else CONFLICT
            change.detail = "replace symlink with real file"
            return change

        if recorded:
            if recorded.fingerprint and recorded.fingerprint != previous:
                change.status = DRIFT
                change.detail = "modified since last apply"
            else:
                change.status = UPDATE
                change.detail = "content changed"
            if entry.mode in {"copy", "template"}:
                change.diff = self._diff_file(entry, target)
            return change

        change.status = CONFLICT
        change.detail = "unmanaged existing file"
        if entry.mode in {"copy", "template"}:
            change.diff = self._diff_file(entry, target)
        return change

    def _tree_mismatches(self, payload: Dict[str, bytes], target: Path) -> List[Tuple[str, bytes]]:
        mismatches: List[Tuple[str, bytes]] = []
        for rel, data in sorted(payload.items()):
            full = target / rel
            if not full.is_file():
                mismatches.append((rel, data))
                continue
            try:
                if read_bytes(full) != data:
                    mismatches.append((rel, data))
            except OSError:  # pragma: no cover
                mismatches.append((rel, data))
        return mismatches

    def _diff_file(self, entry: Entry, target: Path) -> str:
        if entry.target.is_dir():
            return ""
        try:
            payload = self._source_bytes(entry)
        except ActionError:
            return ""
        if list(payload) != [""]:
            return ""
        if not target.is_file() or target.is_symlink():
            return ""
        try:
            old = read_text(target)
        except OSError:
            return ""
        new = payload[""].decode("utf-8", errors="replace")
        if "\x00" in old or "\x00" in new:
            return ""
        return self._unified(old, new, display_path(target), display_path(entry.source_path or Path("")))

    def _diff_paths(self, path: Path, new_data: bytes, rel: str) -> str:
        if not path.is_file():
            return ""
        try:
            old = read_text(path)
        except OSError:
            return ""
        new = new_data.decode("utf-8", errors="replace")
        if "\x00" in old or "\x00" in new:
            return ""
        return self._unified(old, new, rel, rel + " (source)")

    def _unified(self, old: str, new: str, from_label: str, to_label: str) -> str:
        if old == new:
            return ""
        diff = difflib.unified_diff(
            old.splitlines(keepends=False),
            new.splitlines(keepends=False),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
            n=self.diff_context,
        )
        lines = list(diff)
        limit = 400
        if len(lines) > limit:
            lines = [*lines[:limit], f"… diff truncated ({len(lines) - limit} more lines)"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # apply
    # ------------------------------------------------------------------ #
    def apply(self, change: Change) -> Change:
        """Execute a planned change (or report that nothing is needed)."""

        entry = change.entry
        status = change.status
        if status in {UNCHANGED, SKIP, ERROR}:
            return change
        if status == REMOVE:
            known = self.state.get(entry.key) or EntryState(
                name=entry.key,
                target=str(entry.target),
                source=entry.source,
                mode=entry.mode,
                fingerprint=change.previous,
            )
            outcome = self.remove(known, backup=True)
            change.status = outcome.status
            change.detail = outcome.detail
            change.backup = outcome.backup
            change.removed = outcome.removed
            change.error = outcome.error
            return change
        if status == CONFLICT and not self.force:
            if entry.strategy == "skip":
                change.status = SKIP
                change.detail = (change.detail or "") + " (strategy=skip)"
                return change
            confirm = self._confirm_conflict(entry, change)
            if not confirm:
                change.status = SKIP
                change.detail = (change.detail or "") + " (declined)"
                return change
        if status == SKIP:
            return change

        if self.dry_run:
            return change

        try:
            if entry.mode == "mkdir":
                self._apply_mkdir(entry, change)
            else:
                self._apply_file(entry, change)
        except ActionError as exc:
            change.status = ERROR
            change.error = str(exc)
            change.detail = exc.hint or ""
            return change
        except OSError as exc:
            change.status = ERROR
            change.error = f"{exc.strerror or exc} ({getattr(exc, 'filename', entry.target)})"
            return change
        return change

    # -- strategies --------------------------------------------------------
    def _confirm_conflict(self, entry: Entry, change: Change) -> bool:
        strategy = entry.strategy
        if self.assume_yes:
            strategy = "backup" if strategy == "ask" else strategy
        if strategy == "ask":
            from .ui import prompt_yes_no

            question = f"Replace existing {display_path(entry.target)}? (backup kept)"
            if prompt_yes_no(question, default=False, console=self.console,
                             assume_yes=self.assume_yes, no_input=self.no_input):
                return True
            return False
        if strategy in {"backup", "merge"}:
            return True
        if strategy == "override":
            return True
        return False

    def _handle_existing(self, entry: Entry, change: Change) -> None:
        """Deal with whatever already sits at the target, honouring the strategy."""

        target = entry.target
        if not (target.exists() or target.is_symlink()):
            return
        strategy = entry.strategy
        if strategy == "skip":
            raise ActionError(f"target exists and strategy is 'skip': {target}", path=str(target))
        if strategy == "override":
            self._remove_existing(target)
            return
        # backup (default) and ask (already confirmed) / merge
        marker = self.backups.save(target, label=entry.key)
        if marker:
            change.backup = marker
        self._remove_existing(target)

    @staticmethod
    def _remove_existing(target: Path) -> None:
        if target.is_symlink() or target.is_file():
            try:
                target.unlink()
            except FileNotFoundError:  # pragma: no cover - race
                pass
            return
        if target.is_dir():
            shutil.rmtree(target)
            return
        raise ActionError(f"cannot remove unknown target type: {target}", path=str(target))

    # -- mutations ---------------------------------------------------------
    def _apply_mkdir(self, entry: Entry, change: Change) -> None:
        target = entry.target
        if target.exists() and not target.is_dir():
            self._handle_existing(entry, change)
        ensure_dir(target)
        self._apply_ownership(entry)
        self._set_permissions(entry, target)
        self._verify(change)

    def _apply_file(self, entry: Entry, change: Change) -> None:
        target = entry.target
        source = entry.source_path

        if entry.create_parents:
            ensure_dir(target.parent)
        elif not target.parent.is_dir():
            raise ActionError(
                f"parent directory does not exist: {target.parent}",
                path=str(target.parent),
                hint="set create_parents = true on this entry",
            )

        merge = entry.strategy == "merge"
        if merge and target.is_dir() and not target.is_symlink() and self._is_tree_entry(entry):
            self._apply_tree_merge(entry, change)
            self._verify(change)
            return

        if target.exists() or target.is_symlink():
            # keep unchanged symlinks pointing at the same place
            if target.is_symlink() and entry.mode == "symlink" and os.readlink(str(target)) == str(source):
                self._verify(change)
                return
            self._handle_existing(entry, change)

        if entry.mode == "symlink":
            self._make_symlink(entry, source if source is not None else Path())
        elif entry.mode == "copy":
            if source is not None and source.is_dir():
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
            else:
                assert source is not None
                data = self._source_bytes(entry)[""]
                atomic_write(target, data)
                try:
                    shutil.copystat(source, target)
                except OSError:  # pragma: no cover - source may be on another fs
                    pass
        elif entry.mode == "template":
            assert source is not None
            payload = self._source_bytes(entry)
            if list(payload) == [""]:
                atomic_write(target, payload[""])
            else:
                ensure_dir(target)
                for rel, data in payload.items():
                    destination = target / rel
                    ensure_dir(destination.parent)
                    atomic_write(destination, data)
        else:  # pragma: no cover - guarded by config validation
            raise ActionError(f"unsupported mode: {entry.mode}")

        self._apply_ownership(entry)
        self._set_permissions(entry, target)
        self._verify(change)

    def _is_tree_entry(self, entry: Entry) -> bool:
        if entry.mode == "template":
            return entry.source_path is not None and entry.source_path.is_dir()
        return bool(entry.source_path and entry.source_path.is_dir())

    def _apply_tree_merge(self, entry: Entry, change: Change) -> None:
        """Deploy a source tree into an existing target tree file by file."""

        assert entry.source_path is not None
        source = entry.source_path
        target = entry.target
        ensure_dir(target)
        for current, dirs, files in os.walk(source):
            dirs.sort()
            rel_dir = os.path.relpath(current, source)
            destination_dir = target if rel_dir == "." else target / rel_dir
            ensure_dir(destination_dir)
            for name in sorted(files):
                full = Path(current) / name
                destination = destination_dir / name
                if entry.mode == "template":
                    data = self.render_template_file(entry, full)
                    if destination.is_file() and not destination.is_symlink():
                        try:
                            if read_bytes(destination) == data:
                                continue
                        except OSError:  # pragma: no cover
                            pass
                    if destination.exists() or destination.is_symlink():
                        marker = self.backups.save(destination, label=f"{entry.key}-{rel_dir}-{name}")
                        if marker:
                            change.backup = marker
                        self._remove_existing(destination)
                    atomic_write(destination, data)
                else:
                    if destination.exists() or destination.is_symlink():
                        if destination.is_symlink() and os.readlink(str(destination)) == str(full):
                            continue
                        marker = self.backups.save(destination, label=f"{entry.key}-{rel_dir}-{name}")
                        if marker:
                            change.backup = marker
                        self._remove_existing(destination)
                    self._make_symlink(entry, full, link_path=destination)

    def _make_symlink(self, entry: Entry, source: Path, *, link_path: Optional[Path] = None) -> None:
        link = link_path or entry.target
        if not source.exists() and not source.is_symlink():
            raise ActionError(
                f"source does not exist: {source}",
                path=str(source),
                hint=f"entry {entry.key}",
            )
        ensure_dir(link.parent)
        try:
            os.symlink(str(source), str(link))
        except (OSError, NotImplementedError) as exc:
            if is_windows():
                self.console.warn(
                    f"symlinks unavailable ({exc.__class__.__name__}); falling back to a copy for {link}"
                )
                if source.is_dir():
                    shutil.copytree(source, link, symlinks=True, dirs_exist_ok=True)
                else:
                    atomic_write(link, read_bytes(source))
                return
            raise ActionError(f"cannot create symlink {link} → {source}: {exc}", path=str(link)) from exc

    def _set_permissions(self, entry: Entry, target: Path) -> None:
        if entry.permissions is None:
            return
        try:
            if target.is_symlink() and entry.mode == "symlink":
                return  # permissions belong to the source file
            if target.is_dir() and entry.mode in {"copy", "template"} and entry.source_path and \
                    entry.source_path.is_dir():
                for current, _dirs, files in os.walk(target):
                    for name in files:
                        os.chmod(Path(current) / name, entry.permissions)
            else:
                os.chmod(target, entry.permissions)
        except OSError as exc:
            raise ActionError(
                f"cannot set permissions {entry.permissions:04o} on {target}: {exc}",
                path=str(target),
            ) from exc

    def _apply_ownership(self, entry: Entry) -> None:
        if not entry.owner and not entry.group:
            return
        if is_windows():  # pragma: no cover
            self.console.debug("owner/group are ignored on Windows")
            return
        if os.geteuid() != 0:
            self.console.debug(f"skipping chown (not root) for {display_path(entry.target)}")
            return
        try:
            shutil.chown(entry.target, user=entry.owner, group=entry.group)
        except (OSError, LookupError) as exc:
            raise ActionError(f"cannot chown {entry.target} to {entry.owner}:{entry.group}: {exc}") from exc

    def _verify(self, change: Change) -> None:
        """Re-inspect the target and make sure it matches the intent."""

        entry = change.entry
        if entry.mode == "mkdir":
            if not entry.target.is_dir():
                raise ActionError(f"verification failed: {entry.target} is not a directory")
            change.status = UPDATE if change.previous else CREATE
            return
        actual = self._current_fingerprint(entry.target)
        expected = change.fingerprint
        if expected and actual != expected:
            if entry.mode in {"copy", "template"} and entry.source_path and entry.source_path.is_dir():
                pass
            elif entry.mode == "copy" and entry.permissions is not None:
                # permissions are part of the copy fingerprint check only for trees
                pass
            else:
                raise ActionError(
                    "verification failed: deployed content does not match the source "
                    f"({display_path(entry.target)})",
                    path=str(entry.target),
                    hint="this usually means a hook or another process rewrote the file",
                )
        # after a successful mutation the outcome is simply created vs updated
        change.status = UPDATE if change.previous else CREATE

    # ------------------------------------------------------------------ #
    # state bookkeeping
    # ------------------------------------------------------------------ #
    def record(self, change: Change, *, profile: str = "") -> None:
        entry = change.entry
        fingerprint = self._current_fingerprint(entry.target) if entry.target.exists() else change.fingerprint
        self.state.record(
            EntryState(
                name=entry.key,
                target=str(entry.target),
                source=str(entry.source_path) if entry.source_path else None,
                mode=entry.mode,
                fingerprint=fingerprint or change.fingerprint,
                deployed_at=now_iso(),
                backup=change.backup,
                owner=entry.owner,
                group=entry.group,
                permissions=f"{entry.permissions:04o}" if entry.permissions is not None else None,
                tags=list(entry.tags),
                profile=profile,
            )
        )

    # ------------------------------------------------------------------ #
    # removal / uninstall
    # ------------------------------------------------------------------ #
    def remove(self, state_entry: EntryState, *, backup: bool = True) -> Change:
        """Remove a previously deployed target (used by prune / uninstall)."""

        entry = Entry(
            target_raw=state_entry.target,
            source=state_entry.source,
            name=state_entry.name,
            mode=state_entry.mode,
        )
        entry.target = Path(state_entry.target)
        change = Change(entry=entry, status=REMOVE, target=entry.target)
        if self.dry_run:
            if not (entry.target.exists() or entry.target.is_symlink()):
                change.status = UNCHANGED
                change.detail = "already gone"
            return change
        if not (entry.target.exists() or entry.target.is_symlink()):
            change.status = UNCHANGED
            change.detail = "already gone"
            return change

        current = self._current_fingerprint(entry.target)
        if state_entry.fingerprint and current and current != state_entry.fingerprint:
            change.status = DRIFT
            change.detail = "modified since deployment (kept)"
            return change
        if state_entry.fingerprint and not current:
            change.status = UNCHANGED
            change.detail = "already gone"
            return change

        if backup:
            marker = self.backups.save(entry.target, label=f"removed-{entry.key}")
            if marker:
                change.backup = marker
        self._remove_existing(entry.target)
        change.removed.append(str(entry.target))
        change.status = REMOVE
        return change

    def prune(self, orphans: Sequence[EntryState], *, backup: bool = True) -> List[Change]:
        changes: List[Change] = []
        for state_entry in orphans:
            changes.append(self.remove(state_entry, backup=backup))
        return changes
