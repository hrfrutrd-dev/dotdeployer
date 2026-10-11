"""Command line interface: commands, flags, JSON output and exit codes."""

from __future__ import annotations

import json
import unittest

from dotdeployer.cli import EXIT_DRIFT, EXIT_OK, EXIT_PENDING
from tests.support import BASIC_CONFIG, TempProjectTest, basic_project, inject_table, run_cli


class InitTests(TempProjectTest):
    def test_init_creates_project(self) -> None:
        code, out, _ = run_cli(self, ["init", "myproject"])
        self.assertEqual(code, 0)
        self.assertIn("created project", out)
        root = self.project_dir / "myproject"
        self.assertTrue((root / "dotdeployer.toml").is_file())
        self.assertTrue((root / "dotfiles" / "shell" / "zshrc").is_file())
        self.assertTrue((root / "hooks" / "reload-shell.sh").is_file())

    def test_init_refuses_to_overwrite_without_force(self) -> None:
        run_cli(self, ["init", "p"])
        code, _, err = run_cli(self, ["init", "p"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_init_force_overwrites(self) -> None:
        run_cli(self, ["init", "p"])
        code, _, _ = run_cli(self, ["init", "p", "--force"])
        self.assertEqual(code, 0)

    def test_init_minimal(self) -> None:
        run_cli(self, ["init", "bare", "--minimal"])
        self.assertFalse((self.project_dir / "bare" / "dotfiles" / "shell").exists())

    def test_scaffolded_project_is_healthy(self) -> None:
        run_cli(self, ["init", "demo"])
        project_dir = self.project_dir / "demo"
        code, out, err = run_cli(self, ["doctor"], cwd=project_dir)
        self.assertEqual(code, 0, err + out)

        code, out, err = run_cli(self, ["plan", "--check"], cwd=project_dir)
        self.assertEqual(code, 12)

        code, out, err = run_cli(self, ["apply", "-y"], cwd=project_dir)
        self.assertEqual(code, 0, err)

        code, out, _ = run_cli(self, ["plan", "--check"], cwd=project_dir)
        self.assertEqual(code, 0)


class VersionAndHelpTests(TempProjectTest):
    def test_version(self) -> None:
        code, out, _ = run_cli(self, ["version"])
        self.assertEqual(code, 0)
        self.assertIn("dotdeployer", out)

    def test_version_json(self) -> None:
        code, out, _ = run_cli(self, ["version", "--json"])
        payload = json.loads(out)
        self.assertIn("version", payload)

    def test_help_without_command(self) -> None:
        code, out, _ = run_cli(self, [])
        self.assertEqual(code, 0)
        self.assertIn("COMMAND", out)

    def test_help_for_command(self) -> None:
        code, out, _ = run_cli(self, ["help", "apply"])
        self.assertEqual(code, 0)
        self.assertIn("apply", out)

    def test_completion_scripts(self) -> None:
        for shell in ("bash", "zsh", "fish"):
            code, out, _ = run_cli(self, ["completion", shell])
            self.assertEqual(code, 0)
            self.assertIn("dotdeployer", out)


class CommandTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_apply_and_status(self) -> None:
        code, out, _ = run_cli(self, ["apply", "-y"])
        self.assertEqual(code, 0)
        self.assertIn("applied", out)

        code, out, _ = run_cli(self, ["status"])
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)

    def test_plan_check_exit_codes(self) -> None:
        code, _, _ = run_cli(self, ["plan", "--check"])
        self.assertEqual(code, 12)

        run_cli(self, ["apply", "-y"])
        code, _, _ = run_cli(self, ["plan", "--check"])
        self.assertEqual(code, 0)

        (self.home / ".editorrc").write_text("edited\n", encoding="utf-8")
        code, _, _ = run_cli(self, ["plan", "--check"])
        self.assertEqual(code, 12)
        code, _, _ = run_cli(self, ["plan", "--check", "--strict"])
        self.assertEqual(code, 11)

        # replacing a managed symlink with a regular file is drift, not a conflict
        (self.home / ".shellrc").unlink()
        (self.home / ".shellrc").write_text("unmanaged\n", encoding="utf-8")
        code, _, _ = run_cli(self, ["plan", "--check"])
        self.assertEqual(code, 12)
        code, _, _ = run_cli(self, ["plan", "--check", "--strict"])
        self.assertEqual(code, 11)

    def test_plan_conflict_exit_code(self) -> None:
        self.write_target(".shellrc", "not managed by dotdeployer\n")
        code, _, _ = run_cli(self, ["plan", "--check"])
        self.assertEqual(code, 10)

    def test_plan_json(self) -> None:
        code, out, _ = run_cli(self, ["plan", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["create"], 4)
        self.assertEqual(payload["changes"][0]["target_display"].startswith("~"), True)

    def test_apply_json(self) -> None:
        code, out, _ = run_cli(self, ["apply", "--json", "-y"])
        payload = json.loads(out)
        self.assertEqual(len(payload["applied"]), 4)
        self.assertEqual(payload["dry_run"], False)

    def test_apply_dry_run_writes_nothing(self) -> None:
        code, out, _ = run_cli(self, ["apply", "-n"])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertFalse((self.home / ".shellrc").exists())

    def test_diff_shows_content_changes(self) -> None:
        run_cli(self, ["apply", "-y"])
        (self.home / ".editorrc").write_text("editor=nano\n", encoding="utf-8")
        code, out, _ = run_cli(self, ["diff"])
        self.assertEqual(code, EXIT_PENDING)  # `diff` doubles as a CI gate
        self.assertIn("-editor=nano", out)
        self.assertIn("+editor=vim", out)
        code, _, _ = run_cli(self, ["diff", "--strict"])
        self.assertEqual(code, EXIT_DRIFT)
        code, out, _ = run_cli(self, ["diff", "--json"])
        self.assertEqual(code, EXIT_PENDING)
        self.assertTrue(json.loads(out)["changes"])

    def test_status_exit_codes(self) -> None:
        code, out, _ = run_cli(self, ["status"])
        self.assertEqual(code, EXIT_PENDING)
        self.assertIn("create", out)
        run_cli(self, ["apply", "-y"])
        code, _, _ = run_cli(self, ["status"])
        self.assertEqual(code, EXIT_OK)
        code, _, _ = run_cli(self, ["plan"])  # plan only reports
        self.assertEqual(code, EXIT_OK)

    def test_tag_selection(self) -> None:
        code, _, _ = run_cli(self, ["apply", "-y", "--tag", "editor"])
        self.assertEqual(code, 0)
        self.assertTrue((self.home / ".editorrc").exists())
        self.assertFalse((self.home / ".shellrc").exists())

    def test_report_to_stdout(self) -> None:
        code, out, _ = run_cli(self, ["report", "-o", "-", "--format", "json", "--no-diff"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertIn("counts", payload)
        self.assertEqual(payload["config"]["entries"], 4)

        code, out, _ = run_cli(self, ["report", "-o", "-", "--format", "html", "--no-diff"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("<!doctype html>", out)

    def test_plan_hooks_run(self) -> None:
        basic_project(self)
        config = inject_table(self.read_config(), "hooks", ['pre_plan = ["echo planning {{ hostname }}"]'])
        self.write_config(config)
        code, out, _ = run_cli(self, ["plan"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("pre_plan", out)

    def test_init_minimal_creates_a_valid_project(self) -> None:
        target = self.tmp / "fresh"
        code, out, _ = run_cli(self, ["init", str(target), "--minimal"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue((target / "dotdeployer.toml").exists())
        code, out, _ = run_cli(self, ["doctor"], cwd=target)
        self.assertEqual(code, EXIT_OK)

    def test_uninstall_dry_run_reports_without_removing(self) -> None:
        run_cli(self, ["apply", "-y"])
        code, out, _ = run_cli(self, ["uninstall", "-n"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("would remove", out)
        self.assertIn("nothing changed", out)
        self.assertTrue((self.home / ".shellrc").exists())

    def test_rollback_restores_a_backup(self) -> None:
        basic_project(self)
        self.write_target(".shellrc", "unmanaged - will be backed up\n")
        run_cli(self, ["apply", "-y", "--only", "home-.shellrc"])
        self.assertTrue(self.home_path(".shellrc").is_symlink())

        code, out, _ = run_cli(self, ["rollback", "--last", "-y"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("restored", out)
        # the backup set holds the file that `apply` replaced
        self.assertFalse(self.home_path(".shellrc").is_symlink())
        self.assertEqual(
            self.home_path(".shellrc").read_text(encoding="utf-8"),
            "unmanaged - will be backed up\n",
        )

    def test_list(self) -> None:
        code, out, _ = run_cli(self, ["list"])
        self.assertEqual(code, 0)
        self.assertIn("home-.shellrc", out)

        code, out, _ = run_cli(self, ["list", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["count"], 4)

    def test_tags_and_profiles(self) -> None:
        code, out, _ = run_cli(self, ["tags"])
        self.assertIn("editor", out)

        code, out, _ = run_cli(self, ["profiles"])
        self.assertIn("work", out)

        code, out, _ = run_cli(self, ["profiles", "--json"])
        payload = json.loads(out)
        self.assertIn("work", payload["profiles"])

    def test_info(self) -> None:
        code, out, _ = run_cli(self, ["info"])
        self.assertEqual(code, 0)
        self.assertIn("entries", out)
        code, out, _ = run_cli(self, ["info", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["config"]["entries"], 4)

    def test_doctor(self) -> None:
        code, out, _ = run_cli(self, ["doctor"])
        self.assertEqual(code, 0, out)
        code, out, _ = run_cli(self, ["doctor", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["fail"], 0)

    def test_doctor_strict_mode(self) -> None:
        run_cli(self, ["apply", "-y"])
        (self.home / ".editorrc").write_text("edited\n", encoding="utf-8")
        code, _, _ = run_cli(self, ["doctor", "--strict"])
        self.assertEqual(code, 1)

    def test_config_commands(self) -> None:
        code, out, _ = run_cli(self, ["config", "path"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("dotdeployer.toml"))

        code, out, _ = run_cli(self, ["config", "show", "--json"])
        payload = json.loads(out)
        self.assertEqual(len(payload["entries"]), 4)

        code, out, _ = run_cli(self, ["config", "validate"])
        self.assertEqual(code, 0)

    def test_config_validate_fails_on_broken_config(self) -> None:
        (self.project_dir / "dotfiles" / "shellrc").unlink()
        code, out, err = run_cli(self, ["config", "validate"])
        self.assertEqual(code, 1)
        self.assertIn("invalid", (out + err).lower())

    def test_secrets_commands(self) -> None:
        code, _, _ = run_cli(self, ["secrets", "list"])
        self.assertEqual(code, 0)
        code, _, _ = run_cli(self, ["secrets", "check"])
        self.assertEqual(code, 0)

    def test_report_json(self) -> None:
        output = self.tmp / "report.json"
        code, out, _ = run_cli(self, ["report", "-o", str(output), "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["totals"]["entries"], 4)

    def test_report_html(self) -> None:
        output = self.tmp / "report.html"
        run_cli(self, ["report", "-o", str(output)])
        text = output.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", text)
        self.assertIn("~/.shellrc", text)
        self.assertIn("home-.shellrc", text)

    def test_backup_commands(self) -> None:
        self.write_target(".shellrc", "original\n")
        run_cli(self, ["apply", "-y"])
        code, out, _ = run_cli(self, ["backup", "ls"])
        self.assertEqual(code, 0, out)
        self.assertIn("items", out)

        code, out, _ = run_cli(self, ["backup", "purge", "--keep", "0"])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(self, ["backup", "ls"])
        self.assertIn("no backup sets", out)

    def test_rollback(self) -> None:
        self.write_target(".shellrc", "original\n")
        run_cli(self, ["apply", "-y"])
        code, out, _ = run_cli(self, ["rollback", "-y"])
        self.assertEqual(code, 0)
        self.assertEqual((self.home / ".shellrc").read_text(encoding="utf-8"), "original\n")

    def test_uninstall(self) -> None:
        run_cli(self, ["apply", "-y"])
        code, out, _ = run_cli(self, ["uninstall", "-y"])
        self.assertEqual(code, 0)
        self.assertIn("removed 4", out)
        self.assertFalse((self.home / ".zshrc").exists())

    def test_adopt(self) -> None:
        self.write_target(".extra", "extra\n")
        code, out, _ = run_cli(self, ["adopt", "~/.extra", "--tag", "extra"])
        self.assertEqual(code, 0)
        self.assertIn("adopted", out)
        code, out, _ = run_cli(self, ["list", "--json"])
        self.assertIn(".extra", out)

    def test_hook_command(self) -> None:
        self.write_config(inject_table(
            self.read_config(), "hooks", ['pre_apply = ["echo hello-hook"]']))
        code, out, _ = run_cli(self, ["hook", "pre_apply"])
        self.assertEqual(code, 0)
        self.assertIn("ran 1 hook", out)

    def test_hook_command_unknown_phase(self) -> None:
        code, _, err = run_cli(self, ["hook", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_no_hooks_flag(self) -> None:
        log = self.tmp / "hook.log"
        self.write_config(inject_table(
            self.read_config(), "hooks", [f'pre_apply = ["echo x >> {log}"]']))
        run_cli(self, ["apply", "-y", "--no-hooks"])
        self.assertFalse(log.exists())

    def test_json_error_output(self) -> None:
        code, out, _ = run_cli(self, ["apply", "--json", "-c", str(self.tmp / "missing.toml")])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertIn("error", payload)

    def test_missing_config_exit_code(self) -> None:
        code, _, err = run_cli(self, ["plan", "-c", str(self.tmp / "nope.toml")])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_quiet_and_verbose(self) -> None:
        code, out, _ = run_cli(self, ["apply", "-q", "-y"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

        code, out, _ = run_cli(self, ["doctor", "-v", "-v"])
        self.assertEqual(code, 0)
        self.assertIn("platform", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
