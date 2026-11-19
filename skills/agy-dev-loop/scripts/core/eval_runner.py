"""Deterministic verification gate, error fingerprinting, and escalation/circuit-breaker policy."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.git_guard import (
    capture_worktree_diff,
    check_protected_tests_diff,
    verify_new_tests_fail_without_prod_changes,
)
from core.manifest import LoopManifest

_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_FLOAT_DURATION_RE = re.compile(r"\b\d+\.\d+(?:ms|s)\b")
_IN_INT_DURATION_RE = re.compile(r"(?<=\bin\s)\d+(?:ms|s)\b")
_WIN_TEMP_RE = re.compile(
    r"[A-Za-z]:\\(?:[^\s\"'\\]+\\)*Temp\\[^\s\"'\\]+", re.IGNORECASE
)
_POSIX_TEMP_RE = re.compile(
    r"(?:/private)?/var/folders/[^\s/\"']+/[^\s/\"']+/T/[^\s/\"']+|/tmp/[^\s/\"']+"
)


@dataclass(frozen=True)
class GateResult:
    """Outcome of running the deterministic verification gate."""

    passed: bool
    failed_stage: str | None
    output: str
    error_fingerprint: str
    should_run_reviewer: bool


def _normalize_lines(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def _normalize_output(output: str) -> str:
    cleaned = _ISO_TS_RE.sub("<TIMESTAMP>", output)
    cleaned = _FLOAT_DURATION_RE.sub("<TIME>", cleaned)
    cleaned = _IN_INT_DURATION_RE.sub("<TIME>", cleaned)
    cleaned = _WIN_TEMP_RE.sub("<TMP>", cleaned)
    cleaned = _POSIX_TEMP_RE.sub("<TMP>", cleaned)
    return _normalize_lines(cleaned)


def compute_error_fingerprint(output: str, diff_text: str) -> str:
    """Compute a normalized 16-char lowercase hex SHA-256 fingerprint for a failure."""
    norm_output = _normalize_output(output)
    norm_diff = _normalize_lines(diff_text)
    payload = f"{norm_output}\n---\n{norm_diff}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = [
        _coerce_text(stdout).strip(),
        _coerce_text(stderr).strip(),
    ]
    return "\n".join(part for part in parts if part)


def _fail_result(
    worktree_path: Path, base_ref: str, stage: str, stage_output: str
) -> GateResult:
    try:
        diff_text = capture_worktree_diff(worktree_path, base_ref)
    except (RuntimeError, OSError) as exc:
        diff_text = f"<diff unavailable: {exc}>"
    fingerprint = compute_error_fingerprint(stage_output, diff_text)
    return GateResult(
        passed=False,
        failed_stage=stage,
        output=stage_output,
        error_fingerprint=fingerprint,
        should_run_reviewer=False,
    )


def _run_cmd_stage(
    worktree_path: Path,
    cmd: str,
    timeout_sec: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=worktree_path,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _combine_output(exc.stdout, exc.stderr)
        msg = f"Command timed out after {exc.timeout}s: {cmd}"
        return False, f"{msg}\n{partial}" if partial else msg
    except OSError as exc:
        return False, f"Command failed to execute ({cmd}): {exc}"

    output = _combine_output(proc.stdout, proc.stderr)
    if proc.returncode != 0:
        return False, output or f"Command exited with code {proc.returncode}: {cmd}"
    return True, output


def run_deterministic_gate(
    worktree_path: Path,
    manifest: LoopManifest,
    base_ref: str,
    verify_red: bool | None = None,
    env: dict[str, str] | None = None,
) -> GateResult:
    """Run deterministic verification stages in strict short-circuit order."""
    wt = Path(worktree_path)
    outputs: list[str] = []

    try:
        ok, reason = check_protected_tests_diff(
            wt,
            base_ref,
            manifest.protected_test_globs,
            assert_regex=manifest.assert_regex,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        return _fail_result(wt, base_ref, "git_guard", str(exc))
    if not ok:
        return _fail_result(wt, base_ref, "git_guard", reason)
    outputs.append(f"[git_guard] {reason}")

    effective_verify_red = (
        manifest.verify_red if verify_red is None else (verify_red and manifest.verify_red)
    )
    if effective_verify_red:
        try:
            ok, reason = verify_new_tests_fail_without_prod_changes(
                wt,
                base_ref,
                manifest.protected_test_globs,
                manifest.test_cmd,
                timeout_sec=manifest.test_timeout_sec,
                env=env,
            )
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            return _fail_result(wt, base_ref, "verify_red", str(exc))
        if not ok:
            return _fail_result(wt, base_ref, "verify_red", reason)
        outputs.append(f"[verify_red] {reason}")

    cmd_stages: tuple[tuple[str, str | None], ...] = (
        ("test", manifest.test_cmd),
        ("lint", manifest.lint_cmd),
        ("build", manifest.build_cmd),
    )
    for stage_name, cmd in cmd_stages:
        if not cmd:
            continue
        ok, stage_output = _run_cmd_stage(
            wt, cmd, timeout_sec=manifest.test_timeout_sec, env=env
        )
        if not ok:
            return _fail_result(wt, base_ref, stage_name, stage_output)
        outputs.append(
            f"[{stage_name}]\n{stage_output}"
            if stage_output
            else f"[{stage_name}] OK"
        )

    return GateResult(
        passed=True,
        failed_stage=None,
        output="\n".join(outputs),
        error_fingerprint="",
        should_run_reviewer=True,
    )


def decide_next_action(
    current_tier: str,
    consecutive_failures: int,
    previous_fingerprints: list[str],
    new_fingerprint: str,
    circuit_breaker: int = 3,
) -> str:
    """Decide whether to continue on flash, escalate to pro, or abort via circuit-breaker."""
    if consecutive_failures >= circuit_breaker:
        return "abort_circuit_breaker"
    if new_fingerprint and new_fingerprint in previous_fingerprints:
        return "escalate_pro" if current_tier == "flash" else "abort_circuit_breaker"
    if consecutive_failures >= 2 and current_tier == "flash":
        return "escalate_pro"
    return "continue_flash" if current_tier == "flash" else "escalate_pro"
