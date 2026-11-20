"""Pure workflow orchestration logic, schemas, and prompt builders for agy-dev-loop."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.eval_runner import (
    compute_error_fingerprint,
    decide_next_action,
    run_deterministic_gate,
)
from core.git_guard import (
    build_worktree_env,
    capture_worktree_diff,
    checkpoint_commit,
    create_isolated_worktree,
    ensure_clean_repo,
    remove_isolated_worktree,
    rollback_to_ref,
)
from core.manifest import load_manifest, verify_manifest_test_cmd
from core.memory import (
    Lesson,
    RunMetrics,
    record_run_metrics,
    should_trigger_global_optimization,
    upsert_and_prune_lessons,
)
from core.optimizer_guard import PromptRule, stage_and_validate_global_patch
from core.plan_tasks import (
    mark_subtask_completed,
    next_pending_subtask,
    parse_plan_subtasks,
)

SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARKS_ROOT = SKILL_ROOT / "benchmarks"
ALLOWED_TARGET_PROMPTS = ("executor.md", "reviewer.md")

EXECUTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["subtask_index", "files_changed", "test_files_touched", "summary"],
    "properties": {
        "subtask_index": {"type": "integer"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "test_files_touched": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

REVIEWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["approved", "blocking_issues", "lesson_candidate"],
    "properties": {
        "approved": {"type": "boolean"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "lesson_candidate": {
            "type": ["object", "null"],
            "required": ["symptom", "rule"],
            "properties": {
                "symptom": {"type": "string"},
                "rule": {"type": "string"},
            },
        },
    },
}

OPTIMIZER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["target_prompt", "candidate_rules", "rationale"],
    "properties": {
        "target_prompt": {"type": "string", "enum": ["executor.md", "reviewer.md"]},
        "candidate_rules": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["rule_id", "text"],
                "properties": {
                    "rule_id": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "rationale": {"type": "string"},
    },
}


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git_head_sha(worktree_path: Path) -> str:
    proc = _run_git(worktree_path, "rev-parse", "HEAD")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"git rev-parse HEAD failed: {detail}")
    return proc.stdout.strip()


def _has_worktree_changes(worktree_path: Path) -> bool:
    proc = _run_git(worktree_path, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _list_worktree_paths(worktree_path: Path, *git_args: str) -> set[str]:
    proc = _run_git(worktree_path, "-c", "core.quotePath=false", *git_args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"git {' '.join(git_args)} failed: {detail}")
    return {line for line in proc.stdout.splitlines() if line.strip()}


def _run_setup_cmd(
    worktree_path: Path,
    setup_cmd: str,
    timeout_sec: int,
    env: dict[str, str],
) -> None:
    """Execute `setup_cmd` inside the isolated worktree with hydrated environment."""
    try:
        proc = subprocess.run(
            setup_cmd,
            cwd=worktree_path,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            f"Worktree setup_cmd failed to execute ({setup_cmd}): {exc}"
        ) from exc

    if proc.returncode != 0:
        output = "\n".join(
            part
            for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip())
            if part
        )
        raise RuntimeError(
            f"Worktree setup_cmd failed (exit {proc.returncode}): {setup_cmd}"
            + (f"\n{output}" if output else "")
        )


def _clean_dry_run_artifacts(
    worktree_path: Path,
    pre_untracked: set[str],
    pre_tracked: set[str],
) -> None:
    """Remove stray dry-run files/edits while preserving `setup_cmd` outputs."""
    post_untracked = _list_worktree_paths(
        worktree_path, "ls-files", "--others", "--exclude-standard"
    )
    for rel in sorted(post_untracked - pre_untracked):
        target = worktree_path / rel
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        parent = target.parent
        while parent != worktree_path and worktree_path in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    post_tracked = _list_worktree_paths(worktree_path, "diff", "--name-only")
    stray_tracked = post_tracked - pre_tracked
    if stray_tracked:
        checkout_args = (
            ("checkout", "--", ".")
            if not pre_tracked
            else ("checkout", "--", *sorted(stray_tracked))
        )
        proc = _run_git(worktree_path, *checkout_args)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"git checkout failed after dry-run: {detail}")


def _read_text_if_exists(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"args['{field_name}'] must be an integer >= 1")
    return value


def build_executor_prompt(
    template_text: str,
    worktree_path: Path,
    subtask_index: int,
    subtask_title: str,
    subtask_body: str,
    lessons_text: str,
    last_feedback: str = "",
) -> str:
    """Build the Executor prompt for a single subtask attempt."""
    sections = [
        template_text.strip(),
        f"## Worktree\n`{worktree_path}`",
        (
            f"## Subtask #{subtask_index}: {subtask_title}\n"
            + (subtask_body.strip() or "(no additional details)")
        ),
        "## Local Lessons\n" + (lessons_text.strip() or "(none)"),
    ]
    if last_feedback.strip():
        sections.append(f"## Previous Attempt Feedback\n{last_feedback.strip()}")
    return "\n\n".join(sections) + "\n"


def build_reviewer_prompt(
    template_text: str,
    subtask_title: str,
    subtask_body: str,
    git_diff: str,
    gate_output: str,
) -> str:
    """Build the isolated Reviewer prompt without any Executor conversation history."""
    sections = [
        template_text.strip(),
        f"## Subtask: {subtask_title}\n" + (subtask_body.strip() or "(no additional details)"),
        f"## Git Diff\n```diff\n{git_diff.strip()}\n```",
        f"## Deterministic Gate Output\n```\n{gate_output.strip()}\n```",
    ]
    return "\n\n".join(sections) + "\n"


def build_optimizer_prompt(
    template_text: str,
    metrics_runs: list[dict[str, Any]],
    lessons_text: str,
    current_prompts: dict[str, str],
) -> str:
    """Build the Optimizer prompt grounded in run telemetry and local lessons."""
    fp_counts: Counter[str] = Counter()
    for run in metrics_runs:
        for fp in run.get("failure_fingerprints") or ():
            if isinstance(fp, str) and fp.strip():
                fp_counts[fp.strip()] += 1

    fp_summary = (
        "\n".join(f"- `{fp}`: {count} hit(s)" for fp, count in fp_counts.most_common(10))
        if fp_counts
        else "(none)"
    )
    prompts_block = "\n\n".join(
        f"### {name}\n```markdown\n{content.strip()}\n```"
        for name, content in sorted(current_prompts.items())
    )
    sections = [
        template_text.strip(),
        f"## Repeated Failure Fingerprints\n{fp_summary}",
        f"## Run Telemetry ({len(metrics_runs)} runs)\n```json\n{json.dumps(metrics_runs, indent=2)}\n```",
        "## Local Lessons\n" + (lessons_text.strip() or "(none)"),
        f"## Current Prompts\n{prompts_block}",
    ]
    return "\n\n".join(sections) + "\n"


def _extract_lesson(review: Any, run_date: str) -> Lesson | None:
    if not isinstance(review, dict):
        return None
    candidate = review.get("lesson_candidate")
    if not isinstance(candidate, dict):
        return None
    raw_symptom = candidate.get("symptom")
    raw_rule = candidate.get("rule")
    if not isinstance(raw_symptom, str) or not isinstance(raw_rule, str):
        return None
    symptom = raw_symptom.strip()
    rule = raw_rule.strip()
    if not symptom or not rule:
        return None
    lesson_id = f"L-{compute_error_fingerprint(symptom, rule)[:8]}"
    return Lesson(
        lesson_id=lesson_id,
        symptom=symptom,
        rule=rule,
        hits=1,
        last_run=run_date,
    )


def _normalize_blocking_issues(raw_issues: Any) -> list[str]:
    if isinstance(raw_issues, list):
        return [
            item.strip()
            for item in raw_issues
            if isinstance(item, str) and item.strip()
        ]
    if isinstance(raw_issues, str) and raw_issues.strip():
        return [raw_issues.strip()]
    return []


async def run_inner_loop(
    args_dict: dict[str, Any],
    agent_fn: Callable[..., Any],
    phase_fn: Callable[[str], Any],
    log_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Run the isolated subtask-by-subtask inner development loop."""
    raw_repo_root = args_dict.get("repo_root")
    if not raw_repo_root:
        raise ValueError("args['repo_root'] is required")
    raw_plan_path = args_dict.get("plan_path")
    if not raw_plan_path:
        raise ValueError("args['plan_path'] is required")

    repo_root = Path(raw_repo_root).resolve()
    plan_input = Path(raw_plan_path)
    source_plan_path = (
        plan_input if plan_input.is_absolute() else (repo_root / plan_input)
    ).resolve()
    if not source_plan_path.is_file():
        raise ValueError(f"Plan file does not exist: {source_plan_path}")

    run_id = str(
        args_dict.get("run_id")
        or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    )
    prompts_dir = (
        Path(args_dict["prompts_dir"]).resolve()
        if args_dict.get("prompts_dir")
        else (SKILL_ROOT / "prompts")
    )
    metrics_dir = (
        Path(args_dict["metrics_dir"]).resolve()
        if args_dict.get("metrics_dir")
        else (repo_root / ".agents" / "loop" / "telemetry")
    )
    lessons_path = (
        Path(args_dict["lessons_path"]).resolve()
        if args_dict.get("lessons_path")
        else (repo_root / ".agents" / "loop" / "lessons.md")
    )
    verify_red_arg = (
        bool(args_dict["verify_red"])
        if "verify_red" in args_dict and args_dict["verify_red"] is not None
        else None
    )
    min_runs = _require_positive_int(args_dict.get("min_runs", 3), "min_runs")

    phase_fn("Setup")
    manifest = load_manifest(repo_root)
    ensure_clean_repo(repo_root)
    executor_template = (prompts_dir / "executor.md").read_text(encoding="utf-8")
    reviewer_template = (prompts_dir / "reviewer.md").read_text(encoding="utf-8")

    worktree_path = create_isolated_worktree(repo_root, run_id)
    wt_env = build_worktree_env(repo_root)
    try:
        if manifest.setup_cmd:
            _run_setup_cmd(
                worktree_path,
                manifest.setup_cmd,
                timeout_sec=manifest.test_timeout_sec,
                env=wt_env,
            )
        pre_dry_untracked = _list_worktree_paths(
            worktree_path, "ls-files", "--others", "--exclude-standard"
        )
        pre_dry_tracked = _list_worktree_paths(worktree_path, "diff", "--name-only")
        verify_manifest_test_cmd(worktree_path, manifest, env=wt_env)
        _clean_dry_run_artifacts(worktree_path, pre_dry_untracked, pre_dry_tracked)
    except Exception:
        remove_isolated_worktree(repo_root, worktree_path, run_id)
        raise

    log_fn(f"Created isolated worktree at {worktree_path} on branch agy-loop/{run_id}")

    try:
        rel_plan = source_plan_path.relative_to(repo_root)
        wt_plan = worktree_path / rel_plan
        active_plan_path = wt_plan if wt_plan.is_file() else source_plan_path
    except ValueError:
        active_plan_path = source_plan_path

    subtasks_total = len(parse_plan_subtasks(active_plan_path))
    iterations = 0
    rollbacks = 0
    reviewer_rejections = 0
    failure_fingerprints: list[str] = []
    collected_lessons: list[Lesson] = []
    commits: list[str] = []
    status = "completed"
    abort_reason = ""
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    while (subtask := next_pending_subtask(active_plan_path)) is not None:
        phase_fn(f"Subtask {subtask.index}: {subtask.title}")
        base_ref = _git_head_sha(worktree_path)
        current_tier = "flash"
        consecutive_failures = 0
        subtask_fingerprints: list[str] = []
        last_feedback = ""
        subtask_done = False
        lessons_text = _read_text_if_exists(lessons_path)

        for attempt in range(1, manifest.max_iter + 1):
            iterations += 1
            exec_prompt = build_executor_prompt(
                template_text=executor_template,
                worktree_path=worktree_path,
                subtask_index=subtask.index,
                subtask_title=subtask.title,
                subtask_body=subtask.body,
                lessons_text=lessons_text,
                last_feedback=last_feedback,
            )
            await agent_fn(
                prompt=exec_prompt,
                schema=EXECUTOR_SCHEMA,
                role=f"Executor subtask {subtask.index} (attempt {attempt}, {current_tier})",
                workspace="inherit",
                model=current_tier,
            )

            gate = run_deterministic_gate(
                worktree_path=worktree_path,
                manifest=manifest,
                base_ref=base_ref,
                verify_red=verify_red_arg,
                env=wt_env,
            )

            if not gate.passed or not gate.should_run_reviewer:
                consecutive_failures += 1
                fp = gate.error_fingerprint
                if fp:
                    failure_fingerprints.append(fp)
                action = decide_next_action(
                    current_tier=current_tier,
                    consecutive_failures=consecutive_failures,
                    previous_fingerprints=subtask_fingerprints,
                    new_fingerprint=fp,
                    circuit_breaker=manifest.circuit_breaker,
                )
                if fp:
                    subtask_fingerprints.append(fp)
                rollback_to_ref(worktree_path, base_ref)
                rollbacks += 1
                last_feedback = (
                    f"Deterministic gate failed at stage '{gate.failed_stage}':\n"
                    f"{gate.output}"
                )
                log_fn(
                    f"Subtask {subtask.index} attempt {attempt} failed gate "
                    f"({gate.failed_stage}); rolled back. Action: {action}"
                )
                if action == "abort_circuit_breaker":
                    status = "aborted_circuit_breaker"
                    abort_reason = (
                        f"Circuit-breaker triggered on subtask {subtask.index} "
                        f"at stage '{gate.failed_stage}'"
                    )
                    break
                if action == "escalate_pro":
                    current_tier = "pro"
                continue

            git_diff = capture_worktree_diff(worktree_path, base_ref)
            rev_prompt = build_reviewer_prompt(
                template_text=reviewer_template,
                subtask_title=subtask.title,
                subtask_body=subtask.body,
                git_diff=git_diff,
                gate_output=gate.output,
            )
            review = await agent_fn(
                prompt=rev_prompt,
                schema=REVIEWER_SCHEMA,
                role=f"Reviewer subtask {subtask.index} (attempt {attempt})",
                workspace="inherit",
                model="inherit",
            )

            lesson = _extract_lesson(review, run_date)
            if lesson is not None:
                collected_lessons.append(lesson)

            if isinstance(review, dict) and review.get("approved") is True:
                mark_subtask_completed(active_plan_path, subtask.index)
                if _has_worktree_changes(worktree_path):
                    sha = checkpoint_commit(
                        worktree_path,
                        f"feat(loop): subtask {subtask.index} - {subtask.title}",
                    )
                    commits.append(sha)
                subtask_done = True
                log_fn(f"Subtask {subtask.index} approved and checkpointed.")
                break

            reviewer_rejections += 1
            consecutive_failures += 1
            raw_issues = review.get("blocking_issues") if isinstance(review, dict) else None
            issues = _normalize_blocking_issues(raw_issues)
            issues_text = (
                "\n".join(f"- {item}" for item in issues)
                or "Reviewer rejected without blocking_issues."
            )
            fp = compute_error_fingerprint(issues_text, git_diff)
            failure_fingerprints.append(fp)
            action = decide_next_action(
                current_tier=current_tier,
                consecutive_failures=consecutive_failures,
                previous_fingerprints=subtask_fingerprints,
                new_fingerprint=fp,
                circuit_breaker=manifest.circuit_breaker,
            )
            subtask_fingerprints.append(fp)
            rollback_to_ref(worktree_path, base_ref)
            rollbacks += 1
            last_feedback = f"Reviewer rejected the diff:\n{issues_text}"
            log_fn(
                f"Subtask {subtask.index} attempt {attempt} rejected by reviewer; "
                f"rolled back. Action: {action}"
            )
            if action == "abort_circuit_breaker":
                status = "aborted_circuit_breaker"
                abort_reason = (
                    f"Circuit-breaker triggered after reviewer rejection "
                    f"on subtask {subtask.index}"
                )
                break
            if action == "escalate_pro":
                current_tier = "pro"

        if not subtask_done:
            if status == "completed":
                status = "aborted_max_iter"
                abort_reason = (
                    f"Max iterations ({manifest.max_iter}) reached on subtask {subtask.index}"
                )
            break

    phase_fn("Finalize")
    subtasks_passed = sum(
        1 for item in parse_plan_subtasks(active_plan_path) if item.completed
    )
    if collected_lessons:
        upsert_and_prune_lessons(lessons_path, collected_lessons)

    metrics = RunMetrics(
        run_id=run_id,
        repo_name=repo_root.name,
        subtasks_total=subtasks_total,
        subtasks_passed=subtasks_passed,
        iterations=iterations,
        rollbacks=rollbacks,
        reviewer_rejections=reviewer_rejections,
        failure_fingerprints=tuple(failure_fingerprints),
        status=status,
    )
    metrics_path = record_run_metrics(metrics_dir, metrics)
    ready_for_optimization = should_trigger_global_optimization(
        metrics_dir, min_runs=min_runs
    )
    if ready_for_optimization:
        log_fn(
            f"Global optimization ready ({metrics_dir} has >= {min_runs} runs). "
            "Run outer_loop.py to stage a benchmark-validated prompt patch."
        )
    else:
        log_fn(
            f"Recorded metrics at {metrics_path}. "
            f"Global optimization threshold ({min_runs} runs) not yet met."
        )

    return {
        "run_id": run_id,
        "status": status,
        "abort_reason": abort_reason,
        "branch": f"agy-loop/{run_id}",
        "worktree_path": str(worktree_path),
        "plan_path": str(active_plan_path),
        "commits": commits,
        "subtasks_total": subtasks_total,
        "subtasks_passed": subtasks_passed,
        "iterations": iterations,
        "rollbacks": rollbacks,
        "reviewer_rejections": reviewer_rejections,
        "failure_fingerprints": failure_fingerprints,
        "metrics_path": str(metrics_path),
        "lessons_path": str(lessons_path),
        "lessons_added": len(collected_lessons),
        "should_trigger_global_optimization": ready_for_optimization,
    }


