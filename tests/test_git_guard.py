"""Unit tests for `core.git_guard`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.git_guard import (  # noqa: E402
    build_worktree_env,
    capture_worktree_diff,
    check_protected_tests_diff,
    checkpoint_commit,
    create_isolated_worktree,
    ensure_clean_repo,
    remove_isolated_worktree,
    rollback_to_ref,
    verify_new_tests_fail_without_prod_changes,
)


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


def _init_sample_repo(repo_root: Path) -> str:
    _run_git(repo_root, "init")
    _run_git(repo_root, "config", "user.email", "test@example.com")
    _run_git(repo_root, "config", "user.name", "Test User")

    (repo_root / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    tests_dir = repo_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "import unittest\n"
        "from calc import add\n\n\n"
        "class CalcTests(unittest.TestCase):\n"
        "    def test_add_positive(self) -> None:\n"
        "        self.assertEqual(add(1, 2), 3)\n\n"
        "    def test_add_zero(self) -> None:\n"
        "        self.assertEqual(add(0, 0), 0)\n",
        encoding="utf-8",
    )
    _run_git(repo_root, "add", "-A")
    _run_git(repo_root, "commit", "-m", "initial commit")
    return _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()


class GitGuardTests(unittest.TestCase):
    """Tests for git worktree isolation, test diff guard, and RED verifier."""

    def test_ensure_clean_repo_and_create_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            not_a_repo = root / "not_repo"
            not_a_repo.mkdir()
            with self.assertRaises(RuntimeError):
                ensure_clean_repo(not_a_repo)

            repo = root / "repo"
            repo.mkdir()
            _init_sample_repo(repo)
            ensure_clean_repo(repo)

            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_clean_repo(repo)
            with self.assertRaises(RuntimeError):
                create_isolated_worktree(repo, "run-1")
            (repo / "dirty.txt").unlink()

            for bad_id in ("../escape", "a/b", "-flag", "bad id", "..", "."):
                with self.assertRaises(ValueError):
                    create_isolated_worktree(repo, bad_id)

            worktree = create_isolated_worktree(repo, "run-1")
            try:
                expected_path = repo / ".agents" / "worktrees" / "run-1"
                self.assertEqual(worktree.resolve(), expected_path.resolve())
                self.assertTrue(worktree.is_dir())
                branch = _run_git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                self.assertEqual(branch, "agy-loop/run-1")
                ensure_clean_repo(worktree)
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )

    def test_rejects_deleted_or_weakened_existing_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            protected = ("tests/**", "**/*.test.*")
            test_file = repo / "tests" / "test_calc.py"
            original_content = test_file.read_text(encoding="utf-8")

            # Deleting protected test file must be rejected.
            test_file.unlink()
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertTrue(reason)

            # Removing an assertion from existing test file must be rejected.
            test_file.write_text(
                "import unittest\n"
                "from calc import add\n\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add_positive(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n",
                encoding="utf-8",
            )
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertTrue(reason)

            # Commenting out an assertion must also be rejected.
            test_file.write_text(
                original_content.replace(
                    "        self.assertEqual(add(0, 0), 0)",
                    "        # self.assertEqual(add(0, 0), 0)",
                ),
                encoding="utf-8",
            )
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertTrue(reason)

            # Adding assertions to existing test file must be accepted.
            test_file.write_text(
                original_content
                + "\n    def test_add_negative(self) -> None:\n"
                "        self.assertEqual(add(-1, 1), 0)\n",
                encoding="utf-8",
            )
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertEqual((ok, reason), (True, "OK"))

            # Root-level file matching `**/*.test.*` must also be protected.
            js_test = repo / "calc.test.js"
            js_test.write_text("expect(1).toBe(1);\n", encoding="utf-8")
            _run_git(repo, "add", "calc.test.js")
            _run_git(repo, "commit", "-m", "add js test")
            js_base = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
            js_test.unlink()
            ok, reason = check_protected_tests_diff(repo, js_base, protected)
            self.assertFalse(ok)
            self.assertIn("calc.test.js", reason)

    def test_verify_new_tests_fail_without_prod_changes_rejects_tautological_and_accepts_real_red(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            protected = ("tests/**", "**/*.test.*")
            test_cmd = f'"{sys.executable}" -m unittest discover -s tests'

            calc_path = repo / "calc.py"
            test_path = repo / "tests" / "test_calc.py"
            orig_calc = calc_path.read_text(encoding="utf-8")
            orig_test = test_path.read_text(encoding="utf-8")

            # 1. Pure refactor (no test changes) -> skipped/accepted.
            refactored_calc = (
                "def add(a: int, b: int) -> int:\n"
                "    total = a + b\n"
                "    return total\n"
            )
            calc_path.write_text(refactored_calc, encoding="utf-8")
            ok, _ = verify_new_tests_fail_without_prod_changes(
                repo, base_ref, protected, test_cmd
            )
            self.assertTrue(ok)
            self.assertEqual(calc_path.read_text(encoding="utf-8"), refactored_calc)

            # 2a. Tautological test with NO prod changes (`prod_files == []`) -> rejected.
            calc_path.write_text(orig_calc, encoding="utf-8")
            test_path.write_text(
                orig_test
                + "\n    def test_tautology_no_prod(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            ok, reason = verify_new_tests_fail_without_prod_changes(
                repo, base_ref, protected, test_cmd
            )
            self.assertFalse(ok)
            self.assertIn("without any production change", reason)

            # 2b. Tautological test + real prod change -> rejected and calc.py restored identically.
            calc_bytes = (
                b"def add(a: int, b: int) -> int:\r\n"
                b"    return a + b\r\n"
                b"\r\n"
                b"def helper() -> int:\r\n"
                b"    return 42\r\n"
            )
            calc_path.write_bytes(calc_bytes)
            test_path.write_text(
                orig_test
                + "\n    def test_tautology(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            ok, reason = verify_new_tests_fail_without_prod_changes(
                repo, base_ref, protected, test_cmd
            )
            self.assertFalse(ok)
            self.assertIn("Tautological", reason)
            self.assertEqual(calc_path.read_bytes(), calc_bytes)

            # 3. Real test calling newly added staged sub(a, b) in calc.py -> accepted,
            #    calc.py restored, and Git index staging preserved.
            new_calc = (
                orig_calc
                + "\n\ndef sub(a: int, b: int) -> int:\n"
                "    return a - b\n"
            )
            calc_path.write_text(new_calc, encoding="utf-8")
            _run_git(repo, "add", "calc.py")
            test_path.write_text(
                "import unittest\n"
                "from calc import add, sub\n\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add_positive(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n\n"
                "    def test_add_zero(self) -> None:\n"
                "        self.assertEqual(add(0, 0), 0)\n\n"
                "    def test_sub(self) -> None:\n"
                "        self.assertEqual(sub(5, 2), 3)\n",
                encoding="utf-8",
            )
            ok, _ = verify_new_tests_fail_without_prod_changes(
                repo, base_ref, protected, test_cmd
            )
            self.assertTrue(ok)
            self.assertEqual(calc_path.read_text(encoding="utf-8"), new_calc)
            staged_files = _run_git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
            self.assertEqual(staged_files, ["calc.py"])
            _run_git(repo, "reset", "HEAD", "--", "calc.py")

            # 4. Brand-new untracked prod module + brand-new test file -> accepted and restored.
            calc_path.write_text(orig_calc, encoding="utf-8")
            test_path.write_text(orig_test, encoding="utf-8")
            new_mod = repo / "new_mod.py"
            new_mod_content = "def mul(a: int, b: int) -> int:\n    return a * b\n"
            new_mod.write_text(new_mod_content, encoding="utf-8")
            new_test = repo / "tests" / "test_new_mod.py"
            new_test.write_text(
                "import unittest\n"
                "from new_mod import mul\n\n\n"
                "class NewModTests(unittest.TestCase):\n"
                "    def test_mul(self) -> None:\n"
                "        self.assertEqual(mul(3, 4), 12)\n",
                encoding="utf-8",
            )
            ok, _ = verify_new_tests_fail_without_prod_changes(
                repo, base_ref, protected, test_cmd
            )
            self.assertTrue(ok)
            self.assertTrue(new_mod.exists())
            self.assertEqual(new_mod.read_text(encoding="utf-8"), new_mod_content)

    def test_checkpoint_commit_and_rollback_to_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_sample_repo(repo)

            with self.assertRaises(RuntimeError):
                checkpoint_commit(repo, "empty commit should fail")

            calc_path = repo / "calc.py"
            calc_path.write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n# checkpoint\n",
                encoding="utf-8",
            )
            sha = checkpoint_commit(repo, "feat: add checkpoint comment")
            head_sha = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(sha, head_sha)
            ensure_clean_repo(repo)

            calc_path.write_text("broken\n", encoding="utf-8")
            untracked = repo / "temp_extra" / "junk.txt"
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked.write_text("junk\n", encoding="utf-8")

            rollback_to_ref(repo, sha)
            ensure_clean_repo(repo)
            self.assertEqual(_run_git(repo, "rev-parse", "HEAD").stdout.strip(), sha)
            self.assertFalse(untracked.exists())
            self.assertIn("# checkpoint", calc_path.read_text(encoding="utf-8"))

    def test_diff_parser_resists_in_hunk_headers_gitconfig_and_spaced_paths(self) -> None:
        from core.git_guard import _has_added_test_lines, _iter_diff_hunk_lines  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_sample_repo(repo)
            # Simulate hostile ~/.gitconfig setting diff.noprefix=true
            _run_git(repo, "config", "diff.noprefix", "true")

            # (a) Commit a test with a `-- assert` line and another with assertions
            tricky_js = repo / "tests" / "tricky.test.js"
            tricky_js.write_text(
                "const x = 1;\n"
                "-- assert(x === 1);\n"
                "expect(x).toBe(1);\n",
                encoding="utf-8",
            )
            spaced_py = repo / "tests" / "test with space.py"
            spaced_py.write_text(
                "import unittest\n"
                "from calc import add\n\n\n"
                "class SpacedTests(unittest.TestCase):\n"
                "    def test_one(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n\n"
                "    def test_two(self) -> None:\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "add tricky and spaced tests")
            base_ref = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

            protected = ("tests/*.py", "**/*.test.js")

            # 1. Attempting to hijack current_file with an added `++ b/prod.py` line in hunk 1
            #    before deleting an assertion in hunk 2 of tests/test_calc.py must be caught!
            test_calc = repo / "tests" / "test_calc.py"
            orig_test_calc = test_calc.read_text(encoding="utf-8")
            test_calc.write_text(
                orig_test_calc.replace(
                    "import unittest\n",
                    "import unittest\n++ b/prod.py\n",
                ).replace(
                    "        self.assertEqual(add(0, 0), 0)",
                    "        pass",
                ),
                encoding="utf-8",
            )
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertIn("tests/test_calc.py", reason)
            test_calc.write_text(orig_test_calc, encoding="utf-8")

            # 2. Removing a line starting with `-- ` (renders as `--- ...` in diff)
            #    must still be yielded by `_iter_diff_hunk_lines` (not skipped as a file header),
            #    and must not break hunk tracking for subsequent assertion removals!
            orig_tricky = tricky_js.read_text(encoding="utf-8")
            tricky_js.write_text(
                "const x = 1;\n"
                "expect(x).toBe(1);\n",
                encoding="utf-8",
            )
            tricky_diff = capture_worktree_diff(repo, base_ref)
            self.assertIn(
                ("tests/tricky.test.js", "--- assert(x === 1);"),
                list(_iter_diff_hunk_lines(tricky_diff)),
            )
            tricky_js.write_text("const x = 1;\n", encoding="utf-8")
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertIn("tests/tricky.test.js", reason)
            tricky_js.write_text(orig_tricky, encoding="utf-8")

            # 3. Adding `++ b/tests/fake.py` inside a non-protected file must not fool _has_added_test_lines
            fake_prod_diff = (
                "diff --git a/calc.py b/calc.py\n"
                "--- a/calc.py\n"
                "+++ b/calc.py\n"
                "@@ -2,0 +3,2 @@\n"
                "+++ b/tests/fake.py\n"
                "+x = 1\n"
            )
            self.assertFalse(_has_added_test_lines(fake_prod_diff, protected))

            # 4. Weakening a protected test file whose path contains spaces must be caught
            #    even when git emits a trailing `\t` on `+++ b/tests/test with space.py\t`
            spaced_py.write_text(
                "import unittest\n"
                "from calc import add\n\n\n"
                "class SpacedTests(unittest.TestCase):\n"
                "    def test_one(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n",
                encoding="utf-8",
            )
            ok, reason = check_protected_tests_diff(repo, base_ref, protected)
            self.assertFalse(ok)
            self.assertIn("tests/test with space.py", reason)

    def test_capture_worktree_diff_includes_tracked_and_untracked_and_raises_on_git_error(
        self,
    ) -> None:
        from core.git_guard import capture_worktree_diff  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            _run_git(repo, "config", "diff.noprefix", "true")

            (repo / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b + 0\n",
                encoding="utf-8",
            )
            (repo / "brand_new.py").write_text("VALUE = 42\n", encoding="utf-8")

            diff_text = capture_worktree_diff(repo, base_ref)
            self.assertIn("+++ b/calc.py", diff_text)
            self.assertIn("+++ b/brand_new.py", diff_text)
            self.assertIn("+VALUE = 42", diff_text)

            with self.assertRaises(RuntimeError):
                capture_worktree_diff(repo, "nonexistent-ref-deadbeef")

    def test_build_worktree_env_hydrates_venv_and_node_modules_and_remove_worktree_cleans_up(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_sample_repo(repo)

            venv_scripts = repo / ".venv" / "Scripts"
            venv_bin = repo / ".venv" / "bin"
            fallback_venv_bin = repo / "venv" / "bin"
            node_bin = repo / "node_modules" / ".bin"
            for d in (venv_scripts, venv_bin, fallback_venv_bin, node_bin):
                d.mkdir(parents=True, exist_ok=True)

            env = build_worktree_env(
                repo,
                base_env={"Path": "C:\\Base\\Bin", "NODE_PATH": "C:\\Base\\Node"},
            )
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(env["VIRTUAL_ENV"], str((repo / ".venv").resolve()))
            self.assertEqual(
                env["NODE_PATH"],
                f"{(repo / 'node_modules').resolve()}{os.pathsep}C:\\Base\\Node",
            )
            self.assertIn("Path", env)
            self.assertNotIn("PATH", env)
            path_parts = env["Path"].split(os.pathsep)
            self.assertEqual(
                path_parts,
                [
                    str(venv_scripts.resolve()),
                    str(venv_bin.resolve()),
                    str(node_bin.resolve()),
                    "C:\\Base\\Bin",
                ],
            )
            self.assertNotIn(str(fallback_venv_bin.resolve()), path_parts)

            clean_env = build_worktree_env(repo, base_env={})
            self.assertEqual(clean_env["NODE_PATH"], str((repo / "node_modules").resolve()))
            self.assertIn(str(node_bin.resolve()), clean_env["PATH"].split(os.pathsep))

            for bad_id in ("../escape", "a/b", "-flag", "bad id", "", "..", "."):
                with self.assertRaises(ValueError):
                    remove_isolated_worktree(repo, repo / ".agents" / "worktrees" / "x", bad_id)

            wt = create_isolated_worktree(repo, "cleanup-1")
            self.assertTrue(wt.is_dir())
            remove_isolated_worktree(repo, wt, "cleanup-1")
            self.assertFalse(wt.exists())
            branch_ref = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/agy-loop/cleanup-1"],
                cwd=repo,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(branch_ref.returncode, 0)
            # Already-deleted worktree and branch should be tolerated cleanly.
            remove_isolated_worktree(repo, wt, "cleanup-1")

    def test_remove_isolated_worktree_prunes_manually_deleted_dir_and_raises_on_stuck_cleanup(
        self,
    ) -> None:
        from core import git_guard  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_sample_repo(repo)

            # 1. Manual directory deletion: git worktree prune unblocks branch -D.
            wt_manual = create_isolated_worktree(repo, "manual-del")
            shutil.rmtree(wt_manual)
            self.assertFalse(wt_manual.exists())
            remove_isolated_worktree(repo, wt_manual, "manual-del")
            branch_ref = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/agy-loop/manual-del"],
                cwd=repo,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(branch_ref.returncode, 0)

            # 2. Failed worktree remove while directory still exists raises RuntimeError.
            wt_stuck = create_isolated_worktree(repo, "stuck-wt")
            real_run_git = git_guard._run_git
            try:
                def _fail_worktree_remove(
                    cwd: Path, *args: str
                ) -> subprocess.CompletedProcess[str]:
                    if args[:2] == ("worktree", "remove"):
                        return subprocess.CompletedProcess(
                            ["git", *args], 1, "", "worktree locked"
                        )
                    return real_run_git(cwd, *args)

                with mock.patch.object(
                    git_guard, "_run_git", side_effect=_fail_worktree_remove
                ):
                    with self.assertRaisesRegex(RuntimeError, "worktree locked"):
                        remove_isolated_worktree(repo, wt_stuck, "stuck-wt")
            finally:
                remove_isolated_worktree(repo, wt_stuck, "stuck-wt")

            # 3. Failed branch -D while branch still exists raises RuntimeError.
            wt_branch = create_isolated_worktree(repo, "stuck-branch")
            try:
                def _fail_branch_delete(
                    cwd: Path, *args: str
                ) -> subprocess.CompletedProcess[str]:
                    if args[:2] == ("branch", "-D"):
                        return subprocess.CompletedProcess(
                            ["git", *args], 1, "", "branch locked"
                        )
                    return real_run_git(cwd, *args)

                with mock.patch.object(
                    git_guard, "_run_git", side_effect=_fail_branch_delete
                ):
                    with self.assertRaisesRegex(RuntimeError, "branch locked"):
                        remove_isolated_worktree(repo, wt_branch, "stuck-branch")
            finally:
                remove_isolated_worktree(repo, wt_branch, "stuck-branch")

    def test_multilang_assertions_mnemonic_prefix_and_comment_styles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_sample_repo(repo)
            _run_git(repo, "config", "diff.mnemonicPrefix", "true")

            files = {
                "tests/math_test.go": (
                    "package math\n"
                    'import "testing"\n'
                    "func TestAdd(t *testing.T) {\n"
                    '    t.Fatalf("expected 3")\n'
                    "}\n"
                ),
                "tests/lib_test.rs": (
                    "#[test]\n"
                    "fn test_add() {\n"
                    "    assert_eq!(1 + 2, 3);\n"
                    "}\n"
                ),
                "tests/CalcTest.java": (
                    "class CalcTest {\n"
                    "    void testAdd() {\n"
                    "        Assert.assertEquals(3, 1 + 2);\n"
                    "    }\n"
                    "}\n"
                ),
                "tests/CalcTests.cs": (
                    "class CalcTests {\n"
                    "    void TestAdd() {\n"
                    "        result.Should().Be(3);\n"
                    "    }\n"
                    "}\n"
                ),
                "tests/custom.test.sql": "CALL check_eq(1, 1);\n",
            }
            for rel, content in files.items():
                (repo / rel).write_text(content, encoding="utf-8")
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "add multilang tests")
            base_ref = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

            protected = (
                "tests/**",
                "**/*_test.go",
                "**/*_test.rs",
                "**/*Test.java",
                "**/*Tests.cs",
                "**/*.test.*",
            )

            # Removing assertions in Go, Rust, Java, and C# must be blocked under mnemonicPrefix=true.
            for rel, replacement in (
                ("tests/math_test.go", "package math\nfunc TestAdd() {}\n"),
                ("tests/lib_test.rs", "#[test]\nfn test_add() {}\n"),
                ("tests/CalcTest.java", "class CalcTest {\n    void testAdd() {}\n}\n"),
                ("tests/CalcTests.cs", "class CalcTests {\n    void TestAdd() {}\n}\n"),
            ):
                target = repo / rel
                original = files[rel]
                target.write_text(replacement, encoding="utf-8")
                ok, reason = check_protected_tests_diff(repo, base_ref, protected)
                self.assertFalse(ok, msg=f"Expected weakening block for {rel}")
                self.assertIn(rel, reason)
                target.write_text(original, encoding="utf-8")

            # Commenting out an assertion with `--` or `/* ... */` must be blocked.
            java_file = repo / "tests" / "CalcTest.java"
            java_orig = files["tests/CalcTest.java"]
            for commented in (
                java_orig.replace(
                    "        Assert.assertEquals(3, 1 + 2);",
                    "        -- Assert.assertEquals(3, 1 + 2);",
                ),
                java_orig.replace(
                    "        Assert.assertEquals(3, 1 + 2);",
                    "        /* Assert.assertEquals(3, 1 + 2); */",
                ),
            ):
                java_file.write_text(commented, encoding="utf-8")
                ok, reason = check_protected_tests_diff(repo, base_ref, protected)
                self.assertFalse(ok)
                self.assertIn("tests/CalcTest.java", reason)
            java_file.write_text(java_orig, encoding="utf-8")

            # Custom assert_regex catches domain-specific assertion removal.
            sql_file = repo / "tests" / "custom.test.sql"
            sql_file.write_text("-- removed\n", encoding="utf-8")
            ok_default, _ = check_protected_tests_diff(repo, base_ref, protected)
            self.assertTrue(ok_default)
            ok_custom, reason_custom = check_protected_tests_diff(
                repo, base_ref, protected, assert_regex=r"\bcheck_eq\("
            )
            self.assertFalse(ok_custom)
            self.assertIn("tests/custom.test.sql", reason_custom)

            with self.assertRaises(ValueError):
                check_protected_tests_diff(
                    repo, base_ref, protected, assert_regex="[invalid"
                )


if __name__ == "__main__":
    unittest.main()
