"""Persistent, application-owned state for one game-generation chat."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable

from asgiref.sync import sync_to_async
from django.db import close_old_connections, transaction

from gamegen.models import GameChat
from gamegen.models.generated_game import default_execution_state
from .game_contracts import compact_game_contracts
from .source_structure import declaration_spans, function_spans, scene_node_spans
from .stage_policy import current_step, generation_is_complete, next_stage


class GenerationStateError(RuntimeError):
    pass


def unread_required_paths(state: dict[str, Any], field: str) -> list[str]:
    """Return required paths whose full current contents have not been read."""
    coverage = state.get("read_coverage") or {}
    missing: list[str] = []
    for path in state.get(field) or []:
        record = coverage.get(path) or {}
        line_count = int(record.get("line_count", 0))
        ranges = record.get("ranges") or []
        if line_count <= 0 or not ranges:
            missing.append(path)
            continue
        merged_end = 0
        for raw_range in sorted(ranges, key=lambda value: int(value[0])):
            start, end = int(raw_range[0]), int(raw_range[1])
            if start > merged_end + 1:
                break
            merged_end = max(merged_end, end)
        if merged_end < line_count:
            missing.append(path)
    return missing


def first_unread_line(state: dict[str, Any], path: str) -> int | None:
    """Return the first uncovered line, or None when the known file is complete."""
    record = (state.get("read_coverage") or {}).get(path) or {}
    line_count = int(record.get("line_count", 0))
    ranges = record.get("ranges") or []
    if line_count <= 0 or not ranges:
        return 1

    covered_until = 0
    for raw_range in sorted(ranges, key=lambda value: int(value[0])):
        start, end = int(raw_range[0]), int(raw_range[1])
        if start > covered_until + 1:
            return covered_until + 1
        covered_until = max(covered_until, end)
    if covered_until < line_count:
        return covered_until + 1
    return None


def next_required_read(state: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the next mandatory source chunk in application-owned order."""
    for kind, field in (
        ("component", "required_component_reads"),
        ("foundation", "required_main_reads"),
    ):
        for path in state.get(field) or []:
            start_line = first_unread_line(state, path)
            if start_line is not None:
                return {"kind": kind, "path": path, "start_line": start_line}
    return None


def unread_pending_paths(state: dict[str, Any]) -> list[str]:
    """Return changed files that have not been fully re-ingested after promotion."""
    return [
        str(path)
        for path in state.get("pending_changed_paths") or []
        if first_unread_line(state, str(path)) is not None
    ]


def load_execution_state(thread_id: str) -> dict[str, Any]:
    # Agent work crosses long model, sandbox, and build waits. Request lifecycle hooks do not run
    # between those waits, so explicitly retire aged or broken Django connections here.
    close_old_connections()
    try:
        chat = GameChat.objects.only("execution_state").get(id=thread_id)
    except (GameChat.DoesNotExist, ValueError) as exc:
        raise GenerationStateError(f"Unknown game chat: {thread_id}") from exc
    return {**default_execution_state(), **deepcopy(chat.execution_state or {})}


