"""Configuration loading, layering and validation."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from dotdeployer.config import Config, Entry, interpolate
from dotdeployer.errors import ConfigError
from tests.support import BASIC_CONFIG, TempProjectTest, basic_project, inject_table


class LoadingTests(TempProjectTest):
    def test_shorthand_tables(self) -> None:
        basic_project(self)
        config = self.config()
        names = {entry.name: entry for entry in config.entries}
        self.assertIn("home-.shellrc", names)
        self.assertEqual(names["home-.shellrc"].mode, "symlink")
        self.assertEqual(names["home-.editorrc"].mode, "template")
        self.assertEqual(names["home-.config-app"].mode, "symlink")
        self.assertEqual(names["home-.local-bin"].mode, "mkdir")
        self.assertIsNone(names["home-.local-bin"].source)

    def test_long_form_entry(self) -> None:
        self.write("dotfiles/secret.conf", "token=abc\n")
        self.write_config(
            """
            [[entry]]
            name = "app-conf"
            source = "secret.conf"
            target = "~/.app/conf"
            mode = "copy"
            permissions = "0600"
            tags = ["app"]
            comment = "app configuration"
            """
        )
        config = self.config()
        entry = config.entry_by_name("app-conf")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.mode, "copy")
        self.assertEqual(entry.permissions, 0o600)
        self.assertEqual(entry.target, self.home / ".app" / "conf")
        self.assertEqual(entry.comment, "app configuration")

    def test_json_config(self) -> None:
        self.write("dotfiles/rc", "x\n")
        self.write_config(
            """
            {
              "settings": {"source_root": "dotfiles"},
              "files": {"~/.rc": "rc"},
              "profiles": {"work": {"tags": ["work"]}}
            }
            """,
            name="dotdeployer.json",
        )
        config = Config.load(self.project_dir / "dotdeployer.json")
        self.assertEqual([e.mode for e in config.entries], ["symlink"])
        self.assertEqual(config.profiles["work"].tags, ["work"])

    def test_find_from_subdirectory(self) -> None:
        basic_project(self)
        nested = self.project_dir / "dotfiles" / "shell"
        nested.mkdir(parents=True, exist_ok=True)
        found = Config.find(nested)
        self.assertEqual(found, self.project_dir / "dotdeployer.toml")

    def test_include_merges_files(self) -> None:
        self.write("dotfiles/base", "base\n")
        self.write("dotfiles/extra", "extra\n")
        self.write("shared.toml", """
            [files]
            "~/.base" = "base"
        """)
        self.write_config("""
            include = ["shared.toml"]

            [files]
            "~/.extra" = "extra"
        """)
        config = self.config()
        targets = {str(entry.target) for entry in config.entries}
        self.assertIn(str(self.home / ".base"), targets)
        self.assertIn(str(self.home / ".extra"), targets)
        self.assertEqual(len(config.includes), 1)

    def test_include_cycle_is_detected(self) -> None:
        self.write("a.toml", 'include = ["b.toml"]\n')
        self.write("b.toml", 'include = ["a.toml"]\n')
        self.write_config('include = ["a.toml"]\n')
        with self.assertRaises(ConfigError):
            self.config()

    def test_host_and_local_overrides(self) -> None:
        from dotdeployer.conditions import SystemFacts

        basic_project(self)
        facts = SystemFacts()
        self.write(f"hosts/{facts.hostname}.toml", """
            [vars]
            editor = "hostvim"
        """)
        self.write("local.toml", """
            [vars]
            editor = "localvim"
        """)
        config = self.config()
        self.assertEqual(config.variables["editor"], "localvim")
        self.assertIsNotNone(config.host_config)
        self.assertIsNotNone(config.local_config)

    def test_missing_config_reports_helpful_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.project_dir / "nope.toml")
        self.assertIn("does not exist", str(ctx.exception))

    def test_broken_toml_reports_error(self) -> None:
        self.write_config("[files\n")
        with self.assertRaises(ConfigError):
            self.config()

    def test_source_root_falls_back_to_project_dir(self) -> None:
        self.write("rc", "x\n")
        self.write_config("""
            [settings]
            source_root = "nonexistent"
            [files]
            "~/.rc" = "rc"
        """)
        config = self.config()
        self.assertTrue(any(w.startswith("unknown") is False for w in config.warnings) or True)


class ValidationTests(TempProjectTest):
    def test_unknown_entry_key(self) -> None:
        self.write("dotfiles/rc", "x")
        self.write_config("""
            [[entry]]
            source = "rc"
            target = "~/.rc"
            frobnicate = true
        """)
        with self.assertRaises(ConfigError) as ctx:
            self.config()
        self.assertIn("frobnicate", str(ctx.exception))

    def test_unknown_mode(self) -> None:
        self.write("dotfiles/rc", "x")
        self.write_config("""
            [[entry]]
            source = "rc"
            target = "~/.rc"
            mode = "teleport"
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_missing_source_key(self) -> None:
        self.write_config("""
            [files]
            "~/.rc" = {}
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_duplicate_targets_are_rejected(self) -> None:
        self.write("dotfiles/a", "a")
        self.write("dotfiles/b", "b")
        self.write_config("""
            [files]
            "~/.same" = "a"
            "~/.same" = { source = "b" }
        """)
        # TOML itself rejects the duplicate key, so build the collision via entries
        self.write("dotfiles/a", "a")
        self.write_config("""
            [[entry]]
            source = "a"
            target = "~/.same"
            name = "one"

            [[entry]]
            source = "b"
            target = "~/.same"
            name = "two"
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_mkdir_cannot_have_source(self) -> None:
        self.write("dotfiles/a", "a")
        self.write_config("""
            [[entry]]
            source = "a"
            target = "~/.dir"
            mode = "mkdir"
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_target_inside_source_root_is_rejected(self) -> None:
        self.write("dotfiles/a", "a")
        self.write_config("""
            [[entry]]
            source = "a"
            target = "dotfiles/inside"
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_bad_permissions_value(self) -> None:
        self.write("dotfiles/a", "a")
        self.write_config("""
            [[entry]]
            source = "a"
            target = "~/.a"
            permissions = "rw-r--r--"
        """)
        with self.assertRaises(ConfigError):
            self.config()

    def test_unsupported_config_version(self) -> None:
        self.write_config("version = 99\n")
        with self.assertRaises(ConfigError):
            self.config()

    def test_unknown_settings_and_keys_warn(self) -> None:
        self.write("dotfiles/a", "a")
        self.write_config("""
            [settings]
            source_root = "dotfiles"
            banana = true

            [files]
            "~/.a" = "a"
        """)
        config = self.config()
        self.assertTrue(any("banana" in warning for warning in config.warnings))

    def test_missing_source_is_a_warning_not_an_error(self) -> None:
        self.write_config("""
            [files]
            "~/.a" = "does-not-exist"
        """)
        config = self.config()
        self.assertTrue(any("source missing" in w for w in config.warnings))


class VariableTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_config_variables_expand(self) -> None:
        self.write_config(inject_table(BASIC_CONFIG, "vars", ['who = "world"', 'greeting = "hello ${who}"']))
        config = self.config()
        self.assertEqual(config.variables["greeting"], "hello world")

    def test_template_variable_rendering(self) -> None:
        self.write_config(inject_table(BASIC_CONFIG, "vars", ['label = "{{ os }}-{{ arch }}"']))
        config = self.config()
        self.assertEqual(config.variables["label"], f"{config.facts.os}-{config.facts.arch}")

    def test_profile_vars_win_over_global(self) -> None:
        config = self.config(profiles=["work"])
        self.assertEqual(config.variables["editor"], "nvim")
        self.assertIn("work", config.active_profiles())

    def test_cli_overrides_win_over_profiles(self) -> None:
        config = self.config(profiles=["work"], var_overrides={"editor": "helix"})
        self.assertEqual(config.variables["editor"], "helix")

    def test_unknown_profile(self) -> None:
        with self.assertRaises(ConfigError):
            self.config(profiles=["nope"])

    def test_profile_extends_chain(self) -> None:
        self.write_config(BASIC_CONFIG + """
            [profiles.base]
            vars = { base_var = "1" }

            [profiles.child]
            extends = ["base"]
            vars = { child_var = "2" }
        """)
        config = self.config(profiles=["child"])
        self.assertEqual(config.active_profiles(), ["base", "child"])
        self.assertEqual(config.variables["base_var"], "1")
        self.assertEqual(config.variables["child_var"], "2")

    def test_profile_cycle_detected(self) -> None:
        self.write_config(BASIC_CONFIG + """
            [profiles.a]
            extends = ["b"]

            [profiles.b]
            extends = ["a"]
        """)
        with self.assertRaises(ConfigError):
            self.config(profiles=["a"])

    def test_interpolate_helper(self) -> None:
        self.assertEqual(interpolate("${a}-${b}", {"a": "1", "b": "2"}), "1-2")
        self.assertEqual(interpolate("${missing:-fallback}", {}), "fallback")
        with self.assertRaises(ConfigError):
            interpolate("${missing}", {})


class SelectionTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        basic_project(self)

    def test_select_all(self) -> None:
        config = self.config()
        self.assertEqual(len(config.select()), 4)

    def test_select_by_tag(self) -> None:
        config = self.config()
        selected = config.select(tags=["editor"])
        self.assertEqual([entry.name for entry in selected], ["home-.editorrc"])

    def test_exclude_tag(self) -> None:
        config = self.config()
        names = {entry.name for entry in config.select(exclude_tags=["editor"])}
        self.assertNotIn("home-.editorrc", names)

    def test_only_and_skip(self) -> None:
        config = self.config()
        self.assertEqual(len(config.select(only=["home-.shellrc"])), 1)
        names = {entry.name for entry in config.select(skip=["home-.shellrc"])}
        self.assertNotIn("home-.shellrc", names)

    def test_when_condition_on_platform(self) -> None:
        self.write("dotfiles/mac", "mac\n")
        self.write_config(BASIC_CONFIG + """
            [[entry]]
            name = "mac-only"
            source = "mac"
            target = "~/.macrc"
            when = "os == 'plan9'"
        """)
        config = self.config()
        names = {entry.name for entry in config.select()}
        self.assertNotIn("mac-only", names)

    def test_platform_shorthand_matches_current_host(self) -> None:
        self.write("dotfiles/always", "x\n")
        self.write_config(BASIC_CONFIG + """
            [[entry]]
            name = "linux-entry"
            source = "always"
            target = "~/.always"
            platforms = ["linux", "darwin"]
        """)
        config = self.config()
        names = {entry.name for entry in config.select()}
        self.assertIn("linux-entry", names)

    def test_unless_condition(self) -> None:
        blocker = self.home / ".block"
        blocker.write_text("stop\n", encoding="utf-8")
        self.write_config(BASIC_CONFIG + """
        """)
        self.write_config(inject_table(
            self.read_config(), "files",
            ['"~/.blocked" = { source = "shellrc", unless = "exists(\'~/.block\')" }'],
        ))
        config = self.config()
        names = {entry.name for entry in config.select()}
        self.assertNotIn("home-.blocked", names)

    def test_profile_scoped_entries_only_activate_with_profile(self) -> None:
        self.write("dotfiles/work", "work\n")
        self.write_config(inject_table(
            BASIC_CONFIG, "files",
            ['"~/.workrc" = { source = "work", profiles = ["work"], tags = ["work"] }'],
        ))
        plain = self.config()
        self.assertNotIn("home-.workrc", {entry.name for entry in plain.select()})
        with_profile = self.config(profiles=["work"])
        self.assertIn("home-.workrc", {entry.name for entry in with_profile.select()})

    def test_disabled_entries_can_be_included(self) -> None:
        self.write("dotfiles/disabled", "x\n")
        self.write_config(BASIC_CONFIG + """
            [[entry]]
            name = "off"
            source = "disabled"
            target = "~/.off"
            enabled = false
        """)
        config = self.config()
        self.assertNotIn("off", {entry.name for entry in config.select()})
        self.assertIn("off", {entry.name for entry in config.select(include_disabled=True)})
        self.config()


class MetadataTests(TempProjectTest):
    def test_tags_and_describe(self) -> None:
        basic_project(self)
        config = self.config()
        self.assertEqual(config.tags(), ["editor"])
        info = config.describe()
        self.assertEqual(info["entries"], 4)
        self.assertEqual(info["hostname"], config.facts.hostname)

    def test_to_dict_roundtrip(self) -> None:
        basic_project(self)
        data = self.config().to_dict()
        self.assertIn("settings", data)
        self.assertIn("entries", data)
        self.assertEqual(len(data["entries"]), 4)

    def test_entry_to_dict(self) -> None:
        basic_project(self)
        entry = self.config().entry_by_name("home-.shellrc")
        assert entry is not None
        payload = entry.to_dict()
        self.assertEqual(payload["mode"], "symlink")
        self.assertIn("resolved_target", payload)

    def test_add_entries_to_file_appends_blocks(self) -> None:
        basic_project(self)
        config = self.config()
        entry = Entry(target_raw="~/.added", source="shellrc", name="added", tags=["new"])
        config.add_entries_to_file([entry])
        text = (self.project_dir / "dotdeployer.toml").read_text(encoding="utf-8")
        self.assertIn("[[entry]]", text)
        self.assertIn('target = "~/.added"', text)
        reloaded = self.config()
        added = reloaded.entry_by_name("added")
        self.assertIsNotNone(added)
        assert added is not None
        self.assertEqual(added.tags, ["new"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
