"""Git worktree isolation, protected test diff guard, and RED verifier."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ASSERT_RE = re.compile(r"\b(?:assert\w*|strictEqual|deepStrictEqual)\b|expect\(")


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


def _to_posix_rel(path_str: str) -> str:
    rel = path_str.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _matches_glob(rel_posix: str, pattern: str) -> bool:
    norm = _to_posix_rel(rel_posix)
    pat = _to_posix_rel(pattern)
    if fnmatch.fnmatch(norm, pat):
        return True
    if pat.startswith("**/") and fnmatch.fnmatch(norm, pat[3:]):
        return True
    return PurePosixPath(norm).match(pat)


def _is_protected(rel_posix: str, protected_globs: tuple[str, ...]) -> bool:
    return any(_matches_glob(rel_posix, pat) for pat in protected_globs)


def ensure_clean_repo(repo_root: Path) -> None:
    """Verify `repo_root` is a git repository with a clean working tree."""
    root = Path(repo_root)
    if not root.is_dir():
        raise RuntimeError(f"Repository path does not exist: {root}")

    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        raise RuntimeError(f"Not a git repository: {root}")

    status = _run_git(root, "status", "--porcelain")
    if status.returncode != 0:
        raise RuntimeError(f"git status failed in {root}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise RuntimeError(f"Repository has uncommitted changes: {root}")


def create_isolated_worktree(repo_root: Path, run_id: str) -> Path:
    """Create an isolated git worktree on branch `agy-loop/<run_id>`."""
    root = Path(repo_root)
    ensure_clean_repo(root)

    if (
        not _RUN_ID_RE.fullmatch(run_id)
        or run_id in {".", ".."}
        or ".." in run_id
        or run_id.startswith("-")
    ):
        raise ValueError(f"Invalid run_id: {run_id!r}")

    worktree_path = root / ".agents" / "worktrees" / run_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    branch_name = f"agy-loop/{run_id}"

    proc = _run_git(
        root,
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        "HEAD",
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"Failed to create git worktree at {worktree_path}: {detail}")
    return worktree_path


def _parse_diff_counts(diff_text: str) -> dict[str, tuple[int, int]]:
    """Return `{posix_path: (removed_assertions, added_assertions)}` from unified diff."""
    counts: dict[str, list[int]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_file = None
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            target = line[4:].rstrip("\r\n")
            if target == "/dev/null":
                current_file = None
            elif target.startswith("b/"):
                current_file = _to_posix_rel(target[2:])
                counts.setdefault(current_file, [0, 0])
            else:
                current_file = _to_posix_rel(target)
                counts.setdefault(current_file, [0, 0])
            continue
        if current_file is None or line.startswith("@@"):
            continue
        if line.startswith("-"):
            if _ASSERT_RE.search(line[1:]):
                counts[current_file][0] += 1
        elif line.startswith("+"):
            if _ASSERT_RE.search(line[1:]):
                counts[current_file][1] += 1

    return {path: (pair[0], pair[1]) for path, pair in counts.items()}


def check_protected_tests_diff(
    worktree_path: Path,
    base_ref: str,
    protected_globs: tuple[str, ...],
) -> tuple[bool, str]:
    """Reject deletion, renaming, or assertion-weakening of protected test files."""
    wt = Path(worktree_path)
    name_status = _run_git(wt, "-c", "core.quotePath=false", "diff", "--name-status", base_ref)
    if name_status.returncode != 0:
        detail = name_status.stderr.strip() or name_status.stdout.strip()
        raise RuntimeError(f"git diff --name-status failed: {detail}")

    for raw_line in name_status.stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        status = parts[0]
        if status == "D" and len(parts) >= 2:
            rel_path = _to_posix_rel(parts[1])
            if _is_protected(rel_path, protected_globs):
                return False, f"Protected test file deleted: {rel_path}"
        elif status.startswith("R") and len(parts) >= 3:
            old_path = _to_posix_rel(parts[1])
            new_path = _to_posix_rel(parts[2])
            if _is_protected(old_path, protected_globs):
                return False, f"Protected test file renamed away: {old_path} -> {new_path}"

    diff_proc = _run_git(wt, "-c", "core.quotePath=false", "diff", "--unified=0", base_ref)
    if diff_proc.returncode != 0:
        detail = diff_proc.stderr.strip() or diff_proc.stdout.strip()
        raise RuntimeError(f"git diff failed: {detail}")

    for rel_path, (removed, added) in _parse_diff_counts(diff_proc.stdout).items():
        if _is_protected(rel_path, protected_globs) and removed > added:
            return (
                False,
                f"Protected test file weakened (assertions removed={removed}, added={added}): {rel_path}",
            )

    return True, "OK"


def _has_added_test_lines(diff_text: str, protected_globs: tuple[str, ...]) -> bool:
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+++ "):
            target = line[4:].rstrip("\r\n")
            if target == "/dev/null":
                current_file = None
            elif target.startswith("b/"):
                current_file = _to_posix_rel(target[2:])
            else:
                current_file = _to_posix_rel(target)
            continue
        if (
            current_file is not None
            and _is_protected(current_file, protected_globs)
            and line.startswith("+")
            and line[1:].strip()
        ):
            return True
    return False


def verify_new_tests_fail_without_prod_changes(
    worktree_path: Path,
    base_ref: str,
    protected_globs: tuple[str, ...],
    test_cmd: str,
) -> tuple[bool, str]:
    """Verify newly added/updated tests fail (RED) when production changes are reverted."""
    wt = Path(worktree_path)

    diff_names = _run_git(wt, "-c", "core.quotePath=false", "diff", "--name-only", base_ref)
    if diff_names.returncode != 0:
        detail = diff_names.stderr.strip() or diff_names.stdout.strip()
        raise RuntimeError(f"git diff --name-only failed: {detail}")

    untracked_proc = _run_git(
        wt, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"
    )
    if untracked_proc.returncode != 0:
        detail = untracked_proc.stderr.strip() or untracked_proc.stdout.strip()
        raise RuntimeError(f"git ls-files failed: {detail}")

    tracked_changed = [_to_posix_rel(p) for p in diff_names.stdout.splitlines() if p.strip()]
    untracked_files = [_to_posix_rel(p) for p in untracked_proc.stdout.splitlines() if p.strip()]
    untracked_set = set(untracked_files)

    all_changed = sorted(set(tracked_changed) | untracked_set)
    test_files = [p for p in all_changed if _is_protected(p, protected_globs)]
    prod_files = [p for p in all_changed if not _is_protected(p, protected_globs)]

    new_test_created = any(p in test_files for p in untracked_files)
    diff_proc = _run_git(wt, "-c", "core.quotePath=false", "diff", "--unified=0", base_ref)
    if diff_proc.returncode != 0:
        detail = diff_proc.stderr.strip() or diff_proc.stdout.strip()
        raise RuntimeError(f"git diff failed: {detail}")

    if not new_test_created and not _has_added_test_lines(diff_proc.stdout, protected_globs):
        return True, "No new tests added; RED check skipped"

    def _run_tests() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            test_cmd,
            cwd=wt,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    if not prod_files:
        test_proc = _run_tests()
        if test_proc.returncode == 0:
            return False, "New test passes without any production change"
        return True, "RED verified: new tests fail without production changes"

    backups: list[tuple[Path, bytes | None]] = []
    tracked_in_base: list[str] = []
    temp_remove: list[Path] = []
    for rel_path in prod_files:
        abs_path = wt / rel_path
        backups.append((abs_path, abs_path.read_bytes() if abs_path.exists() else None))
        exists_in_base = _run_git(wt, "cat-file", "-e", f"{base_ref}:{rel_path}").returncode == 0
        if rel_path not in untracked_set and exists_in_base:
            tracked_in_base.append(rel_path)
        else:
            temp_remove.append(abs_path)

    try:
        for rel_path in tracked_in_base:
            co = _run_git(wt, "checkout", base_ref, "--", rel_path)
            if co.returncode != 0:
                detail = co.stderr.strip() or co.stdout.strip()
                raise RuntimeError(f"git checkout failed for {rel_path}: {detail}")
        for abs_path in temp_remove:
            abs_path.unlink(missing_ok=True)

        test_proc = _run_tests()
    finally:
        for abs_path, raw_bytes in backups:
            if raw_bytes is None:
                abs_path.unlink(missing_ok=True)
            else:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(raw_bytes)

    if test_proc.returncode != 0:
        return True, "RED verified: new tests fail without production changes"
    return False, "Tautological test detected: tests still pass without production changes"


def checkpoint_commit(worktree_path: Path, message: str) -> str:
    """Stage all changes, commit with `message`, and return the commit SHA."""
    wt = Path(worktree_path)
    add_proc = _run_git(wt, "add", "-A")
    if add_proc.returncode != 0:
        detail = add_proc.stderr.strip() or add_proc.stdout.strip()
        raise RuntimeError(f"git add -A failed: {detail}")

    commit_proc = _run_git(wt, "commit", "-m", message)
    if commit_proc.returncode != 0:
        detail = commit_proc.stderr.strip() or commit_proc.stdout.strip()
        raise RuntimeError(f"git commit failed: {detail}")

    rev_proc = _run_git(wt, "rev-parse", "HEAD")
    if rev_proc.returncode != 0:
        detail = rev_proc.stderr.strip() or rev_proc.stdout.strip()
        raise RuntimeError(f"git rev-parse HEAD failed: {detail}")
    return rev_proc.stdout.strip()


def rollback_to_ref(worktree_path: Path, ref: str) -> None:
    """Reset tracked changes to `ref` and remove untracked files/directories."""
    wt = Path(worktree_path)
    reset_proc = _run_git(wt, "reset", "--hard", ref)
    if reset_proc.returncode != 0:
        detail = reset_proc.stderr.strip() or reset_proc.stdout.strip()
        raise RuntimeError(f"git reset --hard {ref} failed: {detail}")

    clean_proc = _run_git(wt, "clean", "-fd")
    if clean_proc.returncode != 0:
        detail = clean_proc.stderr.strip() or clean_proc.stdout.strip()
        raise RuntimeError(f"git clean -fd failed: {detail}")
