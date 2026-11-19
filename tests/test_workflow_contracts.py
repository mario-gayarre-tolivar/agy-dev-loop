"""Contract tests for workflow logic, entry scripts, prompts, and SKILL.md."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "agy-dev-loop"
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROMPTS_DIR = SKILL_DIR / "prompts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.optimizer_guard import LEARNED_RULES_END, LEARNED_RULES_START  # noqa: E402
from core.workflow_logic import (  # noqa: E402
    EXECUTOR_SCHEMA,
    OPTIMIZER_SCHEMA,
    REVIEWER_SCHEMA,
    build_executor_prompt,
    build_optimizer_prompt,
    build_reviewer_prompt,
    run_inner_loop,
    run_outer_loop,
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


def _remove_worktree_safe(repo: Path, worktree_path: Path | None) -> None:
    if worktree_path is None:
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _init_loop_repo(repo_root: Path, *, max_iter: int = 5, circuit_breaker: int = 3) -> Path:
    _run_git(repo_root, "init")
    _run_git(repo_root, "config", "user.email", "test@example.com")
    _run_git(repo_root, "config", "user.name", "Test User")

    (repo_root / ".gitignore").write_text(
        ".agents/worktrees/\n.agents/loop/telemetry/\n__pycache__/\n",
        encoding="utf-8",
    )
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
        "    def test_add(self) -> None:\n"
        "        self.assertEqual(add(1, 2), 3)\n",
        encoding="utf-8",
    )

    agents_dir = repo_root / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "test_cmd": f'"{sys.executable}" -m unittest discover -s tests -v',
        "lint_cmd": None,
        "build_cmd": None,
        "protected_test_globs": ["tests/**", "test/**", "**/*.test.*", "**/*.spec.*"],
        "max_iter": max_iter,
        "circuit_breaker": circuit_breaker,
    }
    (agents_dir / "loop.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    plan_path = repo_root / "plan.md"
    plan_path.write_text(
        "# Plan\n\n"
        "- [ ] **Step 1: Add sub(a, b)**\n"
        "  Add subtraction helper `sub(a, b)` in `calc.py` with unit tests.\n",
        encoding="utf-8",
    )

    _run_git(repo_root, "add", "-A")
    _run_git(repo_root, "commit", "-m", "initial commit")
    return plan_path


def _write_sample_metrics(metrics_dir: Path, count: int) -> None:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(count):
        payload = {
            "run_id": f"run-{idx + 1}",
            "repo_name": "sample-repo",
            "subtasks_total": 2,
            "subtasks_passed": 2,
            "iterations": 3,
            "rollbacks": 1,
            "reviewer_rejections": 0,
            "failure_fingerprints": ["deadbeef12345678"],
            "status": "completed",
        }
        (metrics_dir / f"run-{idx + 1}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )


class WorkflowContractTests(unittest.TestCase):
    """Contract tests covering prompts, schemas, inner loop, outer loop, and entry scripts."""

    def test_prompts_contain_immutable_core_and_learned_rules_markers(self) -> None:
        for name in ("executor.md", "reviewer.md"):
            prompt_file = PROMPTS_DIR / name
            self.assertTrue(prompt_file.is_file(), f"Missing prompt template: {name}")
            content = prompt_file.read_text(encoding="utf-8")
            self.assertEqual(content.count(LEARNED_RULES_START), 1)
            self.assertEqual(content.count(LEARNED_RULES_END), 1)
            start_idx = content.index(LEARNED_RULES_START)
            end_idx = content.index(LEARNED_RULES_END)
            self.assertGreater(start_idx, 0, f"{name} must have immutable core before markers")
            self.assertLess(start_idx + len(LEARNED_RULES_START), end_idx)
            self.assertTrue(content[:start_idx].strip())

        optimizer_file = PROMPTS_DIR / "optimizer.md"
        self.assertTrue(optimizer_file.is_file())
        opt_content = optimizer_file.read_text(encoding="utf-8")
        self.assertIn("12", opt_content)
        self.assertIn("600", opt_content)

        skill_file = SKILL_DIR / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        skill_text = skill_file.read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\n"))
        parts = skill_text.split("---\n", 2)
        self.assertEqual(len(parts), 3, "SKILL.md must have YAML frontmatter delimited by ---")
        frontmatter = parts[1]
        self.assertIn("name: agy-dev-loop", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertIn("init_manifest", parts[2])
        self.assertIn("inner_loop.py", parts[2])
        self.assertIn("outer_loop.py", parts[2])
        self.assertIn("apply_approved", parts[2])
        self.assertIn("`.agents/loop/`", parts[2])

    def test_reviewer_prompt_excludes_executor_conversation_history(self) -> None:
        # Verify exact required schemas
        self.assertEqual(
            EXECUTOR_SCHEMA["required"],
            ["subtask_index", "files_changed", "test_files_touched", "summary"],
        )
        self.assertEqual(
            REVIEWER_SCHEMA["required"],
            ["approved", "blocking_issues", "lesson_candidate"],
        )
        self.assertEqual(
            OPTIMIZER_SCHEMA["required"],
            ["target_prompt", "candidate_rules", "rationale"],
        )
        self.assertEqual(
            OPTIMIZER_SCHEMA["properties"]["target_prompt"]["enum"],
            ["executor.md", "reviewer.md"],
        )

        # Signature of build_reviewer_prompt must never accept executor history/messages
        sig_params = list(inspect.signature(build_reviewer_prompt).parameters)
        self.assertEqual(
            sig_params,
            ["template_text", "subtask_title", "subtask_body", "git_diff", "gate_output"],
        )

        reviewer_template = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
        prompt = build_reviewer_prompt(
            template_text=reviewer_template,
            subtask_title="Step 1: Add add(a, b)",
            subtask_body="Implement add(a, b) with tests.",
            git_diff="+def add(a, b): return a + b",
            gate_output="[test]\nRan 1 test: OK",
        )
        self.assertIn("Step 1: Add add(a, b)", prompt)
        self.assertIn("Implement add(a, b) with tests.", prompt)
        self.assertIn("+def add(a, b): return a + b", prompt)
        self.assertIn("Ran 1 test: OK", prompt)
        self.assertNotIn("executor_history", prompt.lower())
        self.assertNotIn("previous attempt feedback", prompt.lower())

        # Sanity-check executor and optimizer prompt builders as well
        exec_prompt = build_executor_prompt(
            template_text=(PROMPTS_DIR / "executor.md").read_text(encoding="utf-8"),
            worktree_path=Path("/tmp/wt-1"),
            subtask_index=0,
            subtask_title="Step 1",
            subtask_body="Body text",
            lessons_text="| L1 | sym | rule | 2 | 2026-11-19 |",
            last_feedback="Test failed on line 10",
        )
        self.assertIn("Step 1", exec_prompt)
        self.assertIn("Body text", exec_prompt)
        self.assertIn("Test failed on line 10", exec_prompt)

        opt_prompt = build_optimizer_prompt(
            template_text=(PROMPTS_DIR / "optimizer.md").read_text(encoding="utf-8"),
            metrics_runs=[{"run_id": "r1", "failure_fingerprints": ["abcd1234"]}],
            lessons_text="| L1 | sym | rule | 2 | 2026-11-19 |",
            current_prompts={"executor.md": "# Exec", "reviewer.md": "# Rev"},
        )
        self.assertIn("abcd1234", opt_prompt)
        self.assertIn("executor.md", opt_prompt)

    def test_inner_loop_short_circuits_reviewer_on_gate_failure_and_commits_on_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            plan_path = _init_loop_repo(repo)
            run_id = "run-contract-1"
            expected_wt = repo / ".agents" / "worktrees" / run_id

            executor_calls: list[dict[str, Any]] = []
            reviewer_calls: list[dict[str, Any]] = []
            phases: list[str] = []
            logs: list[str] = []

            async def fake_agent(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                self.assertEqual(workspace, "inherit")
                if schema is EXECUTOR_SCHEMA:
                    executor_calls.append({"prompt": prompt, "role": role, "model": model})
                    attempt_num = len(executor_calls)
                    if attempt_num == 1:
                        # Broken change: breaks existing test_add
                        (expected_wt / "calc.py").write_text(
                            "def add(a: int, b: int) -> int:\n    return a - b\n",
                            encoding="utf-8",
                        )
                        return {
                            "subtask_index": 0,
                            "files_changed": ["calc.py"],
                            "test_files_touched": [],
                            "summary": "SECRET_EXECUTOR_THOUGHT_ATTEMPT_1",
                        }
                    # Attempt 2: verify previous failure feedback was injected & rollback occurred
                    self.assertIn("Deterministic gate failed", prompt)
                    self.assertIn("return a + b", (expected_wt / "calc.py").read_text(encoding="utf-8"))
                    (expected_wt / "calc.py").write_text(
                        "def add(a: int, b: int) -> int:\n"
                        "    return a + b\n\n\n"
                        "def sub(a: int, b: int) -> int:\n"
                        "    return a - b\n",
                        encoding="utf-8",
                    )
                    (expected_wt / "tests" / "test_calc.py").write_text(
                        "import unittest\n"
                        "from calc import add, sub\n\n\n"
                        "class CalcTests(unittest.TestCase):\n"
                        "    def test_add(self) -> None:\n"
                        "        self.assertEqual(add(1, 2), 3)\n\n"
                        "    def test_sub(self) -> None:\n"
                        "        self.assertEqual(sub(5, 2), 3)\n",
                        encoding="utf-8",
                    )
                    return {
                        "subtask_index": 0,
                        "files_changed": ["calc.py", "tests/test_calc.py"],
                        "test_files_touched": ["tests/test_calc.py"],
                        "summary": "SECRET_EXECUTOR_THOUGHT_ATTEMPT_2",
                    }

                if schema is REVIEWER_SCHEMA:
                    reviewer_calls.append({"prompt": prompt, "role": role, "model": model})
                    self.assertEqual(model, "inherit")
                    self.assertIn("def sub", prompt)
                    self.assertIn("test_sub", prompt)
                    self.assertNotIn("SECRET_EXECUTOR_THOUGHT_ATTEMPT_1", prompt)
                    self.assertNotIn("SECRET_EXECUTOR_THOUGHT_ATTEMPT_2", prompt)
                    return {
                        "approved": True,
                        "blocking_issues": [],
                        "lesson_candidate": {
                            "symptom": "Broken arithmetic edit failed unittest",
                            "rule": "Verify existing add() tests stay green when adding sub()",
                        },
                    }

                raise AssertionError(f"Unexpected schema: {schema}")

            try:
                result = asyncio.run(
                    run_inner_loop(
                        {
                            "repo_root": str(repo),
                            "plan_path": str(plan_path),
                            "run_id": run_id,
                        },
                        fake_agent,
                        phases.append,
                        logs.append,
                    )
                )

                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(executor_calls), 2)
                self.assertEqual(len(reviewer_calls), 1)
                self.assertEqual(executor_calls[0]["model"], "flash")
                self.assertEqual(result["iterations"], 2)
                self.assertEqual(result["rollbacks"], 1)
                self.assertEqual(result["reviewer_rejections"], 0)
                self.assertEqual(result["subtasks_total"], 1)
                self.assertEqual(result["subtasks_passed"], 1)
                self.assertEqual(len(result["commits"]), 1)
                self.assertFalse(result["should_trigger_global_optimization"])

                # Worktree plan marked [x] and cleanly committed; main repo plan untouched
                wt_plan_text = (expected_wt / "plan.md").read_text(encoding="utf-8")
                self.assertIn("- [x] **Step 1: Add sub(a, b)**", wt_plan_text)
                self.assertIn("- [ ] **Step 1: Add sub(a, b)**", plan_path.read_text(encoding="utf-8"))
                wt_status = _run_git(expected_wt, "status", "--porcelain").stdout.strip()
                self.assertEqual(wt_status, "")

                # Metrics JSON persisted
                metrics_path = Path(result["metrics_path"])
                self.assertTrue(metrics_path.is_file())
                metrics_data = json.loads(metrics_path.read_text(encoding="utf-8"))
                self.assertEqual(metrics_data["run_id"], run_id)
                self.assertEqual(metrics_data["rollbacks"], 1)
                self.assertEqual(metrics_data["subtasks_passed"], 1)

                # Lessons persisted
                lessons_path = Path(result["lessons_path"])
                self.assertTrue(lessons_path.is_file())
                self.assertIn("Verify existing add() tests stay green", lessons_path.read_text(encoding="utf-8"))
            finally:
                _remove_worktree_safe(repo, expected_wt)

    def test_inner_loop_escalates_on_repeated_fingerprint_and_aborts_on_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            plan_path = _init_loop_repo(repo, max_iter=5, circuit_breaker=3)
            run_id = "run-breaker-1"
            expected_wt = repo / ".agents" / "worktrees" / run_id
            models_used: list[str] = []
            reviewer_called = False

            async def fake_agent(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                nonlocal reviewer_called
                if schema is REVIEWER_SCHEMA:
                    reviewer_called = True
                    return {"approved": False, "blocking_issues": [], "lesson_candidate": None}
                models_used.append(model)
                # Identical failing edit every attempt -> same fingerprint
                (expected_wt / "calc.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return 999\n",
                    encoding="utf-8",
                )
                return {
                    "subtask_index": 0,
                    "files_changed": ["calc.py"],
                    "test_files_touched": [],
                    "summary": "broken",
                }

            try:
                result = asyncio.run(
                    run_inner_loop(
                        {
                            "repo_root": str(repo),
                            "plan_path": str(plan_path),
                            "run_id": run_id,
                        },
                        fake_agent,
                        lambda _name: None,
                        lambda *_args: None,
                    )
                )
                self.assertEqual(result["status"], "aborted_circuit_breaker")
                self.assertFalse(reviewer_called)
                # Attempt 1: flash -> continue_flash; Attempt 2 (dup fp): flash -> escalate_pro;
                # Attempt 3: pro -> circuit_breaker (3 failures) -> abort
                self.assertEqual(models_used, ["flash", "flash", "pro"])
                self.assertEqual(result["rollbacks"], 3)
                self.assertEqual(result["subtasks_passed"], 0)
            finally:
                _remove_worktree_safe(repo, expected_wt)

    def test_outer_loop_stages_benchmark_validated_patch_without_mutating_active_prompt_until_approved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts_copy = root / "prompts"
            shutil.copytree(PROMPTS_DIR, prompts_copy)
            metrics_dir = root / "telemetry"
            benchmarks_root = REPO_ROOT / "benchmarks"

            executor_prompt_path = prompts_copy / "executor.md"
            original_bytes = executor_prompt_path.read_bytes()

            agent_calls = 0

            async def fake_optimizer(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                nonlocal agent_calls
                agent_calls += 1
                self.assertIs(schema, OPTIMIZER_SCHEMA)
                self.assertEqual(workspace, "inherit")
                self.assertEqual(model, "inherit")
                self.assertIn("deadbeef12345678", prompt)
                return {
                    "target_prompt": "executor.md",
                    "candidate_rules": [
                        {
                            "rule_id": "R1",
                            "text": "Run targeted unittest before calling finish.",
                        }
                    ],
                    "rationale": "Fingerprint deadbeef12345678 repeated across 3 runs.",
                }

            # 1. Fewer than 3 runs -> skips without invoking agent
            _write_sample_metrics(metrics_dir, count=2)
            skipped = asyncio.run(
                run_outer_loop(
                    {
                        "metrics_dir": str(metrics_dir),
                        "prompts_dir": str(prompts_copy),
                        "benchmarks_root": str(benchmarks_root),
                    },
                    fake_optimizer,
                    lambda _name: None,
                    lambda *_args: None,
                )
            )
            self.assertEqual(skipped["status"], "skipped_insufficient_runs")
            self.assertFalse(skipped["applied"])
            self.assertEqual(agent_calls, 0)
            self.assertEqual(executor_prompt_path.read_bytes(), original_bytes)

            # 2. >= 3 runs with apply_approved omitted/False -> validates benchmarks, returns diff,
            #    AND restores active prompt file byte-for-byte
            _write_sample_metrics(metrics_dir, count=3)
            staged = asyncio.run(
                run_outer_loop(
                    {
                        "metrics_dir": str(metrics_dir),
                        "prompts_dir": str(prompts_copy),
                        "benchmarks_root": str(benchmarks_root),
                        "apply_approved": False,
                    },
                    fake_optimizer,
                    lambda _name: None,
                    lambda *_args: None,
                )
            )
            self.assertEqual(agent_calls, 1)
            self.assertEqual(staged["status"], "staged_pending_approval")
            self.assertFalse(staged["applied"])
            self.assertTrue(staged["requires_human_approval"])
            self.assertEqual(staged["target_prompt"], "executor.md")
            self.assertIn("+- [R1]", staged["proposed_diff"])
            self.assertIn("Run targeted unittest before calling finish.", staged["proposed_diff"])
            self.assertEqual(
                executor_prompt_path.read_bytes(),
                original_bytes,
                "Active prompt MUST NOT be mutated until human approval is explicit",
            )

            # 2b. Benchmark failure must reject and keep prompt unchanged even if apply_approved=True
            bad_bench = root / "bad_benchmarks"
            py_tests = bad_bench / "py_fixture" / "tests"
            py_tests.mkdir(parents=True)
            (py_tests / "__init__.py").write_text("", encoding="utf-8")
            (py_tests / "test_fail.py").write_text(
                "import unittest\nclass F(unittest.TestCase):\n"
                "    def test_f(self): self.fail('boom')\n",
                encoding="utf-8",
            )
            js_dir = bad_bench / "js_fixture"
            js_dir.mkdir(parents=True)
            (js_dir / "fail.test.mjs").write_text(
                "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                "test('f', () => assert.equal(1, 2));\n",
                encoding="utf-8",
            )
            rejected = asyncio.run(
                run_outer_loop(
                    {
                        "metrics_dir": str(metrics_dir),
                        "prompts_dir": str(prompts_copy),
                        "benchmarks_root": str(bad_bench),
                        "apply_approved": True,
                        "approved_proposal": {
                            "target_prompt": staged["target_prompt"],
                            "candidate_rules": staged["candidate_rules"],
                            "rationale": staged["rationale"],
                        },
                    },
                    fake_optimizer,
                    lambda _name: None,
                    lambda *_args: None,
                )
            )
            self.assertEqual(rejected["status"], "rejected_benchmark")
            self.assertFalse(rejected["applied"])
            self.assertEqual(executor_prompt_path.read_bytes(), original_bytes)

            # 3. With apply_approved=True -> validates benchmarks and persists patch
            applied = asyncio.run(
                run_outer_loop(
                    {
                        "metrics_dir": str(metrics_dir),
                        "prompts_dir": str(prompts_copy),
                        "benchmarks_root": str(benchmarks_root),
                        "apply_approved": True,
                        "approved_proposal": {
                            "target_prompt": staged["target_prompt"],
                            "candidate_rules": staged["candidate_rules"],
                            "rationale": staged["rationale"],
                        },
                    },
                    fake_optimizer,
                    lambda _name: None,
                    lambda *_args: None,
                )
            )
            self.assertEqual(agent_calls, 1, "Pre-approved proposal should not re-roll LLM")
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(applied["applied"])
            self.assertFalse(applied["requires_human_approval"])
            updated_text = executor_prompt_path.read_text(encoding="utf-8")
            self.assertIn("- [R1] Run targeted unittest before calling finish.", updated_text)
            self.assertIn("## Reglas Inmutables", updated_text)

    def test_outer_loop_rejects_apply_approved_without_approved_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts_copy = root / "prompts"
            shutil.copytree(PROMPTS_DIR, prompts_copy)
            metrics_dir = root / "telemetry"
            _write_sample_metrics(metrics_dir, count=3)

            before_bytes = {
                name: (prompts_copy / name).read_bytes()
                for name in ("executor.md", "reviewer.md")
            }
            agent_called = False

            async def fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                nonlocal agent_called
                agent_called = True
                raise AssertionError("agent_fn must not be invoked when approved_proposal is missing")

            for bad_args in (
                {
                    "metrics_dir": str(metrics_dir),
                    "prompts_dir": str(prompts_copy),
                    "benchmarks_root": str(REPO_ROOT / "benchmarks"),
                    "apply_approved": True,
                },
                {
                    "metrics_dir": str(metrics_dir),
                    "prompts_dir": str(prompts_copy),
                    "benchmarks_root": str(REPO_ROOT / "benchmarks"),
                    "apply_approved": True,
                    "approved_proposal": "not-a-dict",
                },
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    r"approved_proposal dict is required when apply_approved is True",
                ):
                    asyncio.run(
                        run_outer_loop(
                            bad_args,
                            fail_if_called,
                            lambda _name: None,
                            lambda *_args: None,
                        )
                    )

            self.assertFalse(agent_called)
            for name, raw in before_bytes.items():
                self.assertEqual((prompts_copy / name).read_bytes(), raw)

    def test_inner_loop_handles_reviewer_rejection_rollback_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            plan_path = _init_loop_repo(repo)
            run_id = "run-rev-reject-1"
            expected_wt = repo / ".agents" / "worktrees" / run_id
            exec_attempt = 0
            rev_attempt = 0

            async def fake_agent(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                nonlocal exec_attempt, rev_attempt
                if schema is EXECUTOR_SCHEMA:
                    exec_attempt += 1
                    if exec_attempt == 2:
                        self.assertIn("Reviewer rejected the diff", prompt)
                        self.assertIn("Handle negative numbers", prompt)
                    (expected_wt / "calc.py").write_text(
                        "def add(a: int, b: int) -> int:\n"
                        "    return a + b\n\n\n"
                        "def sub(a: int, b: int) -> int:\n"
                        "    return a - b\n",
                        encoding="utf-8",
                    )
                    extra_test = (
                        "\n    def test_sub_negative(self) -> None:\n"
                        "        self.assertEqual(sub(-2, -3), 1)\n"
                        if exec_attempt >= 2
                        else ""
                    )
                    (expected_wt / "tests" / "test_calc.py").write_text(
                        "import unittest\n"
                        "from calc import add, sub\n\n\n"
                        "class CalcTests(unittest.TestCase):\n"
                        "    def test_add(self) -> None:\n"
                        "        self.assertEqual(add(1, 2), 3)\n\n"
                        "    def test_sub(self) -> None:\n"
                        "        self.assertEqual(sub(5, 2), 3)\n"
                        + extra_test,
                        encoding="utf-8",
                    )
                    return {
                        "subtask_index": 0,
                        "files_changed": ["calc.py", "tests/test_calc.py"],
                        "test_files_touched": ["tests/test_calc.py"],
                        "summary": f"attempt {exec_attempt}",
                    }

                rev_attempt += 1
                if rev_attempt == 1:
                    return {
                        "approved": False,
                        "blocking_issues": ["Handle negative numbers in test_sub"],
                        "lesson_candidate": None,
                    }
                return {
                    "approved": True,
                    "blocking_issues": [],
                    "lesson_candidate": None,
                }

            try:
                result = asyncio.run(
                    run_inner_loop(
                        {
                            "repo_root": str(repo),
                            "plan_path": str(plan_path),
                            "run_id": run_id,
                        },
                        fake_agent,
                        lambda _name: None,
                        lambda *_args: None,
                    )
                )
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["reviewer_rejections"], 1)
                self.assertEqual(result["rollbacks"], 1)
                self.assertEqual(result["iterations"], 2)
                self.assertEqual(result["subtasks_passed"], 1)
            finally:
                _remove_worktree_safe(repo, expected_wt)

    def test_run_workflow_entry_scripts_parse_with_top_level_await(self) -> None:
        expected = {
            "inner_loop.py": "run_inner_loop",
            "outer_loop.py": "run_outer_loop",
        }
        compiled: dict[str, Any] = {}
        for filename, entry_fn in expected.items():
            script_path = SCRIPTS_DIR / filename
            self.assertTrue(script_path.is_file(), f"Missing entry script: {filename}")
            source = script_path.read_text(encoding="utf-8")

            # Must compile cleanly under run_workflow's top-level await mode
            code = compile(
                source,
                str(script_path),
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )
            self.assertIsNotNone(code)
            compiled[filename] = (code, script_path)

            # Must insert script parent into sys.path before importing core.workflow_logic
            self.assertIn("Path(__file__).resolve().parent", source)
            self.assertIn("sys.path.insert", source)
            self.assertLess(
                source.index("sys.path.insert"),
                source.index("from core.workflow_logic import"),
            )
            self.assertIn(f"await {entry_fn}(", source)
            self.assertIn("json.dumps(", source)

        # Runtime-verify outer_loop.py top-level await coroutine emits valid JSON
        code, outer_path = compiled["outer_loop.py"]
        with tempfile.TemporaryDirectory() as tmp:
            stdout_buf = io.StringIO()
            ns: dict[str, Any] = {
                "__file__": str(outer_path),
                "__name__": "__main__",
                "args": {"metrics_dir": str(Path(tmp) / "empty_metrics")},
                "agent": lambda *_a, **_kw: None,
                "phase": lambda _name: None,
                "log": lambda *_a: None,
            }
            with contextlib.redirect_stdout(stdout_buf):
                asyncio.run(eval(code, ns))
            emitted = json.loads(stdout_buf.getvalue())
            self.assertEqual(emitted["status"], "skipped_insufficient_runs")
            self.assertFalse(emitted["applied"])

    def test_inner_loop_fails_before_worktree_or_agent_when_manifest_test_cmd_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            plan_path = _init_loop_repo(repo)

            # Configure a broken/failing test_cmd and commit so the repo is clean
            manifest_path = repo / ".agents" / "loop.json"
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_data["test_cmd"] = (
                f'"{sys.executable}" -c "import sys; print(\'broken baseline\'); sys.exit(2)"'
            )
            manifest_path.write_text(
                json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8"
            )
            _run_git(repo, "add", ".agents/loop.json")
            _run_git(repo, "commit", "-m", "break baseline test_cmd")

            run_id = "run-broken-cmd"
            expected_wt = repo / ".agents" / "worktrees" / run_id
            agent_called = False

            async def fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                nonlocal agent_called
                agent_called = True
                raise AssertionError("agent_fn must not be invoked when baseline test_cmd fails")

            try:
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(
                        run_inner_loop(
                            {
                                "repo_root": str(repo),
                                "plan_path": str(plan_path),
                                "run_id": run_id,
                            },
                            fail_if_called,
                            lambda _name: None,
                            lambda *_args: None,
                        )
                    )
                self.assertIn("broken baseline", str(ctx.exception))
                self.assertFalse(agent_called)
                self.assertFalse(expected_wt.exists())
                branches = _run_git(
                    repo, "branch", "--list", f"agy-loop/{run_id}"
                ).stdout.strip()
                self.assertEqual(branches, "")
            finally:
                _remove_worktree_safe(repo, expected_wt)

    def test_llm_json_trust_boundaries_reject_non_string_fields_and_malformed_blocking_issues(
        self,
    ) -> None:
        from core.workflow_logic import _extract_lesson, _parse_proposal  # noqa: PLC0415

        # 1. _extract_lesson must return None on None / non-string / empty fields
        for bad_candidate in (
            {"symptom": None, "rule": "valid rule"},
            {"symptom": "valid symptom", "rule": None},
            {"symptom": 123, "rule": "valid rule"},
            {"symptom": "valid symptom", "rule": ["not", "str"]},
            {"symptom": "   ", "rule": "valid rule"},
            {"symptom": "valid symptom", "rule": ""},
        ):
            self.assertIsNone(
                _extract_lesson({"lesson_candidate": bad_candidate}, "2026-11-19")
            )

        # 2. _parse_proposal must reject non-string rule_id, text, or rationale
        for bad_proposal in (
            {
                "target_prompt": "executor.md",
                "candidate_rules": [{"rule_id": None, "text": "valid"}],
                "rationale": "ok",
            },
            {
                "target_prompt": "executor.md",
                "candidate_rules": [{"rule_id": "R1", "text": None}],
                "rationale": "ok",
            },
            {
                "target_prompt": "executor.md",
                "candidate_rules": [{"rule_id": 1, "text": "valid"}],
                "rationale": "ok",
            },
            {
                "target_prompt": "executor.md",
                "candidate_rules": [{"rule_id": "R1", "text": "valid"}],
                "rationale": None,
            },
            {
                "target_prompt": "executor.md",
                "candidate_rules": [{"rule_id": "R1", "text": "valid"}],
                "rationale": 42,
            },
        ):
            with self.assertRaises(ValueError):
                _parse_proposal(bad_proposal)

        # 3. run_inner_loop handles string and integer blocking_issues and None lesson_candidate
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            plan_path = _init_loop_repo(repo, max_iter=5, circuit_breaker=4)
            run_id = "run-llm-boundary"
            expected_wt = repo / ".agents" / "worktrees" / run_id
            exec_prompts: list[str] = []
            rev_calls = 0

            async def fake_agent(
                prompt: str,
                schema: dict[str, Any],
                role: str = "",
                workspace: str = "inherit",
                model: str = "inherit",
            ) -> dict[str, Any]:
                nonlocal rev_calls
                if schema is EXECUTOR_SCHEMA:
                    exec_prompts.append(prompt)
                    attempt = len(exec_prompts)
                    (expected_wt / "calc.py").write_text(
                        "def add(a: int, b: int) -> int:\n"
                        "    return a + b\n\n\n"
                        f"def sub(a: int, b: int) -> int:  # attempt {attempt}\n"
                        "    return a - b\n",
                        encoding="utf-8",
                    )
                    (expected_wt / "tests" / "test_calc.py").write_text(
                        "import unittest\n"
                        "from calc import add, sub\n\n\n"
                        "class CalcTests(unittest.TestCase):\n"
                        "    def test_add(self) -> None:\n"
                        "        self.assertEqual(add(1, 2), 3)\n\n"
                        "    def test_sub(self) -> None:\n"
                        "        self.assertEqual(sub(5, 2), 3)\n",
                        encoding="utf-8",
                    )
                    return {
                        "subtask_index": 0,
                        "files_changed": ["calc.py", "tests/test_calc.py"],
                        "test_files_touched": ["tests/test_calc.py"],
                        "summary": f"attempt {attempt}",
                    }

                rev_calls += 1
                if rev_calls == 1:
                    # String blocking_issues (must NOT be split char-by-char) + None lesson field
                    return {
                        "approved": False,
                        "blocking_issues": "Handle negative numbers",
                        "lesson_candidate": {"symptom": None, "rule": "do not save"},
                    }
                if rev_calls == 2:
                    # Integer blocking_issues (must NOT raise TypeError)
                    return {
                        "approved": False,
                        "blocking_issues": 42,
                        "lesson_candidate": {"symptom": "s", "rule": None},
                    }
                return {
                    "approved": True,
                    "blocking_issues": [],
                    "lesson_candidate": None,
                }

            try:
                result = asyncio.run(
                    run_inner_loop(
                        {
                            "repo_root": str(repo),
                            "plan_path": str(plan_path),
                            "run_id": run_id,
                        },
                        fake_agent,
                        lambda _name: None,
                        lambda *_args: None,
                    )
                )
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["reviewer_rejections"], 2)
                self.assertEqual(result["lessons_added"], 0)
                self.assertIn("- Handle negative numbers", exec_prompts[1])
                self.assertNotIn("- H\n- a\n- n", exec_prompts[1])
                self.assertIn("Reviewer rejected without blocking_issues.", exec_prompts[2])
            finally:
                _remove_worktree_safe(repo, expected_wt)


if __name__ == "__main__":
    unittest.main()
