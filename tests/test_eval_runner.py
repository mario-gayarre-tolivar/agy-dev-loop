"""Unit tests for `core.eval_runner`."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.eval_runner import (  # noqa: E402
    GateResult,
    compute_error_fingerprint,
    decide_next_action,
    run_deterministic_gate,
)
from core.manifest import LoopManifest  # noqa: E402

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


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
        "        self.assertEqual(add(1, 2), 3)\n",
        encoding="utf-8",
    )
    _run_git(repo_root, "add", "-A")
    _run_git(repo_root, "commit", "-m", "initial commit")
    return _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()


class EvalRunnerTests(unittest.TestCase):
    """Tests for deterministic gate, error fingerprinting, and escalation rules."""

    def test_short_circuits_reviewer_and_later_stages_when_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            marker = repo / "lint_ran.marker"

            # Add a real failing test + wrong prod change so git_guard and verify_red pass,
            # while the test stage fails.
            (repo / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n"
                "    return a + b\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_calc.py").write_text(
                "import unittest\n"
                "from calc import add, sub\n\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add_positive(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n\n"
                "    def test_sub(self) -> None:\n"
                "        self.assertEqual(sub(5, 2), 3)\n",
                encoding="utf-8",
            )

            lint_cmd = (
                f'"{sys.executable}" -c '
                '"from pathlib import Path; Path(\'lint_ran.marker\').write_text(\'ran\')"'
            )
            manifest = LoopManifest(
                test_cmd=f'"{sys.executable}" -m unittest discover -s tests -v',
                lint_cmd=lint_cmd,
                build_cmd=f'"{sys.executable}" -c "print(\'build ok\')"',
            )

            result = run_deterministic_gate(repo, manifest, base_ref, verify_red=True)

            self.assertIsInstance(result, GateResult)
            self.assertFalse(result.passed)
            self.assertEqual(result.failed_stage, "test")
            self.assertFalse(result.should_run_reviewer)
            self.assertFalse(marker.exists())
            self.assertRegex(result.error_fingerprint, _HEX16_RE)
            self.assertTrue(result.output)

    def test_gate_passes_and_enables_reviewer_when_all_stages_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)

            (repo / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n"
                "    return a - b\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_calc.py").write_text(
                "import unittest\n"
                "from calc import add, sub\n\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add_positive(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n\n"
                "    def test_sub(self) -> None:\n"
                "        self.assertEqual(sub(5, 2), 3)\n",
                encoding="utf-8",
            )

            manifest = LoopManifest(
                test_cmd=f'"{sys.executable}" -m unittest discover -s tests -v',
                lint_cmd=f'"{sys.executable}" -c "print(\'lint ok\')"',
                build_cmd=f'"{sys.executable}" -c "print(\'build ok\')"',
            )

            result = run_deterministic_gate(repo, manifest, base_ref, verify_red=True)

            self.assertTrue(result.passed)
            self.assertIsNone(result.failed_stage)
            self.assertTrue(result.should_run_reviewer)
            self.assertEqual(result.error_fingerprint, "")
            self.assertIn("lint ok", result.output)
            self.assertIn("build ok", result.output)

    def test_compute_error_fingerprint_ignores_timing_noise_and_detects_diff_change(
        self,
    ) -> None:
        out_fast = (
            "2026-11-19T19:00:01Z ERROR\n"
            "File C:\\Users\\Alice\\AppData\\Local\\Temp\\tmp1111\\calc.py, line 4\n"
            "FAIL: test_sub (tests.test_calc.CalcTests.test_sub)\n"
            "AssertionError: 7 != 3   \n"
            "Ran 2 tests in 0.002s\n"
        )
        out_slow = (
            "2026-11-19T19:05:44Z ERROR\n"
            "File C:\\Users\\Alice\\AppData\\Local\\Temp\\tmp9999\\calc.py, line 4\n"
            "FAIL: test_sub (tests.test_calc.CalcTests.test_sub)\n"
            "AssertionError: 7 != 3\n"
            "Ran 2 tests in 1.456s\n"
        )
        diff_a = "diff --git a/calc.py b/calc.py\n+    return a + b\n"
        diff_b = "diff --git a/calc.py b/calc.py\n+    return a * b\n"

        fp_fast = compute_error_fingerprint(out_fast, diff_a)
        fp_slow = compute_error_fingerprint(out_slow, diff_a)
        fp_diff_changed = compute_error_fingerprint(out_fast, diff_b)

        self.assertRegex(fp_fast, _HEX16_RE)
        self.assertEqual(fp_fast, fp_slow)
        self.assertNotEqual(fp_fast, fp_diff_changed)

    def test_decide_next_action_priority_rules(self) -> None:
        # 1. Hard circuit-breaker at >= 3 consecutive failures regardless of tier/fingerprint
        self.assertEqual(
            decide_next_action("flash", 3, ["aaa"], "bbb", circuit_breaker=3),
            "abort_circuit_breaker",
        )
        self.assertEqual(
            decide_next_action("pro", 3, [], "bbb", circuit_breaker=3),
            "abort_circuit_breaker",
        )

        # 2. Repeated fingerprint: flash -> escalate_pro, pro -> abort_circuit_breaker
        self.assertEqual(
            decide_next_action("flash", 1, ["fp1"], "fp1", circuit_breaker=3),
            "escalate_pro",
        )
        self.assertEqual(
            decide_next_action("pro", 1, ["fp1"], "fp1", circuit_breaker=3),
            "abort_circuit_breaker",
        )

        # 3. 2nd consecutive unique failure on flash -> escalate_pro
        self.assertEqual(
            decide_next_action("flash", 2, ["fp1"], "fp2", circuit_breaker=3),
            "escalate_pro",
        )

        # 4. 1st unique failure on flash -> continue_flash; on pro -> escalate_pro
        self.assertEqual(
            decide_next_action("flash", 1, ["fp1"], "fp2", circuit_breaker=3),
            "continue_flash",
        )
        self.assertEqual(
            decide_next_action("pro", 1, ["fp1"], "fp2", circuit_breaker=3),
            "escalate_pro",
        )

    def test_short_circuits_earlier_stages_and_handles_command_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            marker = repo / "stage_ran.marker"
            marker_cmd = (
                f'"{sys.executable}" -c '
                '"from pathlib import Path; Path(\'stage_ran.marker\').write_text(\'1\')"'
            )

            # 1. git_guard failure short-circuits verify_red and test
            (repo / "tests" / "test_calc.py").unlink()
            res_guard = run_deterministic_gate(
                repo, LoopManifest(test_cmd=marker_cmd), base_ref, verify_red=True
            )
            self.assertFalse(res_guard.passed)
            self.assertEqual(res_guard.failed_stage, "git_guard")
            self.assertFalse(res_guard.should_run_reviewer)
            self.assertFalse(marker.exists())
            self.assertRegex(res_guard.error_fingerprint, _HEX16_RE)

            # Restore test file and make a tautological test -> verify_red fails
            _run_git(repo, "checkout", "--", "tests/test_calc.py")
            orig_test = (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")
            (repo / "tests" / "test_calc.py").write_text(
                orig_test
                + "\n    def test_tautology(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            res_red = run_deterministic_gate(
                repo,
                LoopManifest(test_cmd=f'"{sys.executable}" -m unittest discover -s tests'),
                base_ref,
                verify_red=True,
            )
            self.assertFalse(res_red.passed)
            self.assertEqual(res_red.failed_stage, "verify_red")
            self.assertFalse(res_red.should_run_reviewer)
            self.assertRegex(res_red.error_fingerprint, _HEX16_RE)

            # With verify_red=False, lint failure short-circuits build
            res_lint = run_deterministic_gate(
                repo,
                LoopManifest(
                    test_cmd=f'"{sys.executable}" -m unittest discover -s tests',
                    lint_cmd=f'"{sys.executable}" -c "import sys; sys.exit(2)"',
                    build_cmd=marker_cmd,
                ),
                base_ref,
                verify_red=False,
            )
            self.assertFalse(res_lint.passed)
            self.assertEqual(res_lint.failed_stage, "lint")
            self.assertFalse(res_lint.should_run_reviewer)
            self.assertFalse(marker.exists())

            # build failure is reported cleanly
            res_build = run_deterministic_gate(
                repo,
                LoopManifest(
                    test_cmd=f'"{sys.executable}" -m unittest discover -s tests',
                    build_cmd=f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(1)"',
                ),
                base_ref,
                verify_red=False,
            )
            self.assertFalse(res_build.passed)
            self.assertEqual(res_build.failed_stage, "build")
            self.assertFalse(res_build.should_run_reviewer)
            self.assertIn("boom", res_build.output)

            # TimeoutExpired and OSError on command stage are handled cleanly without crashing
            real_run = subprocess.run

            def _timeout_on_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if kwargs.get("shell"):
                    raise subprocess.TimeoutExpired(cmd="slow-cmd", timeout=120)
                return real_run(*args, **kwargs)  # type: ignore[arg-type]

            with mock.patch("core.eval_runner.subprocess.run", side_effect=_timeout_on_cmd):
                res_timeout = run_deterministic_gate(
                    repo,
                    LoopManifest(test_cmd="slow-cmd"),
                    base_ref,
                    verify_red=False,
                )
            self.assertFalse(res_timeout.passed)
            self.assertEqual(res_timeout.failed_stage, "test")
            self.assertFalse(res_timeout.should_run_reviewer)
            self.assertIn("timed out", res_timeout.output)
            self.assertRegex(res_timeout.error_fingerprint, _HEX16_RE)

            def _oserror_on_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if kwargs.get("shell"):
                    raise OSError("spawn failed")
                return real_run(*args, **kwargs)  # type: ignore[arg-type]

            with mock.patch("core.eval_runner.subprocess.run", side_effect=_oserror_on_cmd):
                res_oserr = run_deterministic_gate(
                    repo,
                    LoopManifest(test_cmd="broken-cmd"),
                    base_ref,
                    verify_red=False,
                )
            self.assertFalse(res_oserr.passed)
            self.assertEqual(res_oserr.failed_stage, "test")
            self.assertFalse(res_oserr.should_run_reviewer)
            self.assertIn("spawn failed", res_oserr.output)
            self.assertRegex(res_oserr.error_fingerprint, _HEX16_RE)

    def test_error_fingerprint_differs_for_different_untracked_file_edits_with_same_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)
            manifest = LoopManifest(
                test_cmd=f'"{sys.executable}" -m unittest discover -s tests -v',
            )

            # Brand-new untracked test + untracked prod module
            (repo / "tests" / "test_greenfield.py").write_text(
                "import unittest\n"
                "from greenfield import compute\n\n\n"
                "class GreenfieldTests(unittest.TestCase):\n"
                "    def test_compute(self) -> None:\n"
                "        self.assertEqual(compute(), 42)\n",
                encoding="utf-8",
            )
            (repo / "greenfield.py").write_text(
                "def compute() -> int:\n"
                "    return 1 + 0\n",
                encoding="utf-8",
            )
            res1 = run_deterministic_gate(repo, manifest, base_ref, verify_red=True)
            self.assertFalse(res1.passed)
            self.assertEqual(res1.failed_stage, "test")

            # Different untracked edit that yields the exact same failing output (1 != 42)
            (repo / "greenfield.py").write_text(
                "def compute() -> int:\n"
                "    return 2 - 1\n",
                encoding="utf-8",
            )
            res2 = run_deterministic_gate(repo, manifest, base_ref, verify_red=True)
            self.assertFalse(res2.passed)
            self.assertEqual(res2.failed_stage, "test")

            # Confirm normalized test output alone is identical, while full fingerprint differs
            self.assertEqual(
                compute_error_fingerprint(res1.output, ""),
                compute_error_fingerprint(res2.output, ""),
            )
            self.assertNotEqual(res1.error_fingerprint, res2.error_fingerprint)

    def test_gate_respects_manifest_verify_red_false_custom_assert_regex_and_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tests").mkdir(parents=True, exist_ok=True)
            base_ref = _init_sample_repo(repo)
            (repo / "tests" / "custom_check.py").write_text(
                "def run_check() -> None:\n    check_value(1, 1)\n",
                encoding="utf-8",
            )
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-m", "add custom check test")
            base_ref = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

            # Add an assertion without prod changes (normally fails verify_red=True).
            test_file = repo / "tests" / "test_calc.py"
            test_file.write_text(
                test_file.read_text(encoding="utf-8")
                + "\n    def test_add_more(self) -> None:\n"
                "        self.assertEqual(add(2, 2), 4)\n",
                encoding="utf-8",
            )

            env_cmd = (
                f'"{sys.executable}" -c '
                '"import os, sys; sys.exit(0 if os.environ.get(\'AGY_CUSTOM_VAR\') == \'hydrated-ok\' else 1)"'
            )
            custom_env = dict(os.environ)
            custom_env["AGY_CUSTOM_VAR"] = "hydrated-ok"

            manifest_no_red = LoopManifest(
                test_cmd=env_cmd,
                verify_red=False,
                test_timeout_sec=30,
                assert_regex=r"\bcheck_value\(",
            )
            res_ok = run_deterministic_gate(
                repo, manifest_no_red, base_ref, verify_red=None, env=custom_env
            )
            self.assertTrue(res_ok.passed, msg=res_ok.output)
            self.assertTrue(res_ok.should_run_reviewer)
            self.assertNotIn("[verify_red]", res_ok.output)

            # Even when caller passes verify_red=True, manifest.verify_red=False disables it.
            res_still_ok = run_deterministic_gate(
                repo, manifest_no_red, base_ref, verify_red=True, env=custom_env
            )
            self.assertTrue(res_still_ok.passed, msg=res_still_ok.output)

            # Custom assert_regex in manifest blocks weakening in git_guard.
            (repo / "tests" / "custom_check.py").write_text(
                "def run_check() -> None:\n    pass\n",
                encoding="utf-8",
            )
            res_guard = run_deterministic_gate(
                repo, manifest_no_red, base_ref, env=custom_env
            )
            self.assertFalse(res_guard.passed)
            self.assertEqual(res_guard.failed_stage, "git_guard")
            self.assertFalse(res_guard.should_run_reviewer)
            self.assertIn("tests/custom_check.py", res_guard.output)

    def test_gate_forwards_timeout_and_env_to_verify_red_and_cmd_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_ref = _init_sample_repo(repo)

            (repo / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n"
                "    return a - b\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_calc.py").write_text(
                "import unittest\n"
                "from calc import add, sub\n\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add_positive(self) -> None:\n"
                "        self.assertEqual(add(1, 2), 3)\n\n"
                "    def test_sub(self) -> None:\n"
                "        self.assertEqual(sub(5, 2), 3)\n",
                encoding="utf-8",
            )

            custom_env = dict(os.environ)
            custom_env["AGY_CUSTOM_VAR"] = "hydrated-ok"
            manifest = LoopManifest(
                test_cmd=f'"{sys.executable}" -m unittest discover -s tests -v',
                lint_cmd=(
                    f'"{sys.executable}" -c '
                    '"import os, sys; sys.exit(0 if os.environ.get(\'AGY_CUSTOM_VAR\') == \'hydrated-ok\' else 1)"'
                ),
                verify_red=True,
                test_timeout_sec=30,
            )

            shell_calls: list[tuple[object, object]] = []
            real_run = subprocess.run

            def _spy_run(
                *args: object, **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if kwargs.get("shell"):
                    shell_calls.append((kwargs.get("timeout"), kwargs.get("env")))
                return real_run(*args, **kwargs)  # type: ignore[arg-type]

            with mock.patch("subprocess.run", side_effect=_spy_run):
                res = run_deterministic_gate(
                    repo, manifest, base_ref, verify_red=True, env=custom_env
                )

            self.assertTrue(res.passed, msg=res.output)
            self.assertIn("[verify_red]", res.output)
            self.assertEqual(len(shell_calls), 3)
            self.assertEqual(shell_calls, [(30, custom_env), (30, custom_env), (30, custom_env)])


if __name__ == "__main__":
    unittest.main()
