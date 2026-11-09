"""Application-owned preparation and validation transitions for game generation."""

from __future__ import annotations

import re
from typing import Any

from asgiref.sync import sync_to_async
from langchain_core.runnables import RunnableConfig

from .execution_state import (
    aload_execution_state,
    aupdate_execution_state,
    first_unread_line,
    load_execution_state,
    next_required_read,
    update_execution_state,
)
from .tools import read_game_files, research_godot_docs, validate_godot_project
from .stage_policy import current_step, generation_is_complete, next_stage


STAGE_RESEARCH_TOPICS = {
    "domain_model": "typed GDScript Dictionary Array RefCounted pure helper functions",
    "initialization": "deterministic GDScript initialization typed arrays dictionaries",
    "core_gameplay": "GDScript state transitions signals turn based game logic",
    "presentation": "Control input mouse touch Button signals responsive containers themes procedural UI",
    "game_flow": "reset scene state signals terminal game state",
    "verification": "headless GDScript assertions isolated test state",
}


def _workflow_phase(state: dict[str, Any]) -> str:
    if generation_is_complete(state):
        return "ready_to_build"
    stage = next_stage(state)
    if stage and stage.get("status") == "needs_plan":
        return "needs_stage_plan"
    if current_step(state):
        return "ready_to_implement_step"
    return "needs_stage_plan"


def _research_query(state: dict[str, Any]) -> str:
    plan = " ".join(str(state.get("game_plan") or "").split())[:700]
    plan = re.sub(r"\bGamegen[A-Za-z0-9_]*\b", "private game component", plan)
    components = set(state.get("components") or [])
    interface_topics = "Control Button signals responsive GUI mouse touch scene lifecycle"
    if "godot.card_system" in components:
        interface_topics += " drag and drop card interface"
    stage = next_stage(state) or {}
    stage_plan = " ".join(str(stage.get("plan_summary") or "").split())[:500]
    step = current_step(state) or {}
    step_focus = " ".join(
        str(value or "").strip()
        for value in (
            step.get("symbol_kind"),
            step.get("operation"),
            step.get("target"),
            step.get("purpose"),
        )
        if str(value or "").strip()
    )[:500]
    focus = step_focus or STAGE_RESEARCH_TOPICS.get(str(stage.get("id") or ""), interface_topics)
    return (
        f"Godot 4.7 exact syntax and API guidance for: {focus}. "
        f"Game plan: {plan}. Current stage {stage.get('id', 'foundation')}: {stage_plan}"
    )


def _research_queries(state: dict[str, Any]) -> tuple[str, ...]:
    """Return deterministic, progressively broader official-doc research queries."""
    components = set(state.get("components") or [])
    focused_topics = [
        "Godot 4.7 GDScript Control Button pressed signals responsive containers",
    ]
    stage_id = str((next_stage(state) or {}).get("id") or "")
    if stage_id in STAGE_RESEARCH_TOPICS:
        focused_topics.insert(0, f"Godot 4.7 {STAGE_RESEARCH_TOPICS[stage_id]}")
    if "godot.card_system" in components:
        focused_topics.append(
            "Godot 4.7 Control card UI buttons drag and drop local turn based game"
        )
    if "godot.turn_system" in components:
        focused_topics.append(
            "Godot 4.7 GDScript state machine turn based input UI official demo"
        )
    focused_topics.append(
        "Godot 4.7 web export compatibility renderer mouse touch UI"
    )
    return tuple(dict.fromkeys([_research_query(state), *focused_topics]))


def _failed(result: dict[str, Any]) -> bool:
    return str(result.get("status") or "") != "success"


