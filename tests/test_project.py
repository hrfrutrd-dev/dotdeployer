"""End-to-end project flows: plan, apply, prune, rollback, uninstall, adopt."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from dotdeployer.actions import CREATE, DRIFT, ERROR, REMOVE, UNCHANGED, UPDATE
from dotdeployer.errors import ActionError, ConfigError
from dotdeployer.project import Project
from tests.support import BASIC_CONFIG, TempProjectTest, basic_project, inject_table


class ProjectFlowTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_plan_reports_creates_then_nothing(self) -> None:
        project = self.project(dry_run=True)
        plan = project.plan()
        self.assertEqual(len(plan.mutating), 4)
        self.assertTrue(all(c.status == CREATE for c in plan.mutating))
        self.assertEqual(plan.exit_code(), 12)

        project = self.project()
        result = project.apply()
        self.assertEqual(result.changed, 4)
        self.assertTrue(result.ok)

        self.assertEqual(len(self.project(dry_run=True).plan().mutating), 0)

    def test_apply_is_idempotent(self) -> None:
        self.project().apply()
        second = self.project().apply()
        self.assertEqual(second.changed, 0)
        self.assertIn("0 applied", second.summary_line())
        self.assertIn("nothing to do", second.summary_line())

    def test_apply_records_state(self) -> None:
        self.project().apply()
        state = self.project().state
        self.assertEqual(len(state.entries), 4)
        self.assertEqual(state.stats()["tracked"], 4)
        entry = state.get("home-.shellrc")
        assert entry is not None
        self.assertTrue(entry.fingerprint)

    def test_targets_exist_after_apply(self) -> None:
        self.project().apply()
        self.assertTrue((self.home / ".shellrc").is_symlink())
        self.assertTrue((self.home / ".editorrc").is_file())
        self.assertTrue((self.home / ".config" / "app").is_symlink())
        self.assertTrue((self.home / ".local" / "bin").is_dir())

    def test_profiles_change_deployed_content(self) -> None:
        self.project(profiles=["work"]).apply()
        self.assertEqual((self.home / ".editorrc").read_text(encoding="utf-8"), "editor=nvim\n")

    def test_cli_variables_override(self) -> None:
        self.project(variables={"editor": "emacs"}).apply()
        self.assertEqual((self.home / ".editorrc").read_text(encoding="utf-8"), "editor=emacs\n")

    def test_dry_run_does_not_touch_anything(self) -> None:
        project = self.project(dry_run=True)
        result = project.apply()
        self.assertTrue(result.dry_run)
        self.assertFalse((self.home / ".shellrc").exists())
        self.assertEqual(len(project.state.entries), 0)

    def test_prune_removes_dropped_entries(self) -> None:
        self.project().apply()
        config_text = self.read_config().replace('"~/.shellrc"  = "shellrc"\n', "")
        self.write_config(config_text)

        plan = self.project(dry_run=True).plan()
        removals = [c for c in plan.removals if c.status == REMOVE]
        self.assertEqual(len(removals), 1)

        result = self.project().apply()
        self.assertEqual(len(result.removals), 1)
        self.assertFalse((self.home / ".shellrc").exists())

    def test_prune_disabled_keeps_files(self) -> None:
        self.project().apply()
        self.write_config(self.read_config().replace('"~/.shellrc"  = "shellrc"\n', ""))
        self.project().apply(prune=False)
        self.assertTrue((self.home / ".shellrc").is_symlink())

    def test_drift_blocks_apply_without_force(self) -> None:
        self.project().apply()
        (self.home / ".editorrc").write_text("editor=edited\n", encoding="utf-8")

        result = self.project().apply()
        self.assertEqual(result.changed, 0)
        self.assertTrue(any("modified" in line for line in self.console.lines))

        forced = self.project(force=True).apply()
        self.assertEqual(forced.changed, 1)
        self.assertEqual((self.home / ".editorrc").read_text(encoding="utf-8"), "editor=vim\n")

    def test_conflict_creates_backup(self) -> None:
        self.write_target(".shellrc", "pre-existing\n")
        result = self.project().apply()
        self.assertEqual(result.changed, 4)
        backed_up = [c for c in result.applied if c.backup]
        self.assertEqual(len(backed_up), 1)
        self.assertIsNotNone(result.backup_id)

    def test_rollback_restores_backup(self) -> None:
        self.write_target(".shellrc", "original\n")
        project = self.project()
        project.apply()

        project = self.project()
        data = project.rollback()
        self.assertEqual((self.home / ".shellrc").read_text(encoding="utf-8"), "original\n")
        self.assertTrue(data["restored"])

    def test_rollback_list_reports_sets(self) -> None:
        self.write_target(".shellrc", "original\n")
        self.project().apply()
        data = self.project(dry_run=True).rollback(list_only=True)
        self.assertEqual(len(data["sets"]), 1)
        self.assertGreater(data["total_size"], 0)

    def test_rollback_without_backups_raises(self) -> None:
        project = self.project()
        with self.assertRaises(ActionError):
            project.rollback()

    def test_uninstall_removes_deployed_targets(self) -> None:
        project = self.project()
        project.apply()
        changes = project.uninstall()
        removed = [c for c in changes if c.status == REMOVE]
        self.assertEqual(len(removed), 4)
        self.assertFalse((self.home / ".shellrc").exists())
        self.assertFalse((self.home / ".editorrc").exists())

    def test_uninstall_keeps_drifted_files(self) -> None:
        project = self.project()
        project.apply()
        (self.home / ".editorrc").write_text("mine\n", encoding="utf-8")
        changes = self.project().uninstall()
        drifted = [c for c in changes if c.status == DRIFT]
        self.assertEqual(len(drifted), 1)
        self.assertEqual((self.home / ".editorrc").read_text(encoding="utf-8"), "mine\n")

    def test_adopt_moves_file_and_registers_entry(self) -> None:
        self.write_target(".tmux.conf", "set -g mouse on\n")
        project = self.project()
        adopted = project.adopt(["~/.tmux.conf"], tags=["tmux"])
        self.assertEqual(len(adopted), 1)
        source = self.project_dir / "dotfiles" / ".tmux.conf"
        self.assertTrue(source.is_file())
        self.assertFalse((self.home / ".tmux.conf").exists())

        config = self.config()
        entry = config.entry_by_name(adopted[0].name)
        self.assertIsNotNone(entry, "adopted entry should be written to the config")
        assert entry is not None
        self.assertEqual(entry.tags, ["tmux"])

        self.project().apply()
        self.assertTrue((self.home / ".tmux.conf").is_symlink())

    def test_adopt_copy_keeps_original(self) -> None:
        self.write_target(".kept", "keep\n")
        self.project().adopt(["~/.kept"], move=False)
        self.assertTrue((self.home / ".kept").is_file())
        self.assertTrue((self.project_dir / "dotfiles" / ".kept").is_file())

    def test_hooks_run_in_order(self) -> None:
        log = self.tmp / "hooks.log"
        self.write_config(inject_table(
            self.read_config(), "hooks",
            [f'pre_apply = ["echo pre >> {log}"]',
             f'post_apply = ["echo post >> {log}"]'],
        ))
        self.project().apply()
        self.assertEqual(log.read_text(encoding="utf-8").split(), ["pre", "post"])

    def test_hooks_can_be_disabled(self) -> None:
        log = self.tmp / "hooks.log"
        self.write_config(inject_table(
            self.read_config(), "hooks", [f'pre_apply = ["echo pre >> {log}"]']))
        self.project(hooks_enabled=False).apply()
        self.assertFalse(log.exists())

    def test_failing_hook_is_reported(self) -> None:
        self.write_config(inject_table(
            self.read_config(), "hooks", ['pre_apply = ["exit 3"]']))
        from dotdeployer.errors import HookError

        with self.assertRaises(HookError):
            self.project().apply()

    def test_fail_fast_stops_after_first_error(self) -> None:
        (self.project_dir / "dotfiles" / "shellrc").unlink()
        result = self.project().apply(fail_fast=True)
        self.assertTrue(result.errors)

    def test_errors_do_not_stop_other_entries(self) -> None:
        (self.project_dir / "dotfiles" / "shellrc").unlink()
        result = self.project().apply()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.changed, 3)

    def test_secrets_are_injected_into_templates(self) -> None:
        self.write("dotfiles/token.tmpl", 'token={{ secret("api_token") }}\n')
        self.write_config(inject_table(
            self.read_config(), "files",
            ['"~/.token" = { source = "token.tmpl", mode = "template", secrets = ["api_token"] }'],
        ))
        os.environ["DOTDEPLOYER_SECRET_API_TOKEN"] = "s3cr3t"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_API_TOKEN", None))
        self.project().apply()
        self.assertEqual((self.home / ".token").read_text(encoding="utf-8"), "token=s3cr3t\n")

    def test_missing_secret_fails_the_entry_only(self) -> None:
        self.write("dotfiles/token.tmpl", 'token={{ secret("nope") }}\n')
        self.write_config(inject_table(
            self.read_config(), "files",
            ['"~/.token" = { source = "token.tmpl", mode = "template", secrets = ["nope"] }'],
        ))
        result = self.project().apply()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.changed, 4)

    def test_selection_by_tag(self) -> None:
        result = self.project().apply(tags=["editor"])
        self.assertEqual(result.changed, 1)
        self.assertTrue((self.home / ".editorrc").exists())
        self.assertFalse((self.home / ".shellrc").exists())

    def test_only_and_skip(self) -> None:
        result = self.project().apply(only=["home-.shellrc"])
        self.assertEqual(result.changed, 1)
        self.assertTrue((self.home / ".shellrc").is_symlink())

        second = self.project().apply(only=["home-.shellrc"])
        self.assertEqual(second.changed, 0)

    def test_plan_json_roundtrip(self) -> None:
        import json

        payload = json.loads(self.project(dry_run=True).plan().to_json())
        self.assertEqual(payload["counts"]["create"], 4)
        self.assertEqual(len(payload["changes"]), 4)

    def test_info_and_doctor(self) -> None:
        project = self.project(dry_run=True)
        info = project.info()
        self.assertIn("config", info)
        self.assertIn("state", info)
        checks = project.doctor()
        self.assertTrue(checks)
        self.assertFalse([c for c in checks if c.status == "fail"], [c.message for c in checks if c.status == "fail"])

    def test_doctor_flags_missing_source(self) -> None:
        (self.project_dir / "dotfiles" / "shellrc").unlink()
        problems = [c for c in self.project(dry_run=True).doctor() if c.status == "fail"]
        self.assertTrue(any("source missing" in c.message for c in problems))

    def test_lock_prevents_concurrent_apply(self) -> None:
        from dotdeployer.lock import ProjectLock

        self.write_config(inject_table(self.read_config(), "settings", ["lock_timeout = 0.3"]))
        project = self.project()
        with ProjectLock(project.config.state_dir, timeout=0.2, command="test"):
            with self.assertRaises(Exception):
                project.apply()

    def test_template_error_marks_entry_as_error(self) -> None:
        self.write("dotfiles/broken.tmpl", "{% if %}\n")
        self.write_config(inject_table(
            self.read_config(), "files",
            ['"~/.broken" = { source = "broken.tmpl", mode = "template" }'],
        ))
        result = self.project().apply()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.exit_code(), 1)


class ApiTests(TempProjectTest):
    def test_module_level_helpers(self) -> None:
        from dotdeployer import apply as api_apply
        from dotdeployer import plan as api_plan

        basic_project(self)
        plan = api_plan(str(self.project_dir / "dotdeployer.toml"))
        self.assertEqual(len(plan.mutating), 4)
        result = api_apply(str(self.project_dir / "dotdeployer.toml"), yes=True)
        self.assertEqual(result.changed, 4)

    def test_missing_config_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            Project.load(str(self.tmp / "nothing"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
