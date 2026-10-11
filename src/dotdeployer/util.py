"""Small, dependency free helpers shared across dotdeployer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from .errors import ActionError

PathLike = Union[str, os.PathLike]

CONFIG_FILENAMES = (
    "dotdeployer.toml",
    "dotdeployer.json",
    ".dotdeployer.toml",
    ".dotdeployer.json",
    "dotdeployer.yaml",  # detected, but requires PyYAML
)

CHUNK = 1024 * 256


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def expand_path(value: PathLike, base: Optional[PathLike] = None) -> Path:
    """Expand ``~``, ``$VARS`` and make relative paths absolute against *base*.

    Home detection honours ``$HOME`` first (like most dotfile managers) so that
    dry-runs inside containers and tests behave predictably.
    """

    text = os.fspath(value)
    text = os.path.expanduser(text)
    text = os.path.expandvars(text)
    path = Path(text)
    if not path.is_absolute():
        path = Path(base or Path.cwd()) / path
    return Path(os.path.normpath(str(path)))


def home() -> Path:
    env_home = os.environ.get("HOME")
    if env_home:
        return Path(env_home)
    return Path.home()


def find_project_root(start: Optional[PathLike] = None) -> Optional[Path]:
    """Walk upwards looking for a dotdeployer config file."""

    current = expand_path(start or Path.cwd())
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        for name in CONFIG_FILENAMES:
            if (candidate / name).is_file():
                return candidate
    return None


def config_candidates(root: PathLike) -> List[Path]:
    root = Path(root)
    return [root / name for name in CONFIG_FILENAMES if (root / name).is_file()]


def display_path(path: PathLike) -> str:
    """Render a path with ``$HOME`` collapsed to ``~`` for friendlier output."""

    text = str(path)
    try:
        h = str(home())
    except (RuntimeError, OSError):  # pragma: no cover - exotic environments
        return text
    if text == h:
        return "~"
    if text.startswith(h + os.sep):
        return "~" + text[len(h) :]
    return text


def is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def safe_relpath(path: Path, start: Path) -> str:
    try:
        return os.path.relpath(str(path), str(start))
    except ValueError:  # different drives on Windows
        return str(path)


# --------------------------------------------------------------------------- #
# hashing / reading
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_bytes(path: PathLike) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def read_text(path: PathLike, encoding: str = "utf-8") -> str:
    return read_bytes(path).decode(encoding, errors="surrogateescape")


def write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    atomic_write(path, text.encode(encoding, errors="surrogateescape"))


def atomic_write(path: PathLike, data: bytes, mode: Optional[int] = None) -> None:
    """Write *data* to *path* atomically (temp file + ``os.replace``).

    The temporary file is created in the destination directory so the final
    rename stays on the same filesystem and is therefore atomic.
    """

    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp = tempfile.mkstemp(prefix=".dd-tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif target.exists():
            try:
                os.chmod(tmp, stat_mode(target))
            except OSError:  # pragma: no cover - permission denied
                pass
        else:
            # mkstemp creates 0600 files; new dotfiles should follow the umask
            os.chmod(tmp, default_file_mode())
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover
            pass
        raise


def stat_mode(path: PathLike) -> int:
    return os.stat(str(path)).st_mode & 0o7777


def default_file_mode() -> int:
    """``0o666`` masked by the process umask, i.e. what a plain ``open()`` gives."""

    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def default_dir_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o777 & ~current


def ensure_dir(path: PathLike) -> Path:
    p = Path(path)
    if p and not p.exists():
        p.mkdir(parents=True, exist_ok=True, mode=default_dir_mode())
    return p


def is_subpath_of_symlink(path: Path) -> bool:
    """True when any parent directory of *path* is a symlink (safety check)."""

    current = Path(path).absolute()
    parts: List[Path] = []
    while True:
        parts.append(current)
        if current.parent == current:
            break
        current = current.parent
    return any(p.is_symlink() for p in reversed(parts[1:]))


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data[:8192].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def tail_bytes(path: PathLike, limit: int = 65536) -> bytes:
    """Read at most *limit* bytes, keeping the *end* of the file."""

    size = os.path.getsize(str(path))
    with open(str(path), "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        return fh.read()


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #
@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    command: Union[str, Sequence[str]],
    *,
    cwd: Optional[PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
    shell: Optional[bool] = None,
    check: bool = False,
    timeout: Optional[float] = None,
    capture: bool = True,
    input_text: Optional[str] = None,
) -> CommandResult:
    """Run a command, returning captured output instead of raising."""

    if isinstance(command, (list, tuple)):
        args: Union[str, List[str]] = list(command)
        use_shell = False if shell is None else shell
        printable = " ".join(shlex.quote(str(a)) for a in args)
    else:
        args = command
        use_shell = True if shell is None else shell
        printable = command

    full_env = dict(os.environ)
    if env:
        full_env.update({k: str(v) for k, v in env.items()})

    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            shell=use_shell,
            input=input_text,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError(f"command timed out after {timeout}s: {printable}") from exc
    except FileNotFoundError as exc:
        raise ActionError(f"command not found: {printable}", hint="install it or fix the hook") from exc

    result = CommandResult(
        command=printable,
        returncode=proc.returncode,
        stdout=(proc.stdout or "").strip(),
        stderr=(proc.stderr or "").strip(),
    )
    if check and not result.ok:
        raise ActionError(
            f"command failed with exit code {result.returncode}: {printable}",
            hint=result.stderr or result.stdout or None,
        )
    return result


def which(program: str) -> Optional[str]:
    return shutil.which(program)


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def timestamp_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\s.-]", "", str(value), flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value.lower() or "unnamed"


def json_dump(data: Any, *, indent: int = 2, sort_keys: bool = True) -> str:
    return json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=False)


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge *overlay* into *base* (lists are concatenated)."""

    result: Dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, (list, tuple)):
            result[key] = list(result[key]) + list(value)
        else:
            result[key] = value
    return result


def dedupe(items: Iterable[Any]) -> List[Any]:
    seen: set = set()
    out: List[Any] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}PiB"


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


def is_windows() -> bool:
    return os.name == "nt"


def is_tty(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:  # pragma: no cover
        return False


def same_file(a: PathLike, b: PathLike) -> bool:
    try:
        return os.path.samefile(str(a), str(b))
    except (OSError, ValueError):
        return False
