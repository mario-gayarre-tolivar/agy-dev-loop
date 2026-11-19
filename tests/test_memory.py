"""Unit tests for `core.memory`."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "agy-dev-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.memory import (  # noqa: E402
    Lesson,
    RunMetrics,
    estimate_tokens,
    load_lessons,
    record_run_metrics,
    should_trigger_global_optimization,
    upsert_and_prune_lessons,
)


class MemoryTests(unittest.TestCase):
    """Tests for bounded lessons memory and run telemetry persistence."""

    def test_estimate_tokens_formula(self) -> None:
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("abc"), 1)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("a" * 80), 20)

    def test_prunes_lessons_above_max_count_and_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lessons_path = Path(tmp) / ".agents" / "loop" / "lessons.md"
            self.assertEqual(load_lessons(lessons_path), [])

            lessons = [
                Lesson(
                    lesson_id=f"L{idx:02d}",
                    symptom=f"Symptom {idx:02d}: " + ("detail " * 8).strip(),
                    rule=f"Rule {idx:02d}: " + ("action " * 8).strip(),
                    hits=idx,
                    last_run=f"2026-11-{10 + (idx % 10):02d}",
                )
                for idx in range(1, 16)
            ]

            kept = upsert_and_prune_lessons(
                lessons_path,
                lessons,
                max_lessons=12,
                max_tokens_est=600,
            )

            self.assertLessEqual(len(kept), 12)
            self.assertGreater(len(kept), 0)

            file_text = lessons_path.read_text(encoding="utf-8")
            self.assertLessEqual(estimate_tokens(file_text), 600)

            expected_top_ids = [
                lesson.lesson_id
                for lesson in sorted(
                    lessons,
                    key=lambda item: (item.hits, item.last_run),
                    reverse=True,
                )[: len(kept)]
            ]
            self.assertEqual([lesson.lesson_id for lesson in kept], expected_top_ids)
            self.assertEqual(kept[0].lesson_id, "L15")

            reloaded = load_lessons(lessons_path)
            self.assertEqual(reloaded, kept)

    def test_upsert_deduplicates_and_increments_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lessons_path = Path(tmp) / "lessons.md"
            initial = [
                Lesson(
                    lesson_id="L01",
                    symptom="Missing __init__.py in tests",
                    rule="Ensure test package has __init__.py",
                    hits=2,
                    last_run="2026-11-10",
                ),
                Lesson(
                    lesson_id="L02",
                    symptom="Unpinned timeout",
                    rule="Pass timeout to subprocess.run",
                    hits=1,
                    last_run="2026-11-11",
                ),
            ]
            upsert_and_prune_lessons(lessons_path, initial)

            update = [
                Lesson(
                    lesson_id="L99",
                    symptom="  missing __init__.py in tests ",
                    rule="ensure test package has __init__.py  ",
                    hits=3,
                    last_run="2026-11-18",
                ),
                Lesson(
                    lesson_id="L03",
                    symptom="Dirty git status",
                    rule="Verify clean tree before edits",
                    hits=1,
                    last_run="2026-11-19",
                ),
            ]
            kept = upsert_and_prune_lessons(lessons_path, update)

            self.assertEqual(len(kept), 3)
            merged = next(item for item in kept if item.lesson_id == "L01")
            self.assertEqual(merged.hits, 5)
            self.assertEqual(merged.last_run, "2026-11-18")
            self.assertFalse(any(item.lesson_id == "L99" for item in kept))

            reloaded = load_lessons(lessons_path)
            self.assertEqual(reloaded, kept)

    def test_markdown_escaping_and_oversized_single_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lessons_path = Path(tmp) / "lessons.md"
            tricky = Lesson(
                lesson_id="L|1",
                symptom="Pipe | inside\nmultiline symptom",
                rule="Escape | and\nnewlines cleanly",
                hits=4,
                last_run="2026-11-19",
            )
            kept = upsert_and_prune_lessons(lessons_path, [tricky])
            reloaded = load_lessons(lessons_path)
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0].lesson_id, "L|1")
            self.assertEqual(reloaded[0].symptom, "Pipe | inside multiline symptom")
            self.assertEqual(reloaded[0].rule, "Escape | and newlines cleanly")
            self.assertEqual(kept, reloaded)

            oversized_path = Path(tmp) / "oversized.md"
            huge = Lesson(
                lesson_id="L_BIG",
                symptom="S " * 600,
                rule="R " * 600,
                hits=10,
                last_run="2026-11-19",
            )
            kept_huge = upsert_and_prune_lessons(
                oversized_path,
                [huge],
                max_lessons=12,
                max_tokens_est=120,
            )
            self.assertEqual(len(kept_huge), 1)
            rendered = oversized_path.read_text(encoding="utf-8")
            self.assertLessEqual(estimate_tokens(rendered), 120)
            self.assertEqual(load_lessons(oversized_path), kept_huge)

    def test_record_run_metrics_and_global_optimization_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metrics_dir = Path(tmp) / ".agents" / "loop" / "runs"
            self.assertFalse(should_trigger_global_optimization(metrics_dir))

            def make_metrics(run_id: str) -> RunMetrics:
                return RunMetrics(
                    run_id=run_id,
                    repo_name="agy-dev-loop",
                    subtasks_total=5,
                    subtasks_passed=5,
                    iterations=6,
                    rollbacks=1,
                    reviewer_rejections=0,
                    failure_fingerprints=("fp-timeout", "fp-lint"),
                    status="passed",
                )

            p1 = record_run_metrics(metrics_dir, make_metrics("run-001"))
            self.assertTrue(p1.exists())
            self.assertFalse(should_trigger_global_optimization(metrics_dir))

            record_run_metrics(metrics_dir, make_metrics("run-002"))
            self.assertFalse(should_trigger_global_optimization(metrics_dir))

            p3 = record_run_metrics(metrics_dir, make_metrics("run-003"))
            self.assertTrue(should_trigger_global_optimization(metrics_dir))

            payload = json.loads(p3.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "run-003")
            self.assertEqual(payload["failure_fingerprints"], ["fp-timeout", "fp-lint"])
            self.assertEqual(payload["status"], "passed")

            for bad_id in ("../evil", ".", "..", "-leading", "", "sub/dir", "bad id"):
                with self.subTest(bad_id=bad_id):
                    with self.assertRaises(ValueError):
                        record_run_metrics(metrics_dir, make_metrics(bad_id))


if __name__ == "__main__":
    unittest.main()
