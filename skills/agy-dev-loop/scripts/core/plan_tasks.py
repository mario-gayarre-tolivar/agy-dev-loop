"""Plan subtask parser and single-step checkbox updater."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s+(.+)$")
_STOP_BODY_RE = re.compile(r"^(?:#{1,6}\s|---\s*$)")


@dataclass(frozen=True)
class PlanSubtask:
    """Single `- [ ]` / `- [x]` subtask parsed from a Phase 3 plan."""

    index: int
    title: str
    body: str
    completed: bool
    line_number: int


def _read_lines(plan_path: Path) -> list[str]:
    with Path(plan_path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read().splitlines(keepends=True)


def _format_body(body_lines: list[str]) -> str:
    trimmed = list(body_lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return "\n".join(trimmed)


def _parse_lines(raw_lines: list[str]) -> list[PlanSubtask]:
    subtasks: list[PlanSubtask] = []
    current_meta: tuple[int, str, bool, int] | None = None
    body_lines: list[str] = []
    collecting_body = False

    def flush() -> None:
        nonlocal current_meta, body_lines
        if current_meta is None:
            return
        idx, title, completed, line_number = current_meta
        subtasks.append(
            PlanSubtask(
                index=idx,
                title=title,
                body=_format_body(body_lines),
                completed=completed,
                line_number=line_number,
            )
        )
        current_meta = None
        body_lines = []

    for line_number, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.rstrip("\r\n")
        match = _CHECKBOX_RE.match(line)
        if match:
            flush()
            current_meta = (
                len(subtasks),
                match.group(2).strip(),
                match.group(1).lower() == "x",
                line_number,
            )
            collecting_body = True
        elif collecting_body:
            if _STOP_BODY_RE.match(line):
                collecting_body = False
            else:
                body_lines.append(line)

    flush()
    return subtasks


def parse_plan_subtasks(plan_path: Path) -> list[PlanSubtask]:
    """Parse all markdown checkbox subtasks from `plan_path`."""
    return _parse_lines(_read_lines(plan_path))


def next_pending_subtask(plan_path: Path) -> PlanSubtask | None:
    """Return the first incomplete subtask in `plan_path`, or `None`."""
    for subtask in parse_plan_subtasks(plan_path):
        if not subtask.completed:
            return subtask
    return None


def mark_subtask_completed(plan_path: Path, subtask_index: int) -> None:
    """Mark subtask `subtask_index` as `[x]` in `plan_path`."""
    path = Path(plan_path)
    raw_lines = _read_lines(path)
    subtasks = _parse_lines(raw_lines)
    if subtask_index < 0 or subtask_index >= len(subtasks):
        raise IndexError(f"Subtask index out of range: {subtask_index}")

    target = subtasks[subtask_index]
    if target.completed:
        return

    line_idx = target.line_number - 1
    raw_lines[line_idx] = raw_lines[line_idx].replace("[ ]", "[x]", 1)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(raw_lines))
