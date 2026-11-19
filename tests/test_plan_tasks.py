"""Unit tests for `core.plan_tasks`."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.plan_tasks import (  # noqa: E402
    PlanSubtask,
    mark_subtask_completed,
    next_pending_subtask,
    parse_plan_subtasks,
)


class PlanTasksTests(unittest.TestCase):
    """Tests for parsing plan subtasks and advancing one checkbox at a time."""

    def test_parse_and_advance_single_subtask_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.md"
            original = (
                "# Feature Plan\r\n"
                "\r\n"
                "## Tasks\r\n"
                "- [ ] First task with [ ] literal\r\n"
                "  First detail line\r\n"
                "  Second detail line\r\n"
                "\r\n"
                "- [ ] Second task\r\n"
                "  Second detail\r\n"
            )
            with plan_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(original)

            subtasks = parse_plan_subtasks(plan_path)
            self.assertEqual(
                subtasks,
                [
                    PlanSubtask(
                        index=0,
                        title="First task with [ ] literal",
                        body="  First detail line\n  Second detail line",
                        completed=False,
                        line_number=4,
                    ),
                    PlanSubtask(
                        index=1,
                        title="Second task",
                        body="  Second detail",
                        completed=False,
                        line_number=8,
                    ),
                ],
            )

            first = next_pending_subtask(plan_path)
            self.assertEqual(first, subtasks[0])

            mark_subtask_completed(plan_path, 0)

            with plan_path.open("r", encoding="utf-8", newline="") as handle:
                updated = handle.read()
            expected_after_first = original.replace(
                "- [ ] First task with [ ] literal",
                "- [x] First task with [ ] literal",
                1,
            )
            self.assertEqual(updated, expected_after_first)

            # Idempotent re-marking of already completed subtask
            mark_subtask_completed(plan_path, 0)
            with plan_path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(handle.read(), expected_after_first)

            second = next_pending_subtask(plan_path)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.index, 1)
            self.assertEqual(second.title, "Second task")
            self.assertEqual(second.body, "  Second detail")
            self.assertEqual(second.line_number, 8)
            self.assertFalse(second.completed)

    def test_body_stops_at_headings_and_horizontal_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.md"
            plan_path.write_text(
                "## Phase 1\n"
                "- [ ] Task A\n"
                "  Line inside A\n"
                "\n"
                "## Section Two\n"
                "Unrelated prose under section two\n"
                "- [ ] Task B\n"
                "  Line inside B\n"
                "---\n"
                "Footer note after rule\n"
                "### Subsection\n"
                "- [ ] Task C\n",
                encoding="utf-8",
            )

            subtasks = parse_plan_subtasks(plan_path)
            self.assertEqual(len(subtasks), 3)
            self.assertEqual(subtasks[0].body, "  Line inside A")
            self.assertEqual(subtasks[1].body, "  Line inside B")
            self.assertEqual(subtasks[2].body, "")

    def test_all_completed_returns_none_and_handles_uppercase_x(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "done.md"
            plan_path.write_text(
                "# Done\n"
                "- [x] Lowercase x\n"
                "- [X] Uppercase X\n",
                encoding="utf-8",
            )

            subtasks = parse_plan_subtasks(plan_path)
            self.assertEqual(len(subtasks), 2)
            self.assertTrue(subtasks[0].completed)
            self.assertTrue(subtasks[1].completed)
            self.assertIsNone(next_pending_subtask(plan_path))

            # Idempotent on [X] as well
            mark_subtask_completed(plan_path, 1)
            self.assertIn("- [X] Uppercase X", plan_path.read_text(encoding="utf-8"))

            # Plan without checkboxes also returns None
            empty_plan = Path(tmp) / "empty.md"
            empty_plan.write_text("# No checkboxes here\nJust text.\n", encoding="utf-8")
            self.assertEqual(parse_plan_subtasks(empty_plan), [])
            self.assertIsNone(next_pending_subtask(empty_plan))

    def test_mark_subtask_completed_rejects_invalid_index_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.md"
            with self.assertRaises(FileNotFoundError):
                parse_plan_subtasks(missing)
            with self.assertRaises(FileNotFoundError):
                next_pending_subtask(missing)
            with self.assertRaises(FileNotFoundError):
                mark_subtask_completed(missing, 0)

            plan_path = Path(tmp) / "plan.md"
            plan_path.write_text("- [ ] Only task\n", encoding="utf-8")

            for bad_index in (-1, 1, 99):
                with self.assertRaises(IndexError):
                    mark_subtask_completed(plan_path, bad_index)

    def test_fenced_code_blocks_preserve_body_and_ignore_fake_checkboxes_and_headings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "fenced_plan.md"
            original = (
                "# Plan With Fences\n"
                "```markdown\n"
                "- [ ] preamble fake checkbox\n"
                "```\n"
                "\n"
                "## Tasks\n"
                "- [ ] First real task\n"
                "  Intro line\n"
                "```python\n"
                "# column-0 python comment\n"
                "---\n"
                "- [ ] fake checkbox inside backticks\n"
                "```\n"
                "  Middle line after backticks\n"
                "~~~~markdown\n"
                "```\n"
                "# column-0 heading inside tildes\n"
                "---\n"
                "- [ ] fake checkbox inside tildes\n"
                "~~~~\n"
                "  Final line of first task\n"
                "\n"
                "- [ ] Second real task\n"
                "  Detail for second task\n"
            )
            plan_path.write_text(original, encoding="utf-8")

            subtasks = parse_plan_subtasks(plan_path)
            self.assertEqual(len(subtasks), 2)
            self.assertEqual(subtasks[0].title, "First real task")
            self.assertEqual(subtasks[1].title, "Second real task")
            self.assertEqual(subtasks[0].line_number, 7)
            self.assertEqual(subtasks[1].line_number, 23)

            self.assertIn("# column-0 python comment", subtasks[0].body)
            self.assertIn("- [ ] fake checkbox inside backticks", subtasks[0].body)
            self.assertIn("  Middle line after backticks", subtasks[0].body)
            self.assertIn("# column-0 heading inside tildes", subtasks[0].body)
            self.assertIn("- [ ] fake checkbox inside tildes", subtasks[0].body)
            self.assertIn("  Final line of first task", subtasks[0].body)
            self.assertEqual(subtasks[1].body, "  Detail for second task")

            mark_subtask_completed(plan_path, 0)
            after_first = plan_path.read_text(encoding="utf-8")
            self.assertIn("- [x] First real task\n", after_first)
            self.assertIn("- [ ] preamble fake checkbox\n", after_first)
            self.assertIn("- [ ] fake checkbox inside backticks\n", after_first)
            self.assertIn("- [ ] fake checkbox inside tildes\n", after_first)
            self.assertIn("- [ ] Second real task\n", after_first)

            mark_subtask_completed(plan_path, 1)
            after_second = plan_path.read_text(encoding="utf-8")
            self.assertIn("- [x] Second real task\n", after_second)
            self.assertIn("- [ ] preamble fake checkbox\n", after_second)
            self.assertIn("- [ ] fake checkbox inside backticks\n", after_second)
            self.assertIn("- [ ] fake checkbox inside tildes\n", after_second)


if __name__ == "__main__":
    unittest.main()
