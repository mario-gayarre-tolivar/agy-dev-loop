"""Unit tests for `core.manifest`."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.manifest import (  # noqa: E402
    LoopManifest,
    detect_stack_manifest,
    init_manifest,
    load_manifest,
)


class ManifestTests(unittest.TestCase):
    """Tests for manifest loading, stack detection, and dry-run initialization."""

    def test_load_manifest_rejects_missing_or_empty_test_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                load_manifest(root)

            agents_dir = root / ".agents"
            agents_dir.mkdir(parents=True)
            manifest_path = agents_dir / "loop.json"

            # Malformed JSON
            manifest_path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(root)

            # Empty and whitespace test_cmd
            for bad_cmd in ("", "   \t\n"):
                manifest_path.write_text(
                    json.dumps({"test_cmd": bad_cmd}), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_manifest(root)

            # Missing test_cmd
            manifest_path.write_text(json.dumps({"max_iter": 3}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(root)

            # Invalid max_iter / circuit_breaker
            for payload in (
                {"test_cmd": "pytest", "max_iter": 0},
                {"test_cmd": "pytest", "max_iter": -1},
                {"test_cmd": "pytest", "max_iter": True},
                {"test_cmd": "pytest", "circuit_breaker": 0},
                {"test_cmd": "pytest", "circuit_breaker": -2},
                {"test_cmd": "pytest", "circuit_breaker": False},
                {"test_cmd": "pytest", "protected_test_globs": "tests/**"},
                {"test_cmd": "pytest", "protected_test_globs": [""]},
                {"test_cmd": "pytest", "protected_test_globs": ["   "]},
            ):
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(root)

    def test_load_manifest_parses_valid_json_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / ".agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "loop.json").write_text(
                json.dumps(
                    {
                        "test_cmd": "python3 -m unittest discover -s tests -v",
                        "lint_cmd": "npm run lint",
                        "protected_test_globs": ["tests/**", "specs/*.py"],
                    }
                ),
                encoding="utf-8",
            )

            manifest = load_manifest(root)
            self.assertEqual(
                manifest.test_cmd, "python3 -m unittest discover -s tests -v"
            )
            self.assertEqual(manifest.lint_cmd, "npm run lint")
            self.assertIsNone(manifest.build_cmd)
            self.assertEqual(manifest.protected_test_globs, ("tests/**", "specs/*.py"))
            self.assertIsInstance(manifest.protected_test_globs, tuple)
            self.assertEqual(manifest.max_iter, 5)
            self.assertEqual(manifest.circuit_breaker, 3)

            # Default protected_test_globs when omitted
            (agents_dir / "loop.json").write_text(
                json.dumps({"test_cmd": "node --test"}),
                encoding="utf-8",
            )
            defaulted = load_manifest(root)
            self.assertEqual(
                defaulted.protected_test_globs,
                LoopManifest(test_cmd="node --test").protected_test_globs,
            )

    def test_detect_stack_manifest_supports_python_and_node_and_rejects_empty_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty_root = Path(tmp) / "empty"
            empty_root.mkdir()
            with self.assertRaises(ValueError):
                detect_stack_manifest(empty_root)

            # Python Django manage.py
            django_root = Path(tmp) / "django_app"
            django_root.mkdir()
            (django_root / "manage.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            django_manifest = detect_stack_manifest(django_root)
            self.assertEqual(django_manifest.test_cmd, "python3 manage.py test")

            # Python tests/ directory
            py_tests_root = Path(tmp) / "py_tests"
            (py_tests_root / "tests").mkdir(parents=True)
            (py_tests_root / "tests" / "test_sample.py").write_text("", encoding="utf-8")
            py_manifest = detect_stack_manifest(py_tests_root)
            self.assertEqual(
                py_manifest.test_cmd, "python3 -m unittest discover -s tests -v"
            )

            # Python root test_*.py
            py_root_tests = Path(tmp) / "py_root"
            py_root_tests.mkdir()
            (py_root_tests / "test_root.py").write_text("", encoding="utf-8")
            py_root_manifest = detect_stack_manifest(py_root_tests)
            self.assertEqual(py_root_manifest.test_cmd, "python3 -m unittest discover -v")

            # Node package.json with npm default stub should be rejected
            npm_stub_root = Path(tmp) / "npm_stub"
            npm_stub_root.mkdir()
            (npm_stub_root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "stub",
                        "scripts": {
                            "test": 'echo "Error: no test specified" && exit 1'
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                detect_stack_manifest(npm_stub_root)

            # Node package.json with real test, lint, and build scripts
            npm_real_root = Path(tmp) / "npm_real"
            npm_real_root.mkdir()
            (npm_real_root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "real",
                        "scripts": {
                            "test": "node --test",
                            "lint": "eslint .",
                            "build": "tsc -b",
                        },
                    }
                ),
                encoding="utf-8",
            )
            npm_manifest = detect_stack_manifest(npm_real_root)
            self.assertEqual(npm_manifest.test_cmd, "npm test")
            self.assertEqual(npm_manifest.lint_cmd, "npm run lint")
            self.assertEqual(npm_manifest.build_cmd, "npm run build")

            # Node native *.test.mjs without package.json test script
            node_native_root = Path(tmp) / "node_native"
            node_native_root.mkdir()
            (node_native_root / "math.test.mjs").write_text("", encoding="utf-8")
            node_manifest = detect_stack_manifest(node_native_root)
            self.assertEqual(node_manifest.test_cmd, "node --test")

            # Ignored directories like node_modules/ must be pruned and ignored
            ignored_root = Path(tmp) / "ignored_dirs"
            (ignored_root / "node_modules").mkdir(parents=True)
            (ignored_root / "node_modules" / "test_fake.py").write_text("", encoding="utf-8")
            (ignored_root / ".venv").mkdir()
            (ignored_root / ".venv" / "test_fake.py").write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                detect_stack_manifest(ignored_root)

            # *_test.py and non-test_* helper modules in tests/ must not trigger unittest
            non_unittest_root = Path(tmp) / "non_unittest"
            (non_unittest_root / "tests").mkdir(parents=True)
            (non_unittest_root / "foo_test.py").write_text("", encoding="utf-8")
            (non_unittest_root / "tests" / "bar_test.py").write_text("", encoding="utf-8")
            (non_unittest_root / "tests" / "testutils.py").write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                detect_stack_manifest(non_unittest_root)

    def test_detect_and_init_manifest_verifies_dry_run_success_and_rejects_failing_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Passing dry-run
            ok_root = Path(tmp) / "ok_repo"
            (ok_root / "tests").mkdir(parents=True)
            (ok_root / "tests" / "test_ok.py").write_text(
                "import unittest\n\n"
                "class OkTest(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        self.assertEqual(1 + 1, 2)\n",
                encoding="utf-8",
            )

            written_path = init_manifest(ok_root, run_dry_check=True)
            self.assertEqual(written_path, ok_root / ".agents" / "loop.json")
            self.assertTrue(written_path.is_file())
            loaded = load_manifest(ok_root)
            self.assertEqual(
                loaded.test_cmd, "python3 -m unittest discover -s tests -v"
            )

            # Failing dry-run must raise RuntimeError and neither create .agents/ nor clobber existing manifest
            fail_root = Path(tmp) / "fail_repo"
            (fail_root / "tests").mkdir(parents=True)
            (fail_root / "tests" / "test_fail.py").write_text(
                "import sys, unittest\n\n"
                "class FailTest(unittest.TestCase):\n"
                "    def test_fail(self):\n"
                "        sys.stdout.buffer.write(b'\\xff\\xfeinvalid-utf8\\n')\n"
                "        sys.stdout.flush()\n"
                "        self.fail('boom')\n",
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError) as ctx:
                init_manifest(fail_root, run_dry_check=True)
            self.assertFalse((fail_root / ".agents").exists())
            self.assertIn("boom", str(ctx.exception))

            # Existing valid .agents/loop.json must remain untouched if dry-run fails
            existing_agents = fail_root / ".agents"
            existing_agents.mkdir()
            existing_manifest = existing_agents / "loop.json"
            original_content = json.dumps({"test_cmd": "pytest -q"}, indent=2) + "\n"
            existing_manifest.write_text(original_content, encoding="utf-8")

            with self.assertRaises(RuntimeError):
                init_manifest(fail_root, run_dry_check=True)
            self.assertEqual(
                existing_manifest.read_text(encoding="utf-8"), original_content
            )

            # OSError from subprocess execution must surface as RuntimeError
            with mock.patch(
                "core.manifest.subprocess.run", side_effect=OSError("spawn failed")
            ):
                with self.assertRaises(RuntimeError) as os_err_ctx:
                    init_manifest(ok_root, run_dry_check=True)
                self.assertIn("spawn failed", str(os_err_ctx.exception))


if __name__ == "__main__":
    unittest.main()
