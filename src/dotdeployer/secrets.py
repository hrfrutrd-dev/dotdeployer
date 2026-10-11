"""Secret resolution and source-file encryption.

Multiple backends are supported and tried in order, so a config can be portable
across machines: ``env`` and ``file`` always work, and richer backends (``gpg``,
``age``, ``pass``, ``op``, ``bw``, ``keyring``, ``command``) are used when the
corresponding tool is installed.

Config example::

    [secrets]
    order       = ["env", "file", "pass"]   # tried left to right
    env_prefix  = "DOTDEPLOYER_SECRET_"
    dir         = "secrets"                 # used by file/gpg/age backends
    pass_prefix = "dotfiles/"
    gpg_dir     = "secrets"                 # *.gpg files
    age_dir     = "secrets"                 # *.age files
    identity    = "~/.config/age/keys.txt"
    op_prefix   = "op://Private/dotfiles"
    command     = "my-vault get {name}"     # custom backend
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .errors import SecretError
from .util import expand_path, run_command, which

__all__ = ["SecretBackend", "SecretResolver", "backends_available", "decrypt_bytes", "encrypt_bytes"]

_MASK = "********"


def _looks_like_placeholder(value: str) -> bool:
    return value.strip().lower() in {"", "changeme", "change-me", "todo", "placeholder", "xxx"}


class SecretBackend:
    """Base class: return the secret or ``None`` when this backend cannot serve it."""

    name = "base"
    requires: Sequence[str] = ()

    def __init__(self, options: Mapping[str, Any], *, root: Path) -> None:
        self.options = dict(options)
        self.root = Path(root)
        self.warning: Optional[str] = None

    # -- API ---------------------------------------------------------------
    def available(self) -> bool:
        if self.requires and not all(which(binary) for binary in self.requires):
            self.warning = f"backend {self.name!r} needs {'/'.join(self.requires)} on PATH"
            return False
        return True

    def get(self, name: str) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.name

    # -- helpers -----------------------------------------------------------
    def _path_option(self, *keys: str, default: Optional[str] = None) -> Optional[Path]:
        for key in keys:
            value = self.options.get(key)
            if value:
                return expand_path(str(value), base=self.root)
        if default:
            return expand_path(default, base=self.root)
        return None

    def _resolve_from_dir(self, directory: Optional[Path], name: str,
                          suffixes: Sequence[str] = ("",)) -> Optional[str]:
        if directory is None or not directory.is_dir():
            return None
        candidates = [directory / (name + suffix) for suffix in suffixes]
        candidates.append(directory / name)
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8").strip()
                except OSError as exc:  # pragma: no cover
                    raise SecretError(f"cannot read secret file {candidate}: {exc}") from exc
        return None


class EnvBackend(SecretBackend):
    name = "env"

    def get(self, name: str) -> Optional[str]:
        prefix = str(self.options.get("env_prefix") or self.options.get("prefix") or "DOTDEPLOYER_SECRET_")
        variants = [
            f"{prefix}{name}",
            f"{prefix}{name.upper()}",
            f"{prefix}{name.upper().replace('-', '_').replace('.', '_')}",
            name,
            name.upper(),
        ]
        for key in variants:
            if key in os.environ and not _looks_like_placeholder(os.environ[key]):
                return os.environ[key]
        return None


class FileBackend(SecretBackend):
    name = "file"

    def get(self, name: str) -> Optional[str]:
        directory = self._path_option("dir", "secrets_dir", "file_dir", default="secrets")
        value = self._resolve_from_dir(directory, name, ("", ".txt", ".secret"))
        if value and not _looks_like_placeholder(value):
            return value
        return None


class PassBackend(SecretBackend):
    name = "pass"
    requires = ("pass",)

    def get(self, name: str) -> Optional[str]:
        prefix = str(self.options.get("pass_prefix") or "")
        result = run_command(["pass", "show", f"{prefix}{name}"])
        if result.ok and result.stdout and not _looks_like_placeholder(result.stdout):
            return result.stdout.strip()
        return None


class GpgBackend(SecretBackend):
    name = "gpg"
    requires = ("gpg",)

    def get(self, name: str) -> Optional[str]:
        directory = self._path_option("gpg_dir", "dir", "secrets_dir", default="secrets")
        if directory is None:
            return None
        for suffix in ("", ".gpg", ".asc"):
            candidate = directory / (name + suffix)
            if not candidate.is_file():
                continue
            result = run_command(
                ["gpg", "--batch", "--quiet", "--decrypt", str(candidate)],
                timeout=float(self.options.get("timeout", 30)),
            )
            if result.ok and result.stdout:
                return result.stdout
            raise SecretError(
                f"gpg could not decrypt {candidate}: {result.stderr or 'unknown error'}",
                hint="import the private key that can decrypt this file (gpg --import)",
            )
        return None


class AgeBackend(SecretBackend):
    name = "age"
    requires = ("age",)

    def get(self, name: str) -> Optional[str]:
        directory = self._path_option("age_dir", "dir", "secrets_dir", default="secrets")
        if directory is None:
            return None
        identity = self._path_option("identity", "age_identity", default="~/.config/age/keys.txt")
        for suffix in (".age", ""):
            candidate = directory / (name + suffix)
            if not candidate.is_file():
                continue
            args = ["age", "--decrypt"]
            if identity:
                args += ["--identity", str(identity)]
            args.append(str(candidate))
            result = run_command(args, timeout=float(self.options.get("timeout", 30)))
            if result.ok and result.stdout:
                return result.stdout
            raise SecretError(
                f"age could not decrypt {candidate}: {result.stderr or 'unknown error'}",
                hint=f"check that {identity} exists and matches the recipient",
            )
        return None


class OpBackend(SecretBackend):
    name = "op"
    requires = ("op",)

    def get(self, name: str) -> Optional[str]:
        prefix = str(self.options.get("op_prefix") or self.options.get("op_vault") or "")
        reference = name if name.startswith("op://") else (
            f"{prefix.rstrip('/')}/{name}/password" if prefix else ""
        )
        if not reference:
            return None
        result = run_command(["op", "read", reference], timeout=float(self.options.get("timeout", 30)))
        if result.ok and result.stdout:
            return result.stdout.strip()
        return None


class BitwardenBackend(SecretBackend):
    name = "bw"
    requires = ("bw",)

    def get(self, name: str) -> Optional[str]:
        if not os.environ.get("BW_SESSION") and self.options.get("require_session", True):
            self.warning = "bw backend needs $BW_SESSION (run 'bw unlock')"
            return None
        result = run_command(["bw", "get", "password", name], timeout=float(self.options.get("timeout", 30)))
        if result.ok and result.stdout:
            return result.stdout.strip()
        return None


class KeyringBackend(SecretBackend):
    name = "keyring"

    def available(self) -> bool:
        try:
            import keyring  # noqa: F401
        except ModuleNotFoundError:
            self.warning = "keyring backend needs 'pip install keyring'"
            return False
        return True

    def get(self, name: str) -> Optional[str]:
        import keyring

        service = str(self.options.get("service") or self.options.get("keyring_service") or "dotdeployer")
        try:
            value = keyring.get_password(service, name)
        except Exception as exc:
            self.warning = f"keyring lookup failed: {exc}"
            return None
        return value or None


class CommandBackend(SecretBackend):
    name = "command"

    def available(self) -> bool:
        command = self.options.get("command") or self.options.get("cmd")
        if not command:
            self.warning = "command backend needs a 'command' option"
            return False
        return True

    def get(self, name: str) -> Optional[str]:
        template = str(self.options.get("command") or self.options.get("cmd") or "")
        expanded = template.replace("{name}", name).replace("{key}", name)
        result = run_command(expanded, timeout=float(self.options.get("timeout", 30)))
        if result.ok and result.stdout:
            return result.stdout.rstrip("\n")
        if result.stderr:
            self.warning = f"command backend failed: {result.stderr.strip()[:200]}"
        return None


class SopsBackend(SecretBackend):
    name = "sops"
    requires = ("sops",)

    def get(self, name: str) -> Optional[str]:
        directory = self._path_option("sops_dir", "dir", "secrets_dir")
        if directory is None:
            return None
        for candidate in directory.glob("*.yaml"):
            result = run_command(["sops", "--decrypt", str(candidate)], timeout=float(self.options.get("timeout", 30)))
            if result.ok and result.stdout:
                try:
                    import json  # noqa: F401

                    import yaml  # type: ignore

                    data = yaml.safe_load(result.stdout) or {}
                except ModuleNotFoundError:
                    continue
                if isinstance(data, dict) and name in data:
                    return str(data[name])
        return None


BACKENDS: Dict[str, type] = {
    "env": EnvBackend,
    "file": FileBackend,
    "pass": PassBackend,
    "password-store": PassBackend,
    "gpg": GpgBackend,
    "age": AgeBackend,
    "op": OpBackend,
    "1password": OpBackend,
    "bw": BitwardenBackend,
    "bitwarden": BitwardenBackend,
    "keyring": KeyringBackend,
    "command": CommandBackend,
    "cmd": CommandBackend,
    "sops": SopsBackend,
}

DEFAULT_ORDER = ("env", "file", "pass", "gpg", "age", "op", "bw", "keyring", "sops", "command")


def backends_available() -> List[Dict[str, Any]]:
    """Describe every backend and whether its external tool is present."""

    out: List[Dict[str, Any]] = []
    for name in DEFAULT_ORDER:
        cls = BACKENDS[name]
        requires = list(cls.requires)
        out.append(
            {
                "name": name,
                "requires": requires,
                "available": all(shutil.which(binary) for binary in requires) if requires else True,
            }
        )
    try:
        import keyring  # noqa: F401

        keyring_ok = True
    except ModuleNotFoundError:
        keyring_ok = False
    for item in out:
        if item["name"] == "keyring":
            item["available"] = keyring_ok
    return out


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
class SecretResolver:
    """Resolves ``secret("name")`` lookups and decrypts encrypted sources."""

    def __init__(self, options: Mapping[str, Any], *, root: Path, cache: bool = True) -> None:
        self.options = dict(options or {})
        self.root = Path(root)
        self.cache_enabled = cache
        self._cache: Dict[str, Optional[str]] = {}
        self.warnings: List[str] = []
        raw_order = self.options.get("order") or self.options.get("providers")
        self.order: List[str] = self._normalise_order(raw_order)
        # a backend the user explicitly asked for must warn when its tool is absent;
        # the default order silently skips whatever this machine does not have
        explicit = {name for name in self._normalise_order(raw_order)} if raw_order else set()
        self.backends: List[SecretBackend] = []
        for name in self.order:
            cls = BACKENDS.get(name)
            if cls is None:
                self.warnings.append(f"unknown secret backend {name!r} (ignored)")
                continue
            backend = cls(self.options, root=self.root)
            if backend.options.get("enabled") is False:
                continue
            if not backend.available():
                if backend.warning and name in explicit:
                    self.warnings.append(backend.warning)
                continue
            self.backends.append(backend)

    @staticmethod
    def _normalise_order(raw: Any) -> List[str]:
        if raw is None:
            order = list(DEFAULT_ORDER)
        elif isinstance(raw, str):
            order = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            order = [str(item) for item in raw]
        out: List[str] = []
        for name in order:
            if name not in out:
                out.append(name)
        return out or list(DEFAULT_ORDER)

    # -- API ---------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.backends)

    def describe(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "active": [b.describe() for b in self.backends],
            "warnings": list(self.warnings),
            "encryption": str(self.options.get("encryption") or self.options.get("encrypt_with") or ""),
        }

    def resolve(self, name: str) -> str:
        value = self.resolve_optional(name)
        if value is None:
            available = ", ".join(b.name for b in self.backends) or "none"
            raise SecretError(
                f"secret {name!r} could not be resolved (tried: {available})",
                hint="add it to the secrets dir/env, or set a fallback with secret('name', 'default')",
            )
        return value

    def resolve_optional(self, name: str) -> Optional[str]:
        if name in self._cache and self.cache_enabled:
            return self._cache[name]
        value: Optional[str] = None
        for backend in self.backends:
            try:
                candidate = backend.get(name)
            except SecretError:
                raise
            except Exception as exc:
                self.warnings.append(f"backend {backend.name} failed for {name!r}: {exc}")
                continue
            if candidate is not None:
                value = candidate
                break
        if self.cache_enabled:
            self._cache[name] = value
        return value

    def resolve_many(self, names: Iterable[str]) -> Dict[str, Optional[str]]:
        return {name: self.resolve_optional(name) for name in names}

    def invalidate(self) -> None:
        self._cache.clear()

    def placeholder_summary(self, name: str) -> Dict[str, Any]:
        value = self.resolve_optional(name)
        return {
            "name": name,
            "resolved": value is not None,
            "length": len(value) if value else 0,
            "preview": _MASK if value else None,
        }

    # -- encryption --------------------------------------------------------
    def decrypt(self, data: bytes, *, source: Optional[Path] = None) -> bytes:
        return decrypt_bytes(data, options=self.options, root=self.root, source=source)

    def encrypt(self, data: bytes, *, recipient: Optional[str] = None) -> bytes:
        return encrypt_bytes(data, options=self.options, root=self.root, recipient=recipient)


# --------------------------------------------------------------------------- #
# raw encryption helpers (used by sources with ``encrypt = true``)
# --------------------------------------------------------------------------- #
def _encryption_method(options: Mapping[str, Any], source: Optional[Path] = None) -> str:
    explicit = str(options.get("encryption") or options.get("encrypt_with") or "").lower()
    if explicit:
        return explicit
    if source is not None:
        if source.suffix == ".gpg":
            return "gpg"
        if source.suffix == ".age":
            return "age"
    if any(options.get(key) for key in ("age_recipient", "age_identity")):
        return "age"
    return "gpg"


def _recipient(options: Mapping[str, Any], explicit: Optional[str]) -> Optional[str]:
    return explicit or options.get("recipient") or options.get("gpg_recipient") or options.get("age_recipient")  # type: ignore[return-value]


def decrypt_bytes(
    data: bytes,
    *,
    options: Optional[Mapping[str, Any]] = None,
    root: Optional[Path] = None,
    source: Optional[Path] = None,
    config: Any = None,
) -> bytes:
    """Decrypt *data* using gpg or age, depending on configuration."""

    if config is not None and options is None:
        options = getattr(config, "secrets_config", {}) or {}
        root = getattr(config, "root", None)
    options = dict(options or {})
    method = _encryption_method(options, source)

    if method == "age":
        if not which("age"):
            raise SecretError("'age' is not installed but encryption = 'age' is configured")
        identity = options.get("identity") or options.get("age_identity")
        args = ["age", "--decrypt"]
        if identity:
            args += ["--identity", str(expand_path(str(identity), base=root or Path.cwd()))]
        if source is not None and Path(source).is_file():
            args.append(str(source))
            result = run_command(args)
            if not result.ok:
                raise SecretError(f"age failed: {result.stderr or result.returncode}")
            return result.stdout.encode("utf-8")
        result = _run_with_input(args, data)
        if not result.ok:
            raise SecretError(f"age failed: {result.stderr or result.returncode}")
        return result.stdout

    if not which("gpg"):
        raise SecretError(
            "'gpg' is not installed but the source is marked as encrypted",
            hint="install gnupg or set [secrets] encryption = 'age'",
        )
    if source is not None and Path(source).is_file():
        result = run_command(["gpg", "--batch", "--quiet", "--decrypt", str(source)])
        if not result.ok:
            raise SecretError(f"gpg failed: {result.stderr or result.returncode}")
        return result.stdout.encode("utf-8")
    result = _run_with_input(["gpg", "--batch", "--quiet", "--decrypt"], data)
    if not result.ok:
        raise SecretError(f"gpg failed: {result.stderr or result.returncode}")
    return result.stdout


def encrypt_bytes(
    data: bytes,
    *,
    options: Optional[Mapping[str, Any]] = None,
    root: Optional[Path] = None,
    recipient: Optional[str] = None,
) -> bytes:
    """Encrypt *data* for the configured recipient (used by ``encrypt``/``adopt``)."""

    options = dict(options or {})
    method = str(options.get("encryption") or options.get("encrypt_with") or "").lower()
    target_recipient = _recipient(options, recipient)
    if not target_recipient:
        raise SecretError(
            "no encryption recipient configured",
            hint="set [secrets] recipient = 'you@example.com' (gpg) or age_recipient = 'age1…'",
        )
    if method == "age" or str(target_recipient).startswith("age1"):
        if not which("age"):
            raise SecretError("'age' is not installed")
        result = _run_with_input(["age", "--encrypt", "--recipient", str(target_recipient)], data)
        if not result.ok:
            raise SecretError(f"age encryption failed: {result.stderr or result.returncode}")
        return result.stdout
    if not which("gpg"):
        raise SecretError("'gpg' is not installed")
    result = _run_with_input(
        ["gpg", "--batch", "--quiet", "--trust-model", "always", "--encrypt",
         "--recipient", str(target_recipient), "--output", "-"],
        data,
    )
    if not result.ok:
        raise SecretError(f"gpg encryption failed: {result.stderr or result.returncode}")
    return result.stdout


def _run_with_input(args: Sequence[str], data: bytes) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args),
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise SecretError(f"command not found: {args[0]}") from exc
