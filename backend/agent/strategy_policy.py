"""Persistent policy for preventing repeated failed implementation strategies."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Any

from .source_structure import StructuralEditError, function_spans


STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
NUMBER_LITERAL_RE = re.compile(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)(?![A-Za-z_])")


def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote:
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = "" if quote == character else character if not quote else quote
            continue
        if character == "#" and not quote:
            return line[:index]
    return line


def _normalized_strategy_source(source: str) -> str:
    lines = []
    for line in source.splitlines():
        code = _strip_comment(line)
        code = STRING_LITERAL_RE.sub("<string>", code)
        code = NUMBER_LITERAL_RE.sub("<number>", code)
        if compact := re.sub(r"\s+", "", code):
            lines.append(compact)
    return "\n".join(lines)


def strategy_fingerprint(content: str, target: str) -> str:
    """Fingerprint control/data structure while ignoring formatting and literal tweaks."""
    source = content
    try:
        span = function_spans(content).get(target)
        if span is not None:
            source = "\n".join(content.splitlines()[span.start_line - 1:span.end_line])
    except StructuralEditError:
        pass
    normalized = _normalized_strategy_source(source)
    return sha256(normalized.encode("utf-8")).hexdigest()


def blocked_strategy_error(
    state: dict[str, Any],
    *,
    target: str,
    content: str,
) -> str | None:
    fingerprint = strategy_fingerprint(content, target)
    for entry in state.get("blocked_stage_strategies") or []:
        if entry.get("target") == target and entry.get("fingerprint") == fingerprint:
            return (
                f"The strategy for {target}() matches a candidate that was rolled back. "
                "Use a materially different control-flow or data-construction approach."
            )
    return None


def rollback_strategy_record(
    *,
    stage_name: str,
    target: str,
    content: str,
    diagnostic: str,
) -> dict[str, str]:
    return {
        "stage_name": stage_name,
        "target": target,
        "fingerprint": strategy_fingerprint(content, target),
        "diagnostic": " ".join(diagnostic.split())[:500],
    }


def different_strategy_directive(stage_name: str, target: str) -> dict[str, str]:
    return {
        "stage_name": stage_name,
        "target": target,
        "instruction": (
            f"Reimplement only {target}() from the last valid revision. The prior {stage_name} "
            "strategy was rolled back. Use a materially different control-flow or "
            "data-construction approach and submit one complete function body."
        ),
    }
