"""Binary generalization evaluation suite covering criteria C1-C6."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "agy-dev-loop"
SCRIPTS_DIR = SKILL_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cli  # noqa: E402
from core.eval_runner import run_deterministic_gate  # noqa: E402
from core.git_guard import check_protected_tests_diff  # noqa: E402
from core.manifest import detect_stack_manifest, load_manifest, resolve_python_cmd  # noqa: E402
from core.optimizer_guard import run_offline_benchmarks  # noqa: E402
from core.workflow_logic import (  # noqa: E402
    DEFAULT_BENCHMARKS_ROOT,
    EXECUTOR_SCHEMA,
    REVIEWER_SCHEMA,
    run_inner_loop,
)

_TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".mjs",
    ".js",
    ".ts",
    ".txt",
    ".yml",
    ".yaml",
    ".toml",
}


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc


def _init_git_repo(repo: Path) -> None:
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "eval@example.com")
    _run_git(repo, "config", "user.name", "Eval User")


def _write_exec_shim(bin_dir: Path, name: str, output_marker: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        target = bin_dir / f"{name}.cmd"
        target.write_text(f"@echo off\r\necho {output_marker}\r\n", encoding="utf-8")
        return target
    target = bin_dir / name
    target.write_text(f"#!/bin/sh\necho {output_marker}\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


class GeneralizationEvalTests(unittest.TestCase):
    """Evaluation suite for generalization criteria C1-C6."""

    def test_c1_multi_os_python_resolution_and_zero_personal_paths(self) -> None:
        # 1. Fallback to `python` when `python3` is missing
        with (
            mock.patch("core.manifest.shutil.which", side_effect=lambda cmd: "/usr/bin/python" if cmd == "python" else None),
            mock.patch("core.manifest.subprocess.run", return_value=subprocess.CompletedProcess(["python", "--version"], 0)),
        ):
            self.assertEqual(resolve_python_cmd(), "python")

        # 2. Fallback to sys.executable when neither `python3` nor `python` is in PATH
        with mock.patch("core.manifest.shutil.which", return_value=None):
            resolved = resolve_python_cmd()
            expected = f'"{sys.executable}"' if " " in sys.executable else sys.executable
            self.assertEqual(resolved, expected)

        # 3. Zero personal paths in skills/ text files and README.md
        targets = [REPO_ROOT / "README.md"]
        for path in (REPO_ROOT / "skills").rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            targets.append(path)

        offenders: list[str] = []
        for path in targets:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if "perrotruco" in text or "c:\\users\\" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f"Found personal paths in: {offenders}")

    def test_c2_worktree_dependency_hydration_and_clean_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_git_repo(repo)

            (repo / ".gitignore").write_text(
                ".agents/worktrees/\n.agents/loop/\n.venv/\nnode_modules/\n__pycache__/\n",
                encoding="utf-8",
            )
            venv_bin = repo / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            _write_exec_shim(venv_bin, "fake_venv_tool", "VENV_OK")

            node_bin = repo / "node_modules" / ".bin"
            _write_exec_shim(node_bin, "fake-js-tool", "NODEBIN_OK")
            pkg_dir = repo / "node_modules" / "fake-pkg"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "index.js").write_text("module.exports = { ok: 42 };\n", encoding="utf-8")

            check_script = (
                "import os, shutil, subprocess, sys\n"
                "from pathlib import Path\n"
                "Path('.dry_run_cache.tmp').write_text('cached', encoding='utf-8')\n"
                "assert os.environ.get('VIRTUAL_ENV', '').endswith('.venv'), os.environ.get('VIRTUAL_ENV')\n"
                "assert 'node_modules' in os.environ.get('NODE_PATH', ''), os.environ.get('NODE_PATH')\n"
                "vt = shutil.which('fake_venv_tool')\n"
                "jt = shutil.which('fake-js-tool')\n"
                "assert vt and '.venv' in vt, vt\n"
                "assert jt and 'node_modules' in jt, jt\n"
                "assert subprocess.run([vt], check=True, capture_output=True, text=True).stdout.strip() == 'VENV_OK'\n"
                "assert subprocess.run([jt], check=True, capture_output=True, text=True).stdout.strip() == 'NODEBIN_OK'\n"
                "subprocess.run(['node', '-e', \"const p = require('fake-pkg'); if (p.ok !== 42) process.exit(1)\"], check=True)\n"
                "from app import val\n"
                "assert val() == 1\n"
            )
            (repo / "verify_env.py").write_text(check_script, encoding="utf-8")
            (repo / "app.py").write_text("def val() -> int:\n    return 1\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "tests" / "test_app.py").write_text(
                "import unittest\nfrom app import val\n\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_val(self) -> None:\n"
                "        self.assertEqual(val(), 1)\n",
                encoding="utf-8",
            )
            manifest = {
                "test_cmd": f'"{sys.executable}" verify_env.py && "{sys.executable}" -m unittest discover -s tests -v',
                "lint_cmd": None,
                "build_cmd": None,
                "max_iter": 3,
                "circuit_breaker": 3,
            }
            (repo / ".agents").mkdir(parents=True, exist_ok=True)
            (repo / ".agents" / "loop.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            plan_path = repo / "plan.md"
            plan_path.write_text("# Plan\n\n- [ ] **Step 1: Add val2**\n  Add `val2()` returning `2`.\n", encoding="utf-8")

            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "initial")

            run_id = "c2-hydration"
            wt_path = repo / ".agents" / "worktrees" / run_id

            async def fake_agent(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                if schema is EXECUTOR_SCHEMA:
                    self.assertFalse((wt_path / ".dry_run_cache.tmp").exists())
                    (wt_path / "app.py").write_text(
                        "def val() -> int:\n    return 1\n\ndef val2() -> int:\n    return 2\n",
                        encoding="utf-8",
                    )
                    (wt_path / "tests" / "test_app.py").write_text(
                        "import unittest\nfrom app import val, val2\n\n\n"
                        "class AppTests(unittest.TestCase):\n"
                        "    def test_val(self) -> None:\n"
                        "        self.assertEqual(val(), 1)\n\n"
                        "    def test_val2(self) -> None:\n"
                        "        self.assertEqual(val2(), 2)\n",
                        encoding="utf-8",
                    )
                    return {
                        "subtask_index": 0,
                        "files_changed": ["app.py", "tests/test_app.py"],
                        "test_files_touched": ["tests/test_app.py"],
                        "summary": "added val2",
                    }
                self.assertIs(schema, REVIEWER_SCHEMA)
                return {"approved": True, "blocking_issues": [], "lesson_candidate": None}

            try:
                result = asyncio.run(
                    run_inner_loop(
                        {"repo_root": str(repo), "plan_path": str(plan_path), "run_id": run_id},
                        fake_agent,
                        lambda _p: None,
                        lambda *_a: None,
                    )
                )
                self.assertEqual(result["status"], "completed")
                self.assertFalse((repo / ".dry_run_cache.tmp").exists())
                self.assertEqual(_run_git(repo, "status", "--porcelain").stdout.strip(), "")
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=repo,
                    capture_output=True,
                    check=False,
                )

    def test_c3_multistack_detection_and_multilang_git_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            py_pytest = root / "py_pytest"
            py_pytest.mkdir()
            (py_pytest / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            self.assertIn("-m pytest", detect_stack_manifest(py_pytest).test_cmd)

            py_unit = root / "py_unit"
            (py_unit / "tests").mkdir(parents=True)
            (py_unit / "tests" / "test_one.py").write_text("import unittest\n", encoding="utf-8")
            self.assertIn("-m unittest", detect_stack_manifest(py_unit).test_cmd)

            js_pnpm = root / "js_pnpm"
            js_pnpm.mkdir()
            (js_pnpm / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (js_pnpm / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
            )
            self.assertEqual(detect_stack_manifest(js_pnpm).test_cmd, "pnpm test")

            go_repo = root / "go_repo"
            go_repo.mkdir()
            (go_repo / "go.mod").write_text("module example.com/m\n", encoding="utf-8")
            go_manifest = detect_stack_manifest(go_repo)
            self.assertEqual(go_manifest.test_cmd, "go test ./...")
            self.assertEqual(go_manifest.build_cmd, "go build ./...")

            rust_repo = root / "rust_repo"
            rust_repo.mkdir()
            (rust_repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
            rust_manifest = detect_stack_manifest(rust_repo)
            self.assertEqual(rust_manifest.test_cmd, "cargo test")
            self.assertEqual(rust_manifest.build_cmd, "cargo check")

            java_repo = root / "java_repo"
            java_repo.mkdir()
            (java_repo / "pom.xml").write_text("<project/>\n", encoding="utf-8")
            java_manifest = detect_stack_manifest(java_repo)
            self.assertIn("test", java_manifest.test_cmd)

            cs_repo = root / "cs_repo"
            cs_repo.mkdir()
            (cs_repo / "App.csproj").write_text("<Project/>\n", encoding="utf-8")
            self.assertEqual(detect_stack_manifest(cs_repo).test_cmd, "dotnet test")

        # Multi-language assertion-weakening checks
        samples = [
            ("tests/test_sample.py", "def test_x():\n    assert 1 == 1\n", "def test_x():\n    pass\n"),
            ("sample.test.js", "test('x', () => {\n  expect(1).toBe(1);\n});\n", "test('x', () => {});\n"),
            ("sample_test.go", "func TestX(t *testing.T) {\n\tt.Fatalf(\"bad\")\n}\n", "func TestX(t *testing.T) {}\n"),
            ("tests/sample_test.rs", "#[test]\nfn t() {\n    assert_eq!(1, 1);\n}\n", "#[test]\nfn t() {}\n"),
            ("SampleTests.cs", "public void T() {\n    Assert.Equal(1, 1);\n}\n", "public void T() {}\n"),
        ]
        globs = ("tests/**", "*.test.*", "*_test.go", "*Tests.cs")
        for rel_path, original, weakened in samples:
            with self.subTest(rel_path=rel_path), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                repo.mkdir()
                _init_git_repo(repo)
                target = repo / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original, encoding="utf-8")
                _run_git(repo, "add", "-A")
                _run_git(repo, "commit", "-m", "init")
                target.write_text(weakened, encoding="utf-8")
                ok, reason = check_protected_tests_diff(repo, "HEAD", globs)
                self.assertFalse(ok, f"Expected weakening catch for {rel_path}: {reason}")

    def test_c4_git_mnemonic_prefix_and_shallow_import_immunity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            _run_git(repo, "config", "diff.mnemonicPrefix", "true")
            test_file = repo / "tests" / "test_m.py"
            test_file.parent.mkdir()
            test_file.write_text("def test_ok():\n    assert 2 == 2\n", encoding="utf-8")
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "init")

            test_file.write_text("def test_ok():\n    pass\n", encoding="utf-8")
            ok, reason = check_protected_tests_diff(repo, "HEAD", ("tests/**",))
            self.assertFalse(ok, reason)

        # Shallow copy import immunity
        with tempfile.TemporaryDirectory() as tmp:
            shallow_skill = Path(tmp) / "s"
            shutil.copytree(SKILL_DIR, shallow_skill)
            probe = (
                "import sys; "
                f"sys.path.insert(0, {str((shallow_skill / 'scripts').resolve())!r}); "
                "from core import workflow_logic; "
                "print(str(workflow_logic.SKILL_ROOT)); "
                "print(str(workflow_logic.DEFAULT_BENCHMARKS_ROOT)); "
                "assert workflow_logic.DEFAULT_BENCHMARKS_ROOT == workflow_logic.SKILL_ROOT / 'benchmarks'; "
                "assert (workflow_logic.DEFAULT_BENCHMARKS_ROOT / 'py_fixture').is_dir(); "
                "assert (workflow_logic.DEFAULT_BENCHMARKS_ROOT / 'js_fixture').is_dir()"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=tmp,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_c5_declarative_verify_red_false_and_timeout_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            (repo / "config.json").write_text('{"env": "prod"}\n', encoding="utf-8")
            (repo / "validate.py").write_text(
                "import json\n"
                "from pathlib import Path\n"
                "data = json.loads(Path('config.json').read_text(encoding='utf-8'))\n"
                "assert data['env'] in {'dev', 'prod'}\n",
                encoding="utf-8",
            )
            (repo / ".agents").mkdir()
            (repo / ".agents" / "loop.json").write_text(
                json.dumps(
                    {
                        "test_cmd": f'"{sys.executable}" validate.py',
                        "verify_red": False,
                        "test_timeout_sec": 45,
                        "assert_regex": r"CHECK_ASSERT\(",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "init")

            manifest = load_manifest(repo)
            self.assertFalse(manifest.verify_red)
            self.assertEqual(manifest.test_timeout_sec, 45)
            self.assertEqual(manifest.assert_regex, r"CHECK_ASSERT\(")

            (repo / "config.json").write_text('{"env": "dev"}\n', encoding="utf-8")
            gate = run_deterministic_gate(repo, manifest, "HEAD")
            self.assertTrue(gate.passed, gate.output)
            self.assertTrue(gate.should_run_reviewer)
            self.assertNotIn("[verify_red]", gate.output)

            # Custom assert_regex enforcement
            (repo / "tests").mkdir()
            (repo / "tests" / "custom.spec").write_text("CHECK_ASSERT(1 == 1)\n", encoding="utf-8")
            _run_git(repo, "add", "tests/custom.spec")
            _run_git(repo, "commit", "-m", "add custom spec")
            (repo / "tests" / "custom.spec").write_text("// removed\n", encoding="utf-8")
            ok, _ = check_protected_tests_diff(
                repo, "HEAD", ("tests/**",), assert_regex=manifest.assert_regex
            )
            self.assertFalse(ok)

    def test_c6_cli_init_doctor_and_self_contained_benchmarks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            (repo / ".gitignore").write_text(
                ".agents/worktrees/\n.agents/loop/\n__pycache__/\n", encoding="utf-8"
            )
            (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "tests" / "test_calc.py").write_text(
                "import unittest\nfrom calc import add\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "initial")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_init = cli.main(["init", "--repo", str(repo)])
            self.assertEqual(rc_init, 0, buf.getvalue())
            init_payload = json.loads(buf.getvalue())
            self.assertEqual(init_payload["status"], "ok")
            self.assertEqual(init_payload["gitignore_hint"], [".agents/worktrees/", ".agents/loop/"])
            self.assertTrue((repo / ".agents" / "loop.json").is_file())

            _run_git(repo, "add", ".agents/loop.json")
            _run_git(repo, "commit", "-m", "add loop manifest")

            doc_buf = io.StringIO()
            with contextlib.redirect_stdout(doc_buf):
                rc_doc = cli.main(["doctor", "--repo", str(repo)])
            self.assertEqual(rc_doc, 0, doc_buf.getvalue())
            doc_payload = json.loads(doc_buf.getvalue())
            self.assertEqual(doc_payload["status"], "ok")
            for check_name in (
                "python_version",
                "git_version",
                "benchmarks_present",
                "clean_repo",
                "manifest_valid",
                "dry_run_ok",
            ):
                self.assertIn(check_name, doc_payload["checks"])
                self.assertTrue(doc_payload["checks"][check_name]["ok"], check_name)

            self.assertEqual(_run_git(repo, "status", "--porcelain").stdout.strip(), "")
            wt_root = repo / ".agents" / "worktrees"
            if wt_root.exists():
                self.assertEqual(list(wt_root.iterdir()), [])

            passed, results = run_offline_benchmarks(DEFAULT_BENCHMARKS_ROOT)
            self.assertTrue(passed, results)
            self.assertEqual(results, {"py_fixture": True, "js_fixture": True})
