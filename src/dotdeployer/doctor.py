"""Health checks: validate a project before it touches the file system.

``dotdeployer doctor`` runs every check below and reports pass/warn/fail with an
actionable hint for each problem.  The same checks power ``--strict`` mode in
CI: any failure makes the command exit non-zero.
"""

from __future__ import annotations

import os
import stat as stat_mod
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Config, Entry
from .secrets import SecretResolver, backends_available
from .template import Template, TemplateError
from .ui import Console, Style
from .util import display_path, is_windows, which

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

# informational checks never count as a problem
_RANK = {OK: 0, INFO: 0, WARN: 2, FAIL: 3}


@dataclass
class Check:
    name: str
    status: str
    message: str
    hint: str = ""
    category: str = "general"
    details: List[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def warned(self) -> bool:
        return self.status == WARN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "hint": self.hint,
            "category": self.category,
            "details": list(self.details),
        }


def _entry_check(name: str, entry: Entry, status: str, message: str, hint: str = "",
                 details: Optional[List[str]] = None) -> Check:
    return Check(name=name, status=status, message=message, hint=hint, category="entries",
                 details=details or [])


def check_environment() -> List[Check]:
    checks: List[Check] = []
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 9):
        checks.append(Check("python", OK, f"Python {version}", category="environment"))
    else:  # pragma: no cover - unsupported interpreter
        checks.append(Check("python", FAIL, f"Python {version} is too old",
                            hint="dotdeployer needs Python 3.9+", category="environment"))

    try:
        import tomllib  # noqa: F401

        checks.append(Check("toml", OK, "built-in TOML parser (tomllib)", category="environment"))
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401

            checks.append(Check("toml", OK, "TOML parser provided by tomli", category="environment"))
        except ModuleNotFoundError:
            checks.append(
                Check("toml", FAIL, "no TOML parser available",
                      hint="install Python 3.11+ or 'pip install tomli'", category="environment")
            )

    checks.append(
        Check("platform", INFO, f"{sys.platform} · {os.name}", category="environment")
    )

    if is_windows():  # pragma: no cover - platform specific
        checks.append(
            Check("symlinks", WARN, "Windows detected: symlink creation needs Developer Mode or admin rights",
                  hint="dotdeployer falls back to copying files when symlinks are unavailable",
                  category="environment")
        )
    return checks


def check_tools() -> List[Check]:
    checks: List[Check] = []
    for tool, role, _required in (
        ("git", "version control / hooks", False),
        ("gpg", "decrypting secrets", False),
        ("age", "decrypting secrets", False),
        ("diff", "showing diffs (optional)", False),
        ("zsh", "hooks that switch shells", False),
    ):
        found = which(tool)
        checks.append(
            Check(
                f"tool:{tool}",
                OK if found else INFO,
                f"{tool}: {found}" if found else f"{tool}: not installed ({role})",
                category="tools",
            )
        )
    for backend in backends_available():
        status = OK if backend["available"] else INFO
        message = f"secret backend {backend['name']}: " + (
            "available" if backend["available"] else f"needs {', '.join(backend['requires']) or 'extra setup'}"
        )
        checks.append(Check(f"secret-backend:{backend['name']}", status, message, category="secrets"))
    return checks


def check_config(config: Config) -> List[Check]:
    checks: List[Check] = [
        Check("config", OK, f"loaded {display_path(config.path)}", category="config"),
    ]
    for warning in config.warnings:
        checks.append(Check("config:warnings", WARN, warning, category="config"))
    if config.host_config:
        checks.append(Check("config:host", OK, f"host overrides from {display_path(config.host_config)}",
                            category="config"))
    if config.local_config:
        checks.append(Check("config:local", OK, f"local overrides from {display_path(config.local_config)}",
                            category="config"))
    if config.includes:
        checks.append(Check("config:includes", OK,
                            f"{len(config.includes)} included file(s)", category="config"))
    if not config.entries:
        checks.append(Check("config:entries", WARN, "no entries defined",
                            hint="add a [files] table or [[entry]] blocks", category="config"))
    if not config.source_root.is_dir():
        checks.append(Check("source_root", FAIL, f"source root does not exist: {config.source_root}",
                            hint="create it or fix settings.source_root", category="config"))
    else:
        checks.append(Check("source_root", OK, f"source root: {display_path(config.source_root)}",
                            category="config"))
    if config.active_profiles():
        checks.append(Check("profiles", OK, "active profiles: " + ", ".join(config.active_profiles()),
                            category="config"))
    return checks


def check_state(project: Any) -> List[Check]:
    checks: List[Check] = []
    config = project.config
    state = project.state
    checks.append(Check("state", OK, f"{len(state.entries)} tracked target(s)", category="state"))
    for name, entry_state in state.entries.items():
        target = Path(entry_state.target)
        if not (target.exists() or target.is_symlink()):
            checks.append(
                Check(f"state:{name}", WARN, f"tracked target is missing: {display_path(target)}",
                      hint="run 'dotdeployer apply' to recreate it, or 'dotdeployer uninstall --prune'",
                      category="state")
            )
    if not os.access(config.state_dir, os.W_OK):
        checks.append(Check("state:dir", FAIL, f"state dir is not writable: {config.state_dir}",
                            category="state"))
    if os.access(config.state_dir, os.W_OK):
        checks.append(Check("state:dir", OK, f"state dir writable: {display_path(config.state_dir)}",
                            category="state"))
    return checks


