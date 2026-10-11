"""State persistence, fingerprints and backup sets."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from dotdeployer.actions import BackupStore
from dotdeployer.errors import StateError
from dotdeployer.state import (
    EntryState,
    State,
    fingerprint_file,
    fingerprint_symlink,
    fingerprint_text,
    fingerprint_tree,
)
from tests.support import TempProjectTest


class StateTests(TempProjectTest):
    def state_path(self) -> Path:
        return self.tmp / "state" / "state.json"

    def test_save_and_load_roundtrip(self) -> None:
        state = State(self.state_path())
        state.record(EntryState(name="a", target=str(self.home / ".a"), mode="symlink",
                                fingerprint="sha256:abc", deployed_at="now"))
        state.add_history({"action": "apply"})
        state.save()

        reloaded = State.load(self.state_path())
        self.assertEqual(reloaded.stats()["tracked"], 1)
        entry = reloaded.get("a")
        assert entry is not None
        self.assertEqual(entry.fingerprint, "sha256:abc")
        self.assertEqual(len(reloaded.history), 1)

    def test_missing_state_file_is_empty(self) -> None:
        state = State.load(self.tmp / "nope.json")
        self.assertEqual(state.stats()["tracked"], 0)

    def test_corrupt_state_file_raises(self) -> None:
        path = self.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateError):
            State.load(path)

    def test_unsupported_version_raises(self) -> None:
        path = self.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "entries": {}}), encoding="utf-8")
        with self.assertRaises(StateError):
            State.load(path)

    def test_forget_and_orphans(self) -> None:
        state = State(self.state_path())
        state.record(EntryState(name="keep", target="/tmp/keep"))
        state.record(EntryState(name="gone", target="/tmp/gone"))
        state.forget("keep")
        # "gone" is still tracked but no longer part of the config → orphan
        self.assertEqual([o.name for o in state.orphaned({"keep"}, {"/tmp/keep"})], ["gone"])
        state.forget("gone")
        self.assertEqual(state.orphaned(set(), set()), [])

    def test_orphan_detection(self) -> None:
        state = State(self.state_path())
        state.record(EntryState(name="old", target=str(self.home / ".old")))
        orphans = state.orphaned({"new"}, {str(self.home / ".new")})
        self.assertEqual([o.name for o in orphans], ["old"])

    def test_prune_history(self) -> None:
        state = State(self.state_path())
        for index in range(10):
            state.add_history({"i": index})
        state.prune_history(3)
        self.assertEqual(len(state.history), 3)


class FingerprintTests(TempProjectTest):
    def test_file_fingerprint_changes_with_content(self) -> None:
        path = self.home / "file"
        path.write_text("one", encoding="utf-8")
        first = fingerprint_file(path)
        path.write_text("two", encoding="utf-8")
        self.assertNotEqual(first, fingerprint_file(path))

    def test_symlink_fingerprint(self) -> None:
        target = self.home / "target"
        target.write_text("x", encoding="utf-8")
        link = self.home / "link"
        os.symlink(target, link)
        first = fingerprint_symlink(link)
        link.unlink()
        os.symlink(self.home / "other", link)
        self.assertNotEqual(first, fingerprint_symlink(link))

    def test_tree_fingerprint(self) -> None:
        directory = self.home / "tree"
        (directory / "sub").mkdir(parents=True)
        (directory / "a.txt").write_text("a", encoding="utf-8")
        first = fingerprint_tree(directory)
        (directory / "sub" / "b.txt").write_text("b", encoding="utf-8")
        second = fingerprint_tree(directory)
        self.assertNotEqual(first, second)
        self.assertEqual(second, fingerprint_tree(directory))

    def test_text_fingerprint_is_stable(self) -> None:
        self.assertEqual(fingerprint_text("hello"), fingerprint_text("hello"))
        self.assertNotEqual(fingerprint_text("hello"), fingerprint_text("hello "))


class BackupStoreTests(TempProjectTest):
    def setUp(self) -> None:
        super().setUp()
        self.store = BackupStore(self.tmp / "backups", keep=2)

    def test_save_file_and_restore(self) -> None:
        target = self.home / ".config"
        target.write_text("original", encoding="utf-8")
        marker = self.store.save(target, label="config")
        self.assertIsNotNone(marker)
        self.store.commit()

        target.write_text("changed", encoding="utf-8")
        self.store.restore(self.store.sets()[0]["id"])
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_save_directory_and_symlink(self) -> None:
        directory = self.home / "dir"
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / "file").write_text("data", encoding="utf-8")
        link = self.home / "link"
        os.symlink(directory, link)

        self.store.save(directory, label="dir")
        self.store.save(link, label="link")
        self.store.commit()

        sets = self.store.sets()
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["count"], 2)

        target_restore = self.home / "dir"
        import shutil

        shutil.rmtree(target_restore)
        self.store.restore(sets[0]["id"])
        self.assertTrue((target_restore / "nested" / "file").is_file())
        self.assertTrue(os.path.islink(link))

    def test_empty_sets_are_not_committed(self) -> None:
        self.store.begin()
        self.store.commit()
        self.assertEqual(self.store.sets(), [])

    def test_purge_keeps_newest(self) -> None:
        first = self.home / "one"
        first.write_text("1", encoding="utf-8")
        self.store.save(first, label="one")
        self.store.commit()
        for index in range(3):
            path = self.home / f"f{index}"
            path.write_text(str(index), encoding="utf-8")
            self.store.save(path, label=f"f{index}")
            self.store.commit()

        removed = self.store.purge(keep=2)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(self.store.sets()), 2)

    def test_total_size_and_missing_set(self) -> None:
        path = self.home / "f"
        path.write_text("x" * 100, encoding="utf-8")
        self.store.save(path, label="f")
        self.store.commit()
        self.assertGreater(self.store.total_size(), 0)
        with self.assertRaises(Exception):
            self.store.restore("does-not-exist")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
