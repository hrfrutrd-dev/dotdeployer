"""Exception hierarchy for dotdeployer.

Every error carries an ``exit_code`` so the CLI can translate failures into
stable, script-friendly process exit statuses.
"""

from __future__ import annotations

from typing import Iterable, List, Optional


class DotDeployerError(Exception):
    """Base class for all dotdeployer errors."""

    exit_code = 1

    def __init__(self, message: str, *, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ConfigError(DotDeployerError):
    """The project configuration is missing, malformed or inconsistent."""

    exit_code = 2


class TemplateError(DotDeployerError):
    """A template could not be parsed or rendered."""

    exit_code = 3


class SecretError(DotDeployerError):
    """A secret backend failed or a secret is unavailable."""

    exit_code = 4


class ActionError(DotDeployerError):
    """A file-system action failed."""

    exit_code = 5

    def __init__(self, message: str, *, path: Optional[str] = None, hint: Optional[str] = None) -> None:
        super().__init__(message, hint=hint)
        self.path = path


class HookError(DotDeployerError):
    """A user supplied hook command failed."""

    exit_code = 6

    def __init__(self, message: str, *, command: Optional[str] = None, returncode: Optional[int] = None) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode


class SelectionError(DotDeployerError):
    """An entry/condition selector referenced something that does not exist."""

    exit_code = 7


class StateError(DotDeployerError):
    """The persisted state file is unreadable or inconsistent."""

    exit_code = 8


class LockError(DotDeployerError):
    """Another dotdeployer process holds the project lock."""

    exit_code = 9


class MultiError(DotDeployerError):
    """Aggregate several failures (used by ``doctor`` and batch operations)."""

    exit_code = 1

    def __init__(self, errors: Iterable[BaseException]) -> None:
        self.errors: List[BaseException] = list(errors)
        super().__init__(f"{len(self.errors)} error(s) occurred")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "\n".join(f"- {e}" for e in self.errors)