def check_entries(project: Any) -> List[Check]:
    config: Config = project.config
    checks: List[Check] = []
    env = config.template_env()
    env.secret_resolver = project.secrets.resolve_optional
    for entry in config.entries:
        label = f"entry:{entry.key}"
        if entry.source_path is None:
            if entry.mode == "mkdir":
                checks.append(_entry_check(label, entry, OK, f"{display_path(entry.target)} (directory)"))
            continue
        source = entry.source_path
        if not source.exists() and not source.is_symlink():
            checks.append(
                _entry_check(label, entry, FAIL, f"source missing: {display_path(source)}",
                             "create the file or fix the 'source' path")
            )
            continue
        if entry.mode == "template":
            template_files: List[Path] = []
            if source.is_dir():
                template_files = [p for p in source.rglob("*") if p.is_file()]
            else:
                template_files = [source]
            problems: List[str] = []
            for path in template_files:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    entry_env = config.template_env({**entry.vars, "entry": {
                        "name": entry.key, "target": str(entry.target), "source": entry.source,
                        "mode": entry.mode, "tags": list(entry.tags),
                    }})
                    entry_env.secret_resolver = project.secrets.resolve_optional
                    Template(text, str(path)).render(entry_env)
                except TemplateError as exc:
                    problems.append(f"{path.name}: {exc}")
                except Exception as exc:
                    problems.append(f"{path.name}: {exc}")
            if problems:
                checks.append(
                    _entry_check(label, entry, FAIL, "template cannot be rendered",
                                 "fix the template or provide the missing variable",
                                 details=problems[:5])
                )
            else:
                checks.append(
                    _entry_check(label, entry, OK,
                                 f"template ok ({len(template_files)} file(s)) → {display_path(entry.target)}")
                )
            continue
        if entry.encrypt and not (which("gpg") or which("age")):
            checks.append(
                _entry_check(label, entry, FAIL, "encrypted source but no gpg/age installed",
                             "install gnupg or age, or remove 'encrypt = true'")
            )
            continue

        if source.is_dir() and entry.mode == "symlink":
            checks.append(_entry_check(label, entry, OK, f"directory link → {display_path(entry.target)}"))
        elif source.is_file():
            try:
                size = source.stat().st_size
            except OSError:  # pragma: no cover
                size = 0
            details = []
            mode_bits = stat_mod.S_IMODE(source.stat().st_mode)
            if entry.permissions is not None and mode_bits != entry.permissions:
                details.append(f"source mode {mode_bits:04o} → target mode {entry.permissions:04o}")
            checks.append(
                _entry_check(label, entry, OK,
                             f"{display_path(entry.target)} ← {display_path(source)} "
                             f"({size} B, {entry.mode})", details=details)
            )
        else:
            checks.append(_entry_check(label, entry, OK, f"{display_path(entry.target)}"))
    return checks


def check_targets(project: Any) -> List[Check]:
    config: Config = project.config
    checks: List[Check] = []
    seen: Dict[str, str] = {}
    home_str = str(Path.home())
    for entry in config.entries:
        target = entry.target
        dup = seen.get(str(target))
        if dup:
            checks.append(_entry_check(f"target:{entry.key}", entry, FAIL,
                                       f"duplicate target with entry {dup!r}",
                                       "remove one of the entries"))
        seen[str(target)] = entry.key

        if target == Path("/"):
            checks.append(_entry_check(f"target:{entry.key}", entry, FAIL, "target is the root directory",
                                       "refusing to manage '/'"))
            continue
        if target.is_symlink():
            link = os.readlink(str(target))
            if not os.path.exists(str(target)):
                checks.append(
                    _entry_check(f"target:{entry.key}", entry, WARN,
                                 f"broken symlink at {display_path(target)} → {link}",
                                 "dotdeployer will replace it on the next apply")
                )
        if entry.source_path and str(target).startswith(str(config.source_root)):
            checks.append(
                _entry_check(f"target:{entry.key}", entry, FAIL,
                             f"target is inside the source tree: {display_path(target)}",
                             "move the target outside source_root")
            )
        parent = target.parent
        if parent.exists() and not os.access(parent, os.W_OK) and not entry.disabled:
            checks.append(
                _entry_check(f"target:{entry.key}", entry, WARN,
                             f"parent directory is not writable: {display_path(parent)}",
                             "run with sudo or fix permissions")
            )
        if home_str not in str(target) and entry.mode == "symlink" and is_windows():  # pragma: no cover
            checks.append(
                _entry_check(f"target:{entry.key}", entry, INFO, "target is outside $HOME on Windows")
            )
    if not checks:
        checks.append(Check("targets", OK, "all targets look sane", category="entries"))
    return checks