async def prepare_generation_workspace(config: RunnableConfig) -> dict[str, Any]:
    """Validate, research, and ingest source without spending model turns."""
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
    state = await aload_execution_state(thread_id)
    if state.get("preparation_complete"):
        return {"status": "success", "step": "preparation", "already_complete": True}

    revision = int(state.get("workspace_revision", 0))
    if int(state.get("validated_revision", -1)) != revision:
        validation = await sync_to_async(
            validate_godot_project,
            thread_sensitive=True,
        )(config=config)
        if _failed(validation):
            return {"status": "error", "step": "foundation_validation", "result": validation}
        state = await aload_execution_state(thread_id)

    if not state.get("research_completed"):
        research_attempts: list[dict[str, Any]] = []
        research: dict[str, Any] = {"status": "error", "errors": ["No query attempted."]}
        for query in _research_queries(state):
            research = await research_godot_docs(
                query,
                include_demo_examples=True,
                config=config,
            )
            attempt = {
                "query": query,
                "status": str(research.get("status") or "error"),
                "phase": str(research.get("phase") or "research"),
                "errors": [str(error)[:300] for error in (research.get("errors") or [])[:2]],
            }
            research_attempts.append(attempt)
            print(
                f"[agent-progress] automatic_research_fallback "
                f"attempt={len(research_attempts)}/{len(_research_queries(state))} "
                f"status={attempt['status']} query={query[:120]}",
                flush=True,
            )
            if not _failed(research):
                break
            if research.get("setup_required"):
                break

        await aupdate_execution_state(
            thread_id,
            lambda current: current.update({"research_attempts": research_attempts[-8:]}),
        )
        if _failed(research):
            await aupdate_execution_state(
                thread_id,
                lambda current: current.update(
                    {"phase": "preparation_failed", "preparation_complete": False}
                ),
            )
            return {
                "status": "error",
                "step": "research",
                "attempts": research_attempts,
                "result": research,
            }

    read_calls = 0
    while next_required_read(await aload_execution_state(thread_id)) is not None:
        read_result = await sync_to_async(
            read_game_files,
            thread_sensitive=True,
        )(config=config)
        if _failed(read_result):
            return {"status": "error", "step": "source_ingestion", "result": read_result}
        read_calls += 1

    def mark_prepared(current: dict[str, Any]) -> None:
        current.update(
            {
                "phase": _workflow_phase(current),
                "preparation_complete": True,
                "initial_context_complete": True,
            }
        )

    state = await aupdate_execution_state(thread_id, mark_prepared)
    print(
        f"[agent-progress] automatic_preparation_complete thread={thread_id} "
        f"research_revision={state.get('research_revision', 0)} read_calls={read_calls}",
        flush=True,
    )
    return {
        "status": "success",
        "step": "preparation",
        "research_completed": True,
        "required_sources_read": True,
        "read_calls": read_calls,
    }


def validate_and_refresh_workspace(config: RunnableConfig) -> dict[str, Any]:
    """Validate a successful model edit and refresh exact source snapshots."""
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
    validation = validate_godot_project(config=config)
    if _failed(validation):
        return {"status": "error", "step": "automatic_validation", "result": validation}

    pending_paths = list((load_execution_state(thread_id).get("pending_changed_paths") or []))
    read_calls = 0
    while next_required_read(load_execution_state(thread_id)) is not None:
        read_result = read_game_files(config=config)
        if _failed(read_result):
            return {"status": "error", "step": "source_refresh", "result": read_result}
        read_calls += 1

    for path in pending_paths:
        while (start_line := first_unread_line(load_execution_state(thread_id), path)) is not None:
            read_result = read_game_files(
                [{"path": path, "start_line": start_line}],
                config=config,
            )
            if _failed(read_result):
                return {"status": "error", "step": "source_refresh", "result": read_result}
            read_calls += 1

    def mark_refreshed(current: dict[str, Any]) -> None:
        current.update(
            {
                "phase": _workflow_phase(current),
                "preparation_complete": True,
                "initial_context_complete": True,
                "pending_changed_paths": [],
            }
        )

    update_execution_state(thread_id, mark_refreshed)
    print(
        f"[agent-progress] automatic_validation_passed thread={thread_id} "
        f"refreshed_reads={read_calls}",
        flush=True,
    )
    return {
        "status": "success",
        "step": "automatic_validation",
        "result": validation,
        "refreshed_reads": read_calls,
    }
