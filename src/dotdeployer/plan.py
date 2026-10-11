"""Plans: an ordered, inspectable list of changes produced before any mutation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .actions import (
    CONFLICT,
    CREATE,
    DRIFT,
    ERROR,
    REMOVE,
    SKIP,
    UNCHANGED,
    UPDATE,
    Change,
)
from .ui import Console, Style, status_glyph
from .util import display_path

_ORDER = {
    CREATE: 0,
    UPDATE: 1,
    DRIFT: 2,
    CONFLICT: 3,
    REMOVE: 4,
    SKIP: 5,
    UNCHANGED: 6,
    ERROR: 7,
}


@dataclass
class Plan:
    """The result of inspecting every selected entry."""

    changes: List[Change] = field(default_factory=list)
    removals: List[Change] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    profiles: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    hostname: str = ""
    duration_ms: float = 0.0

    # -- queries -----------------------------------------------------------
    def all_changes(self) -> List[Change]:
        return [*self.changes, *self.removals]

    def with_status(self, *statuses: str) -> List[Change]:
        wanted = set(statuses)
        return [c for c in self.all_changes() if c.status in wanted]

    @property
    def mutating(self) -> List[Change]:
        return [c for c in self.all_changes() if c.status in {CREATE, UPDATE, DRIFT, REMOVE}]

    @property
    def conflicts(self) -> List[Change]:
        return self.with_status(CONFLICT)

    @property
    def errors(self) -> List[Change]:
        return self.with_status(ERROR)

    @property
    def up_to_date(self) -> bool:
        return not self.mutating and not self.conflicts and not self.errors

    @property
    def has_drift(self) -> bool:
        return bool(self.with_status(DRIFT))

    def counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for change in self.all_changes():
            counts[change.status] = counts.get(change.status, 0) + 1
        return counts

    def exit_code(self) -> int:
        if self.errors:
            return 1
        if self.conflicts:
            return 10
        if self.has_drift:
            return 11
        if self.mutating:
            return 12
        return 0

    def summary_line(self) -> str:
        counts = self.counts()
        parts = []
        for status in (CREATE, UPDATE, DRIFT, CONFLICT, REMOVE, SKIP, UNCHANGED, ERROR):
            if counts.get(status):
                parts.append(f"{counts[status]} {status}")
        return ", ".join(parts) if parts else "nothing to do"

    def to_dict(self, *, include_diff: bool = False) -> Dict[str, Any]:
        return {
            "hostname": self.hostname,
            "profiles": self.profiles,
            "tags": self.tags,
            "counts": self.counts(),
            "warnings": list(self.warnings),
            "changes": [
                {k: v for k, v in change.to_dict().items() if include_diff or k != "diff"}
                for change in self.all_changes()
            ],
        }

    def to_json(self, *, include_diff: bool = False, indent: int = 2) -> str:
        return json.dumps(self.to_dict(include_diff=include_diff), indent=indent, ensure_ascii=False)

    # -- display -----------------------------------------------------------
    def render(
        self,
        console: Console,
        *,
        show_diff: bool = False,
        show_unchanged: bool = False,
        group: bool = True,
        limit: Optional[int] = None,
    ) -> None:
        """Print a human readable plan."""

        changes = self.all_changes()
        if not show_unchanged:
            visible = [c for c in changes if c.status != UNCHANGED]
        else:
            visible = list(changes)
        visible.sort(key=lambda c: (_ORDER.get(c.status, 9), c.entry.key))
        if limit:
            visible = visible[:limit]

        if not visible:
            if not changes:
                console.info("no entries selected (check --tag / --only / --profile filters)")
            else:
                console.ok("everything is already up to date")
            return

        for change in visible:
            glyph = status_glyph(change.status, console)
            target = display_path(change.target)
            detail = f"  {console.paint(change.detail, Style.GREY)}" if change.detail else ""
            console.write(f"  {glyph} {target}{detail}", verbosity=0)
            if change.error:
                console.write(f"      {console.paint(change.error, Style.RED)}", verbosity=0)
            if change.backup:
                console.write(
                    f"      backup: {console.paint(change.backup, Style.GREY)}", verbosity=0
                )
            if show_diff and change.diff:
                console.indent()
                console.diff(change.diff)
                console.dedent()

    def render_summary(self, console: Console) -> None:
        counts = self.counts()
        if not counts:
            console.ok("nothing to do")
            return
        pieces = []
        palette = {
            CREATE: Style.GREEN,
            UPDATE: Style.YELLOW,
            DRIFT: Style.BRIGHT_YELLOW,
            CONFLICT: Style.RED,
            REMOVE: Style.RED,
            SKIP: Style.GREY,
            UNCHANGED: Style.GREY,
            ERROR: Style.RED,
        }
        for status in (CREATE, UPDATE, DRIFT, CONFLICT, REMOVE, SKIP, UNCHANGED, ERROR):
            if counts.get(status):
                pieces.append(console.paint(f"{counts[status]} {status}", palette.get(status, "")))
        console.write("  " + "  ·  ".join(pieces), verbosity=0)
