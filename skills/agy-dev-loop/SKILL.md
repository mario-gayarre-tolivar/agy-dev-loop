---
name: agy-dev-loop
description: >-
  Runs plan-driven Micro-TDD development in an isolated git worktree with
  deterministic verification gates and a short-circuited reviewer, plus a
  telemetry-governed outer loop that stages benchmark-validated prompt rule
  patches for explicit human approval. Use when executing multi-subtask
  implementation plans or optimizing loop rules from accumulated run telemetry.
---

# agy-dev-loop

Plan-driven development loop for Google Antigravity (`agy`) built purely on Python stdlib (`>=3.12`), Git worktrees, and `run_workflow`.

## 1. Bootstrap `.agents/loop.json`

Before running the inner loop on a repository, ensure the working tree is clean and initialize `.agents/loop.json` with a dry-run check of the project's test command:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path("<path-to-skill>/scripts").resolve()))
from core.manifest import init_manifest

manifest_path = init_manifest(Path("<repo-root>"), run_dry_check=True)
```

`.agents/loop.json` schema:
- `test_cmd` (required): command that exits `0` on passing tests.
- `lint_cmd`, `build_cmd` (optional): additional deterministic checks.
- `protected_test_globs` (default: `["tests/**", "test/**", "**/*.test.*", "**/*.spec.*"]`).
- `max_iter` (default: `5`), `circuit_breaker` (default: `3`).

Commit `.agents/loop.json` (and ensure `.agents/worktrees/` and `.agents/loop/telemetry/` are in `.gitignore`) so `ensure_clean_repo` passes before launching the loop.

## 2. Run Inner Loop (`inner_loop.py`)

Execute a markdown plan containing `- [ ]` subtasks one-by-one in an isolated worktree (`agy-loop/<run_id>`):

```python
run_workflow(
    ScriptPath="<path-to-skill>/scripts/inner_loop.py",
    Description="Execute plan subtasks with agy-dev-loop",
    Args={
        "repo_root": "<abs-repo-path>",
        "plan_path": "docs/plans/YYYY-MM-DD-feature.md",
    },
)
```

Behavior:
- Validates `.agents/loop.json` and a clean git working tree before creating `.agents/worktrees/<run_id>`.
- Runs Executor on `workspace="inherit"` (starting on `flash`, escalating to `pro` on repeated error fingerprint or 2 consecutive failures; aborts at `circuit_breaker`).
- Short-circuits the Reviewer (`model="inherit"`) until `git_guard`, `RED` verification, `test_cmd`, `lint_cmd`, and `build_cmd` all pass.
- Gives the Reviewer only the subtask, `git diff`, and deterministic gate output (never Executor history).
- On approval, marks the subtask `[x]` and creates a checkpoint commit; on failure, rolls back cleanly to the last checkpoint.
- Writes run telemetry JSON to `.agents/loop/telemetry/` and updates `.agents/loop/lessons.md` (pruned to `<= 12` lessons and `<= 600` estimated tokens).

## 3. Run Outer Loop (`outer_loop.py`)

Once at least `3` telemetry runs accumulate in `metrics_dir`, run the retrospective optimizer in staging mode:

```python
run_workflow(
    ScriptPath="<path-to-skill>/scripts/outer_loop.py",
    Description="Propose benchmark-validated global rules from loop telemetry",
    Args={
        "metrics_dir": "<abs-repo-path>/.agents/loop/telemetry",
    },
)
```

## 4. Human Approval Gate

By default (`apply_approved` omitted or `False`), `outer_loop.py` **never mutates** the active prompt files:
1. The Optimizer proposes `<= 12` bounded rules (`<= 600` tokens) targeting `executor.md` or `reviewer.md`.
2. `stage_and_validate_global_patch` validates markers, budgets, and offline benchmarks (`benchmarks/py_fixture` and `benchmarks/js_fixture`), generating a unified diff.
3. The active prompt file is restored byte-for-byte to its original content, and the workflow returns `status: "staged_pending_approval"` with `proposed_diff` and `rationale`.

Only after a human explicitly reviews and approves `proposed_diff`, re-run `outer_loop.py` with `"apply_approved": True` and `"approved_proposal"` set to the reviewed proposal object to validate and promote the rules into `## Reglas Aprendidas`.