def update_execution_state(
    thread_id: str,
    updater: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    close_old_connections()
    with transaction.atomic():
        try:
            chat = GameChat.objects.select_for_update().get(id=thread_id)
        except (GameChat.DoesNotExist, ValueError) as exc:
            raise GenerationStateError(f"Unknown game chat: {thread_id}") from exc
        state = {**default_execution_state(), **deepcopy(chat.execution_state or {})}
        updater(state)
        chat.execution_state = state
        chat.save(update_fields=["execution_state", "updated_at"])
        return deepcopy(state)


async def aload_execution_state(thread_id: str) -> dict[str, Any]:
    """Load persisted generation state without blocking an async agent node."""
    return await sync_to_async(load_execution_state, thread_sensitive=True)(thread_id)


async def aupdate_execution_state(
    thread_id: str,
    updater: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Atomically update generation state from an async agent node or tool."""
    return await sync_to_async(update_execution_state, thread_sensitive=True)(
        thread_id,
        updater,
    )


def allowed_tools(state: dict[str, Any]) -> tuple[str, ...]:
    if state.get("run_succeeded"):
        return ()
    if not state.get("workspace_created"):
        return ("create_game_workspace",)

    # Compatibility for persisted pre-blueprint chats and low-level editor tests.
    # Every newly created workspace receives a blueprint.
    if not state.get("game_blueprint"):
        if state.get("candidate_revision") is not None or state.get("phase") == "repair":
            return ("edit_game_files",) if state.get("phase") == "repair" else ("validate_godot_project",)
        revision = int(state.get("workspace_revision", 0))
        if int(state.get("build_revision", -1)) == revision:
            return ("run_game",)
        if int(state.get("validated_revision", -1)) != revision:
            return ("validate_godot_project",)
        if not state.get("research_completed"):
            return ("research_godot_docs",)
        if not state.get("initial_context_complete"):
            return ("read_game_files",)
        tools = ["edit_game_files", "write_files"]
        if int(state.get("gameplay_edit_count", 0)) > 0:
            tools.append("build_godot_game")
        return tuple(tools)

    if state.get("candidate_revision") is not None:
        return ("implement_stage_step",) if state.get("phase") == "repair" else ("validate_godot_project",)
    if state.get("phase") == "step_correction":
        return ("implement_stage_step",)

    revision = int(state.get("workspace_revision", 0))
    validated = int(state.get("validated_revision", -1)) == revision
    built = int(state.get("build_revision", -1)) == revision

    if built:
        return ("run_game",)
    if state.get("phase") == "repair":
        return ("implement_stage_step",)
    if not validated:
        return ("validate_godot_project",)
    # A validated, complete blueprint is immutable input to the final build. Source rereads are
    # useful between implementation stages, but add no information once no implementation work
    # remains and would incorrectly prevent the application-owned build transition.
    if generation_is_complete(state):
        return ("build_godot_game",)
    if not state.get("research_completed"):
        return ("research_godot_docs",)
    if not state.get("initial_context_complete"):
        return ("read_game_files",)
    if unread_pending_paths(state):
        return ("read_game_files",)
    if validated:
        stage = next_stage(state)
        if stage and stage.get("status") == "needs_plan":
            return ("plan_game_stage",)
        if current_step(state):
            return ("implement_stage_step",)
    return ()


def snapshot_source(state: dict[str, Any], path: str) -> tuple[str, str] | None:
    """Reassemble a fully read current source file from persisted exact snapshots."""
    record = (state.get("read_coverage") or {}).get(path) or {}
    file_hash = str(record.get("sha256") or "")
    line_count = int(record.get("line_count", 0))
    if not file_hash or line_count <= 0 or first_unread_line(state, path) is not None:
        return None
    lines: list[str | None] = [None] * line_count
    final_newline = False
    for snapshot in (state.get("read_snapshots") or {}).get(path, []):
        if snapshot.get("sha256") != file_hash:
            continue
        start = int(snapshot.get("start_line", 1))
        content = str(snapshot.get("content") or "")
        if int(snapshot.get("end_line", 0)) == line_count:
            final_newline = content.endswith(("\n", "\r"))
        for offset, code in enumerate(content.splitlines()):
            index = start - 1 + offset
            if 0 <= index < line_count:
                lines[index] = code
    if any(line is None for line in lines):
        return None
    source = "\n".join(str(line) for line in lines) + ("\n" if final_newline else "")
    return source, file_hash


def _active_step_context(state: dict[str, Any]) -> dict[str, Any] | None:
    step = current_step(state)
    if step is None:
        return None
    path = str(step.get("path") or "")
    operation = str(step.get("operation") or "")
    target = str(step.get("target") or "")
    kind = str(step.get("symbol_kind") or "")
    context = {
        "step": step,
        "output_contract": {
            "create_file": (
                "Return exactly the planned function or declaration. The application adds the "
                "GDScript file header; do not add lifecycle functions or unrelated symbols."
            ),
            "replace_function_body": f"Return only the complete body for {target}().",
            "insert_function": f"Return exactly one complete function named {target}().",
            "insert_declaration": f"Return exactly one complete declaration named {target}.",
            "replace_declaration": f"Return exactly one complete declaration named {target}.",
            "replace_scene_node": f"Return exactly the complete existing node section {target}.",
        }.get(operation, "Return source only for this step."),
        "source_kind": kind,
    }
    snapshot = snapshot_source(state, path)
    if snapshot is None:
        return context
    source, file_hash = snapshot
    context["sha256"] = file_hash
    if path.startswith("project/addons/gamegen/"):
        context["component_structure"] = deepcopy(
            (state.get("project_component_contracts") or {}).get(path) or {}
        )
    span = None
    if operation == "replace_function_body":
        span = function_spans(source).get(target)
    elif operation == "replace_declaration":
        span = declaration_spans(source).get(target)
    elif operation == "replace_scene_node":
        span = scene_node_spans(source).get(target)
    if span:
        lines = source.splitlines()
        start = max(1, span.start_line - 8)
        end = min(len(lines), span.end_line + 8)
        context["current_scope"] = {
            "start_line": start,
            "end_line": end,
            "source": "\n".join(lines[start - 1:end]),
        }
    elif operation == "insert_function":
        context["existing_function_signatures"] = [
            line.strip() for line in source.splitlines() if line.startswith("func ")
        ]
        anchor_name = str(step.get("insert_after") or "")
        anchor = function_spans(source).get(anchor_name) if anchor_name else None
        if anchor:
            lines = source.splitlines()
            context["insertion_anchor"] = {
                "name": anchor_name,
                "source": "\n".join(lines[anchor.start_line - 1:anchor.end_line]),
            }
    elif operation == "insert_declaration":
        context["existing_declarations"] = sorted(declaration_spans(source))
    if path.endswith(".gd"):
        lines = source.splitlines()
        functions = function_spans(source)
        first_function_line = min(
            (function.start_line for function in functions.values()),
            default=len(lines) + 1,
        )
        header = "\n".join(lines[: first_function_line - 1]).strip()
        if header:
            context["foundation_declarations"] = header[:4_000]

        helper_sources: dict[str, str] = {}
        for required_call in step.get("required_calls") or []:
            helper_name = str(required_call).split(".", 1)[0]
            helper = functions.get(helper_name)
            if helper is None or helper_name == target:
                continue
            helper_sources[helper_name] = "\n".join(
                lines[helper.start_line - 1:helper.end_line]
            )
            if sum(len(value) for value in helper_sources.values()) >= 4_000:
                break
        if helper_sources:
            context["required_local_helpers"] = helper_sources

        stage_id = str((next_stage(state) or {}).get("id") or "")
        if stage_id == "presentation":
            scene_inventory: dict[str, list[str]] = {}
            for scene_path in state.get("required_main_reads") or []:
                if not str(scene_path).endswith(".tscn"):
                    continue
                scene_snapshot = snapshot_source(state, str(scene_path))
                if scene_snapshot is None:
                    continue
                scene_inventory[str(scene_path)] = sorted(
                    scene_node_spans(scene_snapshot[0])
                )
            if scene_inventory:
                context["scene_nodes"] = scene_inventory

        declarations = declaration_spans(source)
        schema_sources: list[str] = []
        for name, declaration in declarations.items():
            declaration_source = "\n".join(
                lines[declaration.start_line - 1:declaration.end_line]
            ).strip()
            if not re.match(r"^(?:const\b|enum\b)", declaration_source):
                continue
            schema_sources.append(declaration_source)
            if sum(len(value) for value in schema_sources) >= 2_400:
                break
        if schema_sources:
            context["game_schema_declarations"] = schema_sources
        factory_sources: list[str] = []
        for name, function in functions.items():
            if not re.match(r"^(?:create|build|make)_.+(?:definitions|deck|items|records)$", name):
                continue
            factory_source = "\n".join(
                lines[function.start_line - 1:function.end_line]
            ).strip()
            factory_sources.append(factory_source)
            if sum(len(value) for value in factory_sources) >= 3_200:
                break
        if factory_sources:
            context["game_data_factories"] = factory_sources
    return context


def _relevant_component_contracts(state: dict[str, Any]) -> list[dict[str, Any]]:
    step = current_step(state) or {}
    calls_by_owner: dict[str, set[str]] = {}
    for call in step.get("required_calls") or []:
        parts = str(call).split(".", 1)
        if len(parts) == 2:
            calls_by_owner.setdefault(parts[0], set()).add(parts[1].split("(", 1)[0])
    owners = set(calls_by_owner)
    relevant = []
    for contract in state.get("component_contracts") or []:
        member = str(contract.get("foundation_member") or "")
        match = re.search(r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)", member)
        if match and match.group(1) in owners:
            methods = calls_by_owner.get(match.group(1), set())
            relevant.append(_compact_component_contract(contract, methods=methods))
    return relevant


def _correction_component_contracts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Project contracts referenced by either the active step or its diagnostics."""
    calls_by_owner: dict[str, set[str]] = {}
    step = current_step(state) or {}
    for call in step.get("required_calls") or []:
        parts = str(call).split(".", 1)
        if len(parts) == 2:
            calls_by_owner.setdefault(parts[0], set()).add(parts[1].split("(", 1)[0])

    diagnostic_text = "\n".join(
        str(error) for error in (state.get("step_correction") or {}).get("errors") or []
    )
    for owner, method in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*_system)\.([A-Za-z_][A-Za-z0-9_]*)\b",
        diagnostic_text,
    ):
        calls_by_owner.setdefault(owner, set()).add(method)

    relevant: list[dict[str, Any]] = []
    for contract in state.get("component_contracts") or []:
        member = str(contract.get("foundation_member") or "")
        match = re.search(r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)", member)
        if not match or match.group(1) not in calls_by_owner:
            continue
        methods = calls_by_owner[match.group(1)]
        compact = _compact_component_contract(
            contract,
            methods=None,
            include_method_details=False,
        )
        method_contracts = contract.get("method_contracts") or {}
        compact["method_contracts"] = {
            name: deepcopy(method_contracts[name])
            for name in sorted(methods)
            if name in method_contracts
        }
        relevant.append(compact)
    return relevant


