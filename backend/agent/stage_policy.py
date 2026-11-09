"""Application-owned blueprint, stage, and implementation-step policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import get_close_matches
from hashlib import sha256
import json
import re
from typing import Any, Iterable

from .component_library import ComponentRegistry
from .game_contracts import validate_plan_against_contracts
from .source_structure import (
    LIFECYCLE_FUNCTIONS,
    PROTECTED_DECLARATIONS,
    PROTECTED_FUNCTIONS,
    declaration_spans,
    function_spans,
    scene_node_spans,
)


STRUCTURAL_KINDS = frozenset({"function", "declaration", "scene_node", "file"})
PURE_STAGE_ID = "domain_model"
STAGE_REQUIRED_HOOKS = {
    "initialization": frozenset({"initialize_game_state"}),
    "core_gameplay": frozenset({"handle_game_action"}),
    "game_flow": frozenset({"evaluate_game_state"}),
    "presentation": frozenset({"render_game"}),
    "verification": frozenset({"smoke_test_gameplay"}),
}
HOOK_STAGE_OWNERS = {
    hook: stage_id
    for stage_id, hooks in STAGE_REQUIRED_HOOKS.items()
    for hook in hooks
}
# Component lifecycle methods have universal stage ownership regardless of game genre.
# Later stages consume configured systems; they must not silently reset live state.
COMPONENT_METHOD_STAGE_OWNERS = {
    "configure": "initialization",
    "deal": "initialization",
    "start_discard": "initialization",
}
FOUNDATION_HOOK_PATH = "project/game/main.gd"
MAX_COMPONENT_METHODS_PER_STEP = 4
_PRESENTATION_TERMS = re.compile(
    r"\b(render|button|label|layout|theme|animation|input|mouse|touch|ui|visual)\b",
    re.IGNORECASE,
)
_GAME_FLOW_TERMS = re.compile(
    r"\b(victory|winner|win condition|terminal|restart|game over|game_is_active)\b",
    re.IGNORECASE,
)
_DECLARATION_WORK_RE = re.compile(
    r"(?:\b(?:define|declare|add)\w*\b[\s\S]{0,80}"
    r"\b(?:constant|member|signal)s?\b|"
    r"\b(?:constant|member|signal)s?\b[\s\S]{0,40}\b(?:define|declare)\w*\b)",
    re.IGNORECASE,
)
_NAMED_DECLARATION_CLAIM_RE = re.compile(
    r"\b(?:enum|constants?|const|variables?|var|members?|signals?)\s+"
    r"(?:named\s+|called\s+)?[`']?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_UNSUPPORTED_TYPE_ALIAS_RE = re.compile(
    r"\b(?:typed\s+dictionary|type\s+alias|struct)\b",
    re.IGNORECASE,
)


def _looks_like_declared_symbol(value: str) -> bool:
    """Exclude ordinary prose/type words while retaining conventional symbol forms."""
    return "_" in value or value.isupper() or any(char.isupper() for char in value[1:])


def _undocumented_component_api_error(
    *, step_id: str, owner: str, method: str, component: Any
) -> str:
    available = sorted(component.method_contracts)
    suggestions = get_close_matches(method, available, n=4, cutoff=0.2)
    verb = method.split("_", 1)[0]
    suggestions = list(
        dict.fromkeys([*suggestions, *[name for name in available if name.startswith(verb)]])
    )[:8]
    if not suggestions:
        suggestions = available[:8]
    hint = ", ".join(f"{owner}.{name}()" for name in suggestions)
    return (
        f"Step '{step_id}' references undocumented GameGen API {owner}.{method}(). "
        f"Use a documented method instead; closest available methods: {hint}."
    )


def _declaration_claims(description: str) -> list[str]:
    claims: list[str] = []
    for match in _NAMED_DECLARATION_CLAIM_RE.finditer(description):
        value = match.group(1)
        if _looks_like_declared_symbol(value) and value not in claims:
            claims.append(value)
    for value in re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b\s*(?==|\bis\b)", description):
        if value not in claims:
            claims.append(value)
    if re.search(r"\bconstants\b", description, re.IGNORECASE):
        for value in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", description):
            if value not in claims:
                claims.append(value)
    return claims


def _split_grouped_declaration_steps(steps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn explicit grouped declarations into ordered single-symbol structural steps."""
    expanded: list[dict[str, Any]] = []
    terminal_ids: dict[str, str] = {}
    for index, item in enumerate(steps, start=1):
        raw = deepcopy(item)
        raw_id = re.sub(
            r"[^a-z0-9]+", "_", str(raw.get("id") or f"step_{index}").lower()
        ).strip("_")
        raw["depends_on"] = [
            terminal_ids.get(str(value), str(value)) for value in raw.get("depends_on") or []
        ]
        if str(raw.get("symbol_kind") or "") != "declaration":
            expanded.append(raw)
            terminal_ids[raw_id] = raw_id
            continue

        symbol = str(raw.get("symbol") or "").strip()
        criteria = [str(value) for value in raw.get("acceptance_criteria") or []]
        description = " ".join([str(raw.get("purpose") or ""), *criteria])
        claims = [symbol, *[name for name in _declaration_claims(description) if name != symbol]]
        previous_id: str | None = None
        for claim_index, claim in enumerate(claims):
            split = deepcopy(raw)
            split_id = raw_id if claim_index == 0 else f"{raw_id}_{claim.lower()}"
            split["id"] = split_id
            split["symbol"] = claim
            split["purpose"] = (
                str(raw.get("purpose") or "")
                if claim_index == 0
                else f"Declare {claim} as specified by the stage plan."
            )
            matching = (
                list(criteria)
                if len(claims) == 1
                else [
                    fragment.strip(" .") + "."
                    for criterion in criteria
                    for fragment in re.split(r"[,;]", criterion)
                    if re.search(rf"\b{re.escape(claim)}\b", fragment)
                ]
            )
            split["acceptance_criteria"] = matching or [f"Declaration {claim} exists with the planned value."]
            if claim_index:
                split["required_dictionary_keys"] = []
            split["depends_on"] = [previous_id] if previous_id else list(raw.get("depends_on") or [])
            expanded.append(split)
            previous_id = split_id
        terminal_ids[raw_id] = previous_id or raw_id
    return expanded


