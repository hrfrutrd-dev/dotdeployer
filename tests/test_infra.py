"""Locking, doctor checks, reporting and the utility helpers."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from dotdeployer.doctor import Check, render, summarise, worst_status
from dotdeployer.errors import LockError
from dotdeployer.lock import ProjectLock, _pid_alive
from dotdeployer.report import build_report, render_html, to_json, write_report
from dotdeployer.util import (
    deep_merge,
    display_path,
    expand_path,
    human_size,
    is_binary,
    json_dump,
    sha256_text,
    slugify,
    truncate,
)
from tests.support import TempProjectTest, basic_project, inject_table, run_cli


class LockTests(TempProjectTest):
    def test_acquire_and_release(self) -> None:
        lock = ProjectLock(self.tmp / "state", timeout=1, command="test")
        with lock:
            self.assertTrue(lock.acquired)
            self.assertTrue(lock.path.exists())
        self.assertFalse(lock.acquired)

    def test_second_lock_is_refused(self) -> None:
        first = ProjectLock(self.tmp / "state", timeout=0.2, command="first")
        with first:
            second = ProjectLock(self.tmp / "state", timeout=0.2, command="second")
            with self.assertRaises(LockError) as ctx:
                second.acquire()
            self.assertIn("holds the project lock", str(ctx.exception))

    def test_lock_is_reentrant_after_release(self) -> None:
        with ProjectLock(self.tmp / "state", timeout=0.2):
            pass
        with ProjectLock(self.tmp / "state", timeout=0.2):
            pass

    def test_stale_lock_from_dead_process_is_replaced(self) -> None:
        path = self.tmp / "state" / "state.json.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 999999, "host": "nope", "started_at": "", "command": ""}),
                        encoding="utf-8")
        self.assertFalse(_pid_alive(999999))
        lock = ProjectLock(self.tmp / "state", timeout=0.5, command="fresh")
        with lock:
            self.assertTrue(lock.acquired)


class DoctorTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_healthy_project_has_no_failures(self) -> None:
        checks = self.project(dry_run=True).doctor()
        failures = [c for c in checks if c.status == "fail"]
        self.assertEqual(failures, [], [c.message for c in failures])
        self.assertEqual(worst_status(checks), "ok")

    def test_missing_source_is_a_failure(self) -> None:
        (self.project_dir / "dotfiles" / "shellrc").unlink()
        checks = self.project(dry_run=True).doctor()
        self.assertTrue(any(c.status == "fail" and "source missing" in c.message for c in checks))

    def test_broken_template_is_a_failure(self) -> None:
        self.write("dotfiles/broken.tmpl", "{% for %}")
        self.write_config(inject_table(
            self.read_config(), "files", ['"~/.broken" = { source = "broken.tmpl", mode = "template" }']))
        checks = self.project(dry_run=True).doctor()
        self.assertTrue(any("template cannot be rendered" in c.message for c in checks if c.status == "fail"))

    def test_drift_generates_warning(self) -> None:
        self.project().apply()
        (self.home / ".editorrc").write_text("edited\n", encoding="utf-8")
        checks = self.project(dry_run=True).doctor()
        self.assertTrue(any(c.category == "drift" and c.status == "warn" for c in checks))

    def test_broken_symlink_warns(self) -> None:
        link = self.home / ".shellrc"
        os.symlink(self.tmp / "nowhere", link)
        checks = self.project(dry_run=True).doctor()
        self.assertTrue(any("broken symlink" in c.message for c in checks))

    def test_missing_secret_warns(self) -> None:
        self.write("dotfiles/token.tmpl", '{{ secret("nope") }}\n')
        self.write_config(inject_table(
            self.read_config(), "files",
            ['"~/.token" = { source = "token.tmpl", mode = "template", secrets = ["nope"] }'],
        ))
        checks = self.project(dry_run=True).doctor()
        self.assertTrue(any(c.name == "secret:nope" and c.status == "warn" for c in checks))

    def test_summarise_and_render(self) -> None:
        checks = [
            Check("a", "ok", "fine"),
            Check("b", "warn", "watch out"),
            Check("c", "fail", "broken"),
        ]
        counts = summarise(checks)
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(counts["fail"], 1)
        self.assertEqual(worst_status(checks), "fail")
        render(checks, self.console, show_info=True)
        self.assertIn("broken", self.console.output)

    def test_doctor_strict_cli_exit(self) -> None:
        self.project().apply()
        (self.home / ".editorrc").write_text("edited\n", encoding="utf-8")
        code, _, _ = run_cli(self, ["doctor", "--strict"])
        self.assertEqual(code, 1)
        code, _, _ = run_cli(self, ["doctor"])
        self.assertEqual(code, 0)


class ReportTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_build_report_structure(self) -> None:
        report = build_report(self.project(dry_run=True))
        self.assertIn("host", report)
        self.assertIn("entries", report)
        self.assertEqual(report["totals"]["entries"], 4)
        payload = json.loads(to_json(report))
        self.assertEqual(payload["counts"]["create"], 4)

    def test_html_report_contains_everything(self) -> None:
        report = build_report(self.project(dry_run=True))
        text = render_html(report, title="my dotfiles")
        self.assertIn("my dotfiles", text)
        self.assertIn("~/.shellrc", text)
        self.assertIn("home-.shellrc", text)
        self.assertIn("<style>", text)
        self.assertNotIn("http://", text.split("<style>")[0])

    def test_write_report_html_and_json(self) -> None:
        html_path = self.tmp / "out" / "report.html"
        json_path = self.tmp / "out" / "report.json"
        write_report(self.project(dry_run=True), html_path)
        write_report(self.project(dry_run=True), json_path, fmt="json")
        self.assertIn("<html", html_path.read_text(encoding="utf-8"))
        json.loads(json_path.read_text(encoding="utf-8"))

    def test_report_after_apply_shows_state(self) -> None:
        self.project().apply()
        report = build_report(self.project(dry_run=True))
        self.assertEqual(report["totals"]["changes"], 0)
        self.assertEqual(report["state"]["tracked"], 4)

    def test_diffs_can_be_included(self) -> None:
        self.project().apply()
        (self.home / ".editorrc").write_text("editor=emacs\n", encoding="utf-8")
        report = build_report(self.project(dry_run=True), include_diff=True)
        self.assertIn("diffs", report)
        self.assertTrue(any("-editor=emacs" in d for d in report["diffs"].values()))


class UtilTests(unittest.TestCase):
    def test_expand_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DD_UTIL_BASE"] = tmp
            try:
                self.assertEqual(expand_path("$DD_UTIL_BASE/sub"), Path(tmp) / "sub")
            finally:
                os.environ.pop("DD_UTIL_BASE", None)
            self.assertTrue(expand_path("~").is_absolute())
            self.assertTrue(expand_path("relative", base=tmp).is_absolute())

    def test_display_path_collapses_home(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(display_path(Path(tmp) / ".bashrc"), "~/.bashrc")
                self.assertEqual(display_path(Path(tmp)), "~")
            finally:
                pass

    def test_slugify(self) -> None:
        self.assertEqual(slugify("My Fancy_Name.txt"), "my-fancy-name.txt")
        self.assertEqual(slugify("---"), "unnamed")

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512B")
        self.assertIn("KiB", human_size(2048))

    def test_truncate(self) -> None:
        self.assertEqual(truncate("abcdef", 10), "abcdef")
        self.assertEqual(truncate("abcdef", 5), "abcd…")
        self.assertEqual(truncate("a\nb", 5), "a b")

    def test_deep_merge(self) -> None:
        base = {"a": 1, "b": {"c": 2}, "d": [1]}
        overlay = {"b": {"e": 3}, "d": [2], "f": 4}
        merged = deep_merge(base, overlay)
        self.assertEqual(merged["a"], 1)
        self.assertEqual(merged["b"], {"c": 2, "e": 3})
        self.assertEqual(merged["d"], [1, 2])
        self.assertEqual(merged["f"], 4)

    def test_json_dump_is_stable(self) -> None:
        first = json_dump({"b": 1, "a": 2})
        second = json_dump({"a": 2, "b": 1})
        self.assertEqual(first, second)

    def test_sha256_text(self) -> None:
        self.assertEqual(sha256_text("abc"), sha256_text("abc"))
        self.assertTrue(sha256_text("abc").startswith("sha256:"))

    def test_is_binary(self) -> None:
        self.assertFalse(is_binary(b"hello"))
        self.assertTrue(is_binary(b"\x00\x01"))
        self.assertTrue(is_binary(b"\xff\xfe\x00"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