def _compact_component_contract(
    contract: dict[str, Any],
    *,
    methods: set[str] | None = None,
    include_method_details: bool = True,
) -> dict[str, Any]:
    """Keep exact APIs and semantics while omitting repetitive registry prose."""
    method_contracts = contract.get("method_contracts") or {}
    selected_methods = set(method_contracts) if methods is None else methods
    exact_methods = (
        {
            name: deepcopy(value)
            for name, value in method_contracts.items()
            if name in selected_methods
        }
        if include_method_details
        else {}
    )
    api = list(contract.get("api") or [])
    if methods is not None:
        api = [
            signature
            for signature in api
            if any(re.search(rf"(?:^|\.){re.escape(name)}\(", signature) for name in methods)
        ]
    return {
        "name": contract.get("name"),
        "private_gamegen_component": True,
        "foundation_member": contract.get("foundation_member", ""),
        "api": api,
        "method_contracts": exact_methods,
        "data_contracts": deepcopy(contract.get("data_contracts") or {}),
        "invariants": list(contract.get("invariants") or []),
    }


def _compact_step_correction(packet: dict[str, Any]) -> dict[str, Any]:
    """Expose only actionable correction facts; contracts are supplied separately once."""
    compact = {
        key: deepcopy(packet.get(key))
        for key in (
            "attempt",
            "stage_id",
            "step_id",
            "path",
            "operation",
            "target",
            "rejected_sha256",
            "repeated_rejected_source",
            "output_contract",
            "instruction",
        )
        if packet.get(key) is not None
    }
    compact["errors"] = [str(value)[:1_500] for value in packet.get("errors") or []][:4]
    compact["pitfall_guidance"] = [
        {
            "code": str(item.get("code") or "")[:80],
            "repair": str(item.get("repair") or "")[:300],
        }
        for item in packet.get("pitfall_guidance") or []
        if isinstance(item, dict)
    ][:6]
    return compact


