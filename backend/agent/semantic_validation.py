"""Fast, deterministic preflight checks for model-authored Godot source."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .component_library import ComponentRegistry, GameComponent
from .source_structure import LIFECYCLE_FUNCTIONS, declaration_spans, function_spans


DISPLAY_PREFIX_RE = re.compile(r"^\s*\d+\s+\|\s+", re.MULTILINE)
FUNCTION_RE = re.compile(r"^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
UNSUPPORTED_RANGE_LOOP_RE = re.compile(
    r"^\s*for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+.+\s+to\s+.+:\s*(?:#.*)?$",
    re.MULTILINE,
)
BLOCK_HEADER_RE = re.compile(
    r"^\s*(?:func\b.+|if\b.+|elif\b.+|else|for\b.+|while\b.+|match\b.+)"
    r":\s*(?:#.*)?$"
)
MUTATING_COMPONENT_METHODS = frozenset(
    {
        "configure",
        "deal",
        "draw_card",
        "draw_to_hand",
        "draw_many_to_hand",
        "start_discard",
        "remove_from_hand",
        "discard_from_hand",
        "discard",
        "recycle_discards",
        "advance",
        "skip",
        "reverse",
        "restore",
        "add",
        "remove",
        "apply_transaction",
        "clear",
    }
)


@dataclass(frozen=True)
class SemanticIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _function_body(content: str, function_name: str) -> str:
    lines = content.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^func\s+{re.escape(function_name)}\s*\(", line)
        ),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("func ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _component_identity(component: GameComponent) -> tuple[str, str] | None:
    match = re.search(
        r"var\s+([A-Za-z_][A-Za-z0-9_]*)\s*:?[^\n=]*=\s*"
        r"(Gamegen[A-Za-z0-9_]+)\.new\(\)",
        component.foundation_member,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _component_aliases(content: str, class_name: str) -> set[str]:
    pattern = re.compile(
        rf"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^\n=]+)?\s*(?::=|=)\s*"
        rf"{re.escape(class_name)}\.new\(\)"
    )
    return {match.group(1) for match in pattern.finditer(content)}


def _component_binding_issues(
    content: str,
    selected_components: Iterable[str],
    project_component_contracts: dict[str, Any] | None = None,
    *,
    require_foundation_members: bool = True,
) -> list[SemanticIssue]:
    registry = ComponentRegistry()
    issues: list[SemanticIssue] = []
    for name in selected_components:
        component = registry.get(name)
        if component is None or not component.foundation_member:
            continue
        identity = _component_identity(component)
        if identity is None:
            continue
        member_name, class_name = identity
        constructor_re = re.compile(rf"\b{re.escape(class_name)}\.new\(\)")
        source_lines = content.splitlines()
        constructor_lines = [line for line in source_lines if constructor_re.search(line)]
        if not constructor_lines and require_foundation_members:
            issues.append(
                SemanticIssue(
                    "MISSING_COMPONENT_MEMBER",
                    f"The foundation member '{member_name}' for {class_name} was removed.",
                )
            )
            continue
        non_test_constructors = []
        for index, line in enumerate(source_lines):
            if not constructor_re.search(line):
                continue
            preceding = "\n".join(source_lines[: index + 1])
            functions_before = FUNCTION_RE.findall(preceding)
            enclosing = functions_before[-1] if line[:1].isspace() and functions_before else ""
            is_isolated_test = (
                enclosing == "run_isolated_smoke_tests"
                or enclosing.startswith("smoke_test_")
            )
            if not is_isolated_test:
                non_test_constructors.append(line)
        if len(non_test_constructors) > 1:
            issues.append(
                SemanticIssue(
                    "LOCAL_COMPONENT",
                    f"Use the single prewired '{member_name}' member; do not create another {class_name}.",
                )
            )
        if any(line[:1].isspace() for line in non_test_constructors):
            issues.append(
                SemanticIssue(
                    "LOCAL_COMPONENT",
                    f"{class_name} is constructed inside a function. Use persistent member '{member_name}'.",
                )
            )
        allowed_methods = {
            method.group(1)
            for signature in component.api
            if (method := re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)\(", signature))
            and method.group(1) != "new"
        }
        for contract in (project_component_contracts or {}).values():
            if contract.get("class_name") == class_name:
                allowed_methods.update((contract.get("methods") or {}).keys())
        aliases = _component_aliases(content, class_name) | {member_name}
        for alias in sorted(aliases):
            called_methods = set(
                re.findall(rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)\(", content)
            )
            unknown_methods = sorted(called_methods - allowed_methods)
            if unknown_methods:
                issues.append(
                    SemanticIssue(
                        "INVENTED_COMPONENT_API",
                        f"{alias} uses undocumented methods: {', '.join(unknown_methods)}.",
                    )
                )
            dotted_accesses = set(
                re.findall(
                    rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    content,
                )
            )
            property_accesses = dotted_accesses - called_methods
            if property_accesses:
                issues.append(
                    SemanticIssue(
                        "INVENTED_COMPONENT_PROPERTY",
                        f"{alias} accesses undocumented properties: "
                        f"{', '.join(sorted(property_accesses))}. Use documented methods from "
                        "the private GameGen component contract.",
                    )
                )
    return issues


def _undeclared_assignment_issues(content: str) -> list[SemanticIssue]:
    members = set(re.findall(r"^(?:@onready\s+)?var\s+([A-Za-z_]\w*)", content, re.MULTILINE))
    constants = set(re.findall(r"^const\s+([A-Za-z_]\w*)", content, re.MULTILINE))
    known_names = members | constants
    issues: list[SemanticIssue] = []
    for function_match in re.finditer(
        r"^func\s+([A-Za-z_]\w*)\s*\(([^)]*)\)[^:]*:\s*\n([\s\S]*?)(?=^func\s+|\Z)",
        content,
        re.MULTILINE,
    ):
        function_name, raw_params, body = function_match.groups()
        params = {
            token.strip().split(":", 1)[0].split("=", 1)[0].strip()
            for token in raw_params.split(",")
            if token.strip()
        }
        locals_ = set(re.findall(r"^\s*var\s+([A-Za-z_]\w*)", body, re.MULTILINE))
        known = known_names | params | locals_
        for name in re.findall(r"^\s*(?!var\b)([A-Za-z_]\w*)\s*=", body, re.MULTILINE):
            if name not in known:
                issues.append(
                    SemanticIssue(
                        "UNDECLARED_IDENTIFIER",
                        f"Function {function_name} assigns undeclared identifier '{name}'. "
                        "Declare it locally or as a member first.",
                    )
                )
            elif name in constants:
                issues.append(
                    SemanticIssue(
                        "CONST_REASSIGNMENT",
                        f"Function {function_name} assigns to constant '{name}'. Constants are "
                        "read-only; use the constant as a dictionary key or assign a writable member.",
                    )
                )
    return issues


def _state_key_usage_issues(content: str) -> list[SemanticIssue]:
    """Reject treating symbolic game-state keys as the state values they address."""
    state_keys = set(
        re.findall(
            r'^const\s+(STATE_[A-Z0-9_]+)\s*(?::[^=\n]+)?\s*(?::=|=)\s*["\']',
            content,
            re.MULTILINE,
        )
    )
    if not state_keys:
        return []

    issues: list[SemanticIssue] = []
    for key in sorted(state_keys):
        direct_value = re.search(
            rf"^\s*var\s+[A-Za-z_]\w*(?:\s*:[^=\n]+)?\s*(?::=|=)\s*{re.escape(key)}\b",
            content,
            re.MULTILINE,
        )
        self_membership = re.search(
            rf"[\"']{re.escape(key)}[\"']\s+in\s+self\b",
            content,
        )
        quoted_key_access = re.search(
            rf"\bgame_state\s*\[\s*[\"']{re.escape(key)}[\"']\s*\]",
            content,
        )
        quoted_key_membership = re.search(
            rf"[\"']{re.escape(key)}[\"']\s+in\s+game_state\b",
            content,
        )
        if direct_value or self_membership or quoted_key_access or quoted_key_membership:
            issues.append(
                SemanticIssue(
                    "STATE_KEY_AS_VALUE",
                    f"{key} is a dictionary key, not live game state. Read or write the value "
                    f"through game_state[{key}]; do not quote the constant's identifier.",
                )
            )
    return issues


def _component_result_issues(
    content: str,
    selected_components: Iterable[str],
) -> list[SemanticIssue]:
    registry = ComponentRegistry()
    issues: list[SemanticIssue] = []
    for component_name in selected_components:
        component = registry.get(component_name)
        if component is None:
            continue
        identity = _component_identity(component)
        if identity is None:
            continue
        member_name, class_name = identity
        aliases = _component_aliases(content, class_name) | {member_name}
        non_void_methods = {
            method_name
            for method_name, contract in component.method_contracts.items()
            if str(contract.get("returns") or "").strip().lower() != "void"
            and contract.get("result_policy", "required") == "required"
        }
        for alias in aliases:
            for method in non_void_methods:
                pattern = re.compile(
                    rf"^\s*{re.escape(alias)}\.{re.escape(method)}\([^\n]*\)\s*$",
                    re.MULTILINE,
                )
                if pattern.search(content):
                    issues.append(
                        SemanticIssue(
                            "IGNORED_COMPONENT_RESULT",
                            f"Use or check the return value of {alias}.{method}() before continuing.",
                        )
                    )
    process_body = _function_body(content, "_process")
    for member, method in re.findall(
        r"\b(card_system|turn_system|inventory_system)\.([A-Za-z_]\w*)\(",
        process_body,
    ):
        if method in MUTATING_COMPONENT_METHODS:
            issues.append(
                SemanticIssue(
                    "FRAME_STATE_MUTATION",
                    f"_process() calls mutating operation {member}.{method}() every frame.",
                )
            )
    return issues


def _double_initial_discard_issues(
    content: str,
    selected_components: Iterable[str],
) -> list[SemanticIssue]:
    """Reject moving the opening discard twice.

    GamegenCardSystem.start_discard() already removes one card from the draw
    pile and places it on the discard pile. Passing that returned card to
    discard() would duplicate the transition and may fail because the card is
    no longer in a hand.
    """
    if "godot.card_system" not in set(selected_components):
        return []

    component = ComponentRegistry().get("godot.card_system")
    identity = _component_identity(component) if component else None
    if identity is None:
        return []
    member_name, class_name = identity
    aliases = _component_aliases(content, class_name) | {member_name}

    blocks = [content]
    function_matches = list(
        re.finditer(
            r"^func\s+[A-Za-z_]\w*\s*\([^)]*\)[^:]*:\s*\n",
            content,
            re.MULTILINE,
        )
    )
    if function_matches:
        blocks = []
        for index, match in enumerate(function_matches):
            end = (
                function_matches[index + 1].start()
                if index + 1 < len(function_matches)
                else len(content)
            )
            blocks.append(content[match.start():end])

    for block in blocks:
        for alias in aliases:
            direct = re.compile(
                rf"\b{re.escape(alias)}\.discard\(\s*"
                rf"{re.escape(alias)}\.start_discard\(\s*\)\s*\)"
            )
            if direct.search(block):
                return [
                    SemanticIssue(
                        "DOUBLE_INITIAL_DISCARD",
                        f"{alias}.start_discard() already places and returns the opening card; "
                        "do not pass its result to discard().",
                    )
                ]

            assigned = re.finditer(
                rf"\b(?:var\s+)?([A-Za-z_]\w*)\s*(?::[^=\n]+)?\s*(?::=|=)\s*"
                rf"{re.escape(alias)}\.start_discard\(\s*\)",
                block,
            )
            for assignment in assigned:
                variable = assignment.group(1)
                remainder = block[assignment.end():]
                if re.search(
                    rf"\b{re.escape(alias)}\.discard\(\s*{re.escape(variable)}\s*\)",
                    remainder,
                ):
                    return [
                        SemanticIssue(
                            "DOUBLE_INITIAL_DISCARD",
                            f"{variable} came from {alias}.start_discard(), which already "
                            "updated the discard pile; do not discard it again.",
                        )
                    ]
    return []


def _collection_shape_issues(content: str) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    combined_hands = {
        match.group(1)
        for match in re.finditer(
            r"\bvar\s+([A-Za-z_]\w*)[^=]*(?::=|=)\s*"
            r"card_system\.hand\([^\n]+\)\s*\+\s*card_system\.hand\(",
            content,
        )
    }
    for variable in combined_hands:
        if re.search(
            rf"for\s+([A-Za-z_]\w*)\s+in\s+{re.escape(variable)}\s*:[\s\S]*?\b\1\.size\(\)",
            content,
        ):
            issues.append(
                SemanticIssue(
                    "FLATTENED_HANDS",
                    f"'{variable}' concatenates card arrays, so iteration yields cards rather "
                    "than player hands. Check each player's hand separately.",
                )
            )
    node_variables = {
        match.group(1)
        for match in re.finditer(
            r"\bvar\s+([A-Za-z_]\w*)[^=]*(?::=|=)\s*(?:get_node\(|\$)",
            content,
        )
    }
    for variable in sorted(node_variables):
        if re.search(rf"\b{re.escape(variable)}\.clear\s*\(", content):
            issues.append(
                SemanticIssue(
                    "INVENTED_NODE_API",
                    f"Godot Node/Control '{variable}' has no generic clear() method. Remove or "
                    "queue_free its children explicitly, or call a documented method on a typed class.",
                )
            )
    return issues


def _literal_card_array(content: str) -> tuple[str, int] | None:
    direct = re.search(r"card_system\.configure\(\s*(\[[\s\S]*?\])\s*,", content)
    literal = direct.group(1) if direct else ""
    if not literal:
        call = re.search(r"card_system\.configure\(\s*([A-Za-z_]\w*)\s*,", content)
        if call:
            assignment = re.search(
                rf"(?:var|const)\s+{re.escape(call.group(1))}(?:\s*:[^=]+)?\s*(?::=|=)\s*(\[[\s\S]*?\])",
                content,
            )
            literal = assignment.group(1) if assignment else ""
            if literal.strip() == "[]":
                return None
    return (literal, literal.count("{")) if literal else None


def _literal_player_count(content: str) -> int | None:
    call = re.search(
        r"card_system\.configure\([^,]+,\s*(\[[^]]*\]|[A-Za-z_]\w*)\s*,",
        content,
    )
    if not call:
        return None
    value = call.group(1)
    if not value.startswith("["):
        assignment = re.search(
            rf"(?:var|const)\s+{re.escape(value)}(?:\s*:[^=]+)?\s*(?::=|=)\s*(\[[^]]*\])",
            content,
        )
        if not assignment:
            return None
        value = assignment.group(1)
    return len([item for item in value[1:-1].split(",") if item.strip()])


def _card_definition_id_issues(
    content: str,
    selected_components: Iterable[str],
) -> list[SemanticIssue]:
    """Reject card dictionaries whose IDs are statically known to be non-strings."""
    if "godot.card_system" not in set(selected_components):
        return []

    integer_names = set(
        re.findall(r"\bvar\s+([A-Za-z_]\w*)\s*:\s*int\b", content)
    )
    integer_names.update(
        match.group(1)
        for match in re.finditer(
            r"\bvar\s+([A-Za-z_]\w*)\s*(?::=|=)\s*-?\d+\b",
            content,
        )
    )
    for match in re.finditer(r"[\"']id[\"']\s*:\s*([^,}\n]+)", content):
        expression = match.group(1).strip()
        if re.fullmatch(r"-?\d+", expression) or expression in integer_names:
            return [
                SemanticIssue(
                    "CARD_ID_TYPE",
                    "GamegenCardSystem requires every card dictionary id to be a non-empty "
                    f"String, but '{expression}' is an integer. Convert or format it as a "
                    "stable unique string when constructing the card.",
                )
            ]
    return []


def _dictionary_literal_domain_issues(content: str) -> list[SemanticIssue]:
    """Reject comparisons against string values absent from generated definition factories."""
    factory_blocks = [
        _function_body(content, name)
        for name in FUNCTION_RE.findall(content)
        if re.match(r"^(?:create|build|make)_.+(?:definitions|deck|items|records)$", name)
    ]
    domains: dict[str, set[str]] = {}
    for block in factory_blocks:
        for key, value in re.findall(
            r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*:\s*[\"']([^\"']+)[\"']",
            block,
        ):
            domains.setdefault(key, set()).add(value)
    if not domains:
        return []

    variable_keys: dict[str, str] = {}
    for variable, key in re.findall(
        r"\bvar\s+([A-Za-z_]\w*)[^=\n]*(?::=|=)\s*"
        r"[A-Za-z_]\w*\.get\(\s*[\"']([A-Za-z_]\w*)[\"']",
        content,
    ):
        variable_keys[variable] = key

    issues: list[SemanticIssue] = []
    comparisons: list[tuple[str, str]] = []
    for variable, value in re.findall(
        r"\b([A-Za-z_]\w*)\s*==\s*[\"']([^\"']+)[\"']",
        content,
    ):
        if variable in variable_keys:
            comparisons.append((variable_keys[variable], value))
    comparisons.extend(
        (key, value)
        for key, value in re.findall(
            r"\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]\s*==\s*[\"']([^\"']+)[\"']",
            content,
        )
    )
    for key, value in comparisons:
        allowed = domains.get(key)
        if allowed and value not in allowed:
            issues.append(
                SemanticIssue(
                    "DICTIONARY_VALUE_DOMAIN",
                    f"Dictionary key '{key}' is generated with values "
                    f"{', '.join(sorted(allowed))}, but code compares it to '{value}'. "
                    "Use the exact generated representation.",
                )
            )
    return issues


def _card_transition_issues(
    content: str,
    selected_components: Iterable[str],
) -> list[SemanticIssue]:
    """Reject play actions that copy a card to discard without removing it from its hand."""
    if "godot.card_system" not in set(selected_components):
        return []

    issues: list[SemanticIssue] = []
    for function_name in FUNCTION_RE.findall(content):
        body = _function_body(content, function_name)
        is_action_handler = bool(
            re.search(r"(?:handle|submit|apply|play).*(?:action|card)|(?:action|card).*(?:handle|play)", function_name)
        )
        if not is_action_handler or not re.search(r"\bcard_system\.discard\s*\(", body):
            continue
        removes_from_hand = re.search(
            r"\bcard_system\.(?:discard_from_hand|remove_from_hand)\s*\(", body
        )
        if not removes_from_hand:
            issues.append(
                SemanticIssue(
                    "CARD_NOT_REMOVED_FROM_HAND",
                    f"{function_name}() discards a played card without removing it from the "
                    "player's hand. Use discard_from_hand(), or remove_from_hand() followed by "
                    "discard(), so one action performs one complete state transition.",
                )
            )
    return issues


def _action_legality_issues(content: str) -> list[SemanticIssue]:
    """Require a play-card action to use an available legality predicate."""
    handler = _function_body(content, "handle_game_action")
    if not handler or not re.search(r"[\"']play_card[\"']", handler):
        return []
    predicates = [
        name
        for name in FUNCTION_RE.findall(content)
        if re.search(r"(?:^is_|^can_).*(?:play|legal)|(?:playable|legal)", name)
    ]
    if predicates and not any(
        re.search(rf"\b{re.escape(name)}\s*\(", handler) for name in predicates
    ):
        return [
            SemanticIssue(
                "PLAY_BYPASSES_LEGALITY",
                "handle_game_action() accepts play_card but does not call the project's "
                f"legality predicate ({', '.join(sorted(predicates))}). Validate the play "
                "before mutating card or turn state.",
            )
        ]
    return []


def _card_schema_access_issues(content: str) -> list[SemanticIssue]:
    """Reject card-field reads that cannot exist in the generated card schema."""
    factory_blocks = [
        _function_body(content, name)
        for name in FUNCTION_RE.findall(content)
        if re.match(r"^(?:create|build|make)_.+(?:definitions|deck|cards|items|records)$", name)
    ]
    schema_keys = {
        key
        for block in factory_blocks
        for key in re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*:", block)
    }
    if not schema_keys:
        return []

    issues: list[SemanticIssue] = []
    accesses = re.findall(
        r"\b([A-Za-z_]\w*)"
        r"(?:\.get\(\s*[\"']([A-Za-z_]\w*)[\"']|\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\])",
        content,
        re.IGNORECASE,
    )
    for variable, get_key, index_key in accesses:
        if not re.search(r"(?:card|discard)", variable, re.IGNORECASE):
            continue
        key = get_key or index_key
        if key not in schema_keys:
            issues.append(
                SemanticIssue(
                    "UNKNOWN_CARD_FIELD",
                    f"'{variable}' reads card field '{key}', but generated card definitions "
                    f"only provide: {', '.join(sorted(schema_keys))}.",
                )
            )
    return issues


def _signal_arity_issues(content: str) -> list[SemanticIssue]:
    """Check statically declared signal arity at direct emit sites."""
    signals: dict[str, int] = {}
    for name, raw_parameters in re.findall(
        r"^signal\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*$",
        content,
        re.MULTILINE,
    ):
        signals[name] = len(
            [parameter for parameter in raw_parameters.split(",") if parameter.strip()]
        )

    issues: list[SemanticIssue] = []
    for name, raw_arguments in re.findall(
        r"\b([A-Za-z_]\w*)\.emit\s*\(([^()]*)\)", content
    ):
        if name not in signals:
            continue
        actual = len([argument for argument in raw_arguments.split(",") if argument.strip()])
        expected = signals[name]
        if actual != expected:
            issues.append(
                SemanticIssue(
                    "SIGNAL_ARITY",
                    f"Signal '{name}' declares {expected} argument(s) but emit() supplies "
                    f"{actual}.",
                )
            )
    return issues


def validate_replacement_text(text: str) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    if DISPLAY_PREFIX_RE.search(text):
        issues.append(
            SemanticIssue(
                "SOURCE_PREFIX",
                "Replacement text contains display line-number prefixes such as '22 |'. "
                "Send only source code from each line's code field.",
            )
        )
    if UNSUPPORTED_RANGE_LOOP_RE.search(text):
        issues.append(
            SemanticIssue(
                "UNSUPPORTED_LOOP_SYNTAX",
                "GDScript does not support 'for value in start to end'. "
                "Use range(start, end_exclusive).",
            )
        )

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not BLOCK_HEADER_RE.match(line):
            continue
        header_indent = len(line) - len(line.lstrip(" \t"))
        next_line = next(
            (
                candidate
                for candidate in lines[index + 1:]
                if candidate.strip() and not candidate.lstrip().startswith("#")
            ),
            None,
        )
        if next_line is None:
            issues.append(
                SemanticIssue(
                    "INCOMPLETE_BLOCK",
                    f"Block beginning with '{line.strip()}' has no body. Add code or pass.",
                )
            )
            continue
        next_indent = len(next_line) - len(next_line.lstrip(" \t"))
        if next_indent <= header_indent:
            issues.append(
                SemanticIssue(
                    "INCOMPLETE_BLOCK",
                    f"Block beginning with '{line.strip()}' is not followed by an indented body.",
                )
            )
    return issues


def validate_stage_implementation(
    content: str,
    stage_targets: Iterable[str],
) -> list[SemanticIssue]:
    """Ensure an application-owned stage replaced its foundation placeholder."""
    issues: list[SemanticIssue] = []
    for target in stage_targets:
        function_source = _function_body(content, target)
        if not function_source:
            issues.append(
                SemanticIssue(
                    "MISSING_STAGE_FUNCTION",
                    f"Required stage function {target}() is missing or has an invalid declaration.",
                )
            )
            continue
        body_lines = function_source.splitlines()[1:]
        executable = "\n".join(
            line.strip()
            for line in body_lines
            if line.strip() and not line.lstrip().startswith("#")
        )
        compact = re.sub(r"\s+", " ", executable).strip().lower()
        is_placeholder = compact in {"pass", "return []", "return false", 'return ""'}
        is_failing_smoke_placeholder = (
            target.startswith("smoke_test_")
            and compact.startswith("return {")
            and re.search(r"[\"']ok[\"']\s*:\s*false", compact) is not None
        )
        if is_placeholder or is_failing_smoke_placeholder:
            issues.append(
                SemanticIssue(
                    "PLACEHOLDER_STAGE",
                    f"Stage function {target}() still contains a non-functional "
                    "foundation placeholder.",
                )
            )
    return issues


def validate_step_contract(
    path: str,
    content: str,
    step: dict[str, Any],
    selected_components: Iterable[str],
) -> list[SemanticIssue]:
    """Validate deterministic evidence declared by one semantic implementation step."""
    issues: list[SemanticIssue] = []
    target = str(step.get("target") or "")
    operation = str(step.get("operation") or "")
    scope = content
    if path.endswith(".gd") and operation != "create_file" and target:
        scope = _function_body(content, target)
        if operation in {"insert_function", "replace_function_body"} and not scope:
            return [
                SemanticIssue(
                    "MISSING_STEP_SYMBOL",
                    f"The resolved step symbol {target}() is missing from {path}.",
                )
            ]

    for required_call in step.get("required_calls") or []:
        call = str(required_call).strip()
        if not call:
            continue
        if not re.search(rf"\b{re.escape(call)}\s*\(", scope):
            issues.append(
                SemanticIssue(
                    "MISSING_REQUIRED_CALL",
                    f"Step '{step.get('id')}' requires a real call to {call}().",
                )
            )

    for required_key in (
        step.get("required_dictionary_keys")
        or step.get("required_state_keys")
        or []
    ):
        key = str(required_key).strip()
        if not key:
            continue
        patterns = (
            rf"[\"']{re.escape(key)}[\"']",
            rf"\b{re.escape(key)}\s*:",
            rf"\[\s*{re.escape(key)}\s*\]",
        )
        if not any(re.search(pattern, scope) for pattern in patterns):
            issues.append(
                SemanticIssue(
                    "MISSING_DICTIONARY_KEY",
                    f"Step '{step.get('id')}' must contain dictionary key '{key}'.",
                )
            )
    if target == "smoke_test_gameplay":
        for key in ("ok", "details"):
            if not re.search(rf"[\"']{key}[\"']\s*:", scope):
                issues.append(
                    SemanticIssue(
                        "INCOMPLETE_SMOKE_RESULT",
                        f"smoke_test_gameplay() must return Dictionary key '{key}'.",
                    )
                )
        registry = ComponentRegistry()
        for component_name in selected_components:
            component = registry.get(str(component_name))
            identity = _component_identity(component) if component else None
            if component is None or identity is None:
                continue
            _member_name, class_name = identity
            aliases = _component_aliases(scope, class_name)
            if not aliases:
                issues.append(
                    SemanticIssue(
                        "MISSING_ISOLATED_COMPONENT_TEST",
                        f"smoke_test_gameplay() must create an isolated {class_name} test instance.",
                    )
                )
                continue
            if "configure" in component.method_contracts and not any(
                re.search(rf"\b{re.escape(alias)}\.configure\s*\(", scope)
                for alias in aliases
            ):
                issues.append(
                    SemanticIssue(
                        "MISSING_ISOLATED_COMPONENT_TEST",
                        f"smoke_test_gameplay() must configure its isolated {class_name} instance.",
                    )
                )
            transition_methods = {
                name
                for name, contract in component.method_contracts.items()
                if contract.get("side_effects")
                and name not in {"configure", "restore", "clear", "set_auto_recycle"}
            }
            if transition_methods and not any(
                re.search(
                    rf"\b{re.escape(alias)}\.(?:"
                    + "|".join(re.escape(name) for name in sorted(transition_methods))
                    + r")\s*\(",
                    scope,
                )
                for alias in aliases
            ):
                issues.append(
                    SemanticIssue(
                        "MISSING_ISOLATED_COMPONENT_TEST",
                        f"smoke_test_gameplay() must execute a real {class_name} state transition.",
                    )
                )
    return issues


def validate_step_scope(
    path: str,
    content: str,
    step: dict[str, Any],
) -> list[SemanticIssue]:
    """Reject a broad new file when the frozen plan requested one bounded code unit."""
    if str(step.get("operation") or "") != "create_file":
        return []
    issues: list[SemanticIssue] = []
    kind = str(step.get("symbol_kind") or "")
    target = str(step.get("target") or "")
    if path.endswith(".gd"):
        try:
            functions = function_spans(content)
            declarations = declaration_spans(content)
        except ValueError as exc:
            return [SemanticIssue("INVALID_SOURCE_STRUCTURE", str(exc))]
        lifecycle = sorted(set(functions) & LIFECYCLE_FUNCTIONS)
        if lifecycle:
            issues.append(
                SemanticIssue(
                    "OUT_OF_SCOPE_LIFECYCLE",
                    "A bounded new game script may not introduce lifecycle functions: "
                    + ", ".join(lifecycle)
                    + ". Keep bootstrap behavior in the protected foundation.",
                )
            )
        if kind == "function":
            if set(functions) != {target}:
                issues.append(
                    SemanticIssue(
                        "STEP_SCOPE",
                        f"This step may create only function '{target}'. Found functions: "
                        f"{', '.join(sorted(functions)) or '<none>'}.",
                    )
                )
            if declarations:
                issues.append(
                    SemanticIssue(
                        "STEP_SCOPE",
                        "Move top-level declarations into separate planned steps: "
                        + ", ".join(sorted(declarations))
                        + ".",
                    )
                )
        elif kind == "declaration":
            if set(declarations) != {target} or functions:
                issues.append(
                    SemanticIssue(
                        "STEP_SCOPE",
                        f"This step may create only declaration '{target}' and no functions.",
                    )
                )
    return issues


def validate_generated_semantics(
    path: str,
    content: str,
    selected_components: Iterable[str],
    project_component_contracts: dict[str, Any] | None = None,
    *,
    require_component_members: bool = True,
) -> list[SemanticIssue]:
    selected_components = tuple(selected_components)
    issues = validate_replacement_text(content)
    if not path.endswith(".gd"):
        return issues

    functions = FUNCTION_RE.findall(content)
    duplicates = sorted({name for name in functions if functions.count(name) > 1})
    if duplicates:
        issues.append(
            SemanticIssue(
                "DUPLICATE_SYMBOL",
                f"Duplicate function declarations: {', '.join(duplicates)}.",
            )
        )

    if re.search(r"\bexport\s*\(", content) or ".instance()" in content:
        issues.append(
            SemanticIssue(
                "GODOT3_API",
                "Godot 3 syntax detected. Use Godot 4.7 @export and instantiate().",
            )
        )

    issues.extend(
        _component_binding_issues(
            content,
            selected_components,
            project_component_contracts,
            require_foundation_members=require_component_members,
        )
    )
    issues.extend(_double_initial_discard_issues(content, selected_components))

    card_literal = _literal_card_array(content)
    if card_literal:
        cards_literal, card_count = card_literal
        if "{" not in cards_literal:
            issues.append(
                SemanticIssue(
                    "COMPONENT_SHAPE",
                    "card_system.configure() requires an Array of card dictionaries, not strings.",
                )
            )
        elif not re.search(r"[\"']id[\"']\s*:", cards_literal):
            issues.append(
                SemanticIssue(
                    "COMPONENT_SHAPE",
                    "Every card dictionary passed to card_system.configure() requires a unique string id.",
                )
            )
        deal_match = re.search(r"card_system\.deal\(\s*(\d+)\s*\)", content)
        player_count = _literal_player_count(content)
        if deal_match and player_count is not None:
            players = player_count
            required = int(deal_match.group(1)) * players + 1
            if card_count < required:
                issues.append(
                    SemanticIssue(
                        "INSUFFICIENT_DECK",
                        f"Literal deck has {card_count} cards but deal plus initial discard needs at least {required}.",
                    )
                )

    if "card_system.top_discard()" in content and "card_system.start_discard()" not in content:
        issues.append(
            SemanticIssue(
                "DISCARD_NOT_STARTED",
                "The code reads top_discard() but never initializes the discard pile with start_discard().",
            )
        )

    smoke_functions = [
        name for name in functions if name.startswith("smoke_test_")
    ]
    for smoke_name in smoke_functions:
        smoke_body = _function_body(content, smoke_name)
        live_members = sorted(
            member
            for member in ("card_system", "turn_system", "inventory_system")
            if re.search(rf"\b{member}\.", smoke_body)
        )
        if live_members:
            issues.append(
                SemanticIssue(
                    "LIVE_SMOKE_MUTATION",
                    f"{smoke_name}() accesses live member(s): {', '.join(live_members)}. "
                    "Construct isolated test systems instead.",
                )
            )
        if re.search(
            r"\b(?:game_state\s*(?:=|\[)|initialize_game_state\s*\(|handle_game_action\s*\()",
            smoke_body,
        ):
            issues.append(
                SemanticIssue(
                    "LIVE_SMOKE_MUTATION",
                    f"{smoke_name}() must not mutate or drive the live match. Use local state "
                    "and isolated component instances.",
                )
            )
        if not re.search(r"[\"']ok[\"']\s*:\s*true", smoke_body, flags=re.IGNORECASE):
            continue
        evidence_tokens = (
            "assert(",
            "if ",
            ".configure(",
            ".deal(",
            ".hand(",
            ".advance(",
            "play_",
            "draw_",
        )
        if not any(token in smoke_body for token in evidence_tokens):
            issues.append(
                SemanticIssue(
                    "FAKE_SMOKE_TEST",
                    f"{smoke_name}() reports literal success without executing gameplay.",
                )
            )

    issues.extend(_undeclared_assignment_issues(content))
    issues.extend(_state_key_usage_issues(content))
    issues.extend(_component_result_issues(content, selected_components))
    issues.extend(_card_definition_id_issues(content, selected_components))
    issues.extend(_dictionary_literal_domain_issues(content))
    issues.extend(_card_transition_issues(content, selected_components))
    issues.extend(_action_legality_issues(content))
    issues.extend(_card_schema_access_issues(content))
    issues.extend(_signal_arity_issues(content))
    issues.extend(_collection_shape_issues(content))

    return issues
