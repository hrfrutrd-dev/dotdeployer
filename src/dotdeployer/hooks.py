"""Lifecycle hooks: run user commands around deployment phases.

Hooks are declared per phase in the config and rendered with a template
context before execution, so commands can reference entry metadata::

    [hooks]
    pre_apply  = ["echo deploying on {{ hostname }}"]
    post_apply = ["chsh -s $(which zsh) {{ user }}", "reload-shell"]

Every hook receives ``DOTDEPLOYER_*`` environment variables describing the
current operation, which makes scripts context aware without extra plumbing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import HookError
from .template import Environment, Template, TemplateError
from .ui import Console, Style
from .util import CommandResult, run_command, truncate

PHASES = (
    "pre_init",
    "post_init",
    "pre_apply",
    "post_apply",
    "pre_entry",
    "post_entry",
    "pre_remove",
    "post_remove",
    "pre_restore",
    "post_restore",
    "on_error",
    "pre_plan",
    "post_plan",
    "pre_doctor",
    "post_doctor",
)


@dataclass
class HookRun:
    phase: str
    command: str
    result: Optional[CommandResult] = None
    error: Optional[str] = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok


class HookRunner:
    """Executes hook commands for a project."""

    def __init__(
        self,
        config: Any,
        *,
        console: Optional[Console] = None,
        dry_run: bool = False,
        enabled: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.config = config
        self.console = console or Console()
        settings = getattr(config, "settings", {}) or {}
        self.enabled = bool(settings.get("hooks_enabled", True)) if enabled is None else enabled
        env_flag = os.environ.get("DOTDEPLOYER_HOOKS")
        if env_flag is not None:
            self.enabled = env_flag.strip().lower() not in {"0", "false", "no", "off"}
        self.timeout = float(timeout if timeout is not None else settings.get("hook_timeout", 300))
        self.shell = str(settings.get("hook_shell") or "/bin/sh")
        self.continue_on_error = bool(settings.get("hooks_continue_on_error", False))
        self.runs: List[HookRun] = []
        self.context: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def _environment(self, phase: str, context: Mapping[str, Any]) -> Dict[str, str]:
        env = {
            "DOTDEPLOYER_PHASE": phase,
            "DOTDEPLOYER_ROOT": str(getattr(self.config, "root", "")),
            "DOTDEPLOYER_CONFIG": str(getattr(self.config, "path", "")),
            "DOTDEPLOYER_HOSTNAME": str(getattr(self.config, "facts", None).hostname if hasattr(getattr(self.config, "facts", None), "hostname") else ""),
            "DOTDEPLOYER_PROFILES": ",".join(getattr(self.config, "profile_names", []) or []),
        }
        for key, value in context.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                env[f"DOTDEPLOYER_{str(key).upper()}"] = str(value)
        return env

    def _render(self, command: str, phase: str, context: Mapping[str, Any]) -> str:
        if "{{" not in command:
            return command
        variables: Dict[str, Any] = dict(getattr(self.config, "variables", {}) or {})
        variables.update(context)
        variables.setdefault("phase", phase)
        try:
            return Template(command, f"<hook:{phase}>").render(Environment(variables, strict=False))
        except TemplateError as exc:
            raise HookError(f"cannot render {phase} hook {command!r}: {exc}") from exc

    # ------------------------------------------------------------------ #
    def has(self, phase: str) -> bool:
        return bool(self.config.hooks_for(phase))

    def run(
        self,
        phase: str,
        context: Optional[Mapping[str, Any]] = None,
        *,
        extra: Optional[Iterable[str]] = None,
        check: bool = True,
    ) -> List[HookRun]:
        """Run every hook registered for *phase*."""

        commands = list(self.config.hooks_for(phase))
        if extra:
            commands.extend(extra)
        if not commands:
            return []
        ctx = dict(self.context)
        ctx.update(context or {})
        runs: List[HookRun] = []
        for raw in commands:
            run = self._run_one(phase, raw, ctx, check=check)
            runs.append(run)
            self.runs.append(run)
        return runs

    def _run_one(self, phase: str, raw: str, context: Mapping[str, Any], *, check: bool) -> HookRun:
        command = self._render(raw, phase, context)
        if not self.enabled:
            self.console.debug(f"hook {phase} skipped (hooks disabled): {truncate(command, 70)}")
            return HookRun(phase=phase, command=command, skipped=True)
        if getattr(self, "dry_run", False):
            self.console.info(f"[dry-run] {phase}: {command}")
            return HookRun(phase=phase, command=command, skipped=True)

        self.console.info(f"{phase}: {self.console.paint(command, Style.GREY)}")
        try:
            result = run_command(
                command,
                cwd=getattr(self.config, "root", None),
                shell=True,
                timeout=self.timeout,
                env=self._environment(phase, context),
            )
        except Exception as exc:
            run = HookRun(phase=phase, command=command, error=str(exc))
            if check and not self.continue_on_error:
                raise HookError(f"{phase} hook failed: {command}", command=command) from exc
            self.console.warn(f"{phase} hook failed: {exc}")
            return run

        run = HookRun(phase=phase, command=command, result=result)
        if not result.ok:
            message = (result.stderr or result.stdout or "").strip()
            if check and not self.continue_on_error:
                raise HookError(
                    f"{phase} hook exited with {result.returncode}: {command}"
                    + (f"\n  {truncate(message, 400)}" if message else ""),
                    command=command,
                    returncode=result.returncode,
                )
            self.console.warn(f"{phase} hook exited with {result.returncode}: {truncate(message or command, 160)}")
        elif result.stdout:
            for line in result.stdout.splitlines()[:20]:
                self.console.note(line)
        return run

    # ------------------------------------------------------------------ #
    def summary(self) -> Dict[str, Any]:
        return {
            "total": len(self.runs),
            "failed": sum(1 for run in self.runs if run.result is not None and not run.result.ok),
            "skipped": sum(1 for run in self.runs if run.skipped),
            "phases": sorted({run.phase for run in self.runs}),
        }