def _parse_proposal(proposal: Any) -> tuple[str, list[PromptRule], str]:
    if not isinstance(proposal, dict):
        raise ValueError("Optimizer proposal must be a JSON object")
    target_prompt = proposal.get("target_prompt")
    if target_prompt not in ALLOWED_TARGET_PROMPTS:
        raise ValueError(f"Invalid target_prompt from optimizer: {target_prompt!r}")
    raw_rules = proposal.get("candidate_rules")
    if not isinstance(raw_rules, list):
        raise ValueError("Optimizer candidate_rules must be a list")

    rules: list[PromptRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError("Each candidate rule must contain 'rule_id' and 'text'")
        rule_id = item.get("rule_id")
        text = item.get("text")
        if not isinstance(rule_id, str) or not isinstance(text, str):
            raise ValueError("Each candidate rule must have string 'rule_id' and 'text'")
        rules.append(PromptRule(rule_id=rule_id, text=text))

    raw_rationale = proposal.get("rationale")
    if not isinstance(raw_rationale, str):
        raise ValueError("Optimizer rationale must be a string")
    rationale = raw_rationale.strip()
    return target_prompt, rules, rationale


async def run_outer_loop(
    args_dict: dict[str, Any],
    agent_fn: Callable[..., Any],
    phase_fn: Callable[[str], Any],
    log_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Run the telemetry-governed outer loop with benchmark validation and human gate."""
    if args_dict.get("metrics_dir"):
        metrics_dir = Path(args_dict["metrics_dir"]).resolve()
    elif args_dict.get("repo_root"):
        metrics_dir = Path(args_dict["repo_root"]).resolve() / ".agents" / "loop" / "telemetry"
    else:
        raise ValueError("args['metrics_dir'] or args['repo_root'] is required")

    min_runs = _require_positive_int(args_dict.get("min_runs", 3), "min_runs")
    prompts_dir = (
        Path(args_dict["prompts_dir"]).resolve()
        if args_dict.get("prompts_dir")
        else (SKILL_ROOT / "prompts")
    )
    benchmarks_root = (
        Path(args_dict["benchmarks_root"]).resolve()
        if args_dict.get("benchmarks_root")
        else DEFAULT_BENCHMARKS_ROOT
    )
    if args_dict.get("lessons_path"):
        lessons_path = Path(args_dict["lessons_path"]).resolve()
    elif args_dict.get("repo_root"):
        lessons_path = Path(args_dict["repo_root"]).resolve() / ".agents" / "loop" / "lessons.md"
    else:
        lessons_path = metrics_dir.parent / "lessons.md"

    apply_approved = args_dict.get("apply_approved") is True
    if apply_approved and not isinstance(args_dict.get("approved_proposal"), dict):
        raise ValueError("approved_proposal dict is required when apply_approved is True")
    max_rules = int(args_dict.get("max_rules", 12))
    max_tokens_est = int(args_dict.get("max_tokens_est", 600))

    phase_fn("Telemetry")
    if not should_trigger_global_optimization(metrics_dir, min_runs=min_runs):
        existing_runs = len(list(metrics_dir.glob("*.json"))) if metrics_dir.exists() else 0
        log_fn(
            f"Skipping global optimization: {existing_runs}/{min_runs} runs in {metrics_dir}."
        )
        return {
            "status": "skipped_insufficient_runs",
            "applied": False,
            "requires_human_approval": False,
            "runs_analyzed": existing_runs,
            "min_runs": min_runs,
            "target_prompt": None,
            "candidate_rules": [],
            "proposed_diff": "",
            "rationale": "",
        }

    metrics_runs: list[dict[str, Any]] = []
    for json_file in sorted(metrics_dir.glob("*.json")):
        try:
            loaded = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log_fn(f"Skipping unreadable telemetry file {json_file.name}: {exc}")
            continue
        if isinstance(loaded, dict):
            metrics_runs.append(loaded)

    if len(metrics_runs) < min_runs:
        log_fn(
            f"Skipping global optimization: only {len(metrics_runs)} valid runs (< {min_runs})."
        )
        return {
            "status": "skipped_insufficient_runs",
            "applied": False,
            "requires_human_approval": False,
            "runs_analyzed": len(metrics_runs),
            "min_runs": min_runs,
            "target_prompt": None,
            "candidate_rules": [],
            "proposed_diff": "",
            "rationale": "",
        }

    phase_fn("Propose")
    if apply_approved:
        proposal = args_dict["approved_proposal"]
    else:
        optimizer_template = (prompts_dir / "optimizer.md").read_text(encoding="utf-8")
        current_prompts = {
            name: (prompts_dir / name).read_text(encoding="utf-8")
            for name in ALLOWED_TARGET_PROMPTS
        }
        lessons_text = _read_text_if_exists(lessons_path)
        opt_prompt = build_optimizer_prompt(
            template_text=optimizer_template,
            metrics_runs=metrics_runs,
            lessons_text=lessons_text,
            current_prompts=current_prompts,
        )
        proposal = await agent_fn(
            prompt=opt_prompt,
            schema=OPTIMIZER_SCHEMA,
            role="Outer loop optimizer",
            workspace="inherit",
            model="inherit",
        )

    target_prompt, candidate_rules, rationale = _parse_proposal(proposal)
    target_path = prompts_dir / target_prompt

    phase_fn("Validate")
    original_bytes = target_path.read_bytes()
    validated = False
    validation_error = False
    diff_or_message = ""
    try:
        validated, diff_or_message = stage_and_validate_global_patch(
            prompt_path=target_path,
            candidate_rules=candidate_rules,
            benchmarks_root=benchmarks_root,
            max_rules=max_rules,
            max_tokens_est=max_tokens_est,
        )
    except ValueError as exc:
        validation_error = True
        validated = False
        diff_or_message = f"Rule budget or marker validation error: {exc}"
    finally:
        if not validated or not apply_approved:
            target_path.write_bytes(original_bytes)

    serialized_rules = [
        {"rule_id": rule.rule_id, "text": rule.text} for rule in candidate_rules
    ]
    if not validated:
        status = "rejected_validation" if validation_error else "rejected_benchmark"
        log_fn(f"Candidate patch rejected ({status}): {diff_or_message}")
        return {
            "status": status,
            "applied": False,
            "requires_human_approval": False,
            "runs_analyzed": len(metrics_runs),
            "target_prompt": target_prompt,
            "target_path": str(target_path),
            "candidate_rules": serialized_rules,
            "proposed_diff": "",
            "error": diff_or_message,
            "rationale": rationale,
        }

    if apply_approved:
        log_fn(f"Applied benchmark-validated patch to {target_path}.")
        return {
            "status": "applied",
            "applied": True,
            "requires_human_approval": False,
            "runs_analyzed": len(metrics_runs),
            "target_prompt": target_prompt,
            "target_path": str(target_path),
            "candidate_rules": serialized_rules,
            "proposed_diff": diff_or_message,
            "rationale": rationale,
        }

    log_fn(
        f"Validated candidate patch for {target_prompt} against offline benchmarks. "
        "Active prompt restored pending explicit human approval (apply_approved=True)."
    )
    return {
        "status": "staged_pending_approval",
        "applied": False,
        "requires_human_approval": True,
        "runs_analyzed": len(metrics_runs),
        "target_prompt": target_prompt,
        "target_path": str(target_path),
        "candidate_rules": serialized_rules,
        "proposed_diff": diff_or_message,
        "rationale": rationale,
    }
