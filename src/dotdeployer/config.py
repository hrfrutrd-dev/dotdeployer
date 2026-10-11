"""Project configuration: loading, layering, validation and entry modelling.

A dotdeployer project is a directory containing ``dotdeployer.toml`` (TOML,
JSON or YAML are accepted) plus the actual dotfiles.  The configuration is
declarative: every managed file is an *entry* with a source, a target and a
handful of options.

Example (``dotdeployer.toml``)::

    [settings]
    source_root = "dotfiles"
    strategy    = "backup"

    [vars]
    editor = "nvim"

    [files]
    "~/.zshrc"          = "shell/zshrc"
    "~/.gitconfig"      = { source = "git/gitconfig", template = true }
    "~/.config/nvim"    = { source = "nvim", mode = "symlink", tags = ["nvim"] }

    [[entry]]
    name   = "ssh-config"
    source = "ssh/config"
    target = "~/.ssh/config"
    permissions = "0600"
    when   = "not is_container"

Layering (lowest to highest precedence): ``include``d files, main file,
``hosts/<hostname>.toml``, ``local.toml``, active profiles, CLI ``--var``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from .conditions import Condition, SystemFacts, facts_vars
from .errors import ConfigError
from .template import Environment, Template, TemplateError
from .util import (
    CONFIG_FILENAMES,
    atomic_write,
    deep_merge,
    expand_path,
    home,
    is_relative_to,
    json_dump,
    slugify,
)

__all__ = ["MODES", "STRATEGIES", "Config", "Entry", "Profile"]

MODES = ("symlink", "copy", "template", "mkdir", "ignore")
STRATEGIES = ("backup", "override", "skip", "ask", "merge")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "source_root": "dotfiles",
    "strategy": "backup",
    "backup_keep": 10,
    "hooks_enabled": True,
    "hook_shell": "/bin/sh",
    "hook_timeout": 300,
    "prune": True,
    "confirm": False,
    "lock_timeout": 30,
    "snapshot": True,
    "diff_context": 3,
    "color": "auto",
    "expand_env": True,
    "strict_templates": False,
    "fail_on_drift": False,
    "max_backup_bytes": 256 * 1024 * 1024,
}

_SHORTHAND_TABLES = {
    "files": "symlink",
    "symlinks": "symlink",
    "copies": "copy",
    "templates": "template",
    "dirs": "mkdir",
    "directories": "mkdir",
}

_ENTRY_KEYS = {
    "name",
    "id",
    "source",
    "src",
    "target",
    "dest",
    "destination",
    "mode",
    "strategy",
    "on_conflict",
    "permissions",
    "mode_bits",
    "perms",
    "chmod",
    "owner",
    "group",
    "when",
    "unless",
    "tags",
    "profiles",
    "profile",
    "platforms",
    "platform",
    "hosts",
    "host",
    "vars",
    "template",
    "templated",
    "encrypt",
    "decrypt",
    "secrets",
    "comment",
    "description",
    "disabled",
    "enabled",
    "parents",
    "create_parents",
    "strip_trailing_whitespace",
    "ensure_newline",
    "expand_env",
}

_UNSUPPORTED_KEYS = {"os", "if", "requires"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_toml(text: str, path: Path) -> Dict[str, Any]:
    try:
        import tomllib  # type: ignore

        return tomllib.loads(text)
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        pass
    try:
        import tomli  # type: ignore

        return tomli.loads(text)
    except ModuleNotFoundError as exc:
        raise ConfigError(
            f"{path} is TOML but no TOML parser is available",
            hint="upgrade to Python 3.11+ or 'pip install tomli'",
        ) from exc
    except Exception as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc


def load_structured(path: Path) -> Dict[str, Any]:
    """Load TOML/JSON/YAML into a plain dict, with friendly errors."""

    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ModuleNotFoundError as exc:
                raise ConfigError(
                    f"{path} is YAML but PyYAML is not installed",
                    hint="'pip install pyyaml' or convert the file to TOML",
                ) from exc
            data = yaml.safe_load(text)
        else:
            data = _load_toml(text, path)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a table/object, got {type(data).__name__}")
    return data


_INTERP_RE = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")


def interpolate(text: str, variables: Mapping[str, Any], *, expand_env: bool = True,
                where: str = "") -> str:
    """Replace ``${name}`` / ``${name:-fallback}`` / ``$name`` in *text*."""

    def _replace(match: re.Match) -> str:
        name = match.group("braced") or match.group("plain")
        default = match.group("default")
        if name in variables and variables[name] is not None:
            return str(variables[name])
        if expand_env and name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"unknown variable ${{{name}}} in {where or text!r}")

    if "$" not in text:
        return text
    return _INTERP_RE.sub(_replace, text)


def _as_list(value: Any, *, field_name: str = "value") -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            if isinstance(item, (str, int, float)):
                out.append(str(item))
            else:
                raise ConfigError(f"{field_name} must contain strings, got {type(item).__name__}")
        return out
    if isinstance(value, bool):
        return []
    raise ConfigError(f"{field_name} must be a string or list of strings, got {type(value).__name__}")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalise_permissions(value: Any, where: str = "") -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        if value > 0o7777:
            raise ConfigError(f"{where}: permissions {value!r} is out of range")
        return value
    text = str(value).strip().lower()
    text = text.removeprefix("0o")
    if text.startswith("0") and len(text) > 1:
        text = text[1:]
    try:
        bits = int(text, 8)
    except ValueError as exc:
        raise ConfigError(
            f"{where}: cannot parse permissions {value!r}",
            hint="use an octal string such as \"0644\" or \"600\"",
        ) from exc
    if bits > 0o7777:
        raise ConfigError(f"{where}: permissions {value!r} is out of range")
    return bits


def _slug_name(target_raw: str, source: Optional[str]) -> str:
    seed = target_raw or source or "entry"
    cleaned = seed.replace("~", "home").strip("/\\")
    cleaned = re.sub(r"[^A-Za-z0-9._/-]", "-", cleaned)
    return slugify(cleaned.replace("/", "-")) or "entry"


# --------------------------------------------------------------------------- #
# data classes
# --------------------------------------------------------------------------- #
@dataclass
class Profile:
    name: str
    description: str = ""
    vars: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    entries: List[str] = field(default_factory=list)
    extends: List[str] = field(default_factory=list)
    hooks: Dict[str, Any] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, name: str, data: Any) -> "Profile":
        if data is None:
            data = {}
        if isinstance(data, list):
            data = {"entries": data}
        if not isinstance(data, dict):
            raise ConfigError(f"profile {name!r} must be a table")
        unknown = set(data) - {
            "description", "desc", "vars", "tags", "entries", "extends", "hooks", "settings", "options",
        }
        if unknown:
            raise ConfigError(f"profile {name!r}: unknown key(s): {', '.join(sorted(unknown))}")
        return cls(
            name=name,
            description=str(data.get("description") or data.get("desc") or ""),
            vars=dict(data.get("vars") or {}),
            tags=_as_list(data.get("tags"), field_name=f"profile {name}.tags"),
            entries=_as_list(data.get("entries"), field_name=f"profile {name}.entries"),
            extends=_as_list(data.get("extends"), field_name=f"profile {name}.extends"),
            hooks=dict(data.get("hooks") or {}),
            settings=dict(data.get("settings") or data.get("options") or {}),
        )


@dataclass
class Entry:
    """One managed file, directory or symlink."""

    target_raw: str
    source: Optional[str] = None
    name: str = ""
    mode: str = "symlink"
    strategy: str = "backup"
    permissions: Optional[int] = None
    owner: Optional[str] = None
    group: Optional[str] = None
    when: Condition = field(default_factory=Condition)
    unless: Condition = field(default_factory=Condition)
    tags: List[str] = field(default_factory=list)
    profiles: List[str] = field(default_factory=list)
    vars: Dict[str, Any] = field(default_factory=dict)
    secrets: List[str] = field(default_factory=list)
    encrypt: bool = False
    comment: str = ""
    disabled: bool = False
    create_parents: bool = True
    expand_env: bool = True
    strip_trailing_whitespace: bool = False
    ensure_newline: bool = False
    origin: str = ""  # config file the entry came from

    # resolved during Config.load
    target: Path = field(default_factory=lambda: Path())
    source_path: Optional[Path] = None

    @property
    def key(self) -> str:
        return self.name or _slug_name(self.target_raw, self.source)

    @property
    def is_directory(self) -> bool:
        return self.mode == "mkdir"

    @property
    def needs_source(self) -> bool:
        return self.mode != "mkdir"

    def matches_tags(self, selected: Sequence[str], excluded: Sequence[str]) -> bool:
        if selected and not set(self.tags) & set(selected):
            return False
        if excluded and set(self.tags) & set(excluded):
            return False
        return True

    def to_dict(self, *, include_resolved: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.key,
            "source": self.source,
            "target": self.target_raw,
            "mode": self.mode,
            "strategy": self.strategy,
            "tags": list(self.tags),
            "profiles": list(self.profiles),
            "disabled": self.disabled,
        }
        if self.permissions is not None:
            data["permissions"] = f"{self.permissions:04o}"
        if self.owner:
            data["owner"] = self.owner
        if self.group:
            data["group"] = self.group
        if self.when:
            data["when"] = self.when.raw
        if self.unless:
            data["unless"] = self.unless.raw
        if self.vars:
            data["vars"] = dict(self.vars)
        if self.secrets:
            data["secrets"] = list(self.secrets)
        if self.encrypt:
            data["encrypt"] = True
        if self.comment:
            data["comment"] = self.comment
        if include_resolved:
            data["resolved_target"] = str(self.target)
            data["resolved_source"] = str(self.source_path) if self.source_path else None
        return data


# --------------------------------------------------------------------------- #
# configuration container
# --------------------------------------------------------------------------- #
class Config:
    """A fully loaded, validated project configuration."""

    def __init__(
        self,
        root: Path,
        path: Path,
        *,
        data: Optional[Dict[str, Any]] = None,
        facts: Optional[SystemFacts] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.path = Path(path)
        self.facts = facts or SystemFacts()
        self.raw_data: Dict[str, Any] = data or {}
        self.settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.variables: Dict[str, Any] = {}
        self.profiles: Dict[str, Profile] = {}
        self.profile_names: List[str] = []
        self.entries: List[Entry] = []
        self.hooks: Dict[str, List[str]] = {}
        self.secrets_config: Dict[str, Any] = {}
        self.includes: List[Path] = []
        self.host_config: Optional[Path] = None
        self.local_config: Optional[Path] = None
        self.warnings: List[str] = []
        self._merged: Dict[str, Any] = {}
        self._condition_env: Optional[Environment] = None

    # -- construction ------------------------------------------------------
    @classmethod
    def find(cls, start: Optional[Union[str, Path]] = None) -> Path:
        """Locate the project root, accepting a file, a directory or a cwd."""

        candidate = expand_path(start or Path.cwd())
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            # walk upwards so commands work from anywhere inside the project
            for directory in [candidate, *candidate.parents]:
                for name in CONFIG_FILENAMES:
                    found = directory / name
                    if found.is_file():
                        return found
            raise ConfigError(
                f"no dotdeployer configuration found in {candidate} or any parent directory",
                hint=f"create {candidate / 'dotdeployer.toml'} or run 'dotdeployer init'",
            )
        raise ConfigError(f"path does not exist: {candidate}")

    @classmethod
    def load(
        cls,
        start: Optional[Union[str, Path]] = None,
        *,
        path: Optional[Union[str, Path]] = None,
        var_overrides: Optional[Mapping[str, Any]] = None,
        profiles: Optional[Sequence[str]] = None,
        facts: Optional[SystemFacts] = None,
        use_host_config: bool = True,
        use_local_config: bool = True,
    ) -> "Config":
        config_path = expand_path(path) if path else cls.find(start)
        root = config_path.parent
        facts = facts or SystemFacts()
        config = cls(root, config_path, facts=facts)
        config._merged = config._load_layered(
            config_path, use_host_config=use_host_config, use_local_config=use_local_config
        )
        config._build(var_overrides=var_overrides or {}, profiles=list(profiles or []))
        return config

    # -- loading -----------------------------------------------------------
    def _load_layered(
        self,
        path: Path,
        *,
        use_host_config: bool,
        use_local_config: bool,
        _seen: Optional[Set[Path]] = None,
        _depth: int = 0,
    ) -> Dict[str, Any]:
        seen = _seen if _seen is not None else set()
        if _depth > 8:
            raise ConfigError("config 'include' nesting is too deep (max 8)")
        resolved = path.resolve()
        if resolved in seen:
            raise ConfigError(f"circular 'include' detected at {path}")
        seen.add(resolved)

        data = load_structured(path)
        if path != self.path:
            self.includes.append(path)

        includes = _as_list(data.pop("include", None), field_name=f"{path}: include")
        merged: Dict[str, Any] = {}
        for include in includes:
            included_path = expand_path(interpolate(include, {"root": str(self.root)}, where=str(path)),
                                        base=self.root)
            if not included_path.is_file():
                raise ConfigError(f"included config not found: {included_path} (from {path})")
            merged = deep_merge(merged, self._load_layered(
                included_path, use_host_config=False, use_local_config=False, _seen=seen, _depth=_depth + 1
            ))

        merged = deep_merge(merged, data)

        if use_host_config:
            host_candidates = [
                self.root / "hosts" / f"{self.facts.hostname}.toml",
                self.root / "hosts" / f"{self.facts.hostname}.json",
                self.root / "host-{}.toml".format(self.facts.hostname),
            ]
            for candidate in host_candidates:
                if candidate.is_file() and candidate.resolve() != resolved:
                    self.host_config = candidate
                    merged = deep_merge(merged, load_structured(candidate))
                    break

        if use_local_config and self.path == path:
            for name in ("local.toml", "local.json", ".dotdeployer.local.toml"):
                candidate = self.root / name
                if candidate.is_file():
                    self.local_config = candidate
                    merged = deep_merge(merged, load_structured(candidate))
                    break

        return merged

    # -- building ----------------------------------------------------------
    def _build(self, *, var_overrides: Mapping[str, Any], profiles: Sequence[str]) -> None:
        data = self._merged
        self._validate_top_level(data)

        settings = dict(DEFAULT_SETTINGS)
        user_settings = data.get("settings") or data.get("options") or {}
        if not isinstance(user_settings, dict):
            raise ConfigError("'settings' must be a table")
        unknown_settings = set(user_settings) - set(DEFAULT_SETTINGS)
        for key in unknown_settings:
            self.warnings.append(f"unknown setting {key!r} is ignored")
        settings.update(user_settings)

        source_root_raw = str(settings.get("source_root") or "dotfiles")
        source_root = expand_path(source_root_raw, base=self.root)
        if not source_root.exists():
            fallback = self.root
            if (fallback / "dotfiles").exists():
                source_root = fallback / "dotfiles"
            else:
                source_root = fallback
        self.source_root = source_root
        self.settings = settings

        self.state_dir = expand_path(str(settings.get("state_dir") or ".dotdeployer"), base=self.root)
        self.backup_dir = expand_path(
            str(settings.get("backup_dir") or (self.state_dir / "backups")), base=self.root
        )

        self.profiles = self._build_profiles(data.get("profiles") or {})
        self.profile_names = self._resolve_profile_chain(list(profiles))

        # ---- variables -------------------------------------------------
        variables: Dict[str, Any] = {
            "root": str(self.root),
            "project": self.root.name,
            "source_root": str(source_root),
            "home": str(home()),
            "hostname": self.facts.hostname,
            "user": self.facts.user,
            "os": self.facts.os,
            "arch": self.facts.arch,
        }
        variables.update(facts_vars(self.facts))
        for name, profile in self.profiles.items():
            if name in self.profile_names:
                variables.update(profile.vars)
        raw_vars = data.get("vars") or {}
        if not isinstance(raw_vars, dict):
            raise ConfigError("'vars' must be a table")
        variables.update({str(k): v for k, v in raw_vars.items()})
        # profile vars win over global vars, CLI wins over everything
        for name in self.profile_names:
            variables.update(self.profiles[name].vars)
        variables.update({str(k): v for k, v in var_overrides.items()})
        variables["profiles"] = list(self.profile_names)
        variables["profile"] = self.profile_names[0] if self.profile_names else ""

        self.variables = self._render_variables(variables)

        # ---- hooks / secrets -------------------------------------------
        self.hooks = self._build_hooks(data.get("hooks") or {})
        secrets_config = data.get("secrets") or {}
        if not isinstance(secrets_config, dict):
            raise ConfigError("'secrets' must be a table")
        self.secrets_config = dict(secrets_config)

        # ---- entries ----------------------------------------------------
        self.entries = []
        global_when = Condition(data.get("when"), what="top-level when")
        entries_data = data.get("entry", data.get("entries", []))
        if isinstance(entries_data, dict):
            entries_data = [entries_data]
        if not isinstance(entries_data, list):
            raise ConfigError("'entry' must be an array of tables")

        for index, item in enumerate(entries_data):
            if not isinstance(item, dict):
                raise ConfigError(f"entry #{index + 1} must be a table")
            entry = self._build_entry(item, origin=str(self.path), mode_default="symlink")
            self._apply_global_when(entry, global_when)
            self.entries.append(entry)

        for table, default_mode in _SHORTHAND_TABLES.items():
            mapping = data.get(table)
            if mapping is None:
                continue
            if not isinstance(mapping, dict):
                raise ConfigError(f"'{table}' must be a table mapping target → source")
            for target, value in mapping.items():
                entry = self._build_shorthand(str(target), value, mode=default_mode,
                                              origin=f"{self.path}#{table}")
                self._apply_global_when(entry, global_when)
                self.entries.append(entry)

        self._finalise_entries()
        self._validate_entries()

    def _apply_global_when(self, entry: Entry, global_when: Condition) -> None:
        if not global_when:
            return
        combined = list(global_when.sources) + list(entry.when.sources)
        entry.when = Condition(combined, what=f"entry {entry.key}")

    def _build_profiles(self, data: Any) -> Dict[str, Profile]:
        if not isinstance(data, dict):
            raise ConfigError("'profiles' must be a table")
        return {str(name): Profile.from_data(str(name), value) for name, value in data.items()}

    def _resolve_profile_chain(self, requested: List[str]) -> List[str]:
        chain: List[str] = []

        def visit(name: str, stack: List[str]) -> None:
            if name in chain:
                return
            if name in stack:
                raise ConfigError(f"profile inheritance cycle: {' → '.join([*stack, name])}")
            profile = self.profiles.get(name)
            if profile is None:
                known = ", ".join(sorted(self.profiles)) or "(none defined)"
                raise ConfigError(f"unknown profile {name!r}", hint=f"known profiles: {known}")
            for parent in profile.extends:
                visit(parent, [*stack, name])
            if name not in chain:
                chain.append(name)

        for name in requested:
            visit(name, [])
        return chain

    def _render_variables(self, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve ``${var}`` interpolation and ``{{ jinja }}`` inside variable values."""

        result: Dict[str, Any] = {}
        for key, value in variables.items():
            result[key] = self._render_value(value, variables)
        # second pass so variables can reference other variables declared later
        for _ in range(3):
            changed = False
            for key, value in list(result.items()):
                if isinstance(value, str) and ("${" in value or "{{" in value):
                    new_value = self._render_value(value, result)
                    if new_value != value:
                        result[key] = new_value
                        changed = True
            if not changed:
                break
        return result

    def _render_value(self, value: Any, variables: Mapping[str, Any],
                      _seen: Optional[Set[int]] = None) -> Any:
        _seen = _seen if _seen is not None else set()
        if isinstance(value, (dict, list)):
            if id(value) in _seen:
                return value  # guard against self-referential structures
            _seen = _seen | {id(value)}
        if isinstance(value, str):
            text = value
            if "${" in text or re.search(r"\$[A-Za-z_]", text):
                try:
                    text = interpolate(
                        text,
                        variables,
                        expand_env=bool(self.settings.get("expand_env", True)),
                        where="vars",
                    )
                except ConfigError:
                    if not self.settings.get("strict_templates"):
                        return text
                    raise
            if "{{" in text:
                try:
                    return Template(text, "<var>").render(Environment(dict(variables), strict=False))
                except TemplateError as exc:
                    raise ConfigError(f"invalid template in variable {value!r}: {exc}") from exc
            return text
        if isinstance(value, list):
            return [self._render_value(item, variables, _seen) for item in value]
        if isinstance(value, dict):
            return {k: self._render_value(v, variables, _seen) for k, v in value.items()}
        return value

    def _build_hooks(self, data: Any) -> Dict[str, List[str]]:
        from .hooks import PHASES

        if not isinstance(data, dict):
            raise ConfigError("'hooks' must be a table")
        hooks: Dict[str, List[str]] = {}
        for phase, commands in data.items():
            if phase not in PHASES and not phase.startswith("after_") and not phase.startswith("before_"):
                raise ConfigError(
                    f"unknown hook phase {phase!r}",
                    hint=f"known phases: {', '.join(PHASES)}",
                )
            hooks[str(phase)] = _as_list(commands, field_name=f"hooks.{phase}")
        for name in self.profile_names:
            for phase, commands in self.profiles[name].hooks.items():
                hooks.setdefault(str(phase), []).extend(_as_list(commands, field_name=f"profile {name}.hooks"))
        return hooks

    def _build_entry(self, item: Dict[str, Any], *, origin: str, mode_default: str,
                     preset: Optional[Dict[str, Any]] = None) -> Entry:
        data = dict(preset or {})
        data.update(item)
        for key in list(data):
            if key in _UNSUPPORTED_KEYS:
                raise ConfigError(
                    f"unsupported entry key {key!r} in {origin}",
                    hint="use 'when' / 'platforms' / 'profiles' instead",
                )
        unknown = set(data) - _ENTRY_KEYS
        if unknown:
            raise ConfigError(
                f"unknown entry key(s) in {origin}: {', '.join(sorted(unknown))}",
                hint="see README 'Entry reference' for the supported keys",
            )

        target_raw = data.get("target") or data.get("dest") or data.get("destination")
        source = data.get("source") or data.get("src")
        if target_raw is None and source is not None:
            # allow `target` to be omitted when the source is a full path
            target_raw = Path(str(source)).name
        if target_raw is None:
            raise ConfigError(f"entry in {origin} is missing 'target'")
        target_raw = str(target_raw)
        if source is not None:
            source = str(source)

        mode = str(data.get("mode") or mode_default).lower()
        if mode not in MODES:
            raise ConfigError(
                f"unknown mode {mode!r} for {target_raw}",
                hint=f"valid modes: {', '.join(MODES)}",
            )
        if _as_bool(data.get("template") if data.get("template") is not None else data.get("templated")):
            mode = "template"

        strategy = str(data.get("strategy") or data.get("on_conflict") or self.settings["strategy"]).lower()
        if strategy not in STRATEGIES:
            raise ConfigError(
                f"unknown strategy {strategy!r} for {target_raw}",
                hint=f"valid strategies: {', '.join(STRATEGIES)}",
            )

        platforms = _as_list(data.get("platforms") or data.get("platform"), field_name="platforms")
        hosts_raw = _as_list(data.get("hosts") or data.get("host"), field_name="hosts")
        profiles = _as_list(data.get("profiles") or data.get("profile"), field_name="profiles")

        when_source: List[Any] = []
        raw_when = data.get("when")
        if platforms:
            formatted = ", ".join(repr(p) for p in platforms)
            when_source.append(f"os in [{formatted}] or distro in [{formatted}]")
        if hosts_raw:
            formatted = ", ".join(repr(h) for h in hosts_raw)
            when_source.append(f"fnmatch(hostname, [{formatted}])")
        if raw_when is not None:
            when_source.extend(_as_list(raw_when, field_name="when"))

        entry = Entry(
            target_raw=target_raw,
            source=source,
            name=str(data.get("name") or data.get("id") or ""),
            mode=mode,
            strategy=strategy,
            permissions=_normalise_permissions(
                data.get("permissions") if data.get("permissions") is not None
                else (data.get("mode_bits") if data.get("mode_bits") is not None
                      else (data.get("perms") if data.get("perms") is not None else data.get("chmod"))),
                where=f"entry {target_raw}",
            ),
            owner=str(data["owner"]) if data.get("owner") else None,
            group=str(data["group"]) if data.get("group") else None,
            when=Condition(when_source or None, what=f"entry {target_raw} 'when'"),
            unless=Condition(data.get("unless"), what=f"entry {target_raw} 'unless'"),
            tags=_as_list(data.get("tags"), field_name="tags"),
            profiles=profiles,
            vars=dict(data.get("vars") or {}),
            secrets=_as_list(data.get("secrets"), field_name="secrets"),
            encrypt=_as_bool(data.get("encrypt") if data.get("encrypt") is not None else data.get("decrypt")),
            comment=str(data.get("comment") or data.get("description") or ""),
            disabled=not _as_bool(data.get("enabled"), default=True) or _as_bool(data.get("disabled")),
            create_parents=_as_bool(
                data.get("create_parents") if data.get("create_parents") is not None else data.get("parents"),
                default=True,
            ),
            expand_env=_as_bool(data.get("expand_env"), default=True),
            strip_trailing_whitespace=_as_bool(data.get("strip_trailing_whitespace")),
            ensure_newline=_as_bool(data.get("ensure_newline")),
            origin=origin,
        )
        if entry.mode == "mkdir" and entry.source:
            raise ConfigError(f"entry {target_raw!r}: 'mkdir' entries must not define a source")
        if entry.mode != "mkdir" and not entry.source:
            raise ConfigError(f"entry {target_raw!r}: mode {entry.mode!r} requires a source")
        if entry.tags and (set(entry.tags) & {"all", "*"}):
            entry.tags = [t for t in entry.tags if t not in {"all", "*"}]
        if not entry.name:
            entry.name = _slug_name(entry.target_raw, entry.source)
        return entry

    def _build_shorthand(self, target: str, value: Any, *, mode: str, origin: str) -> Entry:
        if isinstance(value, str):
            data = {"source": value}
        elif isinstance(value, list):
            raise ConfigError(f"{origin}: entry {target!r} must map to a source string or a table")
        elif isinstance(value, dict):
            data = dict(value)
            if mode != "mkdir" and "source" not in data and "src" not in data:
                if "path" in data:
                    data["source"] = data.pop("path")
                else:
                    raise ConfigError(f"{origin}: entry {target!r} needs a 'source' key")
        elif value is None:
            data = {"source": target, "target": target}
        else:
            raise ConfigError(f"{origin}: entry {target!r} has unsupported value {value!r}")
        preset = {"target": target if mode != "mkdir" else target}
        if mode == "mkdir":
            data.pop("source", None)
            data.setdefault("mode", "mkdir")
        return self._build_entry(data, origin=origin, mode_default=mode, preset=preset)

    def _finalise_entries(self) -> None:
        """Expand targets/sources, resolve names and keep entry order stable."""

        used_names: Dict[str, int] = {}
        env_expand = bool(self.settings.get("expand_env", True))
        for entry in self.entries:
            target_text = entry.target_raw
            if entry.expand_env and env_expand:
                try:
                    target_text = interpolate(target_text, self.variables, where=f"entry {entry.key}")
                except ConfigError:
                    if self.settings.get("strict_templates"):
                        raise
            if "{{" in target_text:
                target_text = Template(target_text, "<target>").render(
                    Environment(self.variables, strict=False)
                )
            entry.target = expand_path(target_text, base=self.root)

            if entry.source:
                source_text = entry.source
                if env_expand:
                    try:
                        source_text = interpolate(source_text, self.variables, where=f"entry {entry.key} source")
                    except ConfigError:
                        if self.settings.get("strict_templates"):
                            raise
                if "{{" in source_text:
                    source_text = Template(source_text, "<source>").render(
                        Environment(self.variables, strict=False)
                    )
                entry.source_path = expand_path(source_text, base=self.source_root)
                if not is_relative_to(entry.source_path, self.source_root) and not is_relative_to(
                    entry.source_path, self.root
                ):
                    self.warnings.append(
                        f"entry {entry.key}: source {entry.source_path} lives outside the project"
                    )
            if not entry.name:
                entry.name = _slug_name(entry.target_raw, entry.source)

            if entry.name in used_names:
                used_names[entry.name] += 1
                entry.name = f"{entry.name}-{used_names[entry.name]}"
                self.warnings.append(f"duplicate entry name; renamed to {entry.name!r}")
            else:
                used_names[entry.name] = 1

    def _validate_entries(self) -> None:
        targets: Dict[str, str] = {}
        names: Dict[str, str] = {}
        for entry in self.entries:
            key = str(entry.target)
            if key in targets:
                raise ConfigError(
                    f"two entries manage the same target: {entry.target_raw}",
                    hint=f"defined by {targets[key]} and {entry.origin}",
                )
            targets[key] = entry.origin
            if entry.name in names:
                raise ConfigError(f"duplicate entry name {entry.name!r}")
            names[entry.name] = entry.origin

        source_root = self.source_root.resolve()
        for entry in self.entries:
            if entry.source_path is None:
                continue
            if not entry.source_path.exists() and not str(entry.source_path).startswith("~"):
                # only a warning: the source may be created by a hook
                self.warnings.append(f"entry {entry.key}: source missing ({entry.source_path})")
            if entry.mode == "mkdir":  # pragma: no cover - guarded earlier
                continue
            if entry.target == source_root or is_relative_to(entry.target, source_root):
                raise ConfigError(
                    f"entry {entry.key} would deploy into the source tree ({entry.target})",
                    hint="point the target outside source_root",
                )
            if entry.target == self.root:
                raise ConfigError(f"entry {entry.key} targets the project root itself")

        for name in self.profile_names:
            profile = self.profiles[name]
            for wanted in profile.entries:
                if wanted.startswith("tag:"):
                    tag = wanted[4:]
                    if not any(tag in e.tags for e in self.entries):
                        self.warnings.append(f"profile {name}: no entry carries tag {tag!r}")
                    continue
                if not any(e.key == wanted or e.name == wanted for e in self.entries):
                    self.warnings.append(f"profile {name}: unknown entry {wanted!r}")

    def _validate_top_level(self, data: Mapping[str, Any]) -> None:
        allowed = {
            "version",
            "$schema",
            "name",
            "description",
            "settings",
            "options",
            "vars",
            "profiles",
            "entry",
            "entries",
            "hooks",
            "secrets",
            "when",
            *_SHORTHAND_TABLES.keys(),
        }
        unknown = set(data) - allowed
        if unknown:
            self.warnings.append(f"unknown top-level key(s) ignored: {', '.join(sorted(unknown))}")
        version = data.get("version")
        if version not in (None, 1, "1"):
            raise ConfigError(f"unsupported config version {version!r} (expected 1)")

    # -- queries -----------------------------------------------------------
    @property
    def source_root(self) -> Path:  # type: ignore[override]
        return self._source_root

    @source_root.setter
    def source_root(self, value: Path) -> None:
        self._source_root = Path(value)

    def active_profiles(self) -> List[str]:
        return list(self.profile_names)

    def profile_tags(self) -> List[str]:
        tags: List[str] = []
        for name in self.profile_names:
            tags.extend(self.profiles[name].tags)
        return tags

    def profile_entry_names(self) -> List[str]:
        names: List[str] = []
        for name in self.profile_names:
            names.extend(self.profiles[name].entries)
        return names

    def select(
        self,
        *,
        tags: Optional[Sequence[str]] = None,
        exclude_tags: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
        skip: Optional[Sequence[str]] = None,
        include_disabled: bool = False,
    ) -> List[Entry]:
        """Select entries for the current host/profile/tag combination."""

        selected_tags = list(tags or [])
        excluded_tags = list(exclude_tags or [])
        only = list(only or [])
        skip = set(skip or [])
        env = self.condition_env()
        profile_tags = set(self.profile_tags())
        profile_entries = set(self.profile_entry_names())
        result: List[Entry] = []

        for entry in self.entries:
            if entry.name in skip or entry.key in skip:
                continue
            if only and not (entry.name in only or entry.key in only or set(entry.tags) & set(only)):
                continue
            if entry.profiles and not (set(entry.profiles) & set(self.profile_names)):
                continue
            if entry.disabled and entry.name not in profile_entries and not include_disabled:
                continue
            if entry.disabled and include_disabled is False and entry.name in profile_entries:
                pass  # enabled via profile
            if not entry.matches_tags(selected_tags, excluded_tags):
                continue
            if profile_tags and not selected_tags and not entry.tags and entry.profiles:
                # profile-scoped entries without tags only apply when their profile is active
                pass
            try:
                if not entry.when.evaluate(self.variables, env=env):
                    continue
                if entry.unless and entry.unless.evaluate(self.variables, env=env):
                    continue
            except ConfigError:
                raise
            result.append(entry)
        return result

    def condition_env(self) -> Environment:
        if self._condition_env is None:
            self._condition_env = Environment(dict(self.variables), strict=False, base_dir=self.root)
        return self._condition_env

    def template_env(self, extra: Optional[Mapping[str, Any]] = None) -> Environment:
        """Environment used to render ``mode = "template"`` entries."""

        from .template import make_loader

        variables = dict(self.variables)
        variables["entry"] = {}
        variables["tags"] = list(self.variables.get("tags") or [])
        if extra:
            variables.update(extra)
        try:
            loader = make_loader(self.root)
        except OSError:  # pragma: no cover
            loader = None
        return Environment(
            variables,
            strict=bool(self.settings.get("strict_templates")),
            base_dir=self.root,
            loader=loader,
        )

    def entry_by_name(self, name: str) -> Optional[Entry]:
        for entry in self.entries:
            if entry.name == name or entry.key == name:
                return entry
        return None

    def tags(self) -> List[str]:
        tags: Set[str] = set()
        for entry in self.entries:
            tags.update(entry.tags)
        return sorted(tags)

    def hooks_for(self, phase: str) -> List[str]:
        return list(self.hooks.get(phase, []))

    def describe(self) -> Dict[str, Any]:
        return {
            "config": str(self.path),
            "root": str(self.root),
            "source_root": str(self.source_root),
            "state_dir": str(self.state_dir),
            "backup_dir": str(self.backup_dir),
            "hostname": self.facts.hostname,
            "os": self.facts.os,
            "profiles": self.active_profiles(),
            "tags": self.tags(),
            "entries": len(self.entries),
            "includes": [str(p) for p in self.includes],
            "host_config": str(self.host_config) if self.host_config else None,
            "local_config": str(self.local_config) if self.local_config else None,
            "warnings": list(self.warnings),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "settings": {k: v for k, v in self.settings.items()},
            "variables": {k: v for k, v in self.variables.items() if not callable(v)},
            "profiles": {
                name: {"description": p.description, "tags": p.tags, "entries": p.entries, "extends": p.extends}
                for name, p in self.profiles.items()
            },
            "hooks": {k: list(v) for k, v in self.hooks.items()},
            "entries": [e.to_dict() for e in self.entries],
            "meta": self.describe(),
        }

    # -- authoring helpers -------------------------------------------------
    def add_entries_to_file(self, entries: Sequence[Entry], *, path: Optional[Path] = None) -> Path:
        """Append ``[[entry]]`` blocks to the config file (used by ``adopt``)."""

        target = Path(path) if path else self.path
        if target.suffix.lower() == ".json":
            data = load_structured(target)
            blocks = data.setdefault("entry", [])
            if isinstance(blocks, dict):  # pragma: no cover - shape guard
                blocks = [blocks]
                data["entry"] = blocks
            for entry in entries:
                block = {
                    "name": entry.name,
                    "source": entry.source,
                    "target": entry.target_raw,
                    "mode": entry.mode,
                }
                if entry.tags:
                    block["tags"] = list(entry.tags)
                blocks.append(block)
            atomic_write(target, (json_dump(data) + "\n").encode("utf-8"))
            return target

        text = target.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        chunks = [text.rstrip("\n"), ""]
        for entry in entries:
            chunks.append("[[entry]]")
            chunks.append(f'name = "{entry.name}"')
            if entry.source:
                chunks.append(f'source = "{entry.source}"')
            chunks.append(f'target = "{entry.target_raw}"')
            if entry.mode != "symlink":
                chunks.append(f'mode = "{entry.mode}"')
            if entry.tags:
                rendered = ", ".join(f'"{tag}"' for tag in entry.tags)
                chunks.append(f"tags = [{rendered}]")
            chunks.append("")
        atomic_write(target, ("\n".join(chunks).rstrip("\n") + "\n").encode("utf-8"))
        return target
