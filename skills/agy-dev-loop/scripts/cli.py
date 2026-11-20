"""Public CLI (`init` and `doctor`) for agy-dev-loop."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from core.git_guard import (  # noqa: E402
    build_worktree_env,
    create_isolated_worktree,
    ensure_clean_repo,
    remove_isolated_worktree,
)
from core.manifest import (  # noqa: E402
    detect_stack_manifest,
    init_manifest,
    load_manifest,
    verify_manifest_test_cmd,
)
from core.workflow_logic import DEFAULT_BENCHMARKS_ROOT  # noqa: E402


def _check_git_version() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    output = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"(\d+)\.(\d+)", output)
    if proc.returncode == 0 and match:
        version = (int(match.group(1)), int(match.group(2)))
        return version >= (2, 23), output
    return False, output or "git --version failed"


def _cmd_init(repo_root: Path, no_dry_run: bool) -> int:
    try:
        manifest = detect_stack_manifest(repo_root)
        if not no_dry_run:
            verify_manifest_test_cmd(repo_root, manifest, env=build_worktree_env(repo_root))
        manifest_path = init_manifest(repo_root, run_dry_check=False)
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "manifest_path": str(manifest_path),
                "test_cmd": manifest.test_cmd,
                "gitignore_hint": [".agents/worktrees/", ".agents/loop/"],
            }
        )
    )
    return 0


def _cmd_doctor(repo_root: Path) -> int:
    checks: dict[str, dict[str, Any]] = {
        "python_version": {
            "ok": sys.version_info >= (3, 12),
            "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
    }
    git_ok, git_detail = _check_git_version()
    checks["git_version"] = {"ok": git_ok, "detail": git_detail}
    checks["benchmarks_present"] = {
        "ok": (DEFAULT_BENCHMARKS_ROOT / "py_fixture").is_dir()
        and (DEFAULT_BENCHMARKS_ROOT / "js_fixture").is_dir(),
        "detail": str(DEFAULT_BENCHMARKS_ROOT),
    }

    try:
        ensure_clean_repo(repo_root)
        clean_ok, clean_detail = True, "clean"
    except (RuntimeError, OSError) as exc:
        clean_ok, clean_detail = False, str(exc)
    checks["clean_repo"] = {"ok": clean_ok, "detail": clean_detail}

    manifest = None
    try:
        manifest = load_manifest(repo_root)
        checks["manifest_valid"] = {"ok": True, "detail": "valid"}
    except (ValueError, OSError) as exc:
        checks["manifest_valid"] = {"ok": False, "detail": str(exc)}

    if manifest is None:
        checks["dry_run_ok"] = {"ok": False, "detail": "Skipped: manifest invalid"}
    elif not clean_ok:
        checks["dry_run_ok"] = {"ok": False, "detail": "Skipped: repository is not clean"}
    else:
        run_id = f"doctor-{os.getpid()}"
        wt: Path | None = None
        try:
            wt = create_isolated_worktree(repo_root, run_id)
            wt_env = build_worktree_env(repo_root)
            if manifest.setup_cmd:
                setup = subprocess.run(
                    manifest.setup_cmd,
                    cwd=wt,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=manifest.test_timeout_sec,
                    env=wt_env,
                )
                if setup.returncode != 0:
                    raise RuntimeError(
                        f"setup_cmd failed (exit {setup.returncode}): {manifest.setup_cmd}"
                    )
            verify_manifest_test_cmd(wt, manifest, env=wt_env)
            checks["dry_run_ok"] = {"ok": True, "detail": manifest.test_cmd}
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            checks["dry_run_ok"] = {"ok": False, "detail": str(exc)}
        finally:
            if wt is not None:
                remove_isolated_worktree(repo_root, wt, run_id)

    all_ok = all(item["ok"] for item in checks.values())
    print(json.dumps({"status": "ok" if all_ok else "failed", "checks": checks}))
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    """Run the agy-dev-loop CLI (`init` or `doctor`)."""
    parser = argparse.ArgumentParser(prog="agy-dev-loop", description="agy-dev-loop CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Detect stack and write .agents/loop.json")
    p_init.add_argument("--repo", required=True, help="Path to target git repository")
    p_init.add_argument("--no-dry-run", action="store_true", help="Skip dry-run test check")

    p_doc = sub.add_parser("doctor", help="Validate environment, manifest, and isolated dry-run")
    p_doc.add_argument("--repo", required=True, help="Path to target git repository")

    args = parser.parse_args(argv)
    repo_root = Path(args.repo).resolve()
    if args.command == "init":
        return _cmd_init(repo_root, args.no_dry_run)
    return _cmd_doctor(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