def _defer_misplaced_domain_steps(
    steps: list[dict[str, Any]], component_members: set[str]
) -> list[dict[str, Any]]:
    """Drop non-executable or stateful work that belongs to a later stage."""
    deferred: dict[str, list[str]] = {}
    kept: list[dict[str, Any]] = []
    for raw in steps:
        step_id = str(raw.get("id") or "")
        description = " ".join(
            [str(raw.get("purpose") or ""), *map(str, raw.get("acceptance_criteria") or [])]
        )
        calls = [str(value) for value in raw.get("required_calls") or []]
        pseudo_type = (
            str(raw.get("symbol_kind") or "") == "declaration"
            and _UNSUPPORTED_TYPE_ALIAS_RE.search(description)
        )
        stateful = any(call.split(".", 1)[0] in component_members for call in calls)
        if pseudo_type or stateful:
            deferred[step_id] = [str(value) for value in raw.get("depends_on") or []]
            continue
        kept.append(raw)

    def resolve_dependency(value: str, seen: set[str] | None = None) -> list[str]:
        if value not in deferred:
            return [value]
        visited = set(seen or ())
        if value in visited:
            return []
        visited.add(value)
        return [
            resolved
            for dependency in deferred[value]
            for resolved in resolve_dependency(dependency, visited)
        ]

    for raw in kept:
        raw["depends_on"] = list(
            dict.fromkeys(
                resolved
                for dependency in raw.get("depends_on") or []
                for resolved in resolve_dependency(str(dependency))
            )
        )
    return kept


def _project_steps_to_current_stage(
    steps: list[dict[str, Any]],
    stage: dict[str, Any],
    component_members: set[str],
) -> list[dict[str, Any]]:
    """Defer work owned by another universal stage while preserving useful model design."""
    stage_id = str(stage.get("id") or "")
    required_hooks = STAGE_REQUIRED_HOOKS.get(stage_id, frozenset())
    deferred: dict[str, list[str]] = {}
    deferred_targets: set[str] = set()
    kept: list[dict[str, Any]] = []

    for item in steps:
        raw = deepcopy(item)
        step_id = str(raw.get("id") or "")
        target = str(raw.get("symbol") or "")
        purpose = str(raw.get("purpose") or "")
        owner = HOOK_STAGE_OWNERS.get(target)
        required_calls = [str(value) for value in raw.get("required_calls") or []]
        misplaced_component_calls = [
            call
            for call in required_calls
            if "." in call
            and call.split(".", 1)[0] in component_members
            and COMPONENT_METHOD_STAGE_OWNERS.get(call.split(".", 1)[1]) not in {None, stage_id}
        ]
        purpose_is_presentation = stage_id != "presentation" and bool(
            _PRESENTATION_TERMS.search(purpose)
        )
        purpose_is_game_flow = stage_id == "core_gameplay" and bool(
            _GAME_FLOW_TERMS.search(purpose)
        )
        verification_only = stage_id != "verification" and bool(
            re.match(r"^(?:smoke_test|test_)", target)
        )
        application_owned_transition = target in {
            "run_game",
            "build_game",
            "build_godot_game",
            "validate_godot_project",
        }
        if (
            (owner is not None and owner != stage_id)
            or (misplaced_component_calls and target not in required_hooks)
            or ((purpose_is_presentation or purpose_is_game_flow) and target not in required_hooks)
            or verification_only
            or application_owned_transition
        ):
            deferred[step_id] = [str(value) for value in raw.get("depends_on") or []]
            deferred_targets.add(target)
            continue

        acceptance = [
            str(value)
            for value in raw.get("acceptance_criteria") or []
            if not (
                (stage_id != "presentation" and _PRESENTATION_TERMS.search(str(value)))
                or (stage_id == "core_gameplay" and _GAME_FLOW_TERMS.search(str(value)))
            )
        ]
        if not acceptance:
            acceptance = [f"{target} implements only the responsibilities of {stage_id}."]
        raw["acceptance_criteria"] = acceptance
        if purpose_is_presentation or purpose_is_game_flow:
            raw["purpose"] = f"Implement {target} for the {stage_id} stage."
        kept.append(raw)

    def resolve_dependency(value: str, seen: set[str] | None = None) -> list[str]:
        if value not in deferred:
            return [value]
        visited = set(seen or ())
        if value in visited:
            return []
        visited.add(value)
        return [
            resolved
            for dependency in deferred[value]
            for resolved in resolve_dependency(dependency, visited)
        ]

    for raw in kept:
        raw["depends_on"] = list(
            dict.fromkeys(
                resolved
                for dependency in raw.get("depends_on") or []
                for resolved in resolve_dependency(str(dependency))
            )
        )
        raw["required_calls"] = [
            call
            for call in raw.get("required_calls") or []
            if str(call).split(".", 1)[0] not in deferred_targets
        ]
    return kept if kept else steps


def _preserve_completed_stage_steps(
    steps: list[dict[str, Any]], state: dict[str, Any], stage_id: str
) -> list[dict[str, Any]]:
    """Omit already-promoted work when a later step forces the same stage to replan."""
    prefix = f"{stage_id}:"
    completed_targets = {
        str(record.get("name") or "")[len(prefix):]
        for record in state.get("completed_stages") or []
        if str(record.get("name") or "").startswith(prefix)
    }
    if not completed_targets:
        return steps

    removed_steps = {
        str(step.get("id") or ""): str(step.get("symbol") or "")
        for step in steps
        if str(step.get("symbol") or "") in completed_targets
    }
    preserved: list[dict[str, Any]] = []
    for item in steps:
        raw = deepcopy(item)
        if str(raw.get("symbol") or "") in completed_targets:
            continue
        raw["depends_on"] = [
            removed_steps.get(str(dependency), str(dependency))
            for dependency in raw.get("depends_on") or []
        ]
        preserved.append(raw)
    return preserved