def _source_index(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose structural inventory for planning without rehydrating every full game file."""
    stage_id = str((next_stage(state) or {}).get("id") or "")
    indexed: list[dict[str, Any]] = []
    paths = sorted(
        path
        for path in (state.get("read_snapshots") or {})
        if path.startswith("project/game/")
    )
    for path in paths:
        snapshot = snapshot_source(state, path)
        if snapshot is None:
            continue
        source, _file_hash = snapshot
        item: dict[str, Any] = {"path": path}
        if path.endswith(".gd"):
            item["functions"] = [
                match.group(1)
                for line in source.splitlines()
                if (match := re.match(r"^func\s+([A-Za-z_][A-Za-z0-9_]*)", line))
            ]
            item["declarations"] = sorted(declaration_spans(source))
        elif path.endswith(".tscn") and stage_id == "presentation":
            item["scene_nodes"] = sorted(scene_node_spans(source))
        elif path.endswith(".tscn"):
            continue
        indexed.append(item)
    return indexed


def _compact_research_context(state: dict[str, Any]) -> dict[str, Any]:
    """Expose one bounded official reference without carrying search bookkeeping."""
    research = state.get("research_context") or {}
    compact: dict[str, Any] = {}
    documentation = research.get("documentation") or []
    if documentation and isinstance(documentation[0], dict):
        item = documentation[0]
        compact["documentation"] = {
            "url": str(item.get("url") or "")[:300],
            "excerpt": str(item.get("excerpt") or "")[:320],
        }
    demos = research.get("demos") or []
    if demos and isinstance(demos[0], dict):
        item = demos[0]
        compact["demo"] = {
            "source_url": str(item.get("source_url") or "")[:300],
            "excerpt": str(item.get("excerpt") or "")[:320],
        }
    return compact


def _public_stage(state: dict[str, Any]) -> dict[str, Any] | None:
    stage = next_stage(state)
    if stage is None:
        return None
    completed = set(stage.get("completed_step_ids") or [])
    return {
        key: deepcopy(stage.get(key))
        for key in (
            "id",
            "title",
            "purpose",
            "dependencies",
            "foundation_targets",
            "planning_guidance",
            "status",
            "plan_summary",
        )
    } | {
        "completed_step_ids": sorted(completed),
        "remaining_step_count": sum(
            1 for step in stage.get("steps") or [] if step.get("id") not in completed
        ),
    }


def public_execution_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("phase") == "repair" and state.get("repair_packet"):
        return {
            "phase": "repair",
            "workspace_revision": int(state.get("workspace_revision", 0)),
            "candidate_revision": state.get("candidate_revision"),
            "repairs_used": int(state.get("repairs_used", 0)),
            "candidate_failure_count": int(state.get("candidate_failure_count", 0)),
            "repair_packet": deepcopy(state.get("repair_packet")),
            "research_context": _compact_research_context(state),
            "active_stage": _public_stage(state),
            "active_step": current_step(state),
            "established_game_contracts": compact_game_contracts(state),
            "allowed_tools": list(allowed_tools(state)),
            "contract": "Return source only for the repair scope; routing and metadata are application-owned.",
        }
    if state.get("phase") == "step_correction" and state.get("step_correction"):
        return {
            "phase": "step_correction",
            "workspace_revision": int(state.get("workspace_revision", 0)),
            "active_stage": _public_stage(state),
            "active_step_context": _active_step_context(state),
            "correction": _compact_step_correction(state.get("step_correction") or {}),
            "component_contracts": _correction_component_contracts(state),
            "established_game_contracts": compact_game_contracts(
                state,
                path=str((current_step(state) or {}).get("path") or "") or None,
            ),
            "allowed_tools": ["implement_stage_step"],
            "contract": (
                "Correct only the rejected active step. The failed source is intentionally omitted; "
                "the rejection errors and output contract are authoritative."
            ),
        }
    stage_preview = next_stage(state)
    active_read = deepcopy(state.get("active_read"))
    if isinstance(active_read, dict) and isinstance(active_read.get("content"), str):
        active_read.pop("content")
    remaining_components = unread_required_paths(state, "required_component_reads")
    remaining_main = unread_required_paths(state, "required_main_reads")
    stage = stage_preview
    is_stage_planning = bool(stage and stage.get("status") == "needs_plan")
    is_implementation = bool(stage and stage.get("status") == "in_progress")
    public = {
        "phase": state.get("phase", "new"),
        "components": list(state.get("components") or []),
        "game_plan": str(state.get("game_plan") or "")[:2_000],
        "workspace_revision": int(state.get("workspace_revision", 0)),
        "last_valid_revision": int(state.get("last_valid_revision", 0)),
        "candidate_revision": state.get("candidate_revision"),
        "validated_revision": int(state.get("validated_revision", -1)),
        "build_revision": int(state.get("build_revision", -1)),
        "gameplay_edit_count": int(state.get("gameplay_edit_count", 0)),
        "completed_stage_ids": [
            stage.get("id")
            for stage in (state.get("game_blueprint") or {}).get("stages", [])
            if stage.get("status") == "completed"
        ],
        "next_stage": _public_stage(state),
        "active_step_context": _active_step_context(state),
        "source_index": _source_index(state),
        "established_game_contracts": compact_game_contracts(state),
        "stage_retry_directive": deepcopy(state.get("stage_retry_directive")),
        "initial_context_complete": bool(state.get("initial_context_complete")),
        "preparation_complete": bool(state.get("preparation_complete")),
        "research_completed": bool(state.get("research_completed")),
        "next_required_read": next_required_read(state),
        "remaining_read_counts": {
            "components": len(remaining_components),
            "foundation": len(remaining_main),
            "changed_files": len(unread_pending_paths(state)),
        },
        "read_revision": int(state.get("read_revision", 0)),
        "active_read": active_read,
        "repairs_used": int(state.get("repairs_used", 0)),
        "failure_count": int(state.get("failure_count", 0)),
        "last_tool_error": str(state.get("last_tool_error") or "")[:1_500],
        "run_succeeded": bool(state.get("run_succeeded")),
        "terminal": bool(state.get("terminal")),
        "allowed_tools": list(allowed_tools(state)),
        "contract": (
            "Application facts override summaries. Plan only next_stage, then implement only "
            "active_step_context. Required reads, research, validation, commit, rollback, routing, "
            "file hashes, and repair metadata are application-owned. Project-local GameGen component "
            "copies may be extended only by a planned component step."
        ),
    }
    # Project state is durable, but the model receives only what its current phase needs. Full
    # component source is injected by _active_step_context when a frozen step extends that file;
    # planning uses the exact typed contracts and avoids a duplicate copy of every implementation.
    if is_stage_planning:
        public["research_context"] = _compact_research_context(state)
        public["component_contracts"] = [
            _compact_component_contract(
                contract,
                methods=None,
                include_method_details=False,
            )
            for contract in state.get("component_contracts") or []
        ]
        public["game_plan"] = str(public.get("game_plan") or "")[:800]
        if public.get("stage_retry_directive"):
            public.pop("last_tool_error", None)
        for key in (
            "candidate_revision",
            "validated_revision",
            "build_revision",
            "gameplay_edit_count",
            "completed_stage_ids",
            "active_step_context",
            "initial_context_complete",
            "preparation_complete",
            "research_completed",
            "next_required_read",
            "remaining_read_counts",
            "read_revision",
            "active_read",
            "repairs_used",
            "failure_count",
            "run_succeeded",
            "terminal",
        ):
            public.pop(key, None)
    elif is_implementation:
        public["component_contracts"] = _relevant_component_contracts(state)
        public["research_context"] = _compact_research_context(state)
        # A frozen step already contains the exact work contract. Planning history, source
        # inventory, and read/routing bookkeeping are application-owned and only dilute the
        # bounded implementation request sent to the model.
        for key in (
            "game_plan",
            "gameplay_edit_count",
            "completed_stage_ids",
            "source_index",
            "stage_retry_directive",
            "initial_context_complete",
            "preparation_complete",
            "research_completed",
            "next_required_read",
            "remaining_read_counts",
            "read_revision",
            "active_read",
            "failure_count",
            "last_tool_error",
            "run_succeeded",
            "terminal",
        ):
            public.pop(key, None)
    return public
