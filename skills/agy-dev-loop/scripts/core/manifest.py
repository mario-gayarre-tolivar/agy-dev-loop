"""Manifest loader, stack detector, and dry-run initializer for `.agents/loop.json`."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PROTECTED_TEST_GLOBS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/*.test.*",
    "**/*.spec.*",
)

_NPM_DEFAULT_TEST_STUB = 'echo "Error: no test specified" && exit 1'
_IGNORED_SCAN_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})
_NODE_TEST_PATTERNS = ("*.test.js", "*.test.mjs", "*.test.ts")


@dataclass(frozen=True)
class LoopManifest:
    """Configuration manifest for an agy-dev-loop target repository."""

    test_cmd: str
    lint_cmd: str | None = None
    build_cmd: str | None = None
    protected_test_globs: tuple[str, ...] = DEFAULT_PROTECTED_TEST_GLOBS
    max_iter: int = 5
    circuit_breaker: int = 3


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
    max_iter = _require_positive_int(raw.get("max_iter", 5), "max_iter")
    circuit_breaker = _require_positive_int(
        raw.get("circuit_breaker", 3), "circuit_breaker"
    )

    if "protected_test_globs" in raw:
        raw_globs = raw["protected_test_globs"]
        if not isinstance(raw_globs, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in raw_globs
        ):
            raise ValueError(
                "Manifest field 'protected_test_globs' must be a list/tuple of non-empty strings."
            )
        protected_test_globs = tuple(raw_globs)
    else:
        protected_test_globs = DEFAULT_PROTECTED_TEST_GLOBS

    return LoopManifest(
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        build_cmd=build_cmd,
        protected_test_globs=protected_test_globs,
        max_iter=max_iter,
        circuit_breaker=circuit_breaker,
    )


def detect_stack_manifest(repo_root: Path) -> LoopManifest:
    """Detect a verifiable test stack in `repo_root` and build a manifest."""
    root = Path(repo_root)

    if (root / "manage.py").is_file():
        return LoopManifest(test_cmd="python3 manage.py test")

    tests_dir = root / "tests"
    if tests_dir.is_dir() and _has_matching_file(tests_dir, ("test_*.py",)):
        return LoopManifest(test_cmd="python3 -m unittest discover -s tests -v")

    if _has_matching_file(root, ("test_*.py",)):
        return LoopManifest(test_cmd="python3 -m unittest discover -v")

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
                lint_script = scripts.get("lint")
                build_script = scripts.get("build")
                return LoopManifest(
                    test_cmd="npm test",
                    lint_cmd=(
                        "npm run lint"
                        if isinstance(lint_script, str) and lint_script.strip()
                        else None
                    ),
                    build_cmd=(
                        "npm run build"
                        if isinstance(build_script, str) and build_script.strip()
                        else None
                    ),
                )

    if _has_matching_file(root, _NODE_TEST_PATTERNS):
        return LoopManifest(test_cmd="node --test")

    raise ValueError(f"Could not detect a verifiable test command in {root}")


def verify_manifest_test_cmd(repo_root: Path, manifest: LoopManifest) -> None:
    """Dry-run `manifest.test_cmd` in `repo_root` without writing Python bytecode."""
    root = Path(repo_root)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            manifest.test_cmd,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            f"Dry-run test command failed to execute ({manifest.test_cmd}): {exc}"
        ) from exc

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
            f"Dry-run test command failed (exit {result.returncode}): {manifest.test_cmd}"
            + (f"\n{output}" if output else "")
        )


def init_manifest(repo_root: Path, run_dry_check: bool = True) -> Path:
    """Detect stack, verify `test_cmd` if requested, and write `.agents/loop.json`."""
    root = Path(repo_root)
    manifest = detect_stack_manifest(root)

    if run_dry_check:
        verify_manifest_test_cmd(root, manifest)

    manifest_path = root / ".agents" / "loop.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path
