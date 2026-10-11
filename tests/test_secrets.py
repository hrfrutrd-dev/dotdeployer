"""Secret backends, resolution order and fallbacks."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

from dotdeployer.errors import SecretError
from dotdeployer.secrets import (
    CommandBackend,
    EnvBackend,
    FileBackend,
    SecretResolver,
    backends_available,
    decrypt_bytes,
    encrypt_bytes,
)
from tests.support import TempProjectTest


class BackendTests(TempProjectTest):
    def test_env_backend_with_prefix(self) -> None:
        os.environ["DOTDEPLOYER_SECRET_API_TOKEN"] = "from-env"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_API_TOKEN", None))
        backend = EnvBackend({}, root=self.project_dir)
        self.assertEqual(backend.get("api_token"), "from-env")
        self.assertIsNone(backend.get("other"))

    def test_env_backend_ignores_placeholder_values(self) -> None:
        os.environ["DOTDEPLOYER_SECRET_EMPTY"] = "changeme"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_EMPTY", None))
        backend = EnvBackend({}, root=self.project_dir)
        self.assertIsNone(backend.get("empty"))

    def test_file_backend(self) -> None:
        secrets_dir = self.project_dir / "secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "db_password").write_text("hunter2\n", encoding="utf-8")
        backend = FileBackend({"dir": "secrets"}, root=self.project_dir)
        self.assertEqual(backend.get("db_password"), "hunter2")
        self.assertIsNone(backend.get("nope"))

    def test_command_backend(self) -> None:
        backend = CommandBackend({"command": "printf secret-%s {name}"}, root=self.project_dir)
        self.assertEqual(backend.get("db"), "secret-db")

    def test_backends_available_listing(self) -> None:
        listing = backends_available()
        names = {item["name"] for item in listing}
        self.assertIn("env", names)
        self.assertIn("gpg", names)
        for item in listing:
            self.assertIn("available", item)


class ResolverTests(TempProjectTest):
    def test_order_is_respected(self) -> None:
        secrets_dir = self.project_dir / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "token").write_text("from-file", encoding="utf-8")
        os.environ["DOTDEPLOYER_SECRET_TOKEN"] = "from-env"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_TOKEN", None))

        env_first = SecretResolver({"order": ["env", "file"], "dir": "secrets"}, root=self.project_dir)
        self.assertEqual(env_first.resolve("token"), "from-env")

        file_first = SecretResolver({"order": ["file", "env"], "dir": "secrets"}, root=self.project_dir)
        self.assertEqual(file_first.resolve("token"), "from-file")

    def test_missing_secret_raises_with_hint(self) -> None:
        resolver = SecretResolver({"order": ["env", "file"], "dir": "secrets"}, root=self.project_dir)
        with self.assertRaises(SecretError) as ctx:
            resolver.resolve("nope")
        self.assertIn("could not be resolved", str(ctx.exception))

    def test_resolve_optional_returns_none(self) -> None:
        resolver = SecretResolver({"order": ["env"], "env_prefix": "DD_TEST_"}, root=self.project_dir)
        self.assertIsNone(resolver.resolve_optional("missing"))

    def test_caching_and_invalidate(self) -> None:
        os.environ["DOTDEPLOYER_SECRET_CACHED"] = "one"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_CACHED", None))
        resolver = SecretResolver({"order": ["env"]}, root=self.project_dir)
        self.assertEqual(resolver.resolve_optional("cached"), "one")
        os.environ["DOTDEPLOYER_SECRET_CACHED"] = "two"
        self.assertEqual(resolver.resolve_optional("cached"), "one")
        resolver.invalidate()
        self.assertEqual(resolver.resolve_optional("cached"), "two")

    def test_unknown_backend_warns(self) -> None:
        resolver = SecretResolver({"order": ["env", "nonsense"]}, root=self.project_dir)
        self.assertTrue(any("unknown secret backend" in w for w in resolver.warnings))
        self.assertEqual([b.name for b in resolver.backends], ["env"])

    def test_describe(self) -> None:
        resolver = SecretResolver({"order": ["env", "file"], "dir": "secrets"}, root=self.project_dir)
        described = resolver.describe()
        self.assertEqual(described["active"], ["env", "file"])

    def test_resolve_many(self) -> None:
        os.environ["DOTDEPLOYER_SECRET_A"] = "a"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_A", None))
        resolver = SecretResolver({"order": ["env"]}, root=self.project_dir)
        values = resolver.resolve_many(["a", "b"])
        self.assertEqual(values, {"a": "a", "b": None})

    def test_placeholder_summary_never_leaks_values(self) -> None:
        os.environ["DOTDEPLOYER_SECRET_LEAK"] = "super-secret-value"
        self.addCleanup(lambda: os.environ.pop("DOTDEPLOYER_SECRET_LEAK", None))
        resolver = SecretResolver({"order": ["env"]}, root=self.project_dir)
        summary = resolver.placeholder_summary("leak")
        self.assertTrue(summary["resolved"])
        self.assertNotIn("super-secret-value", str(summary))
        self.assertEqual(summary["preview"], "********")


class EncryptionTests(TempProjectTest):
    def test_encrypt_requires_recipient(self) -> None:
        with self.assertRaises(SecretError):
            encrypt_bytes(b"data", options={}, root=self.project_dir)

    def test_decrypt_without_tool_fails_cleanly(self) -> None:
        if shutil_which("gpg") or shutil_which("age"):
            self.skipTest("gpg/age available; this test covers the missing-tool path")
        with self.assertRaises(SecretError):
            decrypt_bytes(b"data", options={"encryption": "gpg"}, root=self.project_dir)
        with self.assertRaises(SecretError):
            decrypt_bytes(b"data", options={"encryption": "age"}, root=self.project_dir)


def shutil_which(program: str):
    import shutil

    return shutil.which(program)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
