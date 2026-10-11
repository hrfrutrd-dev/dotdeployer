"""Test support: temporary HOME + project fixtures.

The package lives under ``src/`` (src-layout), so the tests insert that folder
on ``sys.path`` before importing anything.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotdeployer.config import Config
from dotdeployer.project import Project
from dotdeployer.ui import Console
from dotdeployer.util import expand_path, home


class QuietConsole(Console):
    """A Console that captures output instead of writing to the terminal."""

    def __init__(self) -> None:
        import io

        super().__init__(stream=io.StringIO(), color=False, verbosity=3)
        self.lines: list = []

    def write(self, text: str = "", **kwargs: Any) -> None:  # type: ignore[override]
        self.lines.append(text)
        try:
            super().write(text, **kwargs)
        except Exception:  # pragma: no cover - capture must never fail
            pass

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


class TempProjectTest(unittest.TestCase):
    """Base class giving each test an isolated HOME and project directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dotdeployer-test-"))
        self.home = self.tmp / "home"
        self.project_dir = self.tmp / "project"
        self.home.mkdir(parents=True)
        self.project_dir.mkdir(parents=True)
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("HOME", "USER", "DOTDEPLOYER_HOOKS", "DOTDEPLOYER_SECRET_TOKEN",
                        "DOTDEPLOYER_CONFIG", "DOTDEPLOYER_PROFILE")
        }
        os.environ["HOME"] = str(self.home)
        os.environ["USER"] = "tester"
        for key in ("DOTDEPLOYER_HOOKS", "DOTDEPLOYER_CONFIG", "DOTDEPLOYER_PROFILE"):
            os.environ.pop(key, None)
        self.console = QuietConsole()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture helpers ---------------------------------------------------
    def write(self, relative: str, content: str, *, mode: Optional[int] = None) -> Path:
        path = self.project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        if mode is not None:
            os.chmod(path, mode)
        return path

    def write_config(self, content: str, *, name: str = "dotdeployer.toml") -> Path:
        return self.write(name, content)

    def read_config(self, *, name: str = "dotdeployer.toml") -> str:
        return (self.project_dir / name).read_text(encoding="utf-8")

    def write_target(self, relative: str, content: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def config(self, **kwargs: Any) -> Config:
        return Config.load(self.project_dir / "dotdeployer.toml", **kwargs)

    def project(self, **kwargs: Any) -> Project:
        kwargs.setdefault("console", self.console)
        return Project.load(str(self.project_dir / "dotdeployer.toml"), **kwargs)

    # -- assertions --------------------------------------------------------
    def assertExists(self, path: Path) -> None:
        self.assertTrue(path.exists() or path.is_symlink(), f"{path} should exist")

    def assertMissing(self, path: Path) -> None:
        self.assertFalse(path.exists() or path.is_symlink(), f"{path} should not exist")

    def home_path(self, relative: str) -> Path:
        return self.home / relative


BASIC_CONFIG = """
[settings]
source_root = "dotfiles"
strategy = "backup"
prune = true

[vars]
editor = "vim"

[files]
"~/.editorrc" = { source = "editorrc", template = true, tags = ["editor"] }
"~/.shellrc"  = "shellrc"
"~/.config/app" = { source = "app", mode = "symlink" }

[dirs]
"~/.local/bin" = {}

[profiles.work]
description = "work machine"
tags = ["work"]
vars = { editor = "nvim" }
"""


def basic_project(test: TempProjectTest) -> None:
    """Create a project on disk matching :data:`BASIC_CONFIG`."""

    test.write("dotfiles/editorrc", "editor={{ editor }}\n")
    test.write("dotfiles/shellrc", "export SHELL_MODE=1\n")
    test.write("dotfiles/app/config.toml", "name = 'app'\n")
    test.write_config(BASIC_CONFIG)


def inject_table(config_text: str, table: str, extra_lines: list) -> str:
    """Insert *extra_lines* into an existing ``[table]`` section of *config_text*."""

    header = f"[{table}]"
    lines = config_text.splitlines()
    if header not in lines:
        return config_text.rstrip("\n") + "\n\n" + header + "\n" + "\n".join(extra_lines) + "\n"
    index = lines.index(header)
    end = index + 1
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    return "\n".join(lines[:end] + list(extra_lines) + lines[end:]) + "\n"


def run_cli(test: "TempProjectTest", argv, *, cwd=None):
    """Run the CLI in-process with captured output; returns (code, stdout, stderr)."""

    import contextlib
    import io

    from dotdeployer.cli import main as cli_main

    previous = os.getcwd()
    os.chdir(str(cwd or test.project_dir))
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli_main([str(a) for a in argv])
            except SystemExit as exc:  # argparse exits on bad usage
                code = int(exc.code or 0)
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()
