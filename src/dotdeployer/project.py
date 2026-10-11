"""High level orchestration: plan → apply → verify → rollback.

``Project`` glues configuration, state, hooks and the deployment engine
together and is the entry point used by both the CLI and the Python API::

    from dotdeployer import Project

    project = Project.load(profiles=["work"])
    plan = project.plan()
    if plan.mutating:
        result = project.apply()
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .actions import (
    DRIFT,
    ERROR,
    REMOVE,
    SKIP,
    UNCHANGED,
    BackupStore,
    Change,
    Deployer,
)
from .config import Config, Entry
from .doctor import Check, diagnose, summarise, worst_status
from .errors import ActionError, DotDeployerError, HookError, StateError
from .hooks import HookRunner
from .lock import ProjectLock
from .plan import Plan
from .secrets import SecretResolver
from .state import EntryState, State
from .ui import Console, prompt_yes_no
from .util import display_path, ensure_dir

__all__ = ["ApplyResult", "Project", "apply", "plan", "rollback"]


@dataclass
class ApplyResult:
    """What actually happened during :meth:`Project.apply`."""

    plan: Plan
    applied: List[Change] = field(default_factory=list)
    skipped: List[Change] = field(default_factory=list)
    errors: List[Change] = field(default_factory=list)
    removals: List[Change] = field(default_factory=list)
    hooks: Dict[str, Any] = field(default_factory=dict)
    backup_id: Optional[str] = None
    state_path: Optional[str] = None
    duration_ms: float = 0.0
    dry_run: bool = False
    planned_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> int:
        return len(self.applied)

    @property
    def ok(self) -> bool:
        return not self.errors

    def exit_code(self) -> int:
        if self.errors:
            return 1
        return 0

    def summary_line(self) -> str:
        bits = []
        if self.dry_run:
            bits.append("[dry-run]")
        bits.append(f"{len(self.applied)} applied")
        if self.removals:
            bits.append(f"{len(self.removals)} removed")
        if self.skipped:
            bits.append(f"{len(self.skipped)} skipped")
        if self.errors:
            bits.append(f"{len(self.errors)} failed")
        if not self.applied and not self.removals and not self.errors:
            bits.append("nothing to do")
        return ", ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "applied": [c.to_dict() for c in self.applied],
            "removed": [c.to_dict() for c in self.removals],
            "skipped": [c.to_dict() for c in self.skipped],
            "errors": [c.to_dict() for c in self.errors],
            "hooks": self.hooks,
            "backup_id": self.backup_id,
            "duration_ms": round(self.duration_ms, 2),
            "plan": self.plan.to_dict(include_diff=False),
        }


class Project:
    """A loaded project, ready to plan and apply."""

    def __init__(
        self,
        config: Config,
        *,
        console: Optional[Console] = None,
        state: Optional[State] = None,
        dry_run: bool = False,
        force: bool = False,
        assume_yes: bool = False,
        no_input: bool = False,
        keep_backups: Optional[int] = None,
        hooks_enabled: Optional[bool] = None,
    ) -> None:
        self.config = config
        self.console = console or Console()
        ensure_dir(config.state_dir)
        self.state = state or State.load(config.state_dir / "state.json")
        self.dry_run = dry_run
        self.force = force
        self.assume_yes = assume_yes
        self.no_input = no_input
        self.keep_backups = keep_backups
        self.secrets = SecretResolver(config.secrets_config, root=config.root)
        self.backups = BackupStore(config.backup_dir, keep=keep_backups or config.settings["backup_keep"])
        self.hooks = HookRunner(config, console=self.console, dry_run=dry_run, enabled=hooks_enabled)
        self.deployer = Deployer(
            config,
            self.state,
            backups=self.backups,
            console=self.console,
            dry_run=dry_run,
            force=force,
            assume_yes=assume_yes,
            no_input=no_input,
            secrets=self.secrets,
        )

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        *,
        profiles: Optional[Sequence[str]] = None,
        variables: Optional[Mapping[str, Any]] = None,
        console: Optional[Console] = None,
        dry_run: bool = False,
        force: bool = False,
        assume_yes: bool = False,
        no_input: bool = False,
        use_host_config: bool = True,
        use_local_config: bool = True,
        hooks_enabled: Optional[bool] = None,
    ) -> "Project":
        config_path = None
        if path:
            config_path = Config.find(path)
        config = Config.load(
            path if not config_path else config_path,
            var_overrides=variables,
            profiles=profiles,
            use_host_config=use_host_config,
            use_local_config=use_local_config,
        )
        return cls(
            config,
            console=console,
            dry_run=dry_run,
            force=force,
            assume_yes=assume_yes,
            no_input=no_input,
            hooks_enabled=hooks_enabled,
        )

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def select(
        self,
        *,
        tags: Optional[Sequence[str]] = None,
        exclude_tags: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
        skip: Optional[Sequence[str]] = None,
        include_disabled: bool = False,
    ) -> List[Entry]:
        return self.config.select(
            tags=tags,
            exclude_tags=exclude_tags,
            only=only,
            skip=skip,
            include_disabled=include_disabled,
        )

    def plan(
        self,
        *,
        tags: Optional[Sequence[str]] = None,
        exclude_tags: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
        skip: Optional[Sequence[str]] = None,
        include_disabled: bool = False,
        prune: Optional[bool] = None,
        with_diffs: bool = True,
    ) -> Plan:
        """Inspect every selected entry without touching the file system."""

        started = time.monotonic()
        if self.hooks.enabled:
            self.hooks.run("pre_plan", {"tags": ",".join(tags or [])}, check=False)
        plan = Plan(
            profiles=self.config.active_profiles(),
            tags=list(tags or []),
            hostname=self.config.facts.hostname,
            warnings=list(self.config.warnings),
        )
        entries = self.select(tags=tags, exclude_tags=exclude_tags, only=only, skip=skip,
                              include_disabled=include_disabled)
        for entry in entries:
            change = self.deployer.inspect(entry)
            if change.diff and not with_diffs:
                change.diff = ""
            plan.changes.append(change)

        want_prune = self.config.settings.get("prune", True) if prune is None else prune
        if want_prune:
            known_names = {entry.key for entry in self.config.entries}
            known_targets = {str(entry.target) for entry in self.config.entries}
            for orphan in self.state.orphaned(known_names, known_targets):
                plan.removals.append(self._orphan_change(orphan))

        plan.duration_ms = (time.monotonic() - started) * 1000
        if self.hooks.enabled:
            counts = plan.counts()
            self.hooks.run(
                "post_plan",
                {"mutating": len(plan.mutating), "conflicts": len(plan.conflicts),
                 "changes": len(plan.changes), **{f"count_{k}": v for k, v in counts.items()}},
                check=False,
            )
        return plan

    def _orphan_change(self, orphan: EntryState) -> Change:
        entry = Entry(target_raw=orphan.target, source=orphan.source, name=orphan.name, mode=orphan.mode)
        entry.target = Path(orphan.target)
        change = Change(entry=entry, status=REMOVE, target=entry.target, detail="no longer managed")
        current = self.deployer._current_fingerprint(entry.target)
        change.previous = current
        if not (entry.target.exists() or entry.target.is_symlink()):
            change.status = UNCHANGED
            change.detail = "already gone"
        elif orphan.fingerprint and current and current != orphan.fingerprint:
            change.status = DRIFT
            change.detail = "modified since deployment (kept)"
        return change

    def status(self, **kwargs: Any) -> Plan:
        return self.plan(**kwargs)

    def diff(
        self,
        *,
        tags: Optional[Sequence[str]] = None,
        exclude_tags: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
    ) -> Plan:
        return self.plan(tags=tags, exclude_tags=exclude_tags, only=only, with_diffs=True)

    def doctor(self) -> List[Check]:
        if self.hooks.enabled:
            self.hooks.run("pre_doctor", {}, check=False)
        checks = diagnose(self)
        if self.hooks.enabled:
            counts = summarise(checks)
            self.hooks.run(
                "post_doctor",
                {"status": worst_status(checks), "failures": counts.get("fail", 0),
                 "warnings": counts.get("warn", 0)},
                check=False,
            )
        return checks

    # ------------------------------------------------------------------ #
    # mutation
    # ------------------------------------------------------------------ #
    def apply(
        self,
        plan: Optional[Plan] = None,
        *,
        tags: Optional[Sequence[str]] = None,
        exclude_tags: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
        skip: Optional[Sequence[str]] = None,
        prune: Optional[bool] = None,
        backup: bool = True,
        fail_fast: bool = False,
        confirm: Optional[bool] = None,
        with_hooks: bool = True,
        record_state: bool = True,
    ) -> ApplyResult:
        """Execute a plan (or compute one first) and update the state file."""

        started = time.monotonic()
        plan = plan or self.plan(tags=tags, exclude_tags=exclude_tags, only=only, skip=skip, prune=prune)
        result = ApplyResult(plan=plan, dry_run=self.dry_run, state_path=str(self.state.path))
        result.planned_counts = dict(plan.counts())  # before execution mutates statuses

        want_prune = self.config.settings.get("prune", True) if prune is None else prune
        if want_prune:
            seen = {c.entry.key for c in plan.removals}
            known_names = {entry.key for entry in self.config.entries}
            for orphan in self.state.orphaned(known_names, {str(e.target) for e in self.config.entries}):
                if orphan.name not in seen:
                    plan.removals.append(self._orphan_change(orphan))

        should_confirm = self.config.settings.get("confirm", False) if confirm is None else confirm
        if should_confirm and plan.mutating and not self.assume_yes:
            self.console.write("")
            plan.render_summary(self.console)
            if not prompt_yes_no(
                f"Apply {len(plan.mutating)} change(s)?", default=False, console=self.console,
                assume_yes=self.assume_yes, no_input=self.no_input,
            ):
                self.console.warn("aborted by user")
                result.duration_ms = (time.monotonic() - started) * 1000
                return result

        lock_timeout = float(self.config.settings.get("lock_timeout", 30))
        with ProjectLock(self.config.state_dir, timeout=lock_timeout, command="apply"):
            if with_hooks and not self.dry_run:
                self.hooks.run(
                    "pre_apply",
                    {
                        "changes": len(plan.mutating),
                        "conflicts": len(plan.conflicts),
                        "profiles": ",".join(self.config.active_profiles()),
                    },
                )
            try:
                self._execute(plan, result, backup=backup, fail_fast=fail_fast, with_hooks=with_hooks)
            except DotDeployerError as exc:
                if with_hooks and not self.dry_run:
                    self._run_error_hook(exc)
                raise
            except KeyboardInterrupt:
                self.console.write("")
                self.console.warn("interrupted - already applied changes are kept and recorded")
                self._save_state(record_state)
                raise

            if record_state:
                self._save_state(True)
            if backup and not self.dry_run:
                result.backup_id = self.backups.commit()
                removed = self.backups.purge(keep=self.keep_backups)
                if removed:
                    self.console.debug(f"purged {len(removed)} old backup set(s)")
            if with_hooks and not self.dry_run:
                self.hooks.run(
                    "post_apply",
                    {
                        "applied": len(result.applied),
                        "failed": len(result.errors),
                        "removed": len(result.removals),
                    },
                )

        result.hooks = self.hooks.summary()
        result.duration_ms = (time.monotonic() - started) * 1000
        return result

    def _execute(
        self,
        plan: Plan,
        result: ApplyResult,
        *,
        backup: bool,
        fail_fast: bool,
        with_hooks: bool,
    ) -> None:
        previous_backup_enabled = self.backups.enabled
        self.backups.enabled = backup
        try:
            for change in plan.changes:
                self._apply_one(change, result, with_hooks=with_hooks)
                if fail_fast and change.status == ERROR:
                    self.console.warn("fail-fast: stopping after the first error")
                    break

            for change in plan.removals:
                if change.status in {UNCHANGED, SKIP, DRIFT}:
                    result.skipped.append(change)
                    continue
                outcome = self.deployer.apply(change)
                if outcome.status == ERROR:
                    result.errors.append(outcome)
                elif outcome.status == REMOVE:
                    result.removals.append(outcome)
                    self.state.forget(outcome.entry.key)
                    if with_hooks and not self.dry_run:
                        self.hooks.run(
                            "post_remove",
                            {"entry": outcome.entry.key, "target": str(outcome.target)},
                        )
                else:
                    result.skipped.append(outcome)
        finally:
            self.backups.enabled = previous_backup_enabled

    def _apply_one(self, change: Change, result: ApplyResult, *, with_hooks: bool) -> None:
        entry = change.entry
        if change.status == UNCHANGED:
            # the target already matches; claim it in the state file so that
            # `status`, `uninstall` and rollback know it belongs to this project
            if not self.dry_run and self.state.get(entry.name) is None and change.deployable:
                self.deployer.record(
                    change,
                    profile=self.config.profile_names[0] if self.config.profile_names else "",
                )
            result.skipped.append(change)
            return
        if change.status in {SKIP, DRIFT} and entry.strategy == "skip":
            result.skipped.append(change)
            return
        if change.status == DRIFT and not self.force:
            self.console.warn(
                f"{display_path(entry.target)} was modified locally - skipping "
                "(use --force to overwrite, or --adopt to keep the local version)"
            )
            result.skipped.append(change)
            return

        if with_hooks and not self.dry_run and change.mutating:
            self.hooks.run("pre_entry", {"entry": entry.key, "target": str(entry.target),
                                         "mode": entry.mode, "status": change.status})

        outcome = self.deployer.apply(change)
        if outcome.status == ERROR:
            self.console.error(f"{display_path(outcome.target)}: {outcome.error}")
            result.errors.append(outcome)
            return
        if outcome.status in {UNCHANGED, SKIP}:
            result.skipped.append(outcome)
            return

        # any executed change is recorded - including conflicts we resolved
        if not self.dry_run:
            self.deployer.record(
                outcome,
                profile=self.config.profile_names[0] if self.config.profile_names else "",
            )
        result.applied.append(outcome)
        self.console.ok(
            f"{display_path(outcome.target)}"
            + (f"  {outcome.detail}" if outcome.detail else "")
            + (f"  [backup: {outcome.backup}]" if outcome.backup else "")
        )

        if with_hooks and not self.dry_run:
            self.hooks.run(
                "post_entry",
                {"entry": entry.key, "target": str(entry.target), "mode": entry.mode,
                 "status": outcome.status, "backup": outcome.backup or ""},
            )

    def _run_error_hook(self, exc: BaseException) -> None:
        try:
            self.hooks.run("on_error", {"error": str(exc), "kind": type(exc).__name__}, check=False)
        except HookError:  # pragma: no cover - never mask the original error
            pass

    def _save_state(self, record: bool) -> None:
        if not record or self.dry_run:
            return
        self.state.meta = {
            "config": str(self.config.path),
            "hostname": self.config.facts.hostname,
            "profiles": self.config.active_profiles(),
            "version": self.config.raw_data.get("version", 1),
        }
        self.state.add_history(
            {
                "action": "apply",
                "hostname": self.config.facts.hostname,
                "profiles": self.config.active_profiles(),
            }
        )
        try:
            self.state.save()
        except OSError as exc:
            raise StateError(f"cannot write state file {self.state.path}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # other operations
    # ------------------------------------------------------------------ #
    def rollback(
        self,
        backup_id: Optional[str] = None,
        *,
        list_only: bool = False,
        keep_state: bool = False,
    ) -> Dict[str, Any]:
        """Restore a backup set (newest by default)."""

        sets = self.backups.sets()
        if list_only:
            return {"sets": sets, "total_size": self.backups.total_size()}
        if not sets:
            raise ActionError(
                "no backup sets found",
                hint="backups are created on every apply that replaces an existing file",
            )
        chosen = None
        if backup_id:
            for item in sets:
                if item["id"] == backup_id or item["id"].startswith(backup_id):
                    chosen = item
                    break
            if chosen is None:
                raise ActionError(
                    f"no backup set matches {backup_id!r}",
                    hint="run 'dotdeployer rollback --list' to see the available sets",
                )
        else:
            chosen = sets[0]

        with ProjectLock(self.config.state_dir, timeout=float(self.config.settings["lock_timeout"]),
                         command="rollback"):
            if not self.dry_run:
                self.hooks.run("pre_restore", {"backup": chosen["id"]}, check=False)
            restored = self.backups.restore(chosen["id"], dry_run=self.dry_run, console=self.console)
            forgotten: List[str] = []
            if not keep_state and not self.dry_run:
                targets = {str(Path(t)) for t in restored}
                for name, entry_state in list(self.state.entries.items()):
                    if str(Path(entry_state.target)) in targets:
                        self.state.forget(name)
                        forgotten.append(name)
                self.state.add_history({"action": "rollback", "backup": chosen["id"],
                                        "restored": len(restored)})
                self.state.save()
            if not self.dry_run:
                self.hooks.run("post_restore", {"backup": chosen["id"], "restored": len(restored)}, check=False)
        return {
            "backup": chosen["id"],
            "restored": restored,
            "forgotten": forgotten,
            "dry_run": self.dry_run,
        }

    def uninstall(
        self,
        *,
        backup: bool = True,
        keep_state: bool = False,
        only: Optional[Sequence[str]] = None,
    ) -> List[Change]:
        """Remove every deployed target (the reverse of ``apply``)."""

        changes: List[Change] = []
        wanted = set(only or [])
        with ProjectLock(self.config.state_dir, timeout=float(self.config.settings["lock_timeout"]),
                         command="uninstall"):
            if backup and not self.dry_run:
                pass  # sets are started lazily by BackupStore.save()
            for state_entry in list(self.state.entries.values()):
                if wanted and state_entry.name not in wanted and state_entry.target not in wanted:
                    continue
                change = self.deployer.remove(state_entry, backup=backup)
                changes.append(change)
                if change.status == REMOVE:
                    self.state.forget(state_entry.name)
                    if not self.dry_run:
                        self.hooks.run("post_remove", {"entry": state_entry.name,
                                                       "target": state_entry.target}, check=False)
            if not self.dry_run:
                if backup:
                    self.backups.commit()
                if not keep_state:
                    self.state.add_history({"action": "uninstall", "removed": len(changes)})
                    self.state.save()
        return changes

    def adopt(
        self,
        targets: Sequence[str],
        *,
        move: bool = True,
        mode: str = "symlink",
        tags: Optional[Sequence[str]] = None,
    ) -> List[Entry]:
        """Pull existing files into the project and register them as entries."""

        adopted: List[Entry] = []
        source_root = self.config.source_root
        ensure_dir(source_root)
        for raw in targets:
            path = Path(os.path.expanduser(os.path.expandvars(str(raw))))
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            if not path.exists():
                self.console.warn(f"skipping {path}: does not exist")
                continue
            if not os.access(path, os.R_OK):
                self.console.warn(f"skipping {path}: not readable")
                continue
            try:
                relative = path.relative_to(Path.home())
            except ValueError:
                relative = Path(path.name)
            destination = source_root / relative
            ensure_dir(destination.parent)
            if destination.exists():
                self.console.warn(f"source already exists, not overwriting: {destination}")
                continue
            self.console.info(f"adopting {display_path(path)} → {display_path(destination)}")
            portable_target = display_path(path)
            if self.dry_run:
                adopted.append(
                    Entry(target_raw=portable_target, source=str(destination), mode=mode,
                          tags=list(tags or []))
                )
                continue
            shutil_move = _move_path if move else _copy_path
            shutil_move(path, destination)
            entry = self.config._build_entry(
                {"source": str(destination), "target": portable_target, "mode": mode,
                 "tags": list(tags or [])},
                origin="adopt",
                mode_default=mode,
            )
            entry.source_path = destination
            entry.target = path
            self.config.entries.append(entry)
            adopted.append(entry)

        if adopted and not self.dry_run:
            self.config.add_entries_to_file(adopted)
            self.console.ok(f"added {len(adopted)} entr(ies) to {display_path(self.config.path)}")
            self.console.note("run 'dotdeployer apply' to link the adopted files back into place")
        return adopted

    # ------------------------------------------------------------------ #
    def info(self) -> Dict[str, Any]:
        return {
            "config": self.config.describe(),
            "state": self.state.stats(),
            "backups": {
                "count": len(self.backups.sets()),
                "size": self.backups.total_size(),
                "dir": str(self.backups.root),
            },
            "secrets": self.secrets.describe(),
            "hooks": {phase: len(cmds) for phase, cmds in self.config.hooks.items() if cmds},
        }


def _move_path(source: Path, destination: Path) -> None:
    import shutil

    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        raise ActionError(f"cannot move {source} → {destination}: {exc}") from exc


def _copy_path(source: Path, destination: Path) -> None:
    import shutil

    try:
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    except OSError as exc:
        raise ActionError(f"cannot copy {source} → {destination}: {exc}") from exc


# --------------------------------------------------------------------------- #
# module level convenience API
# --------------------------------------------------------------------------- #
def plan(path: Optional[str] = None, **kwargs: Any) -> Plan:
    project = Project.load(path, dry_run=True)
    return project.plan(**kwargs)


def apply(path: Optional[str] = None, *, yes: bool = False, dry_run: bool = False, **kwargs: Any) -> ApplyResult:
    project = Project.load(path, dry_run=dry_run, assume_yes=yes)
    return project.apply(**kwargs)


def rollback(path: Optional[str] = None, backup_id: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    project = Project.load(path, dry_run=kwargs.pop("dry_run", False))
    return project.rollback(backup_id, **kwargs)