def _omit_existing_foundation_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consume redundant plans to rewrite protected foundation symbols."""
    protected = PROTECTED_FUNCTIONS | PROTECTED_DECLARATIONS | LIFECYCLE_FUNCTIONS
    removed_ids = {
        str(step.get("id") or "")
        for step in steps
        if str(step.get("symbol") or "") in protected
    }
    if not removed_ids or len(removed_ids) == len(steps):
        return steps
    projected: list[dict[str, Any]] = []
    for item in steps:
        raw = deepcopy(item)
        if str(raw.get("id") or "") in removed_ids:
            continue
        raw["depends_on"] = [
            dependency
            for dependency in raw.get("depends_on") or []
            if str(dependency) not in removed_ids
        ]
        projected.append(raw)
    return projected


def _normalize_large_collection_steps(
    steps: Iterable[dict[str, Any]], *, minimum_size: int = 32
) -> list[dict[str, Any]]:
    """Represent large generated collections as compact factory functions."""
    normalized: list[dict[str, Any]] = []
    for item in steps:
        raw = deepcopy(item)
        description = " ".join(
            [str(raw.get("purpose") or ""), *map(str, raw.get("acceptance_criteria") or [])]
        )
        sizes = [int(value) for value in re.findall(r"\b(\d{2,})\b", description)]
        is_large_collection = (
            str(raw.get("symbol_kind") or "") == "declaration"
            and re.search(r"\b(?:Array|Dictionary|entries|records|items|cards)\b", description)
            and any(size >= minimum_size for size in sizes)
        )
        if is_large_collection:
            original = str(raw.get("symbol") or "collection")
            factory = "create_" + re.sub(r"[^a-z0-9]+", "_", original.lower()).strip("_")
            raw["symbol"] = factory
            raw["symbol_kind"] = "function"
            raw["purpose"] = (
                f"Build {original} programmatically with loops; do not enumerate the entire "
                "collection as a literal."
            )
            raw["acceptance_criteria"] = [
                f"Function {factory} returns the planned collection.",
                *[
                    re.sub(
                        rf"\b(?:Constant|Variable)\s+{re.escape(original)}\b(?:\s+is)?",
                        f"The returned collection from {factory}",
                        str(value),
                        flags=re.IGNORECASE,
                    )
                    for value in raw.get("acceptance_criteria") or []
                ],
                "The implementation uses compact generation loops rather than a large literal.",
            ]
        normalized.append(raw)
    return normalized


@dataclass(frozen=True)
class StageSpec:
    """A universal development stage shared by every generated game."""

    id: str
    title: str
    purpose: str
    dependencies: tuple[str, ...] = ()
    foundation_targets: tuple[str, ...] = ()
    planning_guidance: tuple[str, ...] = ()


UNIVERSAL_STAGE_SPECS = (
    StageSpec(
        "domain_model",
        "Domain model",
        "Define game-specific state, data shapes, constants, and pure rule helpers.",
        foundation_targets=(),
        planning_guidance=(
            "Prefer typed declarations and pure helpers.",
            "Do not initialize systems or replace the protected game_state member in this stage.",
            "Prefer game-specific helper functions in project/game/main.gd; use a new script only when a later step explicitly wires it into the playable scene.",
            "Extend a copied GameGen component only when the generic mechanism truly needs it.",
        ),
    ),
    StageSpec(
        "initialization",
        "Initialization",
        "Create a deterministic playable starting state using the domain model and selected systems.",
        dependencies=("domain_model",),
        foundation_targets=("initialize_game_state",),
        planning_guidance=(
            "Implement initialize_game_state() and narrowly required setup helpers.",
            "Do not implement presentation or tests in this stage.",
        ),
    ),
    StageSpec(
        "core_gameplay",
        "Core gameplay",
        "Implement legal player actions, state transitions, and the central game loop.",
        dependencies=("initialization",),
        foundation_targets=("handle_game_action",),
        planning_guidance=(
            "Split independent rules into small named functions.",
            "Use selected GameGen systems through their exact local contracts.",
        ),
    ),
    StageSpec(
        "game_flow",
        "Game flow",
        "Finish victory, terminal state, restart, and complete-match behavior.",
        dependencies=("core_gameplay",),
        foundation_targets=("evaluate_game_state", "game_reset signal"),
        planning_guidance=(
            "Keep end-condition evaluation separate from rendering.",
            "Ensure reset produces the same valid invariants as first initialization.",
        ),
    ),
    StageSpec(
        "presentation",
        "Presentation and interaction",
        "Render a responsive interface and connect mouse or touch input to legal game actions.",
        dependencies=("game_flow",),
        foundation_targets=("render_game", "Playfield"),
        planning_guidance=(
            "Implement render_game() and focused render or input helpers.",
            "Connect controls to protected submit_game_action() and preserve private information where applicable.",
            "Use procedural Godot UI and project-local assets only.",
        ),
    ),
    StageSpec(
        "verification",
        "Verification",
        "Add isolated smoke tests for setup, legal actions, progression, and victory.",
        dependencies=("presentation",),
        foundation_targets=("smoke_test_gameplay",),
        planning_guidance=(
            "Tests must use fresh state and must not mutate the live playable match.",
            "Implement smoke_test_gameplay() with multiple focused assertions.",
        ),
    ),
)

def create_game_blueprint() -> dict[str, Any]:
    """Create the durable application-owned blueprint skeleton."""
    return {
        "version": 2,
        "stages": [
            {
                "id": stage.id,
                "title": stage.title,
                "purpose": stage.purpose,
                "dependencies": list(stage.dependencies),
                "foundation_targets": list(stage.foundation_targets),
                "planning_guidance": list(stage.planning_guidance),
                "status": "needs_plan",
                "plan_summary": "",
                "steps": [],
                "completed_step_ids": [],
            }
            for stage in UNIVERSAL_STAGE_SPECS
        ],
    }


def _blueprint_stages(state: dict[str, Any]) -> list[dict[str, Any]]:
    blueprint = state.get("game_blueprint") or {}
    stages = blueprint.get("stages") if isinstance(blueprint, dict) else None
    return stages if isinstance(stages, list) else []


def next_stage(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first incomplete stage whose dependencies are complete."""
    stages = _blueprint_stages(state)
    if not stages:
        return None
    completed = {str(stage.get("id")) for stage in stages if stage.get("status") == "completed"}
    for stage in stages:
        if stage.get("status") == "completed":
            continue
        dependencies = {str(value) for value in stage.get("dependencies") or []}
        if dependencies - completed:
            return None
        return deepcopy(stage)
    return None


