"""Run telemetry recorder and bounded `.agents/loop/lessons.md` pruner."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_HEADER = (
    "# Local Loop Lessons\n\n"
    "| id | symptom | rule | hits | last_run |\n"
    "| --- | --- | --- | --- | --- |\n"
)
_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class Lesson:
    """Single actionable lesson learned by the development loop."""

    lesson_id: str
    symptom: str
    rule: str
    hits: int
    last_run: str


@dataclass(frozen=True)
class RunMetrics:
    """Telemetry snapshot recorded at the end of a loop run."""

    run_id: str
    repo_name: str
    subtasks_total: int
    subtasks_passed: int
    iterations: int
    rollbacks: int
    reviewer_rejections: int
    failure_fingerprints: tuple[str, ...]
    status: str


def estimate_tokens(text: str) -> int:
    """Estimate token count for prompt/context budgeting."""
    return max(1, len(text) // 4)


def _clean_cell(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip()


def _escape_cell(value: str) -> str:
    return _clean_cell(value).replace("\\", "\\\\").replace("|", "\\|")


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|") and len(stripped) >= 2):
        return None
    cells: list[str] = []
    buf: list[str] = []
    idx = 1
    end = len(stripped) - 1
    while idx < end:
        ch = stripped[idx]
        if ch == "\\" and idx + 1 < end and stripped[idx + 1] in ("|", "\\"):
            buf.append(stripped[idx + 1])
            idx += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            idx += 1
            continue
        buf.append(ch)
        idx += 1
    cells.append("".join(buf).strip())
    return cells


def load_lessons(lessons_path: Path) -> list[Lesson]:
    """Load persisted lessons from a markdown table file."""
    path = Path(lessons_path)
    if not path.exists():
        return []

    lessons: list[Lesson] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        cells = _split_table_row(raw_line)
        if not cells or len(cells) != 5:
            continue
        if [c.lower() for c in cells] == ["id", "symptom", "rule", "hits", "last_run"]:
            continue
        if all(_SEPARATOR_CELL_RE.fullmatch(c) for c in cells):
            continue
        try:
            hits = int(cells[3])
        except ValueError:
            continue
        lessons.append(
            Lesson(
                lesson_id=cells[0],
                symptom=cells[1],
                rule=cells[2],
                hits=hits,
                last_run=cells[4],
            )
        )
    return lessons


def _render_lessons(lessons: list[Lesson]) -> str:
    rows = [
        (
            f"| {_escape_cell(item.lesson_id)} "
            f"| {_escape_cell(item.symptom)} "
            f"| {_escape_cell(item.rule)} "
            f"| {item.hits} "
            f"| {_escape_cell(item.last_run)} |\n"
        )
        for item in lessons
    ]
    return _HEADER + "".join(rows)


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _fit_single_lesson(lesson: Lesson, max_tokens_est: int) -> tuple[Lesson, str]:
    total_len = len(lesson.symptom) + len(lesson.rule)
    low, high = 0, total_len
    best_lesson = Lesson(
        lesson_id=lesson.lesson_id,
        symptom="",
        rule="",
        hits=lesson.hits,
        last_run=lesson.last_run,
    )
    best_rendered = _render_lessons([best_lesson])

    while low <= high:
        mid = (low + high) // 2
        if total_len == 0:
            sym_limit, rule_limit = 0, 0
        else:
            sym_limit = (mid * len(lesson.symptom)) // total_len
            rule_limit = mid - sym_limit
        candidate = Lesson(
            lesson_id=lesson.lesson_id,
            symptom=_truncate_text(lesson.symptom, sym_limit),
            rule=_truncate_text(lesson.rule, rule_limit),
            hits=lesson.hits,
            last_run=lesson.last_run,
        )
        rendered = _render_lessons([candidate])
        if estimate_tokens(rendered) <= max_tokens_est:
            best_lesson = candidate
            best_rendered = rendered
            low = mid + 1
        else:
            high = mid - 1

    return best_lesson, best_rendered


def upsert_and_prune_lessons(
    lessons_path: Path,
    new_lessons: list[Lesson],
    max_lessons: int = 12,
    max_tokens_est: int = 600,
) -> list[Lesson]:
    """Merge, deduplicate, sort, and prune lessons within count and token budgets."""
    path = Path(lessons_path)
    merged: dict[tuple[str, str], Lesson] = {}

    for raw in [*load_lessons(path), *new_lessons]:
        cleaned = Lesson(
            lesson_id=_clean_cell(raw.lesson_id),
            symptom=_clean_cell(raw.symptom),
            rule=_clean_cell(raw.rule),
            hits=raw.hits,
            last_run=_clean_cell(raw.last_run),
        )
        key = (cleaned.symptom.lower(), cleaned.rule.lower())
        existing = merged.get(key)
        if existing is None:
            merged[key] = cleaned
        else:
            merged[key] = Lesson(
                lesson_id=existing.lesson_id,
                symptom=existing.symptom,
                rule=existing.rule,
                hits=existing.hits + cleaned.hits,
                last_run=max(existing.last_run, cleaned.last_run),
            )

    ranked = sorted(
        merged.values(),
        key=lambda item: (item.hits, item.last_run),
        reverse=True,
    )
    kept = ranked[: max(0, max_lessons)]
    rendered = _render_lessons(kept)

    while estimate_tokens(rendered) > max_tokens_est and len(kept) > 1:
        kept.pop()
        rendered = _render_lessons(kept)

    if len(kept) == 1 and estimate_tokens(rendered) > max_tokens_est:
        fitted, rendered = _fit_single_lesson(kept[0], max_tokens_est)
        kept = [fitted]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return kept


def _validate_run_id(run_id: str) -> None:
    if (
        not _RUN_ID_RE.fullmatch(run_id)
        or run_id in {".", ".."}
        or ".." in run_id
        or run_id.startswith("-")
    ):
        raise ValueError(f"Invalid run_id: {run_id!r}")


def record_run_metrics(metrics_dir: Path, metrics: RunMetrics) -> Path:
    """Validate run_id and persist run telemetry as formatted JSON."""
    _validate_run_id(metrics.run_id)
    target_dir = Path(metrics_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    payload = asdict(metrics)
    payload["failure_fingerprints"] = list(metrics.failure_fingerprints)

    out_path = target_dir / f"{metrics.run_id}.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


def should_trigger_global_optimization(metrics_dir: Path, min_runs: int = 3) -> bool:
    """Return True when at least `min_runs` telemetry JSON files exist."""
    target_dir = Path(metrics_dir)
    if not target_dir.exists():
        return False
    return len(list(target_dir.glob("*.json"))) >= min_runs
