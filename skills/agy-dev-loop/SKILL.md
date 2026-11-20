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

## 1. Bootstrap & Doctor (`.agents/loop.json`)

Before running the inner loop on a repository, bootstrap `.agents/loop.json` with `cli.py init` (wraps stack auto-detection and `init_manifest` with hydrated worktree env dry-run):

```bash
python3 <path-to-skill>/scripts/cli.py init --repo <abs-repo-path>
```

Auto-detected stacks: Python (`pytest`, `unittest`, Django `manage.py`), Node/JS (`npm`, `pnpm`, `yarn`, `bun`, `node --test`), Go (`go.mod`), Rust (`Cargo.toml`), Java (`pom.xml`/Maven wrapper, Gradle), and .NET (`*.sln`/`*.csproj`).

Commit `.agents/loop.json` and add `.agents/worktrees/` and `.agents/loop/` to `.gitignore` so `ensure_clean_repo` passes across runs (teams tracking `.agents/loop/lessons.md` should ignore `.agents/loop/telemetry/` instead and commit `lessons.md` before each run).

Verify readiness cleanly (runs dry-run inside a temporary worktree without dirtying the repo):

```bash
python3 <path-to-skill>/scripts/cli.py doctor --repo <abs-repo-path>
```

`.agents/loop.json` schema:
- `test_cmd` (required): non-interactive command exiting `0` on passing tests.
- `lint_cmd`, `build_cmd`, `setup_cmd` (optional): additional checks or worktree bootstrap command (runs once after worktree creation).
- `protected_test_globs` (default covers `tests/**`, `test/**`, Python, JS/TS, Go, Rust, Java, and C# test patterns).
- `max_iter` (default: `5`), `circuit_breaker` (default: `3`).
- `verify_red` (default: `true`): set `false` for non-TDD tasks (e.g., config/schema validation).
- `test_timeout_sec` (default: `120`): timeout for `setup_cmd`, `verify_red`, and gate commands.
- `assert_regex` (optional): custom regex overriding built-in assertion detection in `git_guard`.

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
- Hydrates worktree env (`PATH` for `.venv`/`venv` and `node_modules/.bin`, `VIRTUAL_ENV`, `NODE_PATH`), runs optional `setup_cmd`, verifies `test_cmd` dry-run inside the worktree, and cleans stray dry-run artifacts.
- Runs Executor on `workspace="inherit"` (`flash` -> `pro` on repeated error fingerprint or 2 consecutive failures; aborts at `circuit_breaker`).
- Short-circuits the Reviewer (`model="inherit"`) until `git_guard`, `verify_red`, `test_cmd`, `lint_cmd`, and `build_cmd` all pass.
- Gives the Reviewer only the subtask, `git diff`, and deterministic gate output (never Executor history).
- On approval, marks the subtask `[x]` and creates a checkpoint commit; on failure, rolls back cleanly to the last checkpoint.
- Writes run telemetry JSON to `.agents/loop/telemetry/` and updates `.agents/loop/lessons.md` (`<= 12` lessons, `<= 600` estimated tokens).

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

By default (`apply_approved` omitted or `False`), `outer_loop.py` **never mutates** active prompt files:
1. Optimizer proposes `<= 12` bounded rules (`<= 600` tokens) targeting `executor.md` or `reviewer.md`.
2. `stage_and_validate_global_patch` validates markers, budgets, and self-contained offline benchmarks (`<path-to-skill>/benchmarks/py_fixture` and `js_fixture`), generating a unified diff.
3. The active prompt file is restored byte-for-byte, returning `status: "staged_pending_approval"` with `proposed_diff` and `rationale`.

Only after explicit human review, re-run `outer_loop.py` with `"apply_approved": True` and `"approved_proposal"` set to the reviewed proposal object to persist rules into `## Reglas Aprendidas`.