def current_step(state: dict[str, Any]) -> dict[str, Any] | None:
    stage = next_stage(state)
    if stage is None or stage.get("status") == "needs_plan":
        return None
    completed = set(stage.get("completed_step_ids") or [])
    for step in stage.get("steps") or []:
        if step.get("id") not in completed:
            return deepcopy(step)
    return None


def generation_is_complete(state: dict[str, Any]) -> bool:
    stages = _blueprint_stages(state)
    return bool(stages) and all(stage.get("status") == "completed" for stage in stages)


def required_stage_targets(_components: Iterable[str]) -> tuple[str, ...]:
    """Compatibility helper returning universal stage identifiers."""
    return tuple(stage.id for stage in UNIVERSAL_STAGE_SPECS)


def stage_plan_fingerprint(stage_id: str, steps: Iterable[dict[str, Any]]) -> str:
    """Fingerprint executable plan shape while ignoring prose and generated step ids."""
    shape = [
        {
            "path": str(step.get("path") or ""),
            "target": str(step.get("target") or step.get("symbol") or ""),
            "kind": str(step.get("symbol_kind") or ""),
            "operation": str(step.get("operation") or ""),
            "calls": sorted(str(value) for value in step.get("required_calls") or []),
            "keys": sorted(
                str(value)
                for value in (
                    step.get("required_dictionary_keys")
                    or step.get("required_state_keys")
                    or []
                )
            ),
            "amends_contract": bool(step.get("amends_contract")),
        }
        for step in steps
    ]
    payload = json.dumps(
        {"stage_id": stage_id, "steps": shape},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def validate_stage_plan(
    state: dict[str, Any],
    *,
    plan_summary: str,
    steps: Iterable[dict[str, Any]],
    sources: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a semantic model-authored plan into deterministic source operations."""
    stage = next_stage(state)
    if stage is None:
        raise ValueError("No incomplete stage is available for planning.")
    if stage.get("status") != "needs_plan":
        raise ValueError(f"Stage '{stage['id']}' already has a frozen plan.")
    if not str(plan_summary).strip():
        raise ValueError("A stage plan requires a concise plan_summary.")

    steps = _split_grouped_declaration_steps(_normalize_large_collection_steps(steps))
    contract_errors = validate_plan_against_contracts(
        state,
        stage_id=str(stage.get("id") or ""),
        steps=steps,
    )
    if contract_errors:
        raise ValueError("Stage plan contract issues: " + "; ".join(contract_errors[:6]))
    selected_component_paths = set(state.get("required_component_reads") or [])
    selected_components = {
        component.name: component
        for name in state.get("components") or []
        if (component := ComponentRegistry().get(str(name))) is not None
    }
    component_members: dict[str, Any] = {}
    for component in selected_components.values():
        member_match = re.search(r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)", component.foundation_member)
        if member_match:
            component_members[member_match.group(1)] = component
    if stage.get("id") == PURE_STAGE_ID:
        eligible_steps = _defer_misplaced_domain_steps(steps, set(component_members))
        if eligible_steps:
            steps = eligible_steps
    steps = _project_steps_to_current_stage(list(steps), stage, set(component_members))
    steps = _preserve_completed_stage_steps(steps, state, str(stage.get("id") or ""))
    steps = _omit_existing_foundation_steps(steps)
    source_by_path = sources or {}
    known_files = set(state.get("files") or {}) | set(source_by_path)
    completed_stage_ids = {
        str(candidate.get("id") or "")
        for candidate in _blueprint_stages(state)
        if candidate.get("status") == "completed"
    }
    existing_functions = {
        function_name
        for source_content in source_by_path.values()
        for function_name in function_spans(source_content)
    }
    preflight_errors: list[str] = []
    earlier_step_ids: set[str] = set()
    planned_targets: set[str] = set()
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            preflight_errors.append(f"Step {index} must be an object.")
            continue
        step_id = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(raw.get("id") or f"step_{index}").lower(),
        ).strip("_")
        path = str(raw.get("path") or "").strip()
        target = str(raw.get("symbol") or "").strip()
        planned_targets.add(target)
        unknown_dependencies = [
            str(value).strip()
            for value in raw.get("depends_on") or []
            if str(value).strip()
            and str(value).strip() not in earlier_step_ids
            and str(value).strip() not in existing_functions
            and str(value).strip() not in completed_stage_ids
        ]
        if unknown_dependencies:
            preflight_errors.append(
                f"Step '{step_id}' dependencies must name earlier steps: "
                + ", ".join(unknown_dependencies)
            )
        if not path.startswith(("project/game/", "project/assets/", "project/addons/gamegen/")):
            preflight_errors.append(
                f"Step '{step_id}' path must be under project/game/ or project/assets/."
            )
        description = " ".join(
            [
                str(raw.get("purpose") or ""),
                *[str(value) for value in raw.get("acceptance_criteria") or []],
            ]
        )
        required_calls = [
            " ".join(str(value).split())
            for value in raw.get("required_calls") or []
            if str(value).strip()
        ]
        alternative_pairs = re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\(\)\s+or\s+"
            r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\(\)",
            description,
            re.IGNORECASE,
        )
        for left, right in alternative_pairs:
            if left in required_calls and right in required_calls:
                preflight_errors.append(
                    f"Step '{step_id}' describes {left}() or {right}() as alternatives but "
                    "marks both required. Choose one implementation and list only its call."
                )
        for call in required_calls:
            if "." not in call:
                continue
            owner, method = call.split(".", 1)
            component = component_members.get(owner)
            if component is not None and method not in component.method_contracts:
                preflight_errors.append(
                    _undocumented_component_api_error(
                        step_id=step_id,
                        owner=owner,
                        method=method,
                        component=component,
                    )
                )
            method_stage = COMPONENT_METHOD_STAGE_OWNERS.get(method)
            if (
                component is not None
                and stage.get("id") != PURE_STAGE_ID
                and method_stage not in {None, stage.get("id")}
            ):
                preflight_errors.append(
                    f"Step '{step_id}' calls {call}(), which belongs to the "
                    f"'{method_stage}' stage and cannot run during '{stage.get('id')}'."
                )
        component_calls = [
            call
            for call in required_calls
            if call.split(".", 1)[0] in component_members
        ]
        is_initialization_hook = (
            stage.get("id") == "initialization" and target == "initialize_game_state"
        )
        if (
            len(set(component_calls)) > MAX_COMPONENT_METHODS_PER_STEP
            and not is_initialization_hook
        ):
            preflight_errors.append(
                f"Step '{step_id}' requires {len(set(component_calls))} distinct stateful "
                f"component methods; the limit is {MAX_COMPONENT_METHODS_PER_STEP}. Split "
                "the behavior into small helpers and make the dispatcher call those helpers."
            )
        if stage.get("id") != "presentation" and _PRESENTATION_TERMS.search(description):
            preflight_errors.append(
                f"Step '{step_id}' contains UI/render/input work reserved for the presentation stage."
            )
        if stage.get("id") == "core_gameplay" and _GAME_FLOW_TERMS.search(description):
            preflight_errors.append(
                f"Step '{step_id}' contains victory/terminal/restart work reserved for game_flow."
            )
        earlier_step_ids.add(step_id)

    missing_hooks = sorted(
        STAGE_REQUIRED_HOOKS.get(str(stage.get("id") or ""), frozenset()) - planned_targets
    )
    if missing_hooks:
        preflight_errors.append(
            f"Stage '{stage.get('id')}' must implement required hook(s): "
            + ", ".join(missing_hooks)
        )
    if preflight_errors:
        raise ValueError("Stage plan issues: " + "; ".join(preflight_errors[:10]))

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_symbols: set[tuple[str, str]] = set()
    planned_new_paths: set[str] = set()
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError("Each stage step must be an object.")
        path = str(raw.get("path") or "").strip()
        symbol = str(raw.get("symbol") or "").strip()
        symbol_kind = str(raw.get("symbol_kind") or "").strip()
        purpose = " ".join(str(raw.get("purpose") or "").split())
        acceptance = [
            " ".join(str(value).split())
            for value in raw.get("acceptance_criteria") or []
            if str(value).strip()
        ]
        step_id = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(raw.get("id") or f"step_{index}").lower(),
        ).strip("_")
        if not step_id or step_id in seen_ids:
            raise ValueError(f"Stage step ids must be unique; invalid id '{step_id}'.")
        if not purpose or not acceptance:
            raise ValueError(f"Stage step '{step_id}' requires purpose and acceptance criteria.")
        if not path:
            raise ValueError(f"Stage step '{step_id}' requires a project-relative path.")
        if symbol_kind not in STRUCTURAL_KINDS:
            raise ValueError(
                f"Stage step '{step_id}' requires symbol_kind to be one of: "
                + ", ".join(sorted(STRUCTURAL_KINDS))
                + "."
            )
        requested_dependencies = [
            str(value).strip()
            for value in raw.get("depends_on") or []
            if str(value).strip() and str(value).strip() not in completed_stage_ids
        ]
        depends_on = [value for value in requested_dependencies if value in seen_ids]
        unknown_dependencies = [
            value
            for value in requested_dependencies
            if value not in seen_ids and value not in existing_functions
        ]
        if unknown_dependencies:
            raise ValueError(
                f"Stage step '{step_id}' dependencies must name earlier steps: "
                + ", ".join(unknown_dependencies)
            )
        required_calls = list(
            dict.fromkeys(
                " ".join(str(value).split())
                for value in raw.get("required_calls") or []
                if str(value).strip()
            )
        )
        earlier_steps = {str(step.get("id") or ""): step for step in normalized}
        for dependency_id in depends_on:
            dependency = earlier_steps.get(dependency_id) or {}
            if dependency.get("symbol_kind") != "function":
                continue
            dependency_target = str(dependency.get("target") or "").strip()
            if dependency_target and dependency_target not in required_calls:
                required_calls.append(dependency_target)
        required_dictionary_keys = list(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    raw.get("required_dictionary_keys")
                    or raw.get("required_state_keys")
                    or []
                )
                if str(value).strip()
            )
        )
        if symbol_kind in {"declaration", "scene_node", "file"} and required_calls:
            raise ValueError(
                f"Stage step '{step_id}' cannot require executable calls from a "
                f"{symbol_kind} unit. Move those calls to a planned function step."
            )
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", call) for call in required_calls):
            raise ValueError(
                f"Stage step '{step_id}' required_calls must be callable names such as "
                "card_system.configure or calculate_score."
            )
        for call in required_calls:
            if "." not in call:
                continue
            owner, method = call.split(".", 1)
            component = component_members.get(owner)
            if component is None:
                if owner.endswith("_system"):
                    raise ValueError(
                        f"Stage step '{step_id}' references unavailable component member "
                        f"'{owner}'. Use a selected foundation member or a planned local helper."
                    )
                continue
            if method not in component.method_contracts:
                raise ValueError(
                    _undocumented_component_api_error(
                        step_id=step_id,
                        owner=owner,
                        method=method,
                        component=component,
                    )
                )
        if any(
            len(value) > 80 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
            for value in required_dictionary_keys
        ):
            raise ValueError(f"Stage step '{step_id}' has an invalid required dictionary key.")
        described_calls = set(
            re.findall(
                r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b(?=\s*\()",
                " ".join([purpose, *acceptance]),
            )
        )
        undeclared_calls = sorted(described_calls - set(required_calls))
        if undeclared_calls:
            raise ValueError(
                f"Stage step '{step_id}' mentions required calls but does not declare them in "
                f"required_calls: {', '.join(undeclared_calls)}."
            )
        if path.startswith("project/addons/gamegen/"):
            if path not in selected_component_paths:
                raise ValueError("A stage may extend only a selected project-local GameGen component.")
            if not bool(raw.get("extends_component")):
                raise ValueError(
                    f"Component step '{step_id}' must explicitly set extends_component=true."
                )
            coverage = (state.get("read_coverage") or {}).get(path) or {}
            line_count = int(coverage.get("line_count", 0))
            ranges = sorted(coverage.get("ranges") or [], key=lambda value: int(value[0]))
            covered_until = 0
            for start, end in ranges:
                if int(start) > covered_until + 1:
                    break
                covered_until = max(covered_until, int(end))
            if line_count <= 0 or covered_until < line_count:
                raise ValueError(
                    f"Component step '{step_id}' requires complete current source coverage for {path}."
                )
        elif not path.startswith(("project/game/", "project/assets/")):
            raise ValueError(
                "Stage steps must target project/game/, project/assets/, or a selected "
                "project-local GameGen component."
            )
        source = source_by_path.get(path)
        if source is None and path in known_files:
            raise ValueError(
                f"Stage step '{step_id}' cannot be resolved because exact source for {path} "
                "is unavailable. Refresh that file before planning."
            )
        if stage.get("id") == PURE_STAGE_ID:
            component_calls = [
                call for call in required_calls if call.split(".", 1)[0] in component_members
            ]
            if component_calls:
                raise ValueError(
                    f"Stage '{PURE_STAGE_ID}' defines pure data and rules and cannot perform "
                    "stateful GameGen component calls. Move these calls to initialization or "
                    f"gameplay: {', '.join(component_calls)}."
                )
        owning_stage = HOOK_STAGE_OWNERS.get(symbol)
        if owning_stage and owning_stage != stage.get("id"):
            raise ValueError(
                f"Foundation hook '{symbol}' belongs to stage '{owning_stage}', not "
                f"'{stage.get('id')}'. Choose a stage-local helper name."
            )
        if owning_stage and path != FOUNDATION_HOOK_PATH:
            raise ValueError(
                f"Foundation hook '{symbol}' must update {FOUNDATION_HOOK_PATH}, not {path}."
            )
        step_description = " ".join([purpose, *acceptance])
        if stage.get("id") != "presentation" and _PRESENTATION_TERMS.search(
            step_description
        ):
            raise ValueError(
                f"Stage '{stage.get('id')}' may not implement UI, rendering, or input. "
                "Move that behavior to the presentation stage."
            )
        if stage.get("id") == "core_gameplay" and _GAME_FLOW_TERMS.search(step_description):
            raise ValueError(
                "Core gameplay implements legal actions and transitions, not victory, terminal, "
                "or restart behavior. Move that work to the game_flow stage."
            )
        if symbol_kind == "function" and _DECLARATION_WORK_RE.search(
            " ".join([purpose, *acceptance])
        ):
            raise ValueError(
                f"Function step '{step_id}' also promises top-level declarations. Plan each "
                "constant, member, or signal as its own declaration step, then plan the function."
            )
        if symbol_kind == "declaration":
            declaration_description = " ".join([purpose, *acceptance])
            if _UNSUPPORTED_TYPE_ALIAS_RE.search(declaration_description):
                raise ValueError(
                    f"Declaration step '{step_id}' requests a type alias or struct, which "
                    "GDScript 4.7 does not support. Use ordinary Dictionary values with explicit "
                    "required_dictionary_keys, or plan one concrete var, const, signal, or enum."
                )
            claimed_names = set(_declaration_claims(declaration_description))
            unrelated_claims = sorted(claimed_names - {symbol}, key=str.lower)
            if unrelated_claims:
                raise ValueError(
                    f"Declaration step '{step_id}' targets only '{symbol}' but also promises "
                    f"other declarations: {', '.join(unrelated_claims)}. Plan each declaration "
                    "as its own ordered step."
                )

        if source is None:
            if path.endswith(".gd"):
                if symbol_kind not in {"function", "declaration"} or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", symbol
                ):
                    raise ValueError(
                        f"New GDScript file step '{step_id}' requires one named function or "
                        "declaration. The backend will enforce that bounded first code unit."
                    )
            elif path.endswith(".tscn") and symbol_kind != "file":
                raise ValueError(
                    f"New scene step '{step_id}' must use symbol_kind='file'."
                )
            elif not path.endswith((".gd", ".tscn")) and symbol_kind != "file":
                raise ValueError(
                    f"New asset step '{step_id}' must use symbol_kind='file'."
                )
            if path in planned_new_paths:
                if symbol_kind == "function":
                    operation = "insert_function"
                elif symbol_kind == "declaration":
                    operation = "insert_declaration"
                else:
                    raise ValueError(
                        f"Stage step '{step_id}' cannot recreate planned file {path}."
                    )
            else:
                operation = "create_file"
                planned_new_paths.add(path)
            target = symbol
            insert_after = ""
        elif path.endswith(".gd"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
                raise ValueError(
                    f"Stage step '{step_id}' requires a valid GDScript symbol name."
                )
            functions = function_spans(source)
            declarations = declaration_spans(source)
            if symbol in functions:
                if symbol_kind != "function":
                    raise ValueError(
                        f"Stage step '{step_id}' names function '{symbol}' but declares "
                        f"symbol_kind='{symbol_kind}'."
                    )
                operation = "replace_function_body"
            elif symbol in declarations:
                if symbol_kind != "declaration":
                    raise ValueError(
                        f"Stage step '{step_id}' names declaration '{symbol}' but declares "
                        f"symbol_kind='{symbol_kind}'."
                    )
                operation = "replace_declaration"
            else:
                if symbol_kind == "function":
                    operation = "insert_function"
                elif symbol_kind == "declaration":
                    operation = "insert_declaration"
                else:
                    raise ValueError(
                        f"Existing GDScript step '{step_id}' must target a function or declaration."
                    )
            target = symbol
            insert_after = ""
        elif path.endswith(".tscn"):
            if symbol_kind != "scene_node":
                raise ValueError(
                    f"Existing scene step '{step_id}' must use symbol_kind='scene_node'."
                )
            if symbol not in scene_node_spans(source):
                raise ValueError(
                    f"Stage step '{step_id}' names unknown scene node '{symbol}'. "
                    "Create new scene structure in a dedicated new file instead."
                )
            operation = "replace_scene_node"
            target = symbol
            insert_after = ""
        else:
            raise ValueError(
                f"Existing file {path} has no supported structural editor. Use a new generated file."
            )
        if target in PROTECTED_FUNCTIONS | PROTECTED_DECLARATIONS | LIFECYCLE_FUNCTIONS:
            raise ValueError(
                f"Stage step '{step_id}' targets protected foundation symbol '{target}'."
            )
        if path.startswith("project/addons/gamegen/") and operation != "insert_function":
            raise ValueError(
                f"Component step '{step_id}' must add a new function; existing component "
                "behavior and signatures are protected."
            )
        symbol_key = (path, target)
        if operation != "create_file" and symbol_key in seen_symbols:
            raise ValueError(f"The stage plan targets {path}:{target} more than once.")
        seen_ids.add(step_id)
        seen_symbols.add(symbol_key)
        normalized.append(
            {
                "id": step_id,
                "operation": operation,
                "path": path,
                "target": target,
                "insert_after": insert_after,
                "purpose": purpose[:500],
                "acceptance_criteria": acceptance[:6],
                "depends_on": depends_on,
                "required_calls": required_calls[:8],
                "required_dictionary_keys": required_dictionary_keys[:8],
                "symbol_kind": symbol_kind,
                "extends_component": path.startswith("project/addons/gamegen/"),
                "amends_contract": bool(raw.get("amends_contract")),
                "amendment_reason": " ".join(
                    str(raw.get("amendment_reason") or "").split()
                )[:500],
                "status": "pending",
            }
        )
    if not normalized:
        raise ValueError("A stage plan must contain at least one implementation step.")
    if len(normalized) > 12:
        raise ValueError("A stage plan may contain at most 12 bounded steps.")
    required_hooks = STAGE_REQUIRED_HOOKS.get(str(stage.get("id") or ""), frozenset())
    planned_targets = {str(step.get("target") or "") for step in normalized}
    missing_hooks = sorted(required_hooks - planned_targets)
    if missing_hooks:
        raise ValueError(
            f"Stage '{stage.get('id')}' must implement its playable foundation hook(s): "
            + ", ".join(missing_hooks)
            + ". Plan helpers before the hook when the hook depends on them."
        )
    if stage.get("id") == "initialization":
        required_configuration = {
            f"{member}.configure"
            for member, component in component_members.items()
            if "configure" in component.method_contracts
        }
        initialization_step = next(
            step
            for step in normalized
            if step.get("target") == "initialize_game_state"
        )
        declared_calls = {
            str(call) for call in initialization_step.get("required_calls") or []
        }
        missing_configuration = sorted(required_configuration - declared_calls)
        if missing_configuration:
            raise ValueError(
                "Initialization plan must configure every selected stateful GameGen foundation "
                "member directly in initialize_game_state(). Add these exact required_calls: "
                + ", ".join(missing_configuration)
                + "."
            )
    fingerprint = stage_plan_fingerprint(str(stage.get("id") or ""), normalized)
    if any(
        entry.get("stage_id") == stage.get("id")
        and entry.get("fingerprint") == fingerprint
        for entry in state.get("rejected_stage_plans") or []
    ):
        raise ValueError(
            f"Stage '{stage.get('id')}' repeats a plan that already failed. Change its step "
            "boundaries, helper structure, or required call flow materially."
        )
    return normalized


def freeze_stage_plan(
    state: dict[str, Any],
    *,
    plan_summary: str,
    steps: list[dict[str, Any]],
) -> None:
    stage = next_stage(state)
    if stage is None:
        raise ValueError("No stage is available for planning.")
    for stored in _blueprint_stages(state):
        if stored.get("id") == stage.get("id"):
            stored.update(
                {
                    "status": "in_progress",
                    "plan_summary": " ".join(plan_summary.split())[:1_000],
                    "steps": deepcopy(steps),
                    "completed_step_ids": [],
                }
            )
            state["active_stage_id"] = stored["id"]
            state["active_step_id"] = steps[0]["id"]
            state["phase"] = "ready_to_implement_step"
            state["step_correction"] = None
            state["step_attempt_failures"] = 0
            state["rejected_step_signatures"] = []
            state["rejected_step_strategy_fingerprints"] = []
            state["stage_retry_directive"] = None
            return
    raise ValueError("Current stage disappeared while freezing its plan.")


def reset_active_stage_for_replan(
    state: dict[str, Any],
    *,
    errors: Iterable[str],
) -> None:
    """Discard an unsatisfiable frozen plan while preserving committed project files."""
    stage_id = str(state.get("active_stage_id") or "")
    step = current_step(state) or {}
    if not stage_id:
        raise ValueError("No active stage is available for replanning.")
    compact_errors = [" ".join(str(error).split())[:300] for error in errors][:4]
    for stage in _blueprint_stages(state):
        if stage.get("id") != stage_id:
            continue
        failed_plan = {
            "stage_id": stage_id,
            "plan_summary": str(stage.get("plan_summary") or "")[:500],
            "step_id": str(step.get("id") or ""),
            "target": str(step.get("target") or ""),
            "fingerprint": stage_plan_fingerprint(stage_id, stage.get("steps") or []),
            "errors": compact_errors,
        }
        history = [*list(state.get("rejected_stage_plans") or []), failed_plan][-10:]
        counts = dict(state.get("stage_replan_counts") or {})
        counts[stage_id] = int(counts.get(stage_id, 0)) + 1
        stage.update(
            {
                "status": "needs_plan",
                "plan_summary": "",
                "steps": [],
                "completed_step_ids": [],
            }
        )
        state.update(
            {
                "active_stage_id": "",
                "active_step_id": "",
                "phase": "needs_stage_plan",
                "step_correction": None,
                "step_attempt_failures": 0,
                "rejected_step_signatures": [],
                "rejected_step_strategy_fingerprints": [],
                "rejected_stage_plans": history,
                "stage_replan_counts": counts,
                "stage_retry_directive": {
                    "stage_id": stage_id,
                    "failed_step_id": failed_plan["step_id"],
                    "failed_target": failed_plan["target"],
                    "errors": compact_errors,
                    "instruction": (
                        "Create a materially different plan for this stage. Fix the contract "
                        "contradiction instead of resubmitting the failed step. Preserve behavior "
                        "already committed in project files and keep each new step independently "
                        "satisfiable."
                    ),
                },
            }
        )
        return
    raise ValueError(f"Active stage '{stage_id}' was not found in the blueprint.")


def advance_blueprint_after_commit(state: dict[str, Any]) -> None:
    """Mark exactly one active step complete after atomic candidate promotion."""
    stage_id = str(state.get("active_stage_id") or "")
    step_id = str(state.get("active_step_id") or "")
    if not stage_id or not step_id:
        return
    for stage in _blueprint_stages(state):
        if stage.get("id") != stage_id:
            continue
        completed = list(stage.get("completed_step_ids") or [])
        if step_id not in completed:
            completed.append(step_id)
        stage["completed_step_ids"] = completed
        for step in stage.get("steps") or []:
            if step.get("id") == step_id:
                step["status"] = "completed"
        pending = [
            step for step in stage.get("steps") or [] if step.get("id") not in set(completed)
        ]
        if pending:
            state["active_step_id"] = pending[0]["id"]
            state["phase"] = "ready_to_implement_step"
        else:
            stage["status"] = "completed"
            state["active_stage_id"] = ""
            state["active_step_id"] = ""
            following = next_stage(state)
            state["phase"] = "needs_stage_plan" if following else "ready_to_build"
        return


def operation_targets(edits: Iterable[Any]) -> tuple[str, ...]:
    targets: list[str] = []
    for edit in edits:
        for operation in edit.operations:
            target = str(operation.target or "").strip()
            if operation.operation == "insert_function":
                source = str(operation.new_text or "").lstrip()
                match = re.match(r"func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source)
                target = match.group(1) if match else target
            if target:
                targets.append(target)
    return tuple(dict.fromkeys(targets))


def recognized_stage_targets(targets: Iterable[str], state: dict[str, Any] | None = None) -> tuple[str, ...]:
    if state is None:
        return tuple(dict.fromkeys(targets))
    if not state.get("game_blueprint"):
        return ()
    step = current_step(state)
    expected = str((step or {}).get("target") or "")
    return tuple(target for target in targets if not expected or target == expected)


def derive_stage_name(edits: Iterable[Any], state: dict[str, Any]) -> str:
    if state.get("phase") == "repair" and state.get("candidate_stage_name"):
        return str(state["candidate_stage_name"])
    stage = next_stage(state)
    step = current_step(state)
    if stage and step:
        return f"{stage['id']}:{step['id']}"
    targets = operation_targets(edits)
    return "implement_" + "_and_".join(targets) if targets else "bounded_source_update"


def dependency_errors(targets: Iterable[str], state: dict[str, Any]) -> list[str]:
    """Ensure an edit implements only the application-selected active step."""
    if state.get("phase") == "repair":
        return []
    if not state.get("game_blueprint"):
        return []
    stage = next_stage(state)
    step = current_step(state)
    if stage is None:
        return ["All blueprint stages are complete; no implementation edit is allowed."]
    if stage.get("status") == "needs_plan":
        return [f"Stage '{stage['id']}' must be planned before implementation."]
    if step is None:
        return [f"Stage '{stage['id']}' has no pending implementation step."]
    actual = tuple(dict.fromkeys(str(target) for target in targets if str(target)))
    expected = str(step.get("target") or "")
    if step.get("operation") == "create_file":
        return []
    if actual != (expected,):
        return [
            f"The active step is '{step['id']}' and must modify only '{expected}'. "
            f"Received targets: {', '.join(actual) or '<none>'}."
        ]
    return []
