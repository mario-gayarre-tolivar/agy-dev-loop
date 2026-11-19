"""Unit tests for `core.git_guard`."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.git_guard import (  # noqa: E402
    check_protected_tests_diff,
    checkpoint_commit,
    create_isolated_worktree,
    ensure_clean_repo,
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


if __name__ == "__main__":
    unittest.main()
