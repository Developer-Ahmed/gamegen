"""Deterministic structural edits for model-authored Godot source."""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd
import re
from typing import Literal


PROTECTED_FUNCTIONS = frozenset(
    {
        "_ready",
        "_publish_ready_after_render",
        "start_game",
        "reset_game",
        "set_game_title",
        "set_status",
        "clear_playfield",
        "submit_game_action",
        "publish_smoke_result",
        "run_isolated_smoke_tests",
    }
)
PROTECTED_DECLARATIONS = frozenset(
    {
        "game_started",
        "game_reset",
        "title_label",
        "status_label",
        "playfield",
        "primary_action",
        "game_is_active",
        "session_revision",
        "game_state",
        "turn_system",
        "card_system",
        "inventory_system",
    }
)
LIFECYCLE_FUNCTIONS = frozenset({"_ready", "_process", "_physics_process"})

FUNCTION_HEADER_RE = re.compile(
    r"^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^\n]*\)(?:\s*->\s*[^:]+)?\s*:\s*$"
)
DECLARATION_RE = re.compile(
    r"^(?:@[A-Za-z_][A-Za-z0-9_]*(?:\([^\n]*\))?\s+)*"
    r"(?:var|const|signal|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
TOP_LEVEL_BOUNDARY_RE = re.compile(
    r"^(?:func\s+|class_name\s+|extends\s+|signal\s+|var\s+|const\s+|enum\s+|"
    r"@[A-Za-z_][A-Za-z0-9_]*(?:\([^\n]*\))?\s+(?:var|func)\s+)"
)
SCENE_NODE_RE = re.compile(r'^\[node\s+name="([^"]+)"(?:\s+type="[^"]+")?(?:\s+parent="([^"]+)")?.*\]$')


@dataclass(frozen=True)
class SourceSpan:
    kind: Literal["function", "declaration", "dictionary", "scene_node", "file"]
    name: str
    start_line: int
    end_line: int


class StructuralEditError(ValueError):
    pass


INDENT_WIDTH = 4


def _indent_columns(prefix: str) -> int:
    columns = 0
    for character in prefix:
        if character == "\t":
            columns += INDENT_WIDTH - (columns % INDENT_WIDTH)
        else:
            columns += 1
    return columns


def _canonicalize_lines(lines: list[str], *, base_tabs: int = 0) -> list[str]:
    """Normalize model-authored leading whitespace to one tab per logical level."""
    nonblank_columns = [
        _indent_columns(re.match(r"^[ \t]*", line).group())
        for line in lines
        if line.strip()
    ]
    common_columns = min(nonblank_columns, default=0)
    relative_levels = [
        columns - common_columns
        for columns in nonblank_columns
        if columns > common_columns
    ]
    indent_width = 0
    for columns in relative_levels:
        indent_width = gcd(indent_width, columns)
    if indent_width <= 0:
        indent_width = INDENT_WIDTH
    normalized: list[str] = []
    for line in lines:
        if not line.strip():
            normalized.append("")
            continue
        prefix = re.match(r"^[ \t]*", line).group()
        relative_columns = _indent_columns(prefix) - common_columns
        if relative_columns < 0 or relative_columns % indent_width:
            raise StructuralEditError(
                "Indentation does not form consistent logical levels."
            )
        normalized.append(
            "\t" * (base_tabs + relative_columns // indent_width) + line[len(prefix):]
        )
    return normalized


def canonicalize_gdscript_indentation(content: str) -> str:
    """Convert leading indentation to canonical tabs without changing code tokens."""
    had_final_newline = content.endswith(("\n", "\r"))
    normalized = _canonicalize_lines(content.splitlines())
    return "\n".join(normalized) + ("\n" if had_final_newline else "")


def _line_bounds(content: str) -> list[str]:
    return content.splitlines(keepends=True)


def function_spans(content: str) -> dict[str, SourceSpan]:
    lines = _line_bounds(content)
    starts: list[tuple[str, int]] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        if line[:1].isspace():
            continue
        match = FUNCTION_HEADER_RE.match(line)
        if match:
            starts.append((match.group(1), index))

    spans: dict[str, SourceSpan] = {}
    for name, start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index].rstrip("\r\n")
            if line and not line[:1].isspace() and TOP_LEVEL_BOUNDARY_RE.match(line):
                end = index
                break
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        if name in spans:
            raise StructuralEditError(f"Function '{name}' is declared more than once.")
        spans[name] = SourceSpan("function", name, start + 1, end)
    return spans


def declaration_spans(content: str) -> dict[str, SourceSpan]:
    spans: dict[str, SourceSpan] = {}
    lines = _line_bounds(content)
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip("\r\n")
        if line[:1].isspace():
            index += 1
            continue
        match = DECLARATION_RE.match(line)
        if not match:
            index += 1
            continue
        name = match.group(1)
        if name in spans:
            raise StructuralEditError(f"Declaration '{name}' is not unique.")
        start = index
        balance = sum(line.count(opening) - line.count(closing) for opening, closing in (("[", "]"), ("{", "}"), ("(", ")")))
        while balance > 0 and index + 1 < len(lines):
            index += 1
            continuation = lines[index]
            balance += sum(
                continuation.count(opening) - continuation.count(closing)
                for opening, closing in (("[", "]"), ("{", "}"), ("(", ")"))
            )
        spans[name] = SourceSpan("declaration", name, start + 1, index + 1)
        index += 1
    return spans


def _scene_node_path(name: str, parent: str | None, root_name: str | None) -> str:
    if parent in {None, "."}:
        return name if root_name is None else f"{root_name}/{name}"
    if root_name is None:
        return name
    return f"{root_name}/{parent.removeprefix('./')}/{name}".replace("//", "/")


def scene_node_spans(content: str) -> dict[str, SourceSpan]:
    lines = _line_bounds(content)
    headers: list[tuple[str, int]] = []
    root_name: str | None = None
    for index, raw_line in enumerate(lines):
        match = SCENE_NODE_RE.match(raw_line.rstrip("\r\n"))
        if not match:
            continue
        name, parent = match.groups()
        if root_name is None:
            root_name = name
            path = name
        else:
            path = _scene_node_path(name, parent, root_name)
        headers.append((path, index))

    spans: dict[str, SourceSpan] = {}
    for position, (path, start) in enumerate(headers):
        end = headers[position + 1][1] if position + 1 < len(headers) else len(lines)
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        spans[path] = SourceSpan("scene_node", path, start + 1, end)
    return spans


def enclosing_scope(content: str, line_number: int, *, path: str) -> SourceSpan:
    if path.endswith(".gd"):
        for span in function_spans(content).values():
            if span.start_line <= line_number <= span.end_line:
                return span
        for span in declaration_spans(content).values():
            if span.start_line <= line_number <= span.end_line:
                return span
    elif path.endswith(".tscn"):
        for span in scene_node_spans(content).values():
            if span.start_line <= line_number <= span.end_line:
                return span
    line_count = max(1, len(content.splitlines()))
    return SourceSpan("file", path, 1, line_count)


def _normalized_body(
    new_text: str,
    function_name: str,
    *,
    expected_header: str,
) -> list[str]:
    text = new_text.strip("\n")
    if not text.strip():
        return ["\tpass\n"]
    lines = text.splitlines()
    header_match = FUNCTION_HEADER_RE.match(lines[0].strip())
    if header_match:
        if header_match.group(1) != function_name:
            raise StructuralEditError(
                f"Replacement declares '{header_match.group(1)}', expected '{function_name}'."
            )
        if lines[0].strip() != expected_header.strip():
            raise StructuralEditError(
                f"Replacement for {function_name}() must preserve its exact signature: "
                f"{expected_header.strip()}"
            )
        lines = lines[1:]
    if any(FUNCTION_HEADER_RE.match(line.strip()) for line in lines):
        raise StructuralEditError("A function body cannot declare another top-level function.")
    normalized = _canonicalize_lines(lines, base_tabs=1)
    return [(line if line else "") + "\n" for line in normalized]


def replace_function_body(content: str, function_name: str, new_text: str) -> tuple[str, SourceSpan]:
    if function_name in PROTECTED_FUNCTIONS or function_name in LIFECYCLE_FUNCTIONS:
        raise StructuralEditError(f"Function '{function_name}' is protected foundation code.")
    spans = function_spans(content)
    span = spans.get(function_name)
    if span is None:
        raise StructuralEditError(f"Function '{function_name}' was not found uniquely.")
    lines = _line_bounds(content)
    original_header = lines[span.start_line - 1].rstrip("\r\n")
    body = _normalized_body(
        new_text,
        function_name,
        expected_header=original_header,
    )
    lines[span.start_line:span.end_line] = body
    return "".join(lines), span


def insert_function(content: str, target: str, new_text: str) -> tuple[str, SourceSpan]:
    source_lines = _canonicalize_lines(new_text.strip("\n").splitlines())
    if not source_lines:
        raise StructuralEditError("insert_function requires a complete function.")
    match = FUNCTION_HEADER_RE.match(source_lines[0])
    if not match:
        raise StructuralEditError("insert_function new_text must start with a complete func declaration.")
    name = match.group(1)
    if name in PROTECTED_FUNCTIONS or name in LIFECYCLE_FUNCTIONS:
        raise StructuralEditError(f"Function '{name}' is protected foundation code.")
    spans = function_spans(content)
    if name in spans:
        raise StructuralEditError(f"Function '{name}' already exists; replace its body instead.")
    lines = _line_bounds(content)
    if target:
        anchor = spans.get(target)
        if anchor is None:
            raise StructuralEditError(f"Insertion anchor function '{target}' was not found.")
        insertion = anchor.end_line
    else:
        insertion = len(lines)
    block = ["\n", *[line + "\n" for line in source_lines], "\n"]
    lines[insertion:insertion] = block
    return "".join(lines), SourceSpan("function", name, insertion + 2, insertion + 1 + len(source_lines))


def insert_declaration(content: str, name: str, new_text: str) -> tuple[str, SourceSpan]:
    """Insert one top-level declaration before the first function."""
    if name in PROTECTED_DECLARATIONS:
        raise StructuralEditError(f"Declaration '{name}' is protected foundation code.")
    if name in declaration_spans(content):
        raise StructuralEditError(f"Declaration '{name}' already exists; replace it instead.")
    replacement = "\n".join(_canonicalize_lines(new_text.strip().splitlines()))
    match = DECLARATION_RE.match(replacement)
    if not match or match.group(1) != name:
        raise StructuralEditError(
            f"New declaration must be one complete var, const, signal, or enum named '{name}'."
        )
    lines = _line_bounds(content)
    functions = function_spans(content)
    insertion = min((span.start_line - 1 for span in functions.values()), default=len(lines))
    block = [line + "\n" for line in replacement.splitlines()] + ["\n"]
    lines[insertion:insertion] = block
    return "".join(lines), SourceSpan(
        "declaration", name, insertion + 1, insertion + len(replacement.splitlines())
    )


def replace_declaration(content: str, name: str, new_text: str) -> tuple[str, SourceSpan]:
    if name in PROTECTED_DECLARATIONS:
        raise StructuralEditError(f"Declaration '{name}' is protected foundation code.")
    spans = declaration_spans(content)
    span = spans.get(name)
    if span is None:
        raise StructuralEditError(f"Declaration '{name}' was not found uniquely.")
    replacement = "\n".join(_canonicalize_lines(new_text.strip().splitlines()))
    match = DECLARATION_RE.match(replacement)
    if not match or match.group(1) != name:
        raise StructuralEditError(
            f"Replacement for '{name}' must be one complete var, const, signal, or enum with "
            "the same name."
        )
    lines = _line_bounds(content)
    lines[span.start_line - 1:span.end_line] = [line + "\n" for line in replacement.splitlines()]
    return "".join(lines), span


def replace_scene_node(content: str, node_path: str, new_text: str) -> tuple[str, SourceSpan]:
    spans = scene_node_spans(content)
    span = spans.get(node_path)
    if span is None:
        raise StructuralEditError(f"Scene node '{node_path}' was not found uniquely.")
    replacement = "\n".join(_canonicalize_lines(new_text.strip("\n").splitlines()))
    if not replacement.startswith("[node "):
        raise StructuralEditError("replace_scene_node requires the complete [node ...] section.")
    lines = _line_bounds(content)
    original_header = SCENE_NODE_RE.match(lines[span.start_line - 1].rstrip("\r\n"))
    replacement_header = SCENE_NODE_RE.match(replacement.splitlines()[0])
    if not original_header or not replacement_header or original_header.groups() != replacement_header.groups():
        raise StructuralEditError("A scene-node replacement must preserve the target node header.")
    lines[span.start_line - 1:span.end_line] = [line + "\n" for line in replacement.splitlines()]
    return "".join(lines), span
