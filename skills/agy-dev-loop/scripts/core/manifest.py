"""Manifest loader, stack detector, and dry-run initializer for `.agents/loop.json`."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PROTECTED_TEST_GLOBS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*_test.rs",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*Tests.cs",
    "**/*Test.cs",
)

_NPM_DEFAULT_TEST_STUB = 'echo "Error: no test specified" && exit 1'
_IGNORED_SCAN_DIRS = frozenset(
    {
        ".git",
        ".agents",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "target",
        "bin",
        "obj",
        "dist",
        "build",
        ".pytest_cache",
    }
)
_NODE_TEST_PATTERNS = ("*.test.js", "*.test.mjs", "*.test.ts")


@dataclass(frozen=True)
class LoopManifest:
    """Configuration manifest for an agy-dev-loop target repository."""

    test_cmd: str
    lint_cmd: str | None = None
    build_cmd: str | None = None
    setup_cmd: str | None = None
    protected_test_globs: tuple[str, ...] = DEFAULT_PROTECTED_TEST_GLOBS
    max_iter: int = 5
    circuit_breaker: int = 3
    verify_red: bool = True
    test_timeout_sec: int = 120
    assert_regex: str | None = None


def resolve_python_cmd() -> str:
    """Return a working Python interpreter command (`python3`, `python`, or `sys.executable`)."""
    for candidate in ("python3", "python"):
        if not shutil.which(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    exe = sys.executable
    return f'"{exe}"' if " " in exe else exe


def _is_windows() -> bool:
    return os.name == "nt"


def _resolve_repo_python_cmd(repo_root: Path) -> str:
    """Prefer `"python"` when a Windows venv has `python.exe` without `python3.exe`."""
    if _is_windows():
        for venv_name in (".venv", "venv"):
            scripts_dir = repo_root / venv_name / "Scripts"
            if (scripts_dir / "python.exe").is_file():
                if not (scripts_dir / "python3.exe").is_file():
                    return "python"
                break
    return resolve_python_cmd()


def _require_non_empty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manifest field '{field_name}' must be a non-empty string.")
    return value.strip()


def _optional_cmd(raw: dict[str, object], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    return _require_non_empty_str(value, field_name)


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Manifest field '{field_name}' must be an integer >= 1.")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Manifest field '{field_name}' must be a boolean.")
    return value


def _optional_regex(raw: dict[str, object], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manifest field '{field_name}' must be a non-empty string.")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(
            f"Manifest field '{field_name}' is not a valid regex: {exc}"
        ) from exc
    return value


def _has_matching_file(root: Path, patterns: tuple[str, ...]) -> bool:
    for _, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_SCAN_DIRS]
        for filename in filenames:
            if any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                return True
    return False


def load_manifest(repo_root: Path) -> LoopManifest:
    """Load and validate `.agents/loop.json` from `repo_root`."""
    manifest_path = Path(repo_root) / ".agents" / "loop.json"
    if not manifest_path.is_file():
        raise ValueError(f"Missing manifest file: {manifest_path}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid manifest JSON at {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Manifest JSON root must be an object.")

    test_cmd = _require_non_empty_str(raw.get("test_cmd"), "test_cmd")
    lint_cmd = _optional_cmd(raw, "lint_cmd")
    build_cmd = _optional_cmd(raw, "build_cmd")
    setup_cmd = _optional_cmd(raw, "setup_cmd")
    max_iter = _require_positive_int(raw.get("max_iter", 5), "max_iter")
    circuit_breaker = _require_positive_int(
        raw.get("circuit_breaker", 3), "circuit_breaker"
    )
    verify_red = _require_bool(raw.get("verify_red", True), "verify_red")
    test_timeout_sec = _require_positive_int(
        raw.get("test_timeout_sec", 120), "test_timeout_sec"
    )
    assert_regex = _optional_regex(raw, "assert_regex")

    if "protected_test_globs" in raw:
        raw_globs = raw["protected_test_globs"]
        if not isinstance(raw_globs, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in raw_globs
        ):
            raise ValueError(
                "Manifest field 'protected_test_globs' must be a list/tuple of non-empty strings."
            )
        protected_test_globs = tuple(item.strip() for item in raw_globs)
    else:
        protected_test_globs = DEFAULT_PROTECTED_TEST_GLOBS

    return LoopManifest(
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        build_cmd=build_cmd,
        setup_cmd=setup_cmd,
        protected_test_globs=protected_test_globs,
        max_iter=max_iter,
        circuit_breaker=circuit_breaker,
        verify_red=verify_red,
        test_timeout_sec=test_timeout_sec,
        assert_regex=assert_regex,
    )


def _has_pytest_config(root: Path) -> bool:
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            if "[tool.pytest" in pyproject.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            pass
    return _has_matching_file(root, ("*_test.py",))


def _detect_node_pm(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


def _detect_wrapper_cmd(
    root: Path, win_wrapper: str, posix_wrapper: str, fallback: str
) -> str:
    if _is_windows() and (root / win_wrapper).is_file():
        return f"{win_wrapper} test"
    if not _is_windows() and (root / posix_wrapper).is_file():
        return f"./{posix_wrapper} test"
    return f"{fallback} test"


def detect_stack_manifest(repo_root: Path) -> LoopManifest:
    """Detect a verifiable test stack in `repo_root` and build a manifest."""
    root = Path(repo_root)

    if (root / "manage.py").is_file():
        return LoopManifest(test_cmd=f"{_resolve_repo_python_cmd(root)} manage.py test")

    if _has_pytest_config(root):
        return LoopManifest(test_cmd=f"{_resolve_repo_python_cmd(root)} -m pytest")

    tests_dir = root / "tests"
    if tests_dir.is_dir() and _has_matching_file(tests_dir, ("test_*.py",)):
        return LoopManifest(
            test_cmd=f"{_resolve_repo_python_cmd(root)} -m unittest discover -s tests -v"
        )

    if _has_matching_file(root, ("test_*.py",)):
        return LoopManifest(test_cmd=f"{_resolve_repo_python_cmd(root)} -m unittest discover -v")

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid package.json at {package_json}: {exc}") from exc

        scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
        if isinstance(scripts, dict):
            test_script = scripts.get("test")
            if (
                isinstance(test_script, str)
                and test_script.strip()
                and test_script.strip() != _NPM_DEFAULT_TEST_STUB
            ):
                pm = _detect_node_pm(root)
                lint_script = scripts.get("lint")
                build_script = scripts.get("build")
                return LoopManifest(
                    test_cmd="bun run test" if pm == "bun" else f"{pm} test",
                    lint_cmd=(
                        f"{pm} run lint"
                        if isinstance(lint_script, str) and lint_script.strip()
                        else None
                    ),
                    build_cmd=(
                        f"{pm} run build"
                        if isinstance(build_script, str) and build_script.strip()
                        else None
                    ),
                )

    if _has_matching_file(root, _NODE_TEST_PATTERNS):
        return LoopManifest(test_cmd="node --test")

    if (root / "go.mod").is_file():
        return LoopManifest(test_cmd="go test ./...", build_cmd="go build ./...")

    if (root / "Cargo.toml").is_file():
        return LoopManifest(test_cmd="cargo test", build_cmd="cargo check")

    if (root / "pom.xml").is_file():
        return LoopManifest(
            test_cmd=_detect_wrapper_cmd(root, "mvnw.cmd", "mvnw", "mvn")
        )

    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return LoopManifest(
            test_cmd=_detect_wrapper_cmd(root, "gradlew.bat", "gradlew", "gradle")
        )

    if root.is_dir() and any(
        entry.is_file() and entry.suffix in {".sln", ".csproj"}
        for entry in root.iterdir()
    ):
        return LoopManifest(test_cmd="dotnet test")

    raise ValueError(f"Could not detect a verifiable test command in {root}")


def _run_shell_cmd(
    exec_cwd: Path,
    cmd: str,
    timeout_sec: int,
    env: dict[str, str] | None,
    label: str,
) -> None:
    """Execute `cmd` in `exec_cwd` with `PYTHONDONTWRITEBYTECODE=1` and raise `RuntimeError` on failure."""
    root = Path(exec_cwd)
    run_env = dict(os.environ) if env is None else dict(env)
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=run_env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"{label} failed to execute ({cmd}): {exc}") from exc

    if result.returncode != 0:
        output = "\n".join(
            part
            for part in (
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
            if part
        )
        raise RuntimeError(
            f"{label} failed (exit {result.returncode}): {cmd}"
            + (f"\n{output}" if output else "")
        )


def run_setup_cmd(
    worktree_path: Path,
    manifest: LoopManifest,
    env: dict[str, str] | None = None,
) -> None:
    """Run optional `manifest.setup_cmd` in `worktree_path` and verify outputs are gitignored."""
    if not manifest.setup_cmd:
        return
    wt = Path(worktree_path)
    _run_shell_cmd(
        wt,
        manifest.setup_cmd,
        manifest.test_timeout_sec,
        env,
        "Worktree setup_cmd",
    )
    status_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if status_proc.returncode != 0:
        detail = (status_proc.stderr or status_proc.stdout or "").strip()
        raise RuntimeError(f"git status --porcelain failed in {wt}: {detail}")
    dirty = [line.strip() for line in status_proc.stdout.splitlines() if line.strip()]
    if dirty:
        dirty_paths = ", ".join(dirty)
        raise RuntimeError(
            f"setup_cmd produced unignored worktree paths ({dirty_paths}). "
            "Add setup_cmd outputs to .gitignore so rollback does not delete them, "
            "verify_red does not strip them, and agy-loop/<run_id> never commits them."
        )


def verify_manifest_test_cmd(
    exec_cwd: Path,
    manifest: LoopManifest,
    env: dict[str, str] | None = None,
) -> None:
    """Dry-run `manifest.test_cmd` in `exec_cwd` without writing Python bytecode."""
    _run_shell_cmd(
        Path(exec_cwd),
        manifest.test_cmd,
        manifest.test_timeout_sec,
        env,
        "Dry-run test command",
    )


def init_manifest(
    repo_root: Path,
    run_dry_check: bool = True,
    manifest: LoopManifest | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Detect stack, verify `test_cmd` if requested, and write `.agents/loop.json`."""
    root = Path(repo_root)
    resolved_manifest = detect_stack_manifest(root) if manifest is None else manifest

    if run_dry_check:
        verify_manifest_test_cmd(root, resolved_manifest, env=env)

    manifest_path = root / ".agents" / "loop.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(asdict(resolved_manifest), indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path
