"""Build compact, deterministic repair context from Godot diagnostics."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Any, Callable

from .component_library import ComponentRegistry
from .game_contracts import compact_game_contracts
from .source_structure import SourceSpan, enclosing_scope


GODOT_LOCATION_PATTERNS = (
    re.compile(r"res://(?P<path>[^:\s]+):(?P<line>\d+)(?::(?P<column>\d+))?"),
    re.compile(r"(?P<path>game/[^:\s]+):(?P<line>\d+)(?::(?P<column>\d+))?"),
)
_GODOT_SYNTAX_REFERENCES = (
    (
        re.compile(r"(?:dictionary|key|\{|\})", re.IGNORECASE),
        "https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html#dictionary",
        'var points = {"White": 50, "Yellow": 75}',
        "Dictionary entries use key: value pairs separated by commas.",
    ),
    (
        re.compile(r"(?:indent|block|statement)", re.IGNORECASE),
        "https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html",
        "func my_function():\n    pass",
        "A function body is an indented block and cannot contain a second top-level function.",
    ),
    (
        re.compile(r"(?::=|infer|type)", re.IGNORECASE),
        "https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/static_typing.html",
        "var inferred_type := 42",
        "The := form is valid inferred static typing; change it only when the diagnostic targets it.",
    ),
    (
        re.compile(r"(?:function|func|parameter|argument|return)", re.IGNORECASE),
        "https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html#functions",
        "func my_function(a: int, b: String) -> void:",
        "Use a colon after the function signature and indent its body.",
    ),
)
_DEFAULT_SYNTAX_REFERENCE = (
    "https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html",
    "func my_function():\n    pass",
    "Follow the exact parser location and the surrounding valid Godot 4.7 structure.",
)


def _godot_syntax_reference(errors: list[str], output: str) -> dict[str, str]:
    diagnostic = "\n".join([*errors, output])
    source_url, excerpt, relevant_fact = _DEFAULT_SYNTAX_REFERENCE
    for pattern, candidate_url, candidate_excerpt, candidate_fact in _GODOT_SYNTAX_REFERENCES:
        if pattern.search(diagnostic):
            source_url, excerpt, relevant_fact = (
                candidate_url,
                candidate_excerpt,
                candidate_fact,
            )
            break
    return {
        "authority": "The pinned Godot 4.7 parser diagnostic is authoritative.",
        "source_url": source_url,
        "excerpt": excerpt,
        "relevant_fact": relevant_fact,
    }


def parse_godot_location(errors: list[str], output: str) -> tuple[str, int, int | None] | None:
    text = "\n".join([*errors, output])
    for pattern in GODOT_LOCATION_PATTERNS:
        if match := pattern.search(text):
            path = match.group("path")
            if not path.startswith("project/"):
                path = f"project/{path}"
            return path, int(match.group("line")), (
                int(match.group("column")) if match.groupdict().get("column") else None
            )
    return None


def _relevant_contracts(state: dict[str, Any], content: str) -> list[dict[str, Any]]:
    registry = ComponentRegistry()
    relevant = []
    for name in state.get("components") or []:
        component = registry.get(str(name))
        if component is None:
            continue
        member = component.foundation_member.split(":=", 1)[0].replace("var", "").strip()
        if member and member not in content:
            continue
        called_methods = set(
            re.findall(rf"\b{re.escape(member)}\.([A-Za-z_][A-Za-z0-9_]*)\(", content)
        ) if member else set()
        method_contracts = {
            name: contract
            for name, contract in component.method_contracts.items()
            if name in called_methods
        }
        api = [
            signature
            for signature in component.api
            if any(re.search(rf"(?:\.|^){re.escape(name)}\(", signature) for name in called_methods)
        ]
        if member and not called_methods:
            continue
        relevant.append(
            {
                "name": component.name,
                "api": api,
                "method_contracts": method_contracts,
            }
        )
    return relevant


def build_repair_packet(
    *,
    state: dict[str, Any],
    errors: list[str],
    output: str,
    read_text: Callable[[str], str],
) -> dict[str, Any]:
    location = parse_godot_location(errors, output)
    candidate_paths = list(state.get("candidate_changed_paths") or [])
    path = location[0] if location else (candidate_paths[0] if candidate_paths else "project/game/main.gd")
    line = location[1] if location else 1
    column = location[2] if location else None
    content = read_text(path)
    source_hash = sha256(content.encode("utf-8")).hexdigest()
    scope = enclosing_scope(content, line, path=path)
    lines = content.splitlines()
    snippet_start = max(1, line - 8)
    snippet_end = min(len(lines), line + 8)
    if scope.kind != "file":
        snippet_start = max(scope.start_line, snippet_start)
        snippet_end = min(scope.end_line, snippet_end)

    return {
        "instruction": "Repair this exact error without changing unrelated code.",
        "diagnostic": {
            "errors": errors,
            "path": path,
            "line": line,
            "column": column,
        },
        "candidate": {
            "sha256": source_hash,
            "base_revision": int(state.get("candidate_base_revision", state.get("workspace_revision", 0))),
        },
        "scope": {
            "kind": scope.kind,
            "name": scope.name,
            "start_line": scope.start_line,
            "end_line": scope.end_line,
        },
        "source_excerpt": {
            "start_line": snippet_start,
            "lines": lines[snippet_start - 1:snippet_end],
        },
        "component_contracts": _relevant_contracts(state, content),
        "established_game_contracts": compact_game_contracts(state, path=path),
        "godot_syntax_reference": _godot_syntax_reference(errors, output),
        "allowed_operation": (
            "replace_function_body"
            if scope.kind == "function"
            else "replace_declaration"
            if scope.kind == "declaration"
            else "replace_scene_node"
            if scope.kind == "scene_node"
            else "replace the smallest enclosing structure"
        ),
    }


def repair_scope(packet: dict[str, Any] | None) -> SourceSpan | None:
    if not isinstance(packet, dict):
        return None
    scope = packet.get("scope")
    if not isinstance(scope, dict):
        return None
    try:
        return SourceSpan(
            kind=str(scope["kind"]),  # type: ignore[arg-type]
            name=str(scope["name"]),
            start_line=int(scope["start_line"]),
            end_line=int(scope["end_line"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
