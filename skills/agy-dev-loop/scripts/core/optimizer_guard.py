"""Bounded learned-rules prompt mutator and offline benchmark guard."""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.memory import estimate_tokens

LEARNED_RULES_START = "<!-- LEARNED_RULES_START -->"
LEARNED_RULES_END = "<!-- LEARNED_RULES_END -->"


@dataclass(frozen=True)
class PromptRule:
    """Single bounded rule persisted inside the learned-rules prompt section."""

    rule_id: str
    text: str


def apply_bounded_rule_patch(
    prompt_path: Path,
    candidate_rules: list[PromptRule],
    max_rules: int = 12,
    max_tokens_est: int = 600,
) -> str:
    """Replace only the section between learned-rules markers within budget."""
    path = Path(prompt_path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()

    if text.count(LEARNED_RULES_START) != 1 or text.count(LEARNED_RULES_END) != 1:
        raise ValueError("Prompt must contain exactly one start and one end learned-rules marker")

    start_idx = text.index(LEARNED_RULES_START)
    end_idx = text.index(LEARNED_RULES_END)
    if end_idx < start_idx + len(LEARNED_RULES_START):
        raise ValueError("LEARNED_RULES_END precedes LEARNED_RULES_START")

    if max_rules < 0 or len(candidate_rules) > max_rules:
        raise ValueError(f"Rule count {len(candidate_rules)} exceeds max_rules={max_rules}")

    for rule in candidate_rules:
        if not rule.rule_id.strip() or not rule.text.strip():
            raise ValueError("PromptRule rule_id and text must be non-empty")
        if (
            LEARNED_RULES_START in rule.rule_id
            or LEARNED_RULES_END in rule.rule_id
            or LEARNED_RULES_START in rule.text
            or LEARNED_RULES_END in rule.text
        ):
            raise ValueError("PromptRule fields must not contain learned-rules markers")

    rendered_rules = "\n".join(f"- [{rule.rule_id}] {rule.text}" for rule in candidate_rules)
    if candidate_rules and estimate_tokens(rendered_rules) > max_tokens_est:
        raise ValueError(
            f"Rendered rules exceed token budget: {estimate_tokens(rendered_rules)} > {max_tokens_est}"
        )

    inner = f"\n{rendered_rules}\n" if rendered_rules else "\n"
    updated = text[: start_idx + len(LEARNED_RULES_START)] + inner + text[end_idx:]
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return updated


def _run_suite(cmd: list[str], cwd: Path) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def run_offline_benchmarks(benchmarks_root: Path) -> tuple[bool, dict[str, bool]]:
    """Run offline Python and Node test suites under `benchmarks_root`."""
    root = Path(benchmarks_root)
    results = {
        "py_fixture": _run_suite(
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
            root / "py_fixture",
        ),
        "js_fixture": _run_suite(["node", "--test"], root / "js_fixture"),
    }
    return all(results.values()), results


def stage_and_validate_global_patch(
    prompt_path: Path,
    candidate_rules: list[PromptRule],
    benchmarks_root: Path,
    max_rules: int = 12,
    max_tokens_est: int = 600,
) -> tuple[bool, str]:
    """Patch learned rules, run offline benchmarks, and roll back on failure."""
    path = Path(prompt_path)
    original_bytes = path.read_bytes()
    original_text = original_bytes.decode("utf-8")
    ok = False
    try:
        updated_text = apply_bounded_rule_patch(
            path,
            candidate_rules,
            max_rules=max_rules,
            max_tokens_est=max_tokens_est,
        )
        passed, results = run_offline_benchmarks(benchmarks_root)
        if not passed:
            failed = ", ".join(name for name, status in results.items() if not status)
            return False, f"Offline benchmark failure: {failed}"
        ok = True
        diff = "".join(
            difflib.unified_diff(
                original_text.splitlines(keepends=True),
                updated_text.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
        return True, diff or f"Applied {len(candidate_rules)} rule(s) cleanly."
    finally:
        if not ok:
            path.write_bytes(original_bytes)