def check_secrets(project: Any) -> List[Check]:
    config: Config = project.config
    checks: List[Check] = []
    resolver: SecretResolver = project.secrets
    names = sorted({name for entry in config.entries for name in entry.secrets})
    if not config.secrets_config:
        if names:
            checks.append(
                Check("secrets", WARN,
                      f"entries reference secrets ({', '.join(names)}) but [secrets] is empty",
                      hint="configure a backend, e.g. [secrets] order = [\"env\", \"file\"]",
                      category="secrets")
            )
    else:
        checks.append(
            Check("secrets", OK,
                  "backends: " + (", ".join(b.name for b in resolver.backends) or "none active"),
                  category="secrets")
        )
    for warning in resolver.warnings:
        checks.append(Check("secrets:warning", WARN, warning, category="secrets"))
    for name in names:
        value = resolver.resolve_optional(name)
        if value is None:
            checks.append(
                Check(f"secret:{name}", WARN, f"secret {name!r} cannot be resolved",
                      hint="store it in the configured backend, or give the template a fallback "
                           "with secret('name', 'default')",
                      category="secrets")
            )
        else:
            checks.append(Check(f"secret:{name}", OK, f"secret {name!r} resolved ({len(value)} chars)",
                                category="secrets"))
    return checks


def check_hooks(project: Any) -> List[Check]:
    config: Config = project.config
    checks: List[Check] = []
    for phase, commands in config.hooks.items():
        for command in commands:
            program = command.strip().split()[0] if command.strip() else ""
            bare = os.path.basename(program)
            # scripts shipped with the project (./hooks/x.sh, hooks/x.sh) are fine
            local = bool(program) and (
                (config.root / program).is_file() or (config.root / program.lstrip("./")).is_file()
            )
            if bare and not local and not which(bare) and "{{" not in program:
                checks.append(
                    Check(f"hook:{phase}", WARN, f"hook references {bare!r} which is not on PATH",
                          hint=f"command: {command}", category="hooks")
                )
            else:
                checks.append(Check(f"hook:{phase}", OK, f"{phase}: {command[:60]}", category="hooks"))
    return checks


def check_drift(project: Any) -> List[Check]:
    checks: List[Check] = []
    plan = project.plan(with_diffs=False)
    for change in plan.with_status("drift"):
        checks.append(
            Check(f"drift:{change.entry.key}", WARN,
                  f"{display_path(change.target)} was modified after deployment",
                  hint="run 'dotdeployer diff' to see the changes, then apply --force or adopt",
                  category="drift")
        )
    for change in plan.conflicts:
        checks.append(
            Check(f"conflict:{change.entry.key}", WARN,
                  f"{display_path(change.target)} exists and is not managed by dotdeployer",
                  hint="apply will back it up, or set strategy = 'skip' to leave it alone",
                  category="drift")
        )
    if not checks:
        checks.append(Check("drift", OK, "no drift or conflicts detected", category="drift"))
    return checks


def diagnose(project: Any, *, include_drift: bool = True, include_hooks: bool = True) -> List[Check]:
    """Run every check and return the collected results."""

    checks: List[Check] = []
    checks.extend(check_environment())
    checks.extend(check_tools())
    checks.extend(check_config(project.config))
    checks.extend(check_targets(project))
    checks.extend(check_entries(project))
    checks.extend(check_secrets(project))
    checks.extend(check_state(project))
    if include_hooks:
        checks.extend(check_hooks(project))
    if include_drift:
        checks.extend(check_drift(project))
    return checks


def summarise(checks: Sequence[Check]) -> Dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def worst_status(checks: Sequence[Check]) -> str:
    status = OK
    for check in checks:
        if _RANK.get(check.status, 0) > _RANK.get(status, 0):
            status = check.status
    return status


def render(checks: Sequence[Check], console: Console, *, quiet_pass: bool = True,
           show_info: bool = False, categories: bool = True) -> None:
    """Print check results, grouped by category."""

    colours = {
        OK: Style.GREEN,
        WARN: Style.YELLOW,
        FAIL: Style.RED,
        INFO: Style.GREY,
    }
    glyphs = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "•"}
    if categories:
        groups: Dict[str, List[Check]] = {}
        for check in checks:
            groups.setdefault(check.category, []).append(check)
        for category, items in groups.items():
            console.blank()
            console.rule(category)
            _render_group(items, console, colours, glyphs, quiet_pass, show_info)
    else:
        _render_group(list(checks), console, colours, glyphs, quiet_pass, show_info)


def _render_group(items: Sequence[Check], console: Console, colours: Dict[str, str],
                  glyphs: Dict[str, str], quiet_pass: bool, show_info: bool) -> None:
    rank = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}
    for check in sorted(items, key=lambda c: rank.get(c.status, 0)):
        if check.status == OK and quiet_pass and console.verbosity < 2:
            continue
        if check.status == INFO and not show_info and console.verbosity < 2:
            continue
        glyph = console.paint(glyphs[check.status], colours[check.status])
        console.write(f"  {glyph} {check.name}: {check.message}", verbosity=0)
        if check.hint:
            console.write(f"      {console.paint('→ ' + check.hint, Style.GREY)}", verbosity=0)
        for detail in check.details:
            console.write(f"      {console.paint(detail, Style.GREY)}", verbosity=0)
