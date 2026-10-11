"""Host facts and conditional expressions (``when`` / ``unless`` / ``platform``).

Conditions reuse the template expression language, evaluated against a facts
dictionary, so users get one syntax for variables and for gating::

    when   = "os == 'linux' and 'work' in tags"
    unless = "has('systemctl') == false"
    when   = ["os == 'darwin'", "profile", "hostname matches 'laptop-*'"]
"""

from __future__ import annotations

import os
import platform as platform_mod
import re
import shutil
import socket
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from .errors import ConfigError
from .template import Environment, Expr, TemplateError, parse_expression, truthy
from .util import home, run_command

__all__ = ["Condition", "SystemFacts", "facts_vars", "hostname"]


def hostname() -> str:
    name = os.environ.get("HOSTNAME")
    if name:
        return name.split(".")[0]
    try:
        return socket.gethostname().split(".")[0]
    except Exception:  # pragma: no cover - exotic sandboxes
        return "unknown"


def _read_first_line(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return None


@lru_cache(maxsize=1)
def _os_release() -> Dict[str, str]:
    data: Dict[str, str] = {}
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    data[key.strip()] = value.strip().strip('"').strip("'")
            if data:
                break
        except OSError:
            continue
    return data


class SystemFacts:
    """Cached description of the machine dotdeployer is running on."""

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}

    def _get(self, key: str, producer) -> Any:
        if key not in self._cache:
            self._cache[key] = producer()
        return self._cache[key]

    # -- basics ------------------------------------------------------------
    @property
    def os(self) -> str:
        return self._get("os", lambda: _normalised_os())

    @property
    def family(self) -> str:
        mapping = {"darwin": "bsd", "freebsd": "bsd", "openbsd": "bsd", "netbsd": "bsd",
                   "linux": "unix", "windows": "windows"}
        return mapping.get(self.os, "unix")

    @property
    def arch(self) -> str:
        return self._get("arch", lambda: _normalised_arch(platform_mod.machine()))

    @property
    def kernel(self) -> str:
        return self._get("kernel", platform_mod.release)

    @property
    def hostname(self) -> str:
        return self._get("hostname", hostname)

    @property
    def user(self) -> str:
        return self._get(
            "user",
            lambda: os.environ.get("USER")
            or os.environ.get("LOGNAME")
            or os.environ.get("USERNAME")
            or "user",
        )

    @property
    def home(self) -> Path:
        return self._get("home", home)

    @property
    def distro(self) -> str:
        return self._get("distro", lambda: _os_release().get("ID", ""))

    @property
    def distro_version(self) -> str:
        return self._get("distro_version", lambda: _os_release().get("VERSION_ID", ""))

    @property
    def distro_like(self) -> List[str]:
        def _produce() -> List[str]:
            raw = _os_release().get("ID_LIKE", "")
            return [p for p in raw.replace(",", " ").split() if p]

        return self._get("distro_like", _produce)

    @property
    def python(self) -> str:
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    @property
    def cpu_count(self) -> int:
        return self._get("cpu_count", lambda: os.cpu_count() or 1)

    @property
    def memory_gb(self) -> float:
        def _produce() -> float:
            try:
                pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                return round(pages * page_size / (1024**3), 1)
            except (ValueError, OSError, AttributeError):  # pragma: no cover
                return 0.0

        return self._get("memory_gb", _produce)

    @property
    def is_wsl(self) -> bool:
        def _produce() -> bool:
            if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
                return True
            return "microsoft" in (platform_mod.release() or "").lower() or "wsl" in (
                platform_mod.version() or ""
            ).lower()

        return self._get("is_wsl", _produce)

    @property
    def is_container(self) -> bool:
        def _produce() -> bool:
            if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
                return True
            try:
                text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return any(token in text for token in ("docker", "kubepods", "containerd", "podman"))

        return self._get("is_container", _produce)

    @property
    def is_root(self) -> bool:
        return self._get("is_root", lambda: bool(hasattr(os, "geteuid") and os.geteuid() == 0))

    @property
    def shell(self) -> str:
        return self._get("shell", lambda: os.path.basename(os.environ.get("SHELL", "") or ""))

    @property
    def has_sudo(self) -> bool:
        return bool(shutil.which("sudo"))

    @property
    def is_tty(self) -> bool:
        try:
            return bool(sys.stdout.isatty())
        except Exception:  # pragma: no cover
            return False

    # -- dynamic -----------------------------------------------------------
    def has(self, program: str) -> bool:
        return shutil.which(str(program)) is not None

    def has_all(self, *programs: str) -> bool:
        return all(self.has(p) for p in programs)

    def has_any(self, *programs: str) -> bool:
        return any(self.has(p) for p in programs)

    def command_ok(self, command: str, timeout: float = 5.0) -> bool:
        try:
            return run_command(command, timeout=timeout).ok
        except Exception:
            return False

    def command_output(self, command: str, fallback: str = "", timeout: float = 5.0) -> str:
        try:
            result = run_command(command, timeout=timeout)
        except Exception:
            return fallback
        return result.stdout if result.ok else fallback

    def package_installed(self, package: str) -> bool:
        name = str(package)
        if self.os == "darwin":
            return self.command_ok(f"brew list --versions {name}")
        for manager, query in (
            ("dpkg-query", f"dpkg-query -W -f='${{Status}}' {name}"),
            ("rpm", f"rpm -q {name}"),
            ("apk", f"apk info -e {name}"),
            ("pacman", f"pacman -Q {name}"),
        ):
            if shutil.which(manager.split()[0]):
                return self.command_ok(query)
        return False

    def path_exists(self, path: str) -> bool:
        return Path(os.path.expanduser(os.path.expandvars(str(path)))).exists()

    def env(self, name: str, fallback: Any = "") -> Any:
        return os.environ.get(str(name), fallback)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "os": self.os,
            "family": self.family,
            "arch": self.arch,
            "kernel": self.kernel,
            "hostname": self.hostname,
            "user": self.user,
            "home": str(self.home),
            "distro": self.distro,
            "distro_version": self.distro_version,
            "distro_like": self.distro_like,
            "python": self.python,
            "cpu_count": self.cpu_count,
            "memory_gb": self.memory_gb,
            "is_wsl": self.is_wsl,
            "is_container": self.is_container,
            "is_root": self.is_root,
            "has_sudo": self.has_sudo,
            "shell": self.shell,
        }


