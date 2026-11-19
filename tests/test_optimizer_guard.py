"""Unit tests for `core.optimizer_guard`."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.optimizer_guard import (  # noqa: E402
    LEARNED_RULES_END,
    LEARNED_RULES_START,
    PromptRule,
    apply_bounded_rule_patch,
    run_offline_benchmarks,
    stage_and_validate_global_patch,
)


class OptimizerGuardTests(unittest.TestCase):
    """Tests for bounded prompt rule mutation and offline benchmark validation."""

    def test_preserves_frozen_prompt_core_and_enforces_rule_budget(self) -> None:
        prefix = "# Core Prompt\r\nDo not touch this header.\r\n\r\n"
        suffix = "\r\n\r\n## Footer\r\nKeep exact CRLF bytes.\r\n"
        initial = f"{prefix}{LEARNED_RULES_START}\r\n- [old] Old rule\r\n{LEARNED_RULES_END}{suffix}"

        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "SKILL.md"
            prompt_path.write_bytes(initial.encode("utf-8"))

            # >12 rules rejected
            too_many = [PromptRule(rule_id=f"r{i}", text=f"Rule {i}") for i in range(13)]
            with self.assertRaises(ValueError):
                apply_bounded_rule_patch(prompt_path, too_many)
            self.assertEqual(prompt_path.read_bytes(), initial.encode("utf-8"))

            # >600 estimated tokens rejected (>2400 chars)
            oversized = [PromptRule(rule_id="big", text="x" * 2450)]
            with self.assertRaises(ValueError):
                apply_bounded_rule_patch(prompt_path, oversized)
            self.assertEqual(prompt_path.read_bytes(), initial.encode("utf-8"))

            # Empty/whitespace or marker-containing fields rejected
            for bad_rule in (
                PromptRule(rule_id="   ", text="Valid"),
                PromptRule(rule_id="r1", text="  \n "),
                PromptRule(rule_id="r1", text=f"bad {LEARNED_RULES_END}"),
                PromptRule(rule_id=LEARNED_RULES_START, text="bad"),
            ):
                with self.assertRaises(ValueError):
                    apply_bounded_rule_patch(prompt_path, [bad_rule])

            # Missing / duplicate / inverted markers rejected
            for bad_doc in (
                f"# No markers\n{LEARNED_RULES_START}\n",
                f"# Missing start\n{LEARNED_RULES_END}\n",
                f"{LEARNED_RULES_END}\n{LEARNED_RULES_START}\n",
                f"{LEARNED_RULES_START}\n{LEARNED_RULES_START}\n{LEARNED_RULES_END}\n",
                f"{LEARNED_RULES_START}\n{LEARNED_RULES_END}\n{LEARNED_RULES_END}\n",
            ):
                bad_path = Path(tmp) / "bad.md"
                bad_path.write_text(bad_doc, encoding="utf-8")
                with self.assertRaises(ValueError):
                    apply_bounded_rule_patch(bad_path, [PromptRule(rule_id="r1", text="Keep clean")])

            # Valid patch preserves prefix and suffix byte-for-byte
            rules = [
                PromptRule(rule_id="r1", text="Always pass encoding='utf-8'"),
                PromptRule(rule_id="r2", text="Keep diff minimal"),
            ]
            updated = apply_bounded_rule_patch(prompt_path, rules)
            raw_bytes = prompt_path.read_bytes()
            self.assertEqual(updated.encode("utf-8"), raw_bytes)
            self.assertTrue(raw_bytes.startswith((prefix + LEARNED_RULES_START).encode("utf-8")))
            self.assertTrue(raw_bytes.endswith((LEARNED_RULES_END + suffix).encode("utf-8")))
            self.assertIn("- [r1] Always pass encoding='utf-8'\n- [r2] Keep diff minimal", updated)
            self.assertNotIn("- [old] Old rule", updated)

    def test_offline_benchmarks_pass_with_stdlib_runners(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        passed, details = run_offline_benchmarks(repo_root / "benchmarks")
        self.assertTrue(passed)
        self.assertEqual(details, {"py_fixture": True, "js_fixture": True})

    def test_stage_and_validate_global_patch_rolls_back_on_benchmark_failure(self) -> None:
        prefix = "# Frozen Header\r\n"
        suffix = "\r\n# Frozen Footer\r\n"
        initial = f"{prefix}{LEARNED_RULES_START}\r\n- [r0] Original\r\n{LEARNED_RULES_END}{suffix}"
        initial_bytes = initial.encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "SKILL.md"
            prompt_path.write_bytes(initial_bytes)

            # Create failing benchmark fixtures
            py_tests = root / "benchmarks" / "py_fixture" / "tests"
            py_tests.mkdir(parents=True)
            (py_tests / "__init__.py").write_text("", encoding="utf-8")
            (py_tests / "test_fail.py").write_text(
                "import unittest\n\nclass FailTest(unittest.TestCase):\n"
                "    def test_boom(self):\n        self.fail('intentional failure')\n",
                encoding="utf-8",
            )
            js_dir = root / "benchmarks" / "js_fixture"
            js_dir.mkdir(parents=True)
            (js_dir / "fail.test.mjs").write_text(
                "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                "test('fails', () => { assert.equal(1, 2); });\n",
                encoding="utf-8",
            )

            ok, message = stage_and_validate_global_patch(
                prompt_path=prompt_path,
                candidate_rules=[PromptRule(rule_id="r1", text="New candidate rule")],
                benchmarks_root=root / "benchmarks",
            )
            self.assertFalse(ok)
            self.assertTrue(len(message.strip()) > 0)
            self.assertEqual(prompt_path.read_bytes(), initial_bytes)

    def test_stage_and_validate_global_patch_commits_when_benchmarks_pass(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        initial = f"# Prompt\n{LEARNED_RULES_START}\n{LEARNED_RULES_END}\n"

        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "SKILL.md"
            prompt_path.write_text(initial, encoding="utf-8")

            ok, summary = stage_and_validate_global_patch(
                prompt_path=prompt_path,
                candidate_rules=[PromptRule(rule_id="r1", text="Verified rule")],
                benchmarks_root=repo_root / "benchmarks",
            )
            self.assertTrue(ok)
            self.assertIn("Verified rule", summary)
            self.assertIn("- [r1] Verified rule", prompt_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
