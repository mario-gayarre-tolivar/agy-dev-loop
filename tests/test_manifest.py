"""Unit tests for `core.manifest`."""

from __future__ import annotations

import json
import os
import subprocess
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

            # *_test.py triggers pytest (not unittest); non-test_* helpers alone do not trigger any stack
            pytest_named_root = Path(tmp) / "pytest_named"
            (pytest_named_root / "tests").mkdir(parents=True)
            (pytest_named_root / "foo_test.py").write_text("", encoding="utf-8")
            (pytest_named_root / "tests" / "bar_test.py").write_text("", encoding="utf-8")
            (pytest_named_root / "tests" / "testutils.py").write_text("", encoding="utf-8")
            pytest_named_manifest = detect_stack_manifest(pytest_named_root)
            self.assertTrue(pytest_named_manifest.test_cmd.endswith(" -m pytest"))
            self.assertNotIn("unittest", pytest_named_manifest.test_cmd)

            helper_only_root = Path(tmp) / "helper_only"
            (helper_only_root / "tests").mkdir(parents=True)
            (helper_only_root / "tests" / "testutils.py").write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                detect_stack_manifest(helper_only_root)

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

    def test_verify_manifest_test_cmd_avoids_pycache_and_raises_on_failure(self) -> None:
        from core.manifest import verify_manifest_test_cmd  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_mod.py").write_text(
                "import unittest\n"
                "import mod\n\n"
                "class ModTest(unittest.TestCase):\n"
                "    def test_val(self) -> None:\n"
                "        self.assertEqual(mod.VALUE, 1)\n",
                encoding="utf-8",
            )
            passing = LoopManifest(
                test_cmd=f'"{sys.executable}" -m unittest discover -s tests -v'
            )
            verify_manifest_test_cmd(root, passing)
            self.assertEqual(list(root.rglob("__pycache__")), [])

            failing = LoopManifest(
                test_cmd=f'"{sys.executable}" -c "import sys; print(\'bad\'); sys.exit(2)"'
            )
            with self.assertRaises(RuntimeError) as ctx:
                verify_manifest_test_cmd(root, failing)
            self.assertIn("bad", str(ctx.exception))

    def test_resolve_python_cmd_falls_back_when_python3_missing_or_broken(self) -> None:
        from core.manifest import resolve_python_cmd  # noqa: PLC0415

        ok_proc = subprocess.CompletedProcess(args=[], returncode=0)
        bad_proc = subprocess.CompletedProcess(args=[], returncode=9009)

        # python3 available and working
        with (
            mock.patch("core.manifest.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            mock.patch("core.manifest.subprocess.run", return_value=ok_proc),
        ):
            self.assertEqual(resolve_python_cmd(), "python3")

        # python3 broken (Windows Store alias exit 9009) -> falls back to python
        def _run_python3_broken(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return bad_proc if cmd[0] == "python3" else ok_proc

        with (
            mock.patch("core.manifest.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            mock.patch("core.manifest.subprocess.run", side_effect=_run_python3_broken),
        ):
            self.assertEqual(resolve_python_cmd(), "python")

        # python3 missing, python raises OSError -> falls back to sys.executable (quoted if spaces)
        with (
            mock.patch("core.manifest.shutil.which", side_effect=lambda name: None if name == "python3" else "/bin/python"),
            mock.patch("core.manifest.subprocess.run", side_effect=OSError("not executable")),
            mock.patch.object(sys, "executable", r"C:\Program Files\Python312\python.exe"),
        ):
            self.assertEqual(resolve_python_cmd(), r'"C:\Program Files\Python312\python.exe"')

        # Fallback without spaces is not quoted
        with (
            mock.patch("core.manifest.shutil.which", return_value=None),
            mock.patch.object(sys, "executable", "/usr/bin/python3.12"),
        ):
            self.assertEqual(resolve_python_cmd(), "/usr/bin/python3.12")

    def test_detect_stack_manifest_supports_pytest_go_rust_java_dotnet_and_lockfiles(
        self,
    ) -> None:
        from core.manifest import DEFAULT_PROTECTED_TEST_GLOBS  # noqa: PLC0415

        for expected_glob in (
            "**/test_*.py",
            "**/*_test.py",
            "**/*_test.go",
            "**/*_test.rs",
            "**/*Test.java",
            "**/*Tests.java",
            "**/*Tests.cs",
            "**/*Test.cs",
        ):
            self.assertIn(expected_glob, DEFAULT_PROTECTED_TEST_GLOBS)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            # pyproject.toml with [tool.pytest
            pyproject_root = base / "py_pyproject"
            pyproject_root.mkdir()
            (pyproject_root / "pyproject.toml").write_text(
                "[project]\nname='demo'\n[tool.pytest.ini_options]\ntestpaths=['tests']\n",
                encoding="utf-8",
            )
            self.assertTrue(detect_stack_manifest(pyproject_root).test_cmd.endswith(" -m pytest"))

            # conftest.py
            conftest_root = base / "py_conftest"
            conftest_root.mkdir()
            (conftest_root / "conftest.py").write_text("", encoding="utf-8")
            self.assertTrue(detect_stack_manifest(conftest_root).test_cmd.endswith(" -m pytest"))

            # *_test.py
            star_test_root = base / "py_star_test"
            star_test_root.mkdir()
            (star_test_root / "app_test.py").write_text("", encoding="utf-8")
            self.assertTrue(detect_stack_manifest(star_test_root).test_cmd.endswith(" -m pytest"))

            # Node lockfiles: pnpm, yarn, bun
            for lockfile, pm in (
                ("pnpm-lock.yaml", "pnpm"),
                ("yarn.lock", "yarn"),
                ("bun.lockb", "bun"),
            ):
                node_root = base / f"node_{pm}"
                node_root.mkdir()
                (node_root / lockfile).write_text("", encoding="utf-8")
                (node_root / "package.json").write_text(
                    json.dumps(
                        {
                            "name": f"app-{pm}",
                            "scripts": {
                                "test": "vitest run",
                                "lint": "eslint .",
                                "build": "vite build",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                m = detect_stack_manifest(node_root)
                self.assertEqual(m.test_cmd, f"{pm} test")
                self.assertEqual(m.lint_cmd, f"{pm} run lint")
                self.assertEqual(m.build_cmd, f"{pm} run build")

            # Go
            go_root = base / "go_app"
            go_root.mkdir()
            (go_root / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
            go_m = detect_stack_manifest(go_root)
            self.assertEqual(go_m.test_cmd, "go test ./...")
            self.assertEqual(go_m.build_cmd, "go build ./...")

            # Rust
            rust_root = base / "rust_app"
            rust_root.mkdir()
            (rust_root / "Cargo.toml").write_text("[package]\nname='app'\n", encoding="utf-8")
            rust_m = detect_stack_manifest(rust_root)
            self.assertEqual(rust_m.test_cmd, "cargo test")
            self.assertEqual(rust_m.build_cmd, "cargo check")

            # Java Maven
            mvn_root = base / "mvn_app"
            mvn_root.mkdir()
            (mvn_root / "pom.xml").write_text("<project/>", encoding="utf-8")
            self.assertEqual(detect_stack_manifest(mvn_root).test_cmd, "mvn test")

            # Java Gradle (Kotlin DSL)
            gradle_root = base / "gradle_app"
            gradle_root.mkdir()
            (gradle_root / "build.gradle.kts").write_text("", encoding="utf-8")
            self.assertEqual(detect_stack_manifest(gradle_root).test_cmd, "gradle test")

            # .NET
            dotnet_root = base / "dotnet_app"
            dotnet_root.mkdir()
            (dotnet_root / "App.csproj").write_text("<Project/>", encoding="utf-8")
            self.assertEqual(detect_stack_manifest(dotnet_root).test_cmd, "dotnet test")

    def test_load_manifest_validates_new_optional_fields_and_rejects_bad_assert_regex(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / ".agents"
            agents_dir.mkdir(parents=True)
            manifest_path = agents_dir / "loop.json"

            manifest_path.write_text(
                json.dumps(
                    {
                        "test_cmd": "pytest",
                        "setup_cmd": "  pip install -e .  ",
                        "verify_red": False,
                        "test_timeout_sec": 300,
                        "assert_regex": "AssertionError|FAIL",
                        "protected_test_globs": [" tests/** ", " **/*_test.py "],
                    }
                ),
                encoding="utf-8",
            )
            manifest = load_manifest(root)
            self.assertEqual(manifest.setup_cmd, "pip install -e .")
            self.assertIs(manifest.verify_red, False)
            self.assertEqual(manifest.test_timeout_sec, 300)
            self.assertEqual(manifest.assert_regex, "AssertionError|FAIL")
            self.assertEqual(manifest.protected_test_globs, ("tests/**", "**/*_test.py"))

            # Defaults when omitted
            manifest_path.write_text(json.dumps({"test_cmd": "pytest"}), encoding="utf-8")
            defaulted = load_manifest(root)
            self.assertIsNone(defaulted.setup_cmd)
            self.assertIs(defaulted.verify_red, True)
            self.assertEqual(defaulted.test_timeout_sec, 120)
            self.assertIsNone(defaulted.assert_regex)

            # Invalid new fields must raise ValueError
            for bad_payload in (
                {"test_cmd": "pytest", "assert_regex": "[unclosed"},
                {"test_cmd": "pytest", "assert_regex": "   "},
                {"test_cmd": "pytest", "verify_red": "false"},
                {"test_cmd": "pytest", "verify_red": 0},
                {"test_cmd": "pytest", "verify_red": None},
                {"test_cmd": "pytest", "test_timeout_sec": 0},
                {"test_cmd": "pytest", "test_timeout_sec": -10},
                {"test_cmd": "pytest", "test_timeout_sec": True},
                {"test_cmd": "pytest", "setup_cmd": ""},
            ):
                manifest_path.write_text(json.dumps(bad_payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(root)

    def test_verify_manifest_test_cmd_passes_custom_env_and_timeout(self) -> None:
        from core.manifest import verify_manifest_test_cmd  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import os, sys; "
                "assert os.environ.get('AGY_CUSTOM_TOKEN') == 'ok-42'; "
                "assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'"
            )
            manifest = LoopManifest(
                test_cmd=f'"{sys.executable}" -c "{script}"',
                test_timeout_sec=300,
            )
            caller_env = {**os.environ, "AGY_CUSTOM_TOKEN": "ok-42"}
            caller_env.pop("PYTHONDONTWRITEBYTECODE", None)
            verify_manifest_test_cmd(root, manifest, env=caller_env)
            self.assertNotIn("PYTHONDONTWRITEBYTECODE", caller_env)

            ok_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with mock.patch("core.manifest.subprocess.run", return_value=ok_proc) as run_mock:
                verify_manifest_test_cmd(root, manifest, env=caller_env)
            self.assertEqual(run_mock.call_args.kwargs["timeout"], 300)
            self.assertEqual(run_mock.call_args.kwargs["env"]["AGY_CUSTOM_TOKEN"], "ok-42")
            self.assertEqual(run_mock.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")


if __name__ == "__main__":
    unittest.main()