def _normalised_os() -> str:
    system = platform_mod.system().lower()
    if system.startswith("darwin"):
        return "darwin"
    if system.startswith("win"):
        return "windows"
    if system.startswith(("freebsd", "openbsd", "netbsd", "dragonfly")):
        return system
    if system.startswith("linux"):
        return "linux"
    return system or "unknown"


def _normalised_arch(machine: str) -> str:
    value = (machine or "").lower()
    mapping = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "i386": "i386",
        "i686": "i386",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
        "ppc64le": "ppc64le",
        "s390x": "s390x",
        "riscv64": "riscv64",
    }
    return mapping.get(value, value or "unknown")


def facts_vars(
    facts: Optional[SystemFacts] = None,
    *,
    tags: Optional[Iterable[str]] = None,
    profiles: Optional[Iterable[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Facts exposed to condition expressions and templates as ``facts``/globals."""

    facts = facts or SystemFacts()
    profile_list = [str(p) for p in (profiles or [])]
    plain = facts.as_dict()  # primitives only, safe to nest
    data: Dict[str, Any] = dict(plain)
    data.update(
        {
            "has": facts.has,
            "has_all": facts.has_all,
            "has_any": facts.has_any,
            "which": lambda program: shutil.which(str(program)) or "",
            "command_ok": facts.command_ok,
            "command_output": facts.command_output,
            "package_installed": facts.package_installed,
            "path": facts.path_exists,
            "exists": facts.path_exists,
            "env": facts.env,
            "tags": list(tags or []),
            "profiles": profile_list,
            "profile": (profile_list[0] if profile_list else ""),
            # a shallow copy keeps `facts.os` style access while avoiding a cycle
            "facts": plain,
        }
    )
    if extra:
        data.update(extra)
    return data


ConditionSource = Union[None, str, bool, Sequence[Any]]
_MATCH_RE = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_.]*)\s+matches\s+(?P<pattern>.+)$")


def _normalise(source: ConditionSource) -> List[str]:
    if source is None or source == "" or source is True:
        return []
    if source is False:
        return ["false"]
    if isinstance(source, str):
        return [source]
    if isinstance(source, (list, tuple)):
        out: List[str] = []
        for item in source:
            if isinstance(item, bool):
                out.append("true" if item else "false")
            elif isinstance(item, str):
                out.append(item)
            else:
                raise ConfigError(f"invalid condition element: {item!r}")
        return out
    raise ConfigError(f"invalid condition: {source!r}")


class Condition:
    """A compiled list of expressions that must all be true."""

    __slots__ = ("expressions", "sources")

    def __init__(self, source: ConditionSource = None, *, what: str = "condition") -> None:
        self.sources: List[str] = _normalise(source)
        self.expressions: List[Expr] = []
        for item in self.sources:
            text = _rewrite_sugar(item)
            try:
                self.expressions.append(parse_expression(text))
            except TemplateError as exc:
                raise ConfigError(f"invalid {what}: {item!r} ({exc})") from exc

    def __bool__(self) -> bool:
        return bool(self.expressions)

    @property
    def raw(self) -> ConditionSource:
        if not self.sources:
            return None
        return self.sources[0] if len(self.sources) == 1 else list(self.sources)

    def evaluate(self, variables: Dict[str, Any], *, env: Optional[Environment] = None) -> bool:
        if not self.expressions:
            return True
        environment = env or Environment(variables, strict=False)
        for expr in self.expressions:
            try:
                value = expr.eval(environment)
            except TemplateError as exc:
                raise ConfigError(f"failed to evaluate condition: {exc}") from exc
            if not truthy(value):
                return False
        return True

    def describe(self) -> str:
        return " and ".join(self.sources) if self.sources else "always"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Condition({self.sources!r})"


def _rewrite_sugar(text: str) -> str:
    """Support ``hostname matches 'laptop-*'`` in addition to ``fnmatch(...)``."""

    match = _MATCH_RE.match(text.strip())
    if not match:
        return text
    field = match.group("field")
    pattern = match.group("pattern").strip()
    if " " in field or field.startswith(("fnmatch", "and", "or")):
        return text
    return f"fnmatch({field}, {pattern})"
