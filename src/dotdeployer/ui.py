"""Terminal presentation layer: colours, tables, prompts, progress-free output.

Deliberately tiny and dependency free.  Respects the ``NO_COLOR`` /
``CLICOLOR_FORCE`` conventions plus ``DOTDEPLOYER_COLOR``.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO

from .util import truncate

_ASCII = os.environ.get("DOTDEPLOYER_ASCII") in {"1", "true", "yes"}


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_CYAN = "\033[96m"


GLYPHS = {
    "ok": "✓" if not _ASCII else "OK",
    "fail": "✗" if not _ASCII else "XX",
    "warn": "!" if not _ASCII else "!!",
    "info": "•" if not _ASCII else "-",
    "arrow": "→" if not _ASCII else "->",
    "delta_add": "+",
    "delta_del": "-",
    "delta_mod": "~",
    "unchanged": "=" if not _ASCII else "=",
    "bullet": "•" if not _ASCII else "*",
}


def color_enabled(stream: Optional[TextIO] = None) -> bool:
    stream = stream or sys.stdout
    forced = os.environ.get("DOTDEPLOYER_COLOR") or os.environ.get("CLICOLOR_FORCE")
    if forced is not None:
        if forced.lower() in {"0", "false", "no", "never"}:
            return False
        if forced.lower() in {"1", "true", "yes", "always", "force"}:
            return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # pragma: no cover
        return False


class Console:
    """A tiny console writer with optional colour and indentation."""

    def __init__(self, stream: Optional[TextIO] = None, *, color: Optional[bool] = None, quiet: bool = False,
                 verbosity: int = 1) -> None:
        self.stream: TextIO = stream or sys.stdout
        self.color = color_enabled(self.stream) if color is None else color
        self.quiet = quiet
        self.verbosity = verbosity
        self._depth = 0

    # -- low level ---------------------------------------------------------
    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(styles) + text + Style.RESET

    def write(self, text: str = "", *, end: str = "\n", style: Optional[str] = None,
              verbosity: int = 1, stream: Optional[TextIO] = None, always: bool = False) -> None:
        if self.quiet and not always:
            return
        if verbosity > self.verbosity and not always:
            return
        indent = "  " * self._depth
        line = f"{indent}{text}" if text else ""
        if style:
            line = self.paint(line, style)
        print(line, file=stream or self.stream, end=end)

    def blank(self, *, verbosity: int = 1) -> None:
        self.write("", verbosity=verbosity)

    # -- semantic helpers --------------------------------------------------
    def heading(self, text: str) -> None:
        self.write(self.paint(text, Style.BOLD), verbosity=0)

    def info(self, text: str, *, verbosity: int = 1) -> None:
        self.write(f"{self.paint(GLYPHS['info'], Style.GREY)} {text}", verbosity=verbosity)

    def debug(self, text: str) -> None:
        self.write(f"{self.paint('debug', Style.GREY)} {text}", verbosity=2)

    def ok(self, text: str) -> None:
        self.write(f"{self.paint(GLYPHS['ok'], Style.GREEN)} {text}", verbosity=0)

    def warn(self, text: str) -> None:
        self.write(f"{self.paint(GLYPHS['warn'], Style.YELLOW)} {text}", verbosity=0, always=True)

    def error(self, text: str) -> None:
        self.write(
            f"{self.paint(GLYPHS['fail'], Style.RED)} {text}",
            verbosity=0,
            always=True,
            stream=sys.stderr,
        )

    def note(self, text: str) -> None:
        self.write(self.paint(text, Style.GREY), verbosity=2)

    def kv(self, key: str, value: Any, width: int = 14) -> None:
        self.write(f"{self.paint(key.ljust(width), Style.CYAN)} {value}")

    def write_err(self, text: str = "", *, verbosity: int = 0, style: Optional[str] = None,
                  always: bool = False) -> None:
        """Write a line to stderr regardless of the normal stream."""

        self.write(text, verbosity=verbosity, style=style, stream=sys.stderr, always=always)

    # -- structure ---------------------------------------------------------
    def indent(self) -> "Console":
        self._depth += 1
        return self

    def dedent(self) -> "Console":
        self._depth = max(0, self._depth - 1)
        return self

    def rule(self, title: str = "", width: int = 62) -> None:
        if title:
            pad = max(0, width - len(title) - 3)
            self.write(self.paint(f"── {title} " + "─" * pad, Style.GREY), verbosity=0)
        else:
            self.write(self.paint("─" * width, Style.GREY), verbosity=0)

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]], *, max_width: int = 46,
              aligns: Optional[Sequence[str]] = None) -> None:
        rows = [[truncate(str(c), max_width) for c in row] for row in rows]
        if not rows:
            return
        widths = [len(h) for h in headers]
        for row in rows:
            for idx, cell in enumerate(row):
                if idx < len(widths):
                    widths[idx] = max(widths[idx], len(cell))
        aligns = list(aligns or ["left"] * len(headers))
        header_line = "  ".join(
            h.ljust(widths[i]) if aligns[i] == "left" else h.rjust(widths[i]) for i, h in enumerate(headers)
        )
        self.write(self.paint(header_line, Style.BOLD), verbosity=0)
        self.write(self.paint("  ".join("─" * w for w in widths), Style.GREY), verbosity=0)
        for row in rows:
            cells = []
            for i, cell in enumerate(row):
                if i >= len(widths):
                    continue
                cells.append(cell.ljust(widths[i]) if aligns[i] == "left" else cell.rjust(widths[i]))
            self.write("  ".join(cells), verbosity=0)

    def diff(self, text: str) -> None:
        for line in text.splitlines():
            if line.startswith("@@"):
                self.write(self.paint(line, Style.CYAN), verbosity=0)
            elif line.startswith(("+++", "---")):
                self.write(self.paint(line, Style.BOLD), verbosity=0)
            elif line.startswith("+"):
                self.write(self.paint(line, Style.GREEN), verbosity=0)
            elif line.startswith("-"):
                self.write(self.paint(line, Style.RED), verbosity=0)
            else:
                self.write(line, verbosity=0)


def prompt_yes_no(question: str, *, default: bool = False, console: Optional[Console] = None,
                  assume_yes: bool = False, no_input: bool = False) -> bool:
    if assume_yes:
        if console:
            console.info(f"{question} (auto-yes)")
        return True
    if no_input or not sys.stdin or not sys.stdin.isatty():
        if console:
            console.info(f"{question} (not a tty → {'yes' if default else 'no'})")
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def prompt_text(question: str, *, default: str = "", secret: bool = False) -> str:
    if secret:
        import getpass

        try:
            value = getpass.getpass(f"{question} ")
        except (EOFError, KeyboardInterrupt):  # pragma: no cover
            return default
    else:
        try:
            value = input(f"{question} " + (f"[{default}] " if default else ""))
        except (EOFError, KeyboardInterrupt):  # pragma: no cover
            return default
    return value.strip() or default


def status_glyph(status: str, console: Console) -> str:
    mapping: Dict[str, tuple] = {
        "ok": (GLYPHS["ok"], Style.GREEN),
        "unchanged": (GLYPHS["unchanged"], Style.GREY),
        "create": (GLYPHS["delta_add"], Style.GREEN),
        "update": (GLYPHS["delta_mod"], Style.YELLOW),
        "modify": (GLYPHS["delta_mod"], Style.YELLOW),
        "replace": (GLYPHS["delta_mod"], Style.BRIGHT_YELLOW),
        "remove": (GLYPHS["delta_del"], Style.RED),
        "delete": (GLYPHS["delta_del"], Style.RED),
        "drift": (GLYPHS["warn"], Style.BRIGHT_YELLOW),
        "missing": (GLYPHS["fail"], Style.RED),
        "conflict": (GLYPHS["fail"], Style.RED),
        "skip": (GLYPHS["unchanged"], Style.GREY),
        "info": (GLYPHS["info"], Style.GREY),
    }
    glyph, style = mapping.get(status, (GLYPHS["info"], Style.GREY))
    return console.paint(glyph, style)


def banner(title: str, subtitle: str = "", console: Optional[Console] = None) -> None:
    console = console or Console()
    console.write(console.paint(f"  {title}", Style.BOLD, Style.BRIGHT_CYAN), verbosity=0)
    if subtitle:
        console.write(console.paint(f"  {subtitle}", Style.GREY), verbosity=0)


def summarize_list(items: List[str], *, limit: int = 6, joiner: str = ", ") -> str:
    if len(items) <= limit:
        return joiner.join(items)
    return joiner.join(items[:limit]) + f" … (+{len(items) - limit} more)"
