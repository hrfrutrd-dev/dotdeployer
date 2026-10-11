"""Deployment engine behaviour: modes, strategies, drift and verification."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

from dotdeployer.actions import (
    CONFLICT,
    CREATE,
    DRIFT,
    ERROR,
    SKIP,
    UNCHANGED,
    UPDATE,
    BackupStore,
    Deployer,
)
from dotdeployer.state import State
from tests.support import TempProjectTest, inject_table

CONFIG = """
[settings]
source_root = "dotfiles"
strategy = "backup"

[files]
"~/.link" = "link-target"
"~/.copied" = { source = "copy-source", mode = "copy" }
"~/.templated" = { source = "template-source", mode = "template" }

[dirs]
"~/.created-dir" = {}
"""


class DeployerTest(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        self.write("dotfiles/link-target", "link content\n")
        self.write("dotfiles/copy-source", "copy content\n")
        self.write("dotfiles/template-source", "value={{ editor | default('vim') }}\n")
        self.write_config(CONFIG)
        self.config_obj = self.config()
        self.state = State(self.config_obj.state_dir / "state.json")
        self.deployer = Deployer(
            self.config_obj,
            self.state,
            backups=BackupStore(self.config_obj.backup_dir, keep=5),
            console=self.console,
            assume_yes=True,
            no_input=True,
        )

    def entry(self, name: str):
        entry = self.config_obj.entry_by_name(name)
        self.assertIsNotNone(entry, f"entry {name} not found")
        return entry

    def apply_entry(self, name: str):
        entry = self.entry(name)
        change = self.deployer.inspect(entry)
        outcome = self.deployer.apply(change)
        if outcome.status not in (UNCHANGED, SKIP, ERROR):
            self.deployer.record(outcome)
        return outcome

    # -- creation ---------------------------------------------------------
    def test_create_symlink(self) -> None:
        outcome = self.apply_entry("home-.link")
        self.assertEqual(outcome.status, CREATE)
        link = self.home / ".link"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.config_obj.source_root / "link-target"))

    def test_create_copy(self) -> None:
        outcome = self.apply_entry("home-.copied")
        self.assertEqual(outcome.status, CREATE)
        self.assertEqual((self.home / ".copied").read_text(encoding="utf-8"), "copy content\n")
        self.assertFalse((self.home / ".copied").is_symlink())

    def test_create_template(self) -> None:
        outcome = self.apply_entry("home-.templated")
        self.assertEqual(outcome.status, CREATE)
        self.assertEqual((self.home / ".templated").read_text(encoding="utf-8"), "value=vim\n")
        # default file mode follows the umask, not mkstemp's 0600
        mode = stat.S_IMODE((self.home / ".templated").stat().st_mode)
        self.assertNotEqual(mode, 0o600)

    def test_create_directory(self) -> None:
        outcome = self.apply_entry("home-.created-dir")
        self.assertEqual(outcome.status, CREATE)
        self.assertTrue((self.home / ".created-dir").is_dir())

    def test_parent_directories_are_created(self) -> None:
        self.write("dotfiles/deep", "deep\n")
        self.write_config(inject_table(CONFIG, "files", ['"~/.config/deep/file" = "deep"']))
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        entry = config.entry_by_name("home-.config-deep-file")
        outcome = deployer.apply(deployer.inspect(entry))
        self.assertEqual(outcome.status, CREATE)
        self.assertTrue((self.home / ".config" / "deep" / "file").is_file())

    # -- idempotence ------------------------------------------------------
    def test_second_inspect_is_unchanged(self) -> None:
        self.apply_entry("home-.link")
        change = self.deployer.inspect(self.entry("home-.link"))
        self.assertEqual(change.status, UNCHANGED)

    def test_template_change_is_detected(self) -> None:
        self.apply_entry("home-.templated")
        self.write("dotfiles/template-source", "value=changed\n")
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        entry = config.entry_by_name("home-.templated")
        change = deployer.inspect(entry)
        self.assertEqual(change.status, UPDATE)
        self.assertIn("value=changed", change.diff)

    # -- strategies -------------------------------------------------------
    def test_conflict_backs_up_and_replaces(self) -> None:
        target = self.write_target(".link", "user data\n")
        outcome = self.apply_entry("home-.link")
        self.assertEqual(outcome.status, UPDATE)
        self.assertIsNotNone(outcome.backup)
        self.assertTrue(target.is_symlink())
        backups = self.deployer.backups.sets()
        self.assertEqual(len(backups), 1)
        stored = backups[0]["items"][0]
        self.assertEqual(Path(stored["stored_as"]).read_text(encoding="utf-8"), "user data\n")

    def test_conflict_with_skip_strategy_leaves_file(self) -> None:
        self.write_config(CONFIG.replace('strategy = "backup"', 'strategy = "skip"'))
        config = self.config()
        self.write_target(".link", "keep me\n")
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        entry = config.entry_by_name("home-.link")
        change = deployer.inspect(entry)
        self.assertEqual(change.status, CONFLICT)
        outcome = deployer.apply(change)
        self.assertEqual(outcome.status, SKIP)
        self.assertEqual((self.home / ".link").read_text(encoding="utf-8"), "keep me\n")

    def test_conflict_with_override_strategy_removes_without_backup(self) -> None:
        self.write_config(CONFIG.replace('strategy = "backup"', 'strategy = "override"'))
        config = self.config()
        self.write_target(".link", "gone soon\n")
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        entry = config.entry_by_name("home-.link")
        outcome = deployer.apply(deployer.inspect(entry))
        self.assertEqual(outcome.status, UPDATE)
        self.assertIsNone(outcome.backup)
        self.assertEqual(deployer.backups.sets(), [])

    def test_merge_strategy_keeps_unmanaged_files(self) -> None:
        self.write("dotfiles/tree/a.conf", "a\n")
        self.write("dotfiles/tree/b.conf", "b\n")
        self.write_config(inject_table(CONFIG, "files",
                                       ['"~/.merged" = { source = "tree", mode = "copy", strategy = "merge" }']))
        config = self.config()
        target_dir = self.home / ".merged"
        target_dir.mkdir()
        (target_dir / "local.conf").write_text("local\n", encoding="utf-8")
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        outcome = deployer.apply(deployer.inspect(config.entry_by_name("home-.merged")))
        self.assertEqual(outcome.status, UPDATE)
        self.assertEqual((target_dir / "a.conf").read_text(encoding="utf-8"), "a\n")
        self.assertTrue((target_dir / "local.conf").is_file())

    # -- drift ------------------------------------------------------------
    def test_drift_detection(self) -> None:
        self.apply_entry("home-.templated")
        target = self.home / ".templated"
        target.write_text("hand edited\n", encoding="utf-8")
        change = self.deployer.inspect(self.entry("home-.templated"))
        self.assertEqual(change.status, DRIFT)

    def test_symlink_replaced_by_file_is_drift(self) -> None:
        self.apply_entry("home-.link")
        link = self.home / ".link"
        link.unlink()
        link.write_text("real file now\n", encoding="utf-8")
        change = self.deployer.inspect(self.entry("home-.link"))
        self.assertEqual(change.status, DRIFT)

    def test_unmanaged_file_is_a_conflict(self) -> None:
        self.write_target(".copied", "somebody else's file\n")
        change = self.deployer.inspect(self.entry("home-.copied"))
        self.assertEqual(change.status, CONFLICT)
        self.assertIn("-somebody else's file", change.diff.replace("\n", ""))

    # -- permissions and errors -------------------------------------------
    def test_permissions_are_applied(self) -> None:
        self.write_config(inject_table(
            CONFIG, "files",
            ['"~/.private" = { source = "template-source", mode = "template", permissions = "0600" }'],
        ))
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        deployer.apply(deployer.inspect(config.entry_by_name("home-.private")))
        mode = stat.S_IMODE((self.home / ".private").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_source_is_an_error(self) -> None:
        (self.config_obj.source_root / "copy-source").unlink()
        change = self.deployer.inspect(self.entry("home-.copied"))
        self.assertEqual(change.status, ERROR)
        self.assertIn("source does not exist", change.error or "")

    def test_template_error_is_reported_as_error(self) -> None:
        self.write("dotfiles/broken.tmpl", "{{ 1 + }}\n")
        self.write_config(inject_table(
            CONFIG, "files", ['"~/.broken" = { source = "broken.tmpl", mode = "template" }']))
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console)
        change = deployer.inspect(config.entry_by_name("home-.broken"))
        self.assertEqual(change.status, ERROR)
        self.assertIn("template", (change.error or "").lower())

    def test_dry_run_changes_nothing(self) -> None:
        deployer = Deployer(self.config_obj, self.state, console=self.console,
                            dry_run=True, assume_yes=True)
        outcome = deployer.apply(deployer.inspect(self.entry("home-.link")))
        self.assertFalse((self.home / ".link").exists())
        self.assertEqual(outcome.status, CREATE)

    def test_tree_template_renders_every_file(self) -> None:
        self.write("dotfiles/tree-tpl/a.conf", "a={{ editor | default('vim') }}\n")
        self.write("dotfiles/tree-tpl/nested/b.conf", "b={{ os }}\n")
        self.write_config(inject_table(
            CONFIG, "files", ['"~/.tree" = { source = "tree-tpl", mode = "template" }']))
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console, assume_yes=True)
        outcome = deployer.apply(deployer.inspect(config.entry_by_name("home-.tree")))
        self.assertEqual(outcome.status, CREATE)
        self.assertEqual((self.home / ".tree" / "a.conf").read_text(encoding="utf-8"), "a=vim\n")
        self.assertTrue((self.home / ".tree" / "nested" / "b.conf").is_file())

    def test_remove_is_refused_when_target_drifted(self) -> None:
        outcome = self.apply_entry("home-.templated")
        state_entry = self.state.get(outcome.entry.key)
        target = self.home / ".templated"
        target.write_text("modified\n", encoding="utf-8")
        removal = self.deployer.remove(state_entry)
        self.assertEqual(removal.status, DRIFT)
        self.assertTrue(target.exists())

    def test_remove_deletes_managed_target(self) -> None:
        outcome = self.apply_entry("home-.link")
        state_entry = self.state.get(outcome.entry.key)
        removal = self.deployer.remove(state_entry)
        self.assertEqual(removal.status, "remove")
        self.assertFalse((self.home / ".link").is_symlink())

    def test_encrypted_source_without_key_is_an_error(self) -> None:
        self.write("dotfiles/secret.age", "not really encrypted\n")
        self.write_config(inject_table(
            CONFIG, "files", ['"~/.secret" = { source = "secret.age", mode = "copy", encrypt = true }']))
        config = self.config()
        deployer = Deployer(config, self.state, console=self.console)
        change = deployer.inspect(config.entry_by_name("home-.secret"))
        self.assertEqual(change.status, ERROR)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
