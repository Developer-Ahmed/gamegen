import asyncio
import json
import os
import re
import shlex
import traceback
import time
import uuid
from functools import lru_cache
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Callable, List, Literal
from urllib.parse import urlparse

import requests
from asgiref.sync import sync_to_async
from langchain_tavily import TavilySearch
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg
from typing_extensions import Annotated
from langgraph.prebuilt import ToolNode
from .configuration import Configuration
from .execution_state import (
    aload_execution_state,
    aupdate_execution_state,
    allowed_tools,
    load_execution_state,
    next_required_read,
    snapshot_source,
    unread_required_paths,
    update_execution_state,
    unread_pending_paths,
)
from .generation_control import get_job_sandbox, raise_if_stopped

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from pydantic import BaseModel, Field, model_validator
from .component_library import ComponentRegistry
from .failure_catalog import guidance_for_errors
from .game_storage import store_sandbox_game
from .game_contracts import record_validated_step_contract
from .semantic_validation import (
    validate_generated_semantics,
    validate_replacement_text,
    validate_step_contract,
    validate_step_scope,
    validate_stage_implementation,
)
from .repair_packets import build_repair_packet, repair_scope
from .source_structure import (
    LIFECYCLE_FUNCTIONS,
    PROTECTED_DECLARATIONS,
    PROTECTED_FUNCTIONS,
    StructuralEditError,
    canonicalize_gdscript_indentation,
    insert_declaration,
    insert_function,
    replace_declaration,
    replace_function_body,
    replace_scene_node,
)
from .strategy_policy import (
    blocked_strategy_error,
    different_strategy_directive,
    rollback_strategy_record,
    strategy_fingerprint,
)
from .stage_policy import (
    advance_blueprint_after_commit,
    create_game_blueprint,
    current_step,
    dependency_errors,
    derive_stage_name,
    freeze_stage_plan,
    next_stage,
    operation_targets,
    recognized_stage_targets,
    reset_active_stage_for_replan,
    validate_stage_plan,
)

# to use celery workers:
# from celery import shared_task
# add shared_task before the function
# .delay when calling it

GAME_FILES_ROOT = "/home/user/game_files"
CANDIDATE_GAME_FILES_ROOT = "/home/user/game_files_candidate"
LAST_VALID_GAME_FILES_ROOT = "/home/user/game_files_last_valid"
PROMOTING_GAME_FILES_ROOT = "/home/user/game_files_promoting"
GODOT_PROJECT_ROOT = f"{GAME_FILES_ROOT}/project"
GODOT_WEB_ROOT = f"{GAME_FILES_ROOT}/web"
GODOT_BINARY = os.getenv("GODOT_BIN", "godot")
COMPONENT_REGISTRY = ComponentRegistry()
GODOT_STABLE_DOCS_PREFIX = "https://docs.godotengine.org/en/stable/"
GODOT_47_DEMOS_REPOSITORY = "https://github.com/godotengine/godot-demo-projects"
GODOT_47_DEMOS_REF = "4.7-6ad6167"
GODOT_47_DEMOS_COMMIT = "6ad6167e0577fe3622c18546138f456b107ce93c"
GODOT_47_DEMOS_TREE_API = (
    f"https://api.github.com/repos/godotengine/godot-demo-projects/git/trees/"
    f"{GODOT_47_DEMOS_COMMIT}?recursive=1"
)
GODOT_47_DEMOS_RAW_ROOT = (
    f"https://raw.githubusercontent.com/godotengine/godot-demo-projects/"
    f"{GODOT_47_DEMOS_COMMIT}"
)
_repair_counts: dict[str, int] = {}
_failure_counts: dict[str, int] = {}
_phase_repair_counts: dict[str, dict[str, int]] = {}

MAX_READ_FILES = 6
MAX_READ_CHARACTERS = 3_500
MAX_EDIT_FILES = 1
MAX_WRITE_FILES = 1
MAX_WRITE_LINES = 120
MAX_WRITE_CHARACTERS = 8_000
MAX_STEP_CORRECTION_ATTEMPTS = 3
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _return_research_result(payload: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("QWEN_LOG_RESEARCH", "").strip().lower() in _TRUE_ENV_VALUES:
        print(
            "[research-result] "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}",
            flush=True,
        )
    return payload


class GameFileReadRequest(BaseModel):
    """A bounded source-file read request."""

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class StructuredSourceEdit(BaseModel):
    """A source-aware edit whose exact byte range is resolved by the backend."""

    operation: Literal[
        "replace_function_body",
        "insert_function",
        "insert_declaration",
        "replace_declaration",
        "replace_scene_node",
    ]
    target: str = Field(
        description=(
            "Function/declaration name, scene node path, or insertion-anchor function. "
            "Use an empty target to append insert_function."
        )
    )
    new_text: str


class GameFileEdit(BaseModel):
    """An optimistic edit against one previously read source file."""

    path: str
    expected_sha256: str = Field(min_length=64, max_length=64)
    operations: list[StructuredSourceEdit] = Field(min_length=1, max_length=3)


class StagePlanStep(BaseModel):
    """One model-designed code unit; the backend resolves concrete edit mechanics."""

    id: str = Field(description="Stable snake_case id describing this one bounded step.")
    path: str = Field(description="Project path that should own this code unit.")
    symbol: str = Field(
        description=(
            "Bare identifier only for .gd work (for example create_deck or CardColor), never a "
            "func signature or declaration text; use a node path for scene_node. "
            "Use an empty string only for new_scene or new_asset."
        ),
    )
    symbol_kind: Literal[
        "function",
        "declaration",
        "scene_node",
        "new_scene",
        "new_asset",
    ] = Field(
        description=(
            "Choose function/declaration for any GDScript, scene_node for an existing .tscn node, "
            "new_scene for a new .tscn, or new_asset for a new non-code asset. Never use a whole-"
            "file unit for .gd; create it with one named function or declaration first."
        )
    )
    purpose: str = Field(description="One concrete behavior this step adds.")
    acceptance_criteria: list[str] = Field(
        min_length=1,
        max_length=6,
        description="Specific observable or state-based checks for this step.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Ids of earlier steps whose behavior this code unit calls or requires.",
    )
    required_calls: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Exact calls that must appear in this unit, such as card_system.configure or "
            "calculate_score. Include every component call required by the acceptance criteria. "
            "When two calls are alternatives, choose one implementation and list only that call."
        ),
    )
    required_dictionary_keys: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Exact quoted Dictionary keys that must appear in this unit. Do not list variable, "
            "constant, member, function, or component names here."
        ),
    )
    extends_component: bool = Field(
        default=False,
        description="True only for an additive edit to a selected project-local GameGen component.",
    )
    amends_contract: bool = Field(
        default=False,
        description=(
            "True only when this step must compatibly extend a symbol validated by an earlier "
            "stage. Reuse established symbols by default."
        ),
    )
    amendment_reason: str = Field(
        default="",
        max_length=500,
        description="Concrete reason an established symbol cannot be reused unchanged.",
    )

    @model_validator(mode="before")
    @classmethod
    def canonicalize_symbol_reference(cls, value: Any) -> Any:
        """Accept harmless source notation while retaining one-symbol semantics."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        symbol = str(normalized.get("symbol") or "").strip()
        kind = str(normalized.get("symbol_kind") or "").strip()
        if kind == "function":
            match = re.match(
                r"^(?:func\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|$)",
                symbol,
            )
            if match:
                normalized["symbol"] = match.group(1)
        elif kind == "declaration":
            match = re.match(
                r"^(?:(?:var|const|signal|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b|"
                r"([A-Za-z_][A-Za-z0-9_]*)$)",
                symbol,
            )
            if match:
                normalized["symbol"] = match.group(1) or match.group(2)
        required_calls = normalized.get("required_calls")
        if isinstance(required_calls, list):
            normalized["required_calls"] = [
                re.sub(r"\s*\([^()]*\)\s*$", "", call.strip())
                if isinstance(call, str)
                else call
                for call in required_calls
            ]
        return normalized

    @model_validator(mode="after")
    def validate_source_unit(self) -> "StagePlanStep":
        # The application sanitizes cross-field structure after parsing the whole plan. This
        # lets it retain valid required units when one optional unit is malformed.
        return self


def _sanitize_stage_plan_units(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop malformed optional scene/asset units without losing valid code work."""
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for step in steps:
        kind = str(step.get("symbol_kind") or "")
        path = str(step.get("path") or "").strip()
        symbol = str(step.get("symbol") or "").strip()
        structurally_valid = True
        if kind in {"function", "declaration"}:
            structurally_valid = path.endswith(".gd") and bool(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol)
            )
        elif kind == "scene_node":
            structurally_valid = path.endswith(".tscn") and bool(symbol)
        elif kind == "new_scene":
            structurally_valid = path.endswith(".tscn") and not symbol
        elif kind == "new_asset":
            structurally_valid = (
                path.startswith("project/assets/")
                and path != "project/assets/"
                and not path.endswith((".gd", ".tscn", "/"))
                and not symbol
            )
        (valid if structurally_valid else invalid).append(step)

    if invalid and not valid:
        rejected = ", ".join(str(step.get("id") or "unnamed") for step in invalid)
        raise ValueError(
            "The stage plan contains only malformed structural units: " + rejected
        )
    if not invalid:
        return steps, []

    omitted_ids = {str(step.get("id") or "") for step in invalid}
    for step in valid:
        step["depends_on"] = [
            dependency
            for dependency in step.get("depends_on") or []
            if str(dependency) not in omitted_ids
        ]
    return valid, sorted(value for value in omitted_ids if value)


def _is_allowed_godot_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "docs.godotengine.org"
        and parsed.path.startswith("/en/stable/")
    )


def _is_allowed_godot_demo_url(url: str) -> bool:
    parsed = urlparse(url)
    blob_prefix = f"/godotengine/godot-demo-projects/blob/{GODOT_47_DEMOS_REF}/"
    tree_root = f"/godotengine/godot-demo-projects/tree/{GODOT_47_DEMOS_REF}"
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and (
            parsed.path.startswith(blob_prefix)
            or parsed.path == tree_root
            or parsed.path.startswith(f"{tree_root}/")
        )
    )


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gamegen-godot-research",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token := os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


@lru_cache(maxsize=1)
def _godot_47_demo_tree() -> tuple[dict[str, Any], ...]:
    response = requests.get(GODOT_47_DEMOS_TREE_API, headers=_github_headers(), timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("truncated"):
        raise RuntimeError("The pinned Godot 4.7 demo tree response was truncated.")
    return tuple(item for item in payload.get("tree", []) if isinstance(item, dict))


@lru_cache(maxsize=64)
def _godot_47_demo_file(path: str) -> str:
    safe_path = PurePosixPath(path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise ValueError(f"Unsafe Godot demo path: {path}")
    response = requests.get(f"{GODOT_47_DEMOS_RAW_ROOT}/{safe_path.as_posix()}", timeout=20)
    response.raise_for_status()
    return response.text


def _research_terms(query: str) -> set[str]:
    ignored = {"and", "for", "from", "godot", "how", "the", "use", "with"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) > 2 and token not in ignored
    }


def _focused_excerpt(content: str, terms: set[str], limit: int = 1_200) -> str:
    lowered = content.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 200) if positions else 0
    return content[start:start + limit]


def _search_godot_47_demos(query: str, limit: int = 3) -> list[dict[str, Any]]:
    tree = _godot_47_demo_tree()
    blob_paths = [str(item.get("path", "")) for item in tree if item.get("type") == "blob"]
    roots = {
        str(PurePosixPath(path).parent)
        for path in blob_paths
        if path.endswith("/project.godot")
    }
    terms = _research_terms(query)
    ranked = []
    for root in roots:
        project_paths = [path for path in blob_paths if path.startswith(f"{root}/")]
        searchable_tokens = set(
            re.findall(r"[a-z0-9]+", " ".join([root, *project_paths]).lower().replace("_", " "))
        )
        score = len(terms & searchable_tokens)
        if score:
            ranked.append((score, root, project_paths))

    results = []
    best_score = max((item[0] for item in ranked), default=0)
    best_matches = [item for item in ranked if item[0] == best_score]
    for _, root, project_paths in sorted(best_matches, key=lambda item: item[1])[:limit]:
        text_paths = [
            path
            for path in project_paths
            if path.endswith((".gd", ".tscn", "/README.md")) and not path.endswith(".gd.uid")
        ]
        text_paths.sort(
            key=lambda path: (
                -len(
                    terms
                    & set(re.findall(r"[a-z0-9]+", path.lower().replace("_", " ")))
                ),
                0 if path.endswith(".gd") else 1 if path.endswith(".tscn") else 2,
                path,
            )
        )
        source_path = text_paths[0] if text_paths else f"{root}/project.godot"
        content = _godot_47_demo_file(source_path)
        results.append(
            {
                "title": root.replace("_", " "),
                "project_path": root,
                "url": f"{GODOT_47_DEMOS_REPOSITORY}/tree/{GODOT_47_DEMOS_REF}/{root}",
                "source_file": source_path,
                "source_url": (
                    f"{GODOT_47_DEMOS_REPOSITORY}/blob/{GODOT_47_DEMOS_REF}/{source_path}"
                ),
                "excerpt": _focused_excerpt(content, terms),
            }
        )
    return results


def _thread_id_from_config(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("thread_id") or "default")


def reset_repair_count(thread_id: str) -> None:
    _repair_counts[thread_id] = 0
    _failure_counts[thread_id] = 0
    _phase_repair_counts[thread_id] = {}
    print(f"[agent-progress] thread={thread_id} repairs=0 reset")


def initialize_repair_count(thread_id: str) -> None:
    try:
        state = load_execution_state(thread_id)
    except Exception:
        reset_repair_count(thread_id)
        return
    _repair_counts[thread_id] = int(state.get("repairs_used", 0))
    _failure_counts[thread_id] = int(state.get("failure_count", 0))
    _phase_repair_counts.setdefault(thread_id, {})
    print(
        f"[agent-progress] thread={thread_id} repairs={_repair_counts[thread_id]} "
        f"failures={_failure_counts[thread_id]} restored"
    )


def get_repair_count(thread_id: str) -> int:
    return _repair_counts.get(thread_id, 0)


def get_phase_repair_count(thread_id: str, phase: str) -> int:
    return _phase_repair_counts.get(thread_id, {}).get(phase, 0)


def _repair_guidance(phase: str, failure_count: int) -> str:
    return (
        f"Failure {failure_count} occurred during {phase}. Apply a targeted repair to the "
        "reported issue and continue generation."
    )


def _record_repair_attempt(config: RunnableConfig, phase: str, reason: str) -> tuple[int, int]:
    thread_id = _thread_id_from_config(config)
    failure_count = _failure_counts.get(thread_id, 0) + 1
    _failure_counts[thread_id] = failure_count
    _repair_counts[thread_id] = failure_count
    phase_counts = _phase_repair_counts.setdefault(thread_id, {})
    phase_counts[phase] = phase_counts.get(phase, 0) + 1
    repair_count = _repair_counts[thread_id]
    phase_count = phase_counts[phase]
    try:
        update_execution_state(
            thread_id,
            lambda state: state.update(
                {
                    "repairs_used": failure_count,
                    "failure_count": failure_count,
                    "terminal": False,
                    "run_finished": False,
                    "phase": "repair",
                    **({"build_revision": -1} if phase == "runtime" else {}),
                }
            ),
        )
    except Exception:
        # Unit tests may exercise repair accounting without a persisted chat.
        pass
    print(
        f"[agent-progress] thread={thread_id} repairs={repair_count} "
        f"failures={failure_count} phase={phase} phase_failures={phase_count} "
        f"terminal=false reason={reason}"
    )
    return repair_count, failure_count


def _repair_status(config: RunnableConfig, phase: str, reason: str) -> dict[str, Any]:
    repairs, failure_count = _record_repair_attempt(config, phase, reason)
    thread_id = _thread_id_from_config(config)
    phase_repairs = get_phase_repair_count(thread_id, phase)
    return {
        "repair_attempts": repairs,
        "failure_count": failure_count,
        "phase_repair_attempts": phase_repairs,
        "terminal": False,
        "repair_guidance": _repair_guidance(phase, failure_count),
    }


def _exception_reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _phase_error(
    config: RunnableConfig,
    phase: str,
    reason: str,
    *,
    exc: Exception | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if exc:
        print(f"[agent-progress] phase={phase} exception={_exception_reason(exc)}")
        print(traceback.format_exc())

    if _is_infrastructure_error(exc or reason):
        return {
            "status": "error",
            "phase": "infrastructure",
            "terminal": False,
            "retryable": True,
            "errors": [reason],
            **(extra or {}),
        }

    repair_status = _repair_status(config, phase, reason)
    result: dict[str, Any] = {
        "status": "error",
        "phase": phase,
        **repair_status,
        "errors": [reason],
    }
    if extra:
        result.update(extra)
    return result


def get_sandbox(config: RunnableConfig):
    thread_id = _thread_id_from_config(config)
    return get_job_sandbox(thread_id)


def _tool_rejection(config: RunnableConfig, tool_name: str, reason: str) -> dict[str, Any]:
    state = load_execution_state(_thread_id_from_config(config))
    return {
        "status": "rejected",
        "phase": str(state.get("phase", "generation")),
        "terminal": False,
        "errors": [reason],
        "allowed_tools": list(allowed_tools(state)),
    }


def _step_output_contract(step: dict[str, Any]) -> str:
    operation = str(step.get("operation") or "")
    target = str(step.get("target") or "")
    kind = str(step.get("symbol_kind") or "")
    if operation == "create_file" and kind == "function":
        return (
            f"Return exactly one complete function named '{target}'. The application adds the "
            "GDScript file header. Do not add lifecycle functions or top-level declarations."
        )
    if operation == "create_file" and kind == "declaration":
        return (
            f"Return exactly one complete declaration named '{target}'. The application adds the "
            "GDScript file header. Do not add functions or unrelated declarations."
        )
    if operation == "replace_function_body":
        return f"Return only the complete corrected body of function '{target}'."
    if operation == "insert_function":
        return f"Return exactly one complete function declaration named '{target}'."
    if operation in {"insert_declaration", "replace_declaration"}:
        return (
            f"Return exactly one complete var, const, signal, or enum declaration named "
            f"'{target}'."
        )
    if operation == "replace_scene_node":
        return f"Return exactly the complete existing [node] section for '{target}'."
    return "Return only source for the single active step."


def _compose_created_step_file(path: str, source: str) -> str:
    """Add deterministic file boilerplate while leaving the semantic unit model-authored."""
    content = source.strip()
    if path.endswith(".gd") and not re.match(
        r"^\s*(?:class_name\s+\w+\s+)?extends\s+",
        content,
    ):
        content = f"extends RefCounted\n\n{content}"
    return content.rstrip() + "\n"


def _contracts_for_step(execution: dict[str, Any], step: dict[str, Any]) -> list[dict[str, Any]]:
    owners = {
        str(call).split(".", 1)[0]
        for call in step.get("required_calls") or []
        if "." in str(call)
    }
    include_all_stateful = str(step.get("target") or "") == "smoke_test_gameplay"
    contracts = []
    for contract in execution.get("component_contracts") or []:
        foundation_member = str(contract.get("foundation_member") or "")
        member_match = re.search(r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)", foundation_member)
        if member_match and (include_all_stateful or member_match.group(1) in owners):
            contracts.append(contract)
    return contracts


def _reject_step_output(
    config: RunnableConfig,
    *,
    execution: dict[str, Any],
    path: str,
    content: str,
    errors: list[str],
    phase: str,
) -> dict[str, Any]:
    """Persist a compact correction packet without retaining failed source text."""
    thread_id = _thread_id_from_config(config)
    step = current_step(execution) or {}
    candidate_sha = sha256(content.encode("utf-8")).hexdigest()
    target = str(step.get("target") or "")
    candidate_strategy = strategy_fingerprint(content, target)
    repeated_exact = candidate_sha in set(execution.get("rejected_step_signatures") or [])
    repeated_strategy = candidate_strategy in set(
        execution.get("rejected_step_strategy_fingerprints") or []
    )
    attempt = int(execution.get("step_attempt_failures", 0)) + 1
    compact_errors = [" ".join(str(error).split())[:500] for error in errors[:6]]
    should_replan = (
        attempt >= MAX_STEP_CORRECTION_ATTEMPTS
        or repeated_exact
        or repeated_strategy
    )
    if should_replan:
        def persist_replan(state: dict[str, Any]) -> None:
            state["failure_count"] = int(state.get("failure_count", 0)) + 1
            reset_active_stage_for_replan(state, errors=compact_errors)

        replanned = update_execution_state(thread_id, persist_replan)
        directive = replanned.get("stage_retry_directive") or {}
        print(
            f"[agent-progress] step_plan_rejected phase={phase} attempt={attempt} "
            f"step={step.get('id')} repeated={repeated_exact or repeated_strategy} "
            f"next=plan_game_stage errors={compact_errors}",
            flush=True,
        )
        return {
            "status": "rejected",
            "phase": "needs_stage_plan",
            "terminal": False,
            "errors": compact_errors,
            "retry_directive": directive,
            "allowed_tools": ["plan_game_stage"],
        }
    packet = {
        "attempt": attempt,
        "stage_id": str(execution.get("active_stage_id") or ""),
        "step_id": str(step.get("id") or ""),
        "path": path,
        "operation": str(step.get("operation") or ""),
        "target": target,
        "rejected_sha256": candidate_sha,
        "repeated_rejected_source": False,
        "errors": compact_errors,
        "pitfall_guidance": guidance_for_errors(compact_errors),
        "relevant_component_contracts": _contracts_for_step(execution, step),
        "output_contract": _step_output_contract(step),
        "instruction": (
            "Produce a materially different implementation for only this step. Correct every "
            "reported issue without adding unrelated symbols or lifecycle behavior."
        ),
    }

    def persist(state: dict[str, Any]) -> None:
        signatures = list(state.get("rejected_step_signatures") or [])
        if candidate_sha not in signatures:
            signatures.append(candidate_sha)
        strategies = list(state.get("rejected_step_strategy_fingerprints") or [])
        if candidate_strategy not in strategies:
            strategies.append(candidate_strategy)
        state.update(
            {
                "phase": "step_correction",
                "step_correction": packet,
                "rejected_step_signatures": signatures[-20:],
                "rejected_step_strategy_fingerprints": strategies[-20:],
                "step_attempt_failures": attempt,
            }
        )

    update_execution_state(thread_id, persist)
    print(
        f"[agent-progress] step_output_rejected phase={phase} attempt={attempt} "
        f"step={packet['step_id']} sha={candidate_sha[:12]} errors={compact_errors}",
        flush=True,
    )
    return {
        "status": "rejected",
        "phase": "step_correction",
        "terminal": False,
        "errors": compact_errors,
        "correction": packet,
        "allowed_tools": ["implement_stage_step"],
    }


def _guard_tool(config: RunnableConfig, tool_name: str) -> dict[str, Any] | None:
    thread_id = _thread_id_from_config(config)
    raise_if_stopped(thread_id)
    state = load_execution_state(thread_id)
    if tool_name not in allowed_tools(state):
        return _tool_rejection(
            config,
            tool_name,
            f"{tool_name} is not allowed during phase '{state.get('phase')}'.",
        )
    return None


def _workspace_root(state: dict[str, Any]) -> str:
    return CANDIDATE_GAME_FILES_ROOT if state.get("candidate_revision") is not None else GAME_FILES_ROOT


def _workspace_file_path(root: str, path: str) -> str:
    rel_path = PurePosixPath(path)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError(f"Unsafe game file path: {path}")

    rel_path = rel_path.as_posix()
    return f"{root}/{rel_path}"


def _game_file_path(path: str) -> str:
    return _workspace_file_path(GAME_FILES_ROOT, path)


def _is_infrastructure_error(exc: Exception | str) -> bool:
    reason = str(exc).lower()
    return any(
        marker in reason
        for marker in (
            "502",
            "bad gateway",
            "connection reset",
            "connection error",
            "network error",
            "temporarily unavailable",
            "service unavailable",
            "gateway timeout",
            "timed out",
            "timeout",
            "rate limit",
            "429",
            "sandbox was not found",
            "sandbox is no longer available",
            "sandbox timeout",
        )
    )


def _retry_infrastructure(operation: Callable[[], Any], *, attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if not _is_infrastructure_error(exc) or attempt + 1 >= attempts:
                raise
            delay = 0.25 * (2**attempt)
            print(
                f"[agent-progress] infrastructure_retry attempt={attempt + 1}/{attempts} "
                f"reason={_exception_reason(exc)}",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("Infrastructure retry exhausted.") from last_error


def _prepare_candidate_workspace(sandbox) -> None:
    command = (
        f"rm -rf {shlex.quote(CANDIDATE_GAME_FILES_ROOT)} && "
        f"cp -a {shlex.quote(GAME_FILES_ROOT)} {shlex.quote(CANDIDATE_GAME_FILES_ROOT)}"
    )
    result = _retry_infrastructure(lambda: sandbox.commands.run(command, timeout=60))
    if getattr(result, "exit_code", 1) != 0:
        raise RuntimeError(f"Failed to create staged candidate: {_command_output(result)}")


def _discard_candidate_workspace(sandbox) -> None:
    result = _retry_infrastructure(
        lambda: sandbox.commands.run(
            f"rm -rf {shlex.quote(CANDIDATE_GAME_FILES_ROOT)}",
            timeout=30,
        )
    )
    if getattr(result, "exit_code", 1) != 0:
        raise RuntimeError(f"Failed to discard staged candidate: {_command_output(result)}")


def _promote_candidate_workspace(sandbox) -> None:
    command = (
        f"if ! test -d {shlex.quote(GAME_FILES_ROOT)}; then "
        f"test -d {shlex.quote(LAST_VALID_GAME_FILES_ROOT)} && "
        f"cp -a {shlex.quote(LAST_VALID_GAME_FILES_ROOT)} {shlex.quote(GAME_FILES_ROOT)}; fi && "
        f"rm -rf {shlex.quote(PROMOTING_GAME_FILES_ROOT)} && "
        f"cp -a {shlex.quote(CANDIDATE_GAME_FILES_ROOT)} "
        f"{shlex.quote(PROMOTING_GAME_FILES_ROOT)} && "
        f"rm -rf {shlex.quote(LAST_VALID_GAME_FILES_ROOT)} && "
        f"cp -a {shlex.quote(GAME_FILES_ROOT)} {shlex.quote(LAST_VALID_GAME_FILES_ROOT)} && "
        f"rm -rf {shlex.quote(GAME_FILES_ROOT)} && "
        f"if mv {shlex.quote(PROMOTING_GAME_FILES_ROOT)} {shlex.quote(GAME_FILES_ROOT)}; "
        f"then true; else cp -a {shlex.quote(LAST_VALID_GAME_FILES_ROOT)} "
        f"{shlex.quote(GAME_FILES_ROOT)}; exit 1; fi"
    )
    result = _retry_infrastructure(lambda: sandbox.commands.run(command, timeout=60))
    if getattr(result, "exit_code", 1) != 0:
        raise RuntimeError(f"Failed to promote staged candidate: {_command_output(result)}")


def _game_file_parent(path: str) -> str | None:
    parent = PurePosixPath(path).parent.as_posix()
    if parent == ".":
        return None
    return parent


def _validate_generated_file_path(path: str) -> None:
    rel_path = PurePosixPath(path).as_posix()
    allowed_roots = ("project/game/", "project/assets/")
    if not rel_path.startswith(allowed_roots):
        raise ValueError(
            "Generated files must be inside project/game/ or project/assets/. "
            "Use workspace paths such as project/game/main.gd; res:// is only valid "
            "inside Godot file contents. Core project settings and installed components are immutable."
        )


def _validate_planned_edit_path(path: str, execution: dict[str, Any]) -> None:
    """Allow game files plus explicitly planned edits to selected local components."""
    rel_path = PurePosixPath(path).as_posix()
    if rel_path.startswith(("project/game/", "project/assets/")):
        return
    step = current_step(execution)
    selected_paths = set(execution.get("required_component_reads") or [])
    if (
        rel_path.startswith("project/addons/gamegen/")
        and rel_path in selected_paths
        and step
        and step.get("path") == rel_path
        and step.get("extends_component")
    ):
        return
    raise ValueError(
        "Edits must target generated game files or the selected project-local GameGen "
        "component named by the active planned step."
    )


def _validate_readable_file_path(path: str) -> None:
    rel_path = PurePosixPath(path).as_posix()
    readable_roots = (
        "project/game/",
        "project/assets/",
        "project/addons/gamegen/",
    )
    if not rel_path.startswith(readable_roots):
        raise ValueError(
            "Readable files must be inside project/game/, project/assets/, or the "
            "read-only component directory project/addons/gamegen/."
        )


def _validate_generated_file_content(path: str, content: str) -> None:
    if not isinstance(content, str):
        raise ValueError(f"Generated file content must be text: {path}")
    if path.endswith(".tscn") and not content.lstrip().startswith("[gd_scene"):
        raise ValueError(
            f"Godot scene file {path} must use .tscn scene syntax beginning with [gd_scene]; "
            "GDScript belongs in a .gd file."
        )
    if path.endswith(".gd"):
        stripped = content.lstrip()
        if not stripped.startswith("extends "):
            raise ValueError(f"GDScript file {path} must begin with an extends declaration.")
        if re.search(r"^\s*\[(?:gd_scene|node|resource|export)\b", content, re.MULTILINE):
            raise ValueError(
                f"GDScript file {path} contains scene/resource section syntax. Put [gd_scene] "
                "and [node] sections only in .tscn files."
            )
        extends_target = stripped.splitlines()[0].removeprefix("extends ").strip()
        if extends_target == "Godot" or extends_target.lower().startswith("godot."):
            raise ValueError(
                f"Invalid GDScript base type '{extends_target}'. Extend a real Godot class or a "
                "project script resource, not the engine namespace or a GameGen component id."
            )
        if re.search(r"\bGodot\.[A-Za-z_][A-Za-z0-9_]*", content):
            raise ValueError(
                "Godot classes are not referenced through a Godot.* namespace in GDScript. "
                "Use exact Godot or GameGen class names."
            )
        if "=>" in content:
            raise ValueError(
                "JavaScript-style arrow lambdas are not valid GDScript 4.7. Use a named helper "
                "function or a supported Callable."
            )


def _source_lines(content: str, start_line: int) -> list[dict[str, Any]]:
    """Separate source text from line metadata so prefixes cannot leak into edits."""
    return [
        {"line": line_number, "code": line.rstrip("\r\n")}
        for line_number, line in enumerate(content.splitlines(keepends=True), start=start_line)
    ]


def _merge_line_ranges(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    merged: list[list[int]] = []
    for current_start, current_end in sorted([*ranges, [start, end]]):
        if not merged or current_start > merged[-1][1] + 1:
            merged.append([current_start, current_end])
        else:
            merged[-1][1] = max(merged[-1][1], current_end)
    return merged


def _covered_line_count(ranges: list[list[int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)


def _record_file_reads(thread_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    def update(state: dict[str, Any]) -> None:
        snapshots = dict(state.get("read_snapshots") or {})
        coverage = dict(state.get("read_coverage") or {})
        lines_added = 0
        for result in results:
            path = result["path"]
            file_hash = result["sha256"]
            existing_coverage = coverage.get(path) or {}
            ranges = (
                list(existing_coverage.get("ranges") or [])
                if existing_coverage.get("sha256") == file_hash
                else []
            )
            merged_ranges = _merge_line_ranges(
                ranges,
                result["start_line"],
                result["end_line"],
            )
            lines_added += _covered_line_count(merged_ranges) - _covered_line_count(ranges)
            coverage[path] = {
                "sha256": file_hash,
                "line_count": result["line_count"],
                "ranges": merged_ranges,
            }
            path_snapshots = [
                snapshot
                for snapshot in snapshots.get(path, [])
                if snapshot.get("sha256") == file_hash
                and not (
                    snapshot.get("start_line") == result["start_line"]
                    and snapshot.get("end_line") == result["end_line"]
                )
            ]
            path_snapshots.append(
                {
                    "sha256": file_hash,
                    "line_count": result["line_count"],
                    "start_line": result["start_line"],
                    "end_line": result["end_line"],
                    "content": result["content"],
                }
            )
            snapshots[path] = path_snapshots

        state["read_snapshots"] = snapshots
        state["read_coverage"] = coverage
        if lines_added:
            state["read_revision"] = int(state.get("read_revision", 0)) + 1
            state["read_lines_covered"] = int(state.get("read_lines_covered", 0)) + lines_added
        state["active_read"] = {
            key: results[-1][key]
            for key in ("path", "sha256", "line_count", "start_line", "end_line", "content")
        }
        components_done = not unread_required_paths(state, "required_component_reads")
        mains_done = not unread_required_paths(state, "required_main_reads")
        pending_done = not unread_pending_paths(state)
        state["initial_context_complete"] = components_done and mains_done and pending_done
        if state["initial_context_complete"]:
            state["phase"] = "context_ready"

    return update_execution_state(thread_id, update)


async def research_godot_docs(
    query: str,
    include_demo_examples: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> dict[str, Any]:
    """Research official Godot 4.7 APIs and optional official 4.7 demos.

    This tool does not document the project's private prewritten ``Gamegen*``
    components. Their contracts are supplied directly in the system prompt.

    Args:
        query: A focused question about one Godot concept, class, method, scene
            format, Web limitation, or recommended design.
        include_demo_examples: Also search the official Godot demo repository at
            the pinned Godot 4.7 tag for small implementation examples.

    Returns:
        Compact stable-documentation findings and, when requested, a separate
        ``demo_examples`` section. Results are restricted to official stable docs
        and the exact official Godot 4.7 demo tag; this is not general web search.
    """
    execution = await aload_execution_state(_thread_id_from_config(config))
    if not execution.get("preparation_complete"):
        include_demo_examples = True
    if re.search(r"\bGamegen[A-Za-z0-9_]*\b", query, flags=re.IGNORECASE):
        return _return_research_result({
            "status": "rejected",
            "phase": "research",
            "terminal": False,
            "errors": [
                "Gamegen* classes are private prewritten GameGen components, not official "
                "Godot 4.7 APIs. Use the component contracts in the system prompt instead."
            ],
        })

    if not os.getenv("TAVILY_API_KEY"):
        return _return_research_result({
            "status": "error",
            "phase": "research",
            "terminal": False,
            "errors": ["TAVILY_API_KEY is required for official Godot documentation research."],
            "setup_required": "Add TAVILY_API_KEY to backend/.env and restart the backend.",
        })

    docs_search = TavilySearch(
        name="official_godot_4_7_docs",
        description="Search only official Godot documentation for exact Godot 4.7 guidance.",
        include_domains=["docs.godotengine.org"],
        search_depth="advanced",
        max_results=3,
        include_answer="basic",
        include_raw_content=False,
    )
    raw: Any = None
    demo_results: list[dict[str, Any]] = []
    for attempt in range(3):
        try:
            raw = await docs_search.ainvoke(
                {"query": f"site:docs.godotengine.org/en/stable/ Godot official documentation {query}"}
            )
            if isinstance(raw, dict) and raw.get("error"):
                raise RuntimeError(str(raw["error"]))
            demo_results = (
                await sync_to_async(_search_godot_47_demos, thread_sensitive=False)(query)
                if include_demo_examples
                else []
            )
            break
        except Exception as exc:
            retryable = _is_infrastructure_error(exc)
            if retryable and attempt < 2:
                print(
                    f"[agent-progress] infrastructure_retry operation=godot_research "
                    f"attempt={attempt + 1}/3 reason={_exception_reason(exc)}",
                    flush=True,
                )
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            return _return_research_result({
                "status": "error",
                "phase": "infrastructure" if retryable else "research",
                "terminal": False,
                "retryable": retryable,
                "errors": [f"Official Godot research failed: {type(exc).__name__}: {exc}"],
            })
    if not isinstance(raw, dict) or raw.get("error"):
        return _return_research_result({
            "status": "error",
            "phase": "research",
            "terminal": False,
            "errors": [f"Official Godot documentation search failed: {raw.get('error') if isinstance(raw, dict) else raw}"],
        })

    results = []
    for item in raw.get("results", []):
        url = str(item.get("url", ""))
        if not _is_allowed_godot_doc_url(url):
            continue
        results.append(
            {
                "title": str(item.get("title", "")),
                "url": url,
                "excerpt": str(item.get("content", ""))[:900],
            }
        )

    if not results:
        return _return_research_result({
            "status": "error",
            "phase": "research",
            "terminal": False,
            "errors": ["No latest stable official Godot documentation results were found for the focused query."],
        })

    payload = {
        "status": "success",
        "phase": "research",
        "query": query,
        "documentation_scope": GODOT_STABLE_DOCS_PREFIX,
        "answer": str(raw.get("answer", ""))[:1200],
        "results": results,
        "demo_examples": {
            "requested": include_demo_examples,
            "repository": GODOT_47_DEMOS_REPOSITORY,
            "godot_version": "4.7",
            "ref": GODOT_47_DEMOS_REF,
            "commit": GODOT_47_DEMOS_COMMIT,
            "results": demo_results,
            "usage": (
                "Use demos as implementation examples only. Confirm APIs in stable docs, "
                "adapt only the smallest relevant pattern, and validate the result locally."
            ),
        },
    }
    compact_context = {
        "query": query[:240],
        "documentation": [
            {
                "title": item["title"],
                "url": item["url"],
                "excerpt": item["excerpt"][:260],
            }
            for item in results[:1]
        ],
        "demos": [
            {
                "project_path": item["project_path"],
                "source_file": item["source_file"],
                "source_url": item["source_url"],
                "excerpt": item["excerpt"][:260],
            }
            for item in demo_results[:1]
        ],
    }
    await aupdate_execution_state(
        _thread_id_from_config(config),
        lambda state: state.update(
            {
                "research_completed": True,
                "research_context": compact_context,
                "research_revision": int(state.get("research_revision", 0)) + 1,
                "phase": "researched",
            }
        ),
    )
    return _return_research_result(payload)

# Inactive reference only.
# The agent should write sandbox files in batches with write_files so generation
# does not drift into partial-file states or spend extra recursions.
'''
def write_to_file(file_path: str, file_content: str, *, config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Write to file in sandbox to prepare for testing.

    Args:
        file_path: Path of the file inside the sandbox
        file_content: Content of the file

    Returns:
        A string describing the result of writing to the file.

    Usage:
        Use this tool after generating source code for a file.
        Use the correct filepath that will fit the whole file structure.
    """
    try:
        sandbox = get_sandbox()
        res = sandbox.files.write(file_path, file_content)
    except Exception as exc:
        return _phase_error(config, "write", _exception_reason(exc), exc=exc)

    print(f"wrote to file res: {res}")
    return (
        "File write succeeded. Continue writing any remaining required files; "
        "do not produce a final response until build_godot_game and run_game both succeed."
    )
'''


def read_game_files(
    requests: list[GameFileReadRequest] | None = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """Read bounded sections of game files or installed GameGen component source.

    Args:
        requests: Optional source paths and inclusive ranges. Omit this during mandatory
            component/foundation reading; the backend chooses the next unread chunk.

    Returns:
        Structured source lines, range metadata, and full-file SHA-256 revisions.
        The application privately persists the exact source for editing.
    """
    if rejection := _guard_tool(config, "read_game_files"):
        return rejection
    requests = requests or []
    if len(requests) > MAX_READ_FILES:
        return _phase_error(
            config,
            "read",
            f"Read at most {MAX_READ_FILES} files per call.",
        )

    thread_id = _thread_id_from_config(config)
    execution = load_execution_state(thread_id)
    required_read = next_required_read(execution)
    deterministic_required_read = required_read is not None
    request_overridden = deterministic_required_read and bool(requests)
    if required_read:
        effective_requests: list[GameFileReadRequest | dict[str, Any]] = [
            GameFileReadRequest(
                path=str(required_read["path"]),
                start_line=int(required_read["start_line"]),
            )
        ]
        print(
            "[agent-progress] deterministic_required_read "
            f"kind={required_read['kind']} path={required_read['path']} "
            f"start_line={required_read['start_line']} "
            f"request_overridden={str(request_overridden).lower()}",
            flush=True,
        )
    elif requests:
        effective_requests = list(requests)
    else:
        return {
            "status": "rejected",
            "phase": "read",
            "terminal": False,
            "errors": ["Provide a target file and range now that mandatory reads are complete."],
        }

    before_read_revision = int(execution.get("read_revision", 0))
    before_lines_covered = int(execution.get("read_lines_covered", 0))
    workspace_root = _workspace_root(execution)

    try:
        sandbox = get_sandbox(config)
        results: list[dict[str, Any]] = []
        total_characters = 0

        for raw_request in effective_requests:
            request = (
                raw_request
                if isinstance(raw_request, GameFileReadRequest)
                else GameFileReadRequest.model_validate(raw_request)
            )
            _validate_readable_file_path(request.path)
            if request.end_line is not None and request.end_line < request.start_line:
                raise ValueError(f"end_line must be at least start_line for {request.path}.")

            content = _retry_infrastructure(
                lambda: sandbox.files.read(_workspace_file_path(workspace_root, request.path))
            )
            if not isinstance(content, str):
                raise ValueError(f"Source file is not UTF-8 text: {request.path}")

            lines = content.splitlines(keepends=True)
            start_index = request.start_line - 1
            if start_index >= len(lines):
                raise ValueError(
                    f"start_line {request.start_line} exceeds the {len(lines)} lines in {request.path}."
                )
            requested_end = min(
                request.end_line if request.end_line is not None else len(lines),
                len(lines),
            )
            remaining_characters = MAX_READ_CHARACTERS - total_characters
            if remaining_characters <= 0 and results:
                break
            selected_parts: list[str] = []
            selected_length = 0
            end_index = start_index
            for index in range(start_index, requested_end):
                line = lines[index]
                if selected_length + len(line) > remaining_characters:
                    break
                selected_parts.append(line)
                selected_length += len(line)
                end_index = index + 1
            if not selected_parts:
                if results:
                    break
                raise ValueError(
                    f"The next line in {request.path} exceeds the remaining read budget. "
                    "Request fewer files in this call."
                )
            selected = "".join(selected_parts)
            total_characters += len(selected)
            truncated = end_index < requested_end

            results.append(
                {
                    "path": request.path,
                    "sha256": sha256(content.encode("utf-8")).hexdigest(),
                    "line_count": len(lines),
                    "start_line": request.start_line,
                    "end_line": min(end_index, len(lines)),
                    "content": selected,
                    "truncated": truncated,
                    "next_start_line": end_index + 1 if truncated else None,
                }
            )
    except Exception as exc:
        return _phase_error(config, "read", _exception_reason(exc), exc=exc)

    state = _record_file_reads(thread_id, results)
    read_revision = int(state.get("read_revision", 0))
    lines_added = int(state.get("read_lines_covered", 0)) - before_lines_covered
    if deterministic_required_read and read_revision == before_read_revision:
        return {
            "status": "rejected",
            "phase": "read_no_progress",
            "terminal": False,
            "errors": ["The mandatory read added no new source coverage."],
            "next_required_read": next_required_read(state),
        }
    remaining_components = unread_required_paths(state, "required_component_reads")
    remaining_mains = unread_required_paths(state, "required_main_reads")
    public_results = [
        {
            **{key: value for key, value in result.items() if key != "content"},
            "source_lines": _source_lines(result["content"], result["start_line"]),
        }
        for result in results
    ]
    return {
        "status": "success",
        "phase": "read",
        "files": public_results,
        "required_context_complete": bool(state.get("initial_context_complete")),
        "remaining_required_reads": remaining_components or remaining_mains,
        "next_required_read": next_required_read(state),
        "coverage_progress": {
            "lines_added": lines_added,
            "read_revision": read_revision,
        },
        "request_overridden": request_overridden,
        "message": (
            "The exact source was saved. Automatic preparation advances mandatory coverage. "
            "For a later targeted repair, use the current SHA-256 and saved line range."
        ),
    }


def _apply_structured_operation(
    content: str,
    operation: StructuredSourceEdit,
) -> tuple[str, Any]:
    replacement_issues = validate_replacement_text(operation.new_text)
    if replacement_issues:
        raise StructuralEditError(
            "; ".join(f"[{issue.code}] {issue.message}" for issue in replacement_issues)
        )
    if operation.operation == "replace_function_body":
        return replace_function_body(content, operation.target, operation.new_text)
    if operation.operation == "insert_function":
        return insert_function(content, operation.target, operation.new_text)
    if operation.operation == "insert_declaration":
        return insert_declaration(content, operation.target, operation.new_text)
    if operation.operation == "replace_declaration":
        return replace_declaration(content, operation.target, operation.new_text)
    if operation.operation == "replace_scene_node":
        if not operation.target.endswith("/Playfield"):
            raise StructuralEditError(
                "Only the generated Playfield scene section is editable; foundation UI is protected."
            )
        return replace_scene_node(content, operation.target, operation.new_text)
    raise StructuralEditError(f"Unsupported structured edit: {operation.operation}")


def _normalized_edit_signature(edit: GameFileEdit, base_revision: int) -> str:
    payload = {
        "base_revision": base_revision,
        "path": edit.path,
        "expected_sha256": edit.expected_sha256.lower(),
        "operations": [
            {
                "operation": operation.operation,
                "target": operation.target.strip(),
                "new_text": operation.new_text.replace("\r\n", "\n").strip(),
            }
            for operation in edit.operations
        ],
    }
    return sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_repair_operation(execution: dict[str, Any], edit: GameFileEdit) -> None:
    if execution.get("phase") != "repair":
        return
    packet = execution.get("repair_packet")
    scope = repair_scope(packet)
    diagnostic = packet.get("diagnostic") if isinstance(packet, dict) else {}
    if scope is None or not isinstance(diagnostic, dict):
        raise StructuralEditError("Repair state is missing its authoritative scope.")
    if edit.path != diagnostic.get("path"):
        raise StructuralEditError("A repair may modify only the file named by repair_packet.")
    if len(edit.operations) != 1:
        raise StructuralEditError("A repair must contain exactly one structural operation.")
    operation = edit.operations[0]
    expected_operation = {
        "function": "replace_function_body",
        "declaration": "replace_declaration",
        "scene_node": "replace_scene_node",
    }.get(scope.kind)
    if expected_operation is None:
        raise StructuralEditError("The reported file-level failure needs a new bounded stage, not a broad repair.")
    if operation.operation != expected_operation or operation.target != scope.name:
        raise StructuralEditError(
            f"Repair only {scope.kind} '{scope.name}' using {expected_operation}."
        )
    if (
        operation.target in PROTECTED_FUNCTIONS
        or operation.target in PROTECTED_DECLARATIONS
        or operation.target in LIFECYCLE_FUNCTIONS
    ):
        raise StructuralEditError("Lifecycle and bootstrap functions are protected foundation code.")
    original_lines = max(1, scope.end_line - scope.start_line + 1)
    replacement_lines = max(1, len(operation.new_text.splitlines()))
    if replacement_lines > max(original_lines * 2, original_lines + 20):
        raise StructuralEditError(
            "Repair replacement is substantially larger than the failing scope. "
            "Make the smallest correction that resolves the diagnostic."
        )


def _relevant_contract_guidance(execution: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    lowered = reason.lower()
    relevant = []
    for name in execution.get("components") or []:
        component = COMPONENT_REGISTRY.get(str(name))
        if component is None:
            continue
        member = component.foundation_member.split(":=", 1)[0].replace("var", "").strip()
        class_names = re.findall(r"Gamegen[A-Za-z0-9_]+", " ".join(component.api))
        if member and member.lower() in lowered or any(value.lower() in lowered for value in class_names):
            relevant.append(component.to_contract_dict())
    return relevant


def _application_change_summary(
    execution: dict[str, Any],
    stage_name: str,
    targets: tuple[str, ...],
) -> str:
    if execution.get("phase") == "repair":
        packet = execution.get("repair_packet") or {}
        scope = packet.get("scope") or {}
        diagnostic = packet.get("diagnostic") or {}
        scope_name = str(scope.get("name") or (targets[0] if targets else "source"))
        message = " ".join(str(diagnostic.get("message") or "validation failure").split())
        return f"Repair {scope_name}: {message[:240]}"
    if targets:
        return f"Implement {', '.join(f'{target}()' for target in targets)}"
    return f"Apply bounded source update for {stage_name}"


def _gdscript_public_contract(content: str) -> dict[str, Any]:
    """Extract stable class identity and top-level method signatures."""
    extends_match = re.search(r"^extends\s+(.+)$", content, re.MULTILINE)
    class_match = re.search(r"^class_name\s+([A-Za-z_][A-Za-z0-9_]*)", content, re.MULTILINE)
    methods = {
        match.group(1): " ".join(match.group(0).split())
        for match in re.finditer(
            r"^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^\n]*\)(?:\s*->\s*[^:\n]+)?\s*:",
            content,
            re.MULTILINE,
        )
    }
    return {
        "extends": extends_match.group(1).strip() if extends_match else "",
        "class_name": class_match.group(1) if class_match else "",
        "methods": methods,
    }


def _validate_component_extension(path: str, before: str, after: str, execution: dict[str, Any]) -> None:
    if not path.startswith("project/addons/gamegen/"):
        return
    required = (execution.get("project_component_contracts") or {}).get(path)
    required = required or _gdscript_public_contract(before)
    after_contract = _gdscript_public_contract(after)
    if after_contract["extends"] != required.get("extends"):
        raise StructuralEditError("A component extension may not change its extends declaration.")
    if after_contract["class_name"] != required.get("class_name"):
        raise StructuralEditError("A component extension may not change its class_name.")
    changed = [
        name
        for name, signature in (required.get("methods") or {}).items()
        if after_contract["methods"].get(name) != signature
    ]
    if changed:
        raise StructuralEditError(
            "A project-local component extension must preserve existing public signatures: "
            + ", ".join(sorted(changed))
        )


def plan_game_stage(
    plan_summary: str,
    steps: list[StagePlanStep],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """Freeze the implementation plan for the current universal game stage."""
    if rejection := _guard_tool(config, "plan_game_stage"):
        return rejection
    thread_id = _thread_id_from_config(config)
    execution = load_execution_state(thread_id)
    try:
        raw_steps = [
            step.model_dump()
            if isinstance(step, StagePlanStep)
            else StagePlanStep.model_validate(step).model_dump()
            for step in steps
        ]
        raw_steps, omitted_step_ids = _sanitize_stage_plan_units(raw_steps)
        for raw_step in raw_steps:
            if raw_step.get("symbol_kind") in {"new_scene", "new_asset"}:
                raw_step["symbol_kind"] = "file"
        sources: dict[str, str] = {}
        for raw_step in raw_steps:
            path = str(raw_step.get("path") or "")
            snapshot = snapshot_source(execution, path)
            if snapshot is not None:
                sources[path] = snapshot[0]
        normalized = validate_stage_plan(
            execution,
            plan_summary=plan_summary,
            steps=raw_steps,
            sources=sources,
        )

        def persist(current: dict[str, Any]) -> None:
            freeze_stage_plan(current, plan_summary=plan_summary, steps=normalized)
            current["stage_plan_revision"] = int(current.get("stage_plan_revision", 0)) + 1
            current["preparation_complete"] = False
            current["research_completed"] = False
            current["research_context"] = {}
            current["last_tool_signature"] = ""
            current["last_tool_progress_token"] = ""

        state = update_execution_state(thread_id, persist)
    except Exception as exc:
        return _tool_rejection(config, "plan_game_stage", _exception_reason(exc))
    print(
        f"[agent-progress] stage_plan_frozen stage={state.get('active_stage_id')} "
        f"steps={len(normalized)} first_step={state.get('active_step_id')} "
        f"omitted_optional_steps={omitted_step_ids}",
        flush=True,
    )
    return {
        "status": "success",
        "phase": "stage_planned",
        "stage": next_stage(state),
        "active_step": current_step(state),
        "omitted_optional_step_ids": omitted_step_ids,
        "message": "Plan frozen. Implement only the active step; validation advances it.",
    }


def _edit_game_files_impl(
    edits: list[GameFileEdit],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
    guard_name: str,
):
    """Stage source-aware edits; Godot validation commits them atomically."""
    if rejection := _guard_tool(config, guard_name):
        return rejection
    if not edits or len(edits) > MAX_EDIT_FILES:
        return _tool_rejection(
            config,
            "edit_game_files",
            f"Edit between 1 and {MAX_EDIT_FILES} files per call.",
        )
    thread_id = _thread_id_from_config(config)
    execution = load_execution_state(thread_id)
    if not execution.get("initial_context_complete"):
        return {
            "status": "rejected",
            "phase": "read_required_context",
            "terminal": False,
            "errors": ["Automatic component and foundation ingestion must finish before editing."],
        }
    try:
        parsed_edits = [
            edit if isinstance(edit, GameFileEdit) else GameFileEdit.model_validate(edit)
            for edit in edits
        ]
        operation_target_names = operation_targets(parsed_edits)
        stage_targets = (
            tuple(execution.get("candidate_stage_targets") or [])
            if execution.get("phase") == "repair" and execution.get("candidate_revision")
            else recognized_stage_targets(operation_target_names, execution)
        )
        stage_name = derive_stage_name(parsed_edits, execution)
        application_summary = _application_change_summary(
            execution,
            stage_name,
            stage_targets,
        )
        prerequisite_errors = dependency_errors(operation_target_names, execution)
        if prerequisite_errors:
            raise StructuralEditError(" ".join(prerequisite_errors))
        for edit in parsed_edits:
            _validate_planned_edit_path(edit.path, execution)
            _validate_repair_operation(execution, edit)

        base_revision = int(
            execution.get("candidate_base_revision")
            or execution.get("workspace_revision", 0)
        )
        signatures = [_normalized_edit_signature(edit, base_revision) for edit in parsed_edits]
        previous_signatures = set(execution.get("attempted_edit_signatures") or [])
        if any(signature in previous_signatures for signature in signatures):
            raise StructuralEditError(
                "This exact edit was already attempted against the same valid revision. "
                "Choose a materially different implementation."
            )

        sandbox = get_sandbox(config)
        candidate_exists = execution.get("candidate_revision") is not None
        source_root = CANDIDATE_GAME_FILES_ROOT if candidate_exists else GAME_FILES_ROOT
        prepared: list[tuple[str, str]] = []
        for edit in parsed_edits:
            current = _retry_infrastructure(
                lambda path=edit.path: sandbox.files.read(_workspace_file_path(source_root, path))
            )
            if not isinstance(current, str):
                raise ValueError(f"Generated source file is not UTF-8 text: {edit.path}")
            current_hash = sha256(current.encode("utf-8")).hexdigest()
            if current_hash != edit.expected_sha256.lower():
                raise StructuralEditError(
                    f"Stale edit for {edit.path}: expected {edit.expected_sha256}, "
                    f"current candidate is {current_hash}. Use the authoritative SHA-256."
                )
            updated = current
            touched = []
            for operation in edit.operations:
                updated, span = _apply_structured_operation(updated, operation)
                touched.append({"kind": span.kind, "name": span.name})
            _validate_component_extension(edit.path, current, updated, execution)
            _validate_generated_file_content(edit.path, updated)
            semantic_issues = validate_generated_semantics(
                edit.path,
                updated,
                [] if edit.path.startswith("project/addons/gamegen/") else execution.get("components") or [],
                execution.get("project_component_contracts") or {},
                require_component_members=edit.path == "project/game/main.gd",
            )
            function_targets = tuple(
                target
                for target in stage_targets
                if any(
                    operation.operation in {"replace_function_body", "insert_function"}
                    and target in operation_target_names
                    for parsed in parsed_edits
                    for operation in parsed.operations
                )
            )
            if edit.path.endswith(".gd") and function_targets:
                semantic_issues.extend(
                    validate_stage_implementation(updated, function_targets)
                )
            active_step = current_step(execution)
            if active_step is not None and execution.get("phase") != "repair":
                semantic_issues.extend(
                    validate_step_contract(
                        edit.path,
                        updated,
                        active_step,
                        execution.get("components") or [],
                    )
                )
            if semantic_issues:
                raise StructuralEditError(
                    "; ".join(f"[{issue.code}] {issue.message}" for issue in semantic_issues)
                )
            if not candidate_exists and execution.get("phase") != "repair":
                for target in stage_targets:
                    if strategy_error := blocked_strategy_error(
                        execution,
                        target=target,
                        content=updated,
                    ):
                        raise StructuralEditError(strategy_error)
            prepared.append((edit.path, updated))

        if not candidate_exists:
            _prepare_candidate_workspace(sandbox)
        for path, content in prepared:
            _retry_infrastructure(
                lambda path=path, content=content: sandbox.files.write(
                    _workspace_file_path(CANDIDATE_GAME_FILES_ROOT, path), content
                )
            )
    except Exception as exc:
        if _is_infrastructure_error(exc):
            return _phase_error(config, "edit", _exception_reason(exc), exc=exc)
        if guard_name == "implement_stage_step":
            attempted_text = "\n".join(
                operation.new_text
                for parsed in locals().get("parsed_edits", [])
                for operation in parsed.operations
            )
            return _reject_step_output(
                config,
                execution=execution,
                path=str((current_step(execution) or {}).get("path") or ""),
                content=attempted_text,
                errors=[_exception_reason(exc)],
                phase="edit",
            )
        return {
            "status": "rejected",
            "phase": "edit",
            "terminal": False,
            "errors": [_exception_reason(exc)],
            "pitfall_guidance": guidance_for_errors([str(exc)]),
            "relevant_component_contracts": _relevant_contract_guidance(execution, str(exc)),
        }

    revisions = {path: sha256(content.encode("utf-8")).hexdigest() for path, content in prepared}

    def mark_candidate(state: dict[str, Any]) -> None:
        candidate_files = dict(state.get("candidate_files") or {})
        candidate_files.update(revisions)
        candidate_sources = dict(state.get("candidate_sources") or {})
        candidate_sources.update(dict(prepared))
        changed_paths = list(dict.fromkeys([*(state.get("candidate_changed_paths") or []), *revisions]))
        state.update(
            {
                "phase": "candidate_needs_validation",
                "candidate_revision": uuid.uuid4().hex,
                "candidate_base_revision": int(
                    state.get("candidate_base_revision")
                    if state.get("candidate_base_revision") is not None
                    else state.get("workspace_revision", 0)
                ),
                "candidate_changed_paths": changed_paths,
                "candidate_files": candidate_files,
                "candidate_sources": candidate_sources,
                "candidate_stage_name": stage_name.strip(),
                "candidate_stage_targets": list(stage_targets),
                "candidate_change_summary": application_summary,
                "repair_packet": None,
                "step_correction": None,
                "step_attempt_failures": 0,
                "rejected_step_signatures": [],
                "rejected_step_strategy_fingerprints": [],
                "attempted_edit_signatures": [
                    *list(state.get("attempted_edit_signatures") or []),
                    *signatures,
                ][-50:],
            }
        )

    state = update_execution_state(thread_id, mark_candidate)
    print(
        f"[agent-progress] staged_candidate id={state.get('candidate_revision')} "
        f"base={state.get('candidate_base_revision')} paths={list(revisions)}",
        flush=True,
    )
    return {
        "status": "success",
        "phase": "candidate_staged",
        "candidate_revision": state.get("candidate_revision"),
        "base_revision": state.get("candidate_base_revision"),
        "files_edited": list(revisions),
        "sha256": revisions,
        "stage_name": stage_name,
        "change_summary": application_summary,
        "message": "Candidate staged. The application will validate it before promotion.",
    }


def edit_game_files(
    edits: list[GameFileEdit],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """Legacy structured editor retained for internal tests and compatibility."""
    return _edit_game_files_impl(edits, config=config, guard_name="edit_game_files")


def implement_stage_step(
    new_text: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """Implement only the application-selected step using source text, not routing metadata."""
    if rejection := _guard_tool(config, "implement_stage_step"):
        return rejection
    execution = load_execution_state(_thread_id_from_config(config))
    if execution.get("phase") == "repair":
        packet = execution.get("repair_packet") or {}
        scope = packet.get("scope") or {}
        diagnostic = packet.get("diagnostic") or {}
        candidate = packet.get("candidate") or {}
        path = str(diagnostic.get("path") or "")
        target = str(scope.get("name") or "")
        operation = str(packet.get("allowed_operation") or "")
        expected_sha = str(candidate.get("sha256") or "")
    else:
        step = current_step(execution)
        if step is None:
            return _tool_rejection(config, "implement_stage_step", "No active implementation step exists.")
        path = str(step.get("path") or "")
        target = str(step.get("target") or "")
        operation = str(step.get("operation") or "")
        expected_sha = str(
            ((execution.get("read_coverage") or {}).get(path) or {}).get("sha256") or ""
        )
        if operation == "insert_function":
            target = str(step.get("insert_after") or "")
        if operation == "create_file":
            file_text = _compose_created_step_file(path, new_text)
            return _write_files_impl(
                {path: file_text},
                change_summary="",
                config=config,
                guard_name="implement_stage_step",
            )
    if operation not in {
        "replace_function_body",
        "insert_function",
        "insert_declaration",
        "replace_declaration",
        "replace_scene_node",
    }:
        return _tool_rejection(
            config,
            "implement_stage_step",
            f"The active step has unsupported operation '{operation}'.",
        )
    if len(expected_sha) != 64:
        return _tool_rejection(
            config,
            "implement_stage_step",
            "The application has no exact current SHA for the active file; source refresh is required.",
        )
    edit = GameFileEdit(
        path=path,
        expected_sha256=expected_sha,
        operations=[StructuredSourceEdit(operation=operation, target=target, new_text=new_text)],
    )
    return _edit_game_files_impl(
        [edit],
        config=config,
        guard_name="implement_stage_step",
    )


def _write_files_impl(
    files: dict[str, str],
    change_summary: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
    guard_name: str,
):
    """
    Write multiple files to the sandbox in one tool call.

    Args:
        files: Mapping of workspace-relative paths to complete file contents; never pass Godot resource paths such as res://.
        change_summary: Concise description of behavior introduced by these new files.

    Returns:
        A structured result describing the files that were written.

    Usage:
        Write one coherent planned stage or targeted refinement. This tool may be
        called multiple times before the final build.
    """
    if rejection := _guard_tool(config, guard_name):
        return rejection
    execution = load_execution_state(_thread_id_from_config(config))
    if not files or len(files) > MAX_WRITE_FILES:
        reason = f"Create between 1 and {MAX_WRITE_FILES} files per call."
        if guard_name == "implement_stage_step":
            return _reject_step_output(
                config,
                execution=execution,
                path=str((current_step(execution) or {}).get("path") or ""),
                content="\n".join(str(value) for value in files.values()),
                errors=[reason],
                phase="write",
            )
        return _tool_rejection(config, "write_files", reason)
    if sum(len(content) for content in files.values() if isinstance(content, str)) > MAX_WRITE_CHARACTERS:
        reason = f"Write output exceeds {MAX_WRITE_CHARACTERS} characters."
        if guard_name == "implement_stage_step":
            return _reject_step_output(
                config,
                execution=execution,
                path=str((current_step(execution) or {}).get("path") or ""),
                content="\n".join(str(value) for value in files.values()),
                errors=[reason],
                phase="write",
            )
        return _tool_rejection(config, "write_files", reason)
    if any(len(content.splitlines()) > MAX_WRITE_LINES for content in files.values() if isinstance(content, str)):
        reason = f"A new file may contain at most {MAX_WRITE_LINES} lines."
        if guard_name == "implement_stage_step":
            return _reject_step_output(
                config,
                execution=execution,
                path=str((current_step(execution) or {}).get("path") or ""),
                content="\n".join(str(value) for value in files.values()),
                errors=[reason],
                phase="write",
            )
        return _tool_rejection(config, "write_files", reason)

    try:
        for file_path, file_content in files.items():
            _validate_generated_file_path(file_path)
            _validate_generated_file_content(file_path, file_content)
            semantic_issues = validate_generated_semantics(
                file_path,
                file_content,
                execution.get("components") or [],
                execution.get("project_component_contracts") or {},
                require_component_members=file_path == "project/game/main.gd",
            )
            active_step = current_step(execution)
            if active_step is not None:
                semantic_issues.extend(
                    validate_step_scope(file_path, file_content, active_step)
                )
                semantic_issues.extend(
                    validate_step_contract(
                        file_path,
                        file_content,
                        active_step,
                        execution.get("components") or [],
                    )
                )
            if semantic_issues:
                raise ValueError(
                    "; ".join(
                        f"[{issue.code}] {issue.message}" for issue in semantic_issues
                    )
                )

        sandbox = get_sandbox(config)
        existence_probe = " ".join(
            f"if test -e {shlex.quote(_game_file_path(file_path))}; "
            f"then printf '%s\\n' {shlex.quote(file_path)}; fi;"
            for file_path in files
        )
        exists_result = sandbox.commands.run(existence_probe, timeout=30)
        existing_paths = [
            path
            for path in (getattr(exists_result, "stdout", "") or "").splitlines()
            if path
        ]
        if existing_paths:
            raise ValueError(
                "write_files only creates new files. Read and edit existing files instead: "
                + ", ".join(existing_paths)
            )

        _prepare_candidate_workspace(sandbox)
        written_paths = []
        directories = sorted(
            {
                parent
                for file_path in files
                if (parent := _game_file_parent(file_path))
            }
        )
        if directories:
            mkdir_command = "mkdir -p " + " ".join(
                shlex.quote(_workspace_file_path(CANDIDATE_GAME_FILES_ROOT, directory))
                for directory in directories
            )
            sandbox.commands.run(mkdir_command, timeout=30)

        for file_path, file_content in files.items():
            sandbox.files.write(
                _workspace_file_path(CANDIDATE_GAME_FILES_ROOT, file_path), file_content
            )
            written_paths.append(file_path)
    except Exception as exc:
        if _is_infrastructure_error(exc):
            return _phase_error(config, "write", _exception_reason(exc), exc=exc)
        if guard_name == "implement_stage_step":
            return _reject_step_output(
                config,
                execution=execution,
                path=str((current_step(execution) or {}).get("path") or ""),
                content="\n".join(str(value) for value in files.values()),
                errors=[_exception_reason(exc)],
                phase="write",
            )
        return {
            "status": "rejected",
            "phase": "write",
            "terminal": False,
            "errors": [_exception_reason(exc)],
        }

    print(f"[agent-progress] wrote_files count={len(written_paths)} paths={written_paths}")
    thread_id = _thread_id_from_config(config)
    revisions = {
        path: sha256(content.encode("utf-8")).hexdigest()
        for path, content in files.items()
    }
    active_stage = next_stage(execution)
    active_step = current_step(execution)
    stage_name = (
        f"{active_stage['id']}:{active_step['id']}"
        if active_stage and active_step
        else "create_" + "_and_".join(
            re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")
            for path in sorted(written_paths)
        )
    )
    application_summary = (
        f"Implement {active_step['purpose']}"
        if active_step
        else change_summary.strip()
    )
    update_execution_state(
        thread_id,
        lambda state: state.update(
            {
                "phase": "candidate_needs_validation",
                "candidate_revision": uuid.uuid4().hex,
                "candidate_base_revision": int(state.get("workspace_revision", 0)),
                "candidate_changed_paths": written_paths,
                "candidate_files": revisions,
                "candidate_sources": dict(files),
                "candidate_stage_name": stage_name.strip(),
                "candidate_stage_targets": [],
                "candidate_change_summary": application_summary,
                "candidate_failure_count": 0,
                "repair_packet": None,
                "step_correction": None,
                "step_attempt_failures": 0,
                "rejected_step_signatures": [],
                "rejected_step_strategy_fingerprints": [],
            }
        ),
    )
    return {
        "status": "success",
        "phase": "candidate_staged",
        "files_written": written_paths,
        "sha256": revisions,
        "stage_name": stage_name,
        "change_summary": application_summary,
        "message": (
            "Stage write succeeded. The application will validate and reread it now. "
            "Build only after the final stage validates."
        ),
    }


def write_files(
    files: dict[str, str],
    change_summary: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """Legacy new-file writer retained for internal tests and compatibility."""
    return _write_files_impl(
        files,
        change_summary=change_summary,
        config=config,
        guard_name="write_files",
    )


def create_game_workspace(
    components: list[str],
    title: str = "Generated Multiplayer Game",
    game_plan: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
):
    """
    Create a clean game workspace and install selected reusable components.

    Args:
        components: Component names from the available component registry.
        title: Browser title for generated shell files.
        game_plan: Concise whole-game design: rules, player model, interactions,
            presentation direction, terminal state, and verification strategy.

    Returns:
        A structured result with installed files and the Godot integration contract.

    Usage:
        Call this exactly once. It installs broad helper components and a valid
        rule-neutral foundation; the application then creates and enforces the
        universal staged-development blueprint.
    """
    if rejection := _guard_tool(config, "create_game_workspace"):
        return rejection

    try:
        selected = COMPONENT_REGISTRY.resolve(["godot.web_runtime", *components])
        files = COMPONENT_REGISTRY.read_files(selected, title=title)
    except Exception as exc:
        available_components = [component.to_public_dict() for component in COMPONENT_REGISTRY.list_components()]
        return {
            "status": "error",
            "phase": "component",
            "errors": [_exception_reason(exc)],
            "available_components": available_components,
            "recommended_sets": COMPONENT_REGISTRY.recommended_sets(),
        }

    try:
        sandbox = get_sandbox(config)
        sandbox.commands.run(
            f"rm -rf {shlex.quote(GAME_FILES_ROOT)} "
            f"{shlex.quote(CANDIDATE_GAME_FILES_ROOT)} "
            f"{shlex.quote(LAST_VALID_GAME_FILES_ROOT)} "
            f"{shlex.quote(PROMOTING_GAME_FILES_ROOT)} && "
            f"mkdir -p {shlex.quote(GAME_FILES_ROOT)}",
            timeout=30,
        )

        directories = {"project", "project/game", "web"}
        for target in files:
            parent = _game_file_parent(target)
            if parent:
                directories.add(parent)

        mkdir_command = "mkdir -p " + " ".join(
            shlex.quote(_game_file_path(directory)) for directory in sorted(directories)
        )
        mkdir_result = sandbox.commands.run(mkdir_command, timeout=30)
        if getattr(mkdir_result, "exit_code", 0) not in {0, None}:
            return _phase_error(
                config,
                "component",
                "Failed to create game workspace directories.",
                extra={"output": _command_output(mkdir_result), "exit_code": getattr(mkdir_result, "exit_code", None)},
            )

        written_paths = []
        for rel_path, content in files.items():
            sandbox.files.write(_game_file_path(rel_path), content)
            written_paths.append(rel_path)
    except Exception as exc:
        return _phase_error(config, "component", _exception_reason(exc), exc=exc)

    component_contracts = [component.to_contract_dict() for component in selected]
    selected_public = [
        {
            "name": component.name,
            "version": component.version,
            "contract": component.to_contract_dict(),
        }
        for component in selected
    ]
    thread_id = _thread_id_from_config(config)
    foundation_hashes = {
        path: sha256(content.encode("utf-8")).hexdigest()
        for path, content in files.items()
        if path.startswith("project/game/")
    }
    required_component_reads = sorted(
        {
            component_file.target
            for component in selected
            for component_file in component.files
            if component_file.target.startswith("project/addons/gamegen/")
        }
    )
    required_main_reads = ["project/game/main.gd", "project/game/main.tscn"]
    project_component_contracts = {
        path: _gdscript_public_contract(content)
        for path, content in files.items()
        if path.startswith("project/addons/gamegen/") and path.endswith(".gd")
    }

    def mark_created(state: dict[str, Any]) -> None:
        if state.get("workspace_created"):
            raise RuntimeError("Workspace was already created for this chat.")
        state.update(
            {
                "phase": "needs_validation",
                "workspace_created": True,
                "components": [component.name for component in selected],
                "files": foundation_hashes,
                "workspace_sources": dict(files),
                "required_component_reads": required_component_reads,
                "required_main_reads": required_main_reads,
                "read_coverage": {},
                "read_snapshots": {},
                "active_read": None,
                "read_revision": 0,
                "read_lines_covered": 0,
                "initial_context_complete": False,
                "preparation_complete": False,
                "research_completed": False,
                "research_context": {},
                "research_revision": 0,
                "research_attempts": [],
                "workspace_revision": 1,
                "last_valid_revision": 0,
                "candidate_revision": None,
                "candidate_base_revision": None,
                "candidate_changed_paths": [],
                "candidate_files": {},
                "candidate_sources": {},
                "candidate_stage_name": "",
                "candidate_stage_targets": [],
                "candidate_change_summary": "",
                "completed_stages": [],
                "completed_stage_targets": [],
                "game_blueprint": create_game_blueprint(),
                "active_stage_id": "",
                "active_step_id": "",
                "stage_plan_revision": 0,
                "project_component_contracts": project_component_contracts,
                "candidate_failure_count": 0,
                "repair_packet": None,
                "step_correction": None,
                "step_attempt_failures": 0,
                "rejected_step_signatures": [],
                "rejected_step_strategy_fingerprints": [],
                "rejected_stage_plans": [],
                "stage_replan_counts": {},
                "last_validation_fingerprint": "",
                "attempted_edit_signatures": [],
                "blocked_stage_strategies": [],
                "stage_retry_directive": None,
                "gameplay_edit_count": 0,
                "validated_revision": -1,
                "build_revision": -1,
                "game_plan": game_plan,
                "game_title": title,
                "component_contracts": component_contracts,
                "pending_changed_paths": [],
                "last_tool_signature": "",
                "last_tool_progress_token": "",
                "repeated_tool_call_count": 0,
            }
        )

    update_execution_state(thread_id, mark_created)
    print(
        f"[agent-progress] create_game_workspace components={[component.name for component in selected]} "
        f"count={len(written_paths)} paths={written_paths}"
    )
    if game_plan:
        compact_plan = " ".join(str(game_plan).split())
        print(f"[agent-progress] create_game_workspace plan={compact_plan}")
    return {
        "status": "success",
        "phase": "component",
        "components": selected_public,
        "files_written": written_paths,
        "game_plan": game_plan,
        "required_component_reads": required_component_reads,
        "required_main_reads": required_main_reads,
        "validation_tool": "validate_godot_project",
        "build_tool": "build_godot_game",
        "foundation_files": ["project/game/main.tscn", "project/game/main.gd"],
        "integration_contract": {
            "engine": "Godot 4.7",
            "language": "GDScript",
            "component_scripts": [
                f"res://{file.target.removeprefix('project/')}"
                for component in selected
                for file in component.files
                if file.target.endswith(".gd")
            ],
            "required_browser_hooks": [
                "GamegenWebRuntime.publish_ready(smoke_result)",
            ],
        },
        "message": (
            "Godot workspace and broad foundation are ready. The application will now validate, "
            "research pinned documentation and demos, and ingest source before the first edit."
        ),
    }

def _command_output(result) -> str:
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return "\n".join(part for part in [stdout, stderr] if part).strip()


def _godot_script_errors(output: str) -> list[str]:
    markers = (
        "SCRIPT ERROR:",
        "Parse Error:",
        "Failed to load script",
        "Failed loading resource",
        "Error parsing",
        "Cannot open file",
    )
    return [line.strip() for line in output.splitlines() if any(marker in line for marker in markers)]


PURE_FORMATTING_ERROR_MARKERS = (
    "mixed use of tabs and spaces",
    "used tab character for indentation",
    "used space character for indentation",
)


def _has_pure_formatting_error(errors: list[str], output: str) -> bool:
    diagnostic = "\n".join([*errors, output]).lower()
    return any(marker in diagnostic for marker in PURE_FORMATTING_ERROR_MARKERS)


def _run_godot_validation(sandbox, project_root: str):
    editor_command = (
        f"{shlex.quote(GODOT_BINARY)} --headless --editor "
        f"--path {shlex.quote(project_root)} --quit"
    )
    startup_command = (
        f"{shlex.quote(GODOT_BINARY)} --headless "
        f"--path {shlex.quote(project_root)} --quit-after 2"
    )
    return _retry_infrastructure(
        lambda: sandbox.commands.run(
            f"bash -lc {shlex.quote(f'{editor_command} && {startup_command}')}",
            timeout=180,
        )
    )


def _repair_candidate_formatting(
    sandbox,
    thread_id: str,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Canonicalize candidate GDScript indentation and persist the new revision."""
    revisions: dict[str, str] = {}
    normalized_sources: dict[str, str] = {}
    for path in state.get("candidate_changed_paths") or []:
        if not str(path).endswith(".gd"):
            continue
        absolute_path = _workspace_file_path(CANDIDATE_GAME_FILES_ROOT, str(path))
        original = _retry_infrastructure(lambda: sandbox.files.read(absolute_path))
        if not isinstance(original, str):
            continue
        normalized = canonicalize_gdscript_indentation(original)
        if normalized == original:
            continue
        _retry_infrastructure(lambda: sandbox.files.write(absolute_path, normalized))
        revisions[str(path)] = sha256(normalized.encode("utf-8")).hexdigest()
        normalized_sources[str(path)] = normalized

    if not revisions:
        return None

    def record(current: dict[str, Any]) -> None:
        files = dict(current.get("candidate_files") or {})
        files.update(revisions)
        sources = dict(current.get("candidate_sources") or {})
        sources.update(normalized_sources)
        current.update(
            {
                "candidate_revision": uuid.uuid4().hex,
                "candidate_files": files,
                "candidate_sources": sources,
            }
        )

    updated = update_execution_state(thread_id, record)
    print(
        f"[agent-progress] automatic_format_repair paths={list(revisions)} "
        f"candidate={updated.get('candidate_revision')}",
        flush=True,
    )
    return updated


def _clear_candidate_fields(state: dict[str, Any], *, phase: str) -> None:
    state.update(
        {
            "phase": phase,
            "candidate_revision": None,
            "candidate_base_revision": None,
            "candidate_changed_paths": [],
            "candidate_files": {},
            "candidate_sources": {},
            "candidate_stage_name": "",
            "candidate_stage_targets": [],
            "candidate_change_summary": "",
            "candidate_failure_count": 0,
            "repair_packet": None,
            "last_validation_fingerprint": "",
        }
    )


def _candidate_validation_failure(
    *,
    config: RunnableConfig,
    sandbox,
    state: dict[str, Any],
    errors: list[str],
    output: str,
    exit_code: int | None,
) -> dict[str, Any]:
    thread_id = _thread_id_from_config(config)
    fingerprint = sha256("\n".join(errors).encode()).hexdigest()
    failure_count = int(state.get("candidate_failure_count", 0)) + 1
    repeated = fingerprint == str(state.get("last_validation_fingerprint") or "")
    repair_status = _repair_status(
        config,
        "validate",
        "Godot stage validation found script or scene errors",
    )
    packet = build_repair_packet(
        state=state,
        errors=errors,
        output=output,
        read_text=lambda path: _retry_infrastructure(
            lambda: sandbox.files.read(_workspace_file_path(CANDIDATE_GAME_FILES_ROOT, path))
        ),
    )

    if failure_count >= 3:
        stage_name = str(state.get("candidate_stage_name") or "stage")
        targets = [str(target) for target in state.get("candidate_stage_targets") or []]
        strategy_records: list[dict[str, str]] = []
        for target in targets:
            candidate_path = next(
                (
                    str(path)
                    for path in state.get("candidate_changed_paths") or []
                    if str(path).endswith(".gd")
                ),
                "",
            )
            if not candidate_path:
                continue
            candidate_content = _retry_infrastructure(
                lambda path=candidate_path: sandbox.files.read(
                    _workspace_file_path(CANDIDATE_GAME_FILES_ROOT, path)
                )
            )
            if isinstance(candidate_content, str):
                strategy_records.append(
                    rollback_strategy_record(
                        stage_name=stage_name,
                        target=target,
                        content=candidate_content,
                        diagnostic=errors[0] if errors else "validation failure",
                    )
                )
        retry_directive = different_strategy_directive(
            stage_name,
            targets[0] if targets else "the current stage function",
        )
        _discard_candidate_workspace(sandbox)

        def rollback(current: dict[str, Any]) -> None:
            _clear_candidate_fields(current, phase="ready_to_implement_step")
            current["preparation_complete"] = True
            current["blocked_stage_strategies"] = [
                *list(current.get("blocked_stage_strategies") or []),
                *strategy_records,
            ][-20:]
            current["stage_retry_directive"] = retry_directive

        update_execution_state(thread_id, rollback)
        return {
            "status": "error",
            "phase": "candidate_rolled_back",
            **repair_status,
            "exit_code": exit_code,
            "errors": errors,
            "repeated_diagnostic": repeated,
            "rollback": {
                "last_valid_revision": int(state.get("workspace_revision", 0)),
                "reason": "Three candidate validations failed; staged files were discarded.",
            },
            "retry_directive": retry_directive,
            "instruction": retry_directive["instruction"],
        }

    update_execution_state(
        thread_id,
        lambda current: current.update(
            {
                "phase": "repair",
                "candidate_failure_count": failure_count,
                "repair_packet": packet,
                "last_validation_fingerprint": fingerprint,
            }
        ),
    )
    return {
        "status": "error",
        "phase": "repair",
        **repair_status,
        "exit_code": exit_code,
        "errors": errors,
        "repeated_diagnostic": repeated,
        "repair_packet": packet,
    }


def _promote_validated_candidate(sandbox, thread_id: str, state: dict[str, Any]) -> int:
    _promote_candidate_workspace(sandbox)
    changed_paths = list(state.get("candidate_changed_paths") or [])
    candidate_files = dict(state.get("candidate_files") or {})
    promoted_sources: dict[str, str] = {}
    changed_component_contracts: dict[str, Any] = {}
    for path in changed_paths:
        content = _retry_infrastructure(
            lambda path=path: sandbox.files.read(_workspace_file_path(GAME_FILES_ROOT, path))
        )
        if isinstance(content, str):
            promoted_sources[path] = content
            if path.startswith("project/addons/gamegen/") and path.endswith(".gd"):
                changed_component_contracts[path] = _gdscript_public_contract(content)

    def commit(current: dict[str, Any]) -> None:
        new_revision = int(current.get("workspace_revision", 0)) + 1
        active_stage = next_stage(current) or {}
        active_step = current_step(current)
        completed_stages = list(current.get("completed_stages") or [])
        completed_targets = list(current.get("completed_stage_targets") or [])
        completed_targets = list(
            dict.fromkeys([*completed_targets, *(state.get("candidate_stage_targets") or [])])
        )
        completed_stages.append(
            {
                "name": str(state.get("candidate_stage_name") or "stage"),
                "summary": str(state.get("candidate_change_summary") or ""),
                "revision": new_revision,
            }
        )
        files = dict(current.get("files") or {})
        files.update(candidate_files)
        workspace_sources = dict(current.get("workspace_sources") or {})
        workspace_sources.update(promoted_sources)
        snapshots = dict(current.get("read_snapshots") or {})
        coverage = dict(current.get("read_coverage") or {})
        for path in changed_paths:
            snapshots.pop(path, None)
            coverage.pop(path, None)
        current.update(
            {
                "files": files,
                "workspace_sources": workspace_sources,
                "read_snapshots": snapshots,
                "read_coverage": coverage,
                "active_read": None,
                "workspace_revision": new_revision,
                "last_valid_revision": new_revision,
                "validated_revision": new_revision,
                "build_revision": -1,
                "gameplay_edit_count": int(current.get("gameplay_edit_count", 0)) + 1,
                "completed_stages": completed_stages[-20:],
                "completed_stage_targets": completed_targets,
                "pending_changed_paths": changed_paths,
                "initial_context_complete": False,
                "stage_retry_directive": None,
            }
        )
        component_contracts = dict(current.get("project_component_contracts") or {})
        component_contracts.update(changed_component_contracts)
        current["project_component_contracts"] = component_contracts
        if active_step is not None:
            record_validated_step_contract(
                current,
                step=active_step,
                stage_id=str(active_stage.get("id") or ""),
                revision=new_revision,
                sources={**workspace_sources, **promoted_sources},
            )
        _clear_candidate_fields(current, phase="validated")
        advance_blueprint_after_commit(current)
        current["step_correction"] = None
        current["step_attempt_failures"] = 0
        current["rejected_step_signatures"] = []
        current["rejected_step_strategy_fingerprints"] = []

    committed = update_execution_state(thread_id, commit)
    try:
        _discard_candidate_workspace(sandbox)
    except Exception as exc:
        print(
            f"[agent-progress] candidate_cleanup_failed reason={_exception_reason(exc)}",
            flush=True,
        )
    return int(committed.get("workspace_revision", 0))


def validate_godot_project(*, config: Annotated[RunnableConfig, InjectedToolArg]):
    """Import and validate the current Godot stage without exporting it.

    Call this after each planned implementation stage. Godot preserves the
    project/.godot import cache for later validations and the final export.
    """
    if rejection := _guard_tool(config, "validate_godot_project"):
        return rejection
    thread_id = _thread_id_from_config(config)
    state = load_execution_state(thread_id)
    candidate = state.get("candidate_revision") is not None
    workspace_root = CANDIDATE_GAME_FILES_ROOT if candidate else GAME_FILES_ROOT
    project_root = f"{workspace_root}/project"
    try:
        sandbox = get_sandbox(config)
        binary_check = _retry_infrastructure(
            lambda: sandbox.commands.run(
                f"command -v {shlex.quote(GODOT_BINARY)}",
                timeout=30,
            )
        )
    except Exception as exc:
        return _phase_error(config, "validate", _exception_reason(exc), exc=exc)

    if getattr(binary_check, "exit_code", 1) != 0:
        reason = f"Godot editor binary '{GODOT_BINARY}' is not installed in the sandbox image."
        return {
            "status": "error",
            "phase": "environment",
            "terminal": False,
            "errors": [reason],
            "setup_required": "Build and select the pinned Godot E2B template.",
        }

    required_files = [
        f"{project_root}/project.godot",
        f"{project_root}/game/main.tscn",
        f"{project_root}/game/main.gd",
    ]
    required_check = " && ".join(f"test -f {shlex.quote(path)}" for path in required_files)
    try:
        required_result = sandbox.commands.run(required_check, timeout=30)
    except Exception as exc:
        return _phase_error(config, "validate", _exception_reason(exc), exc=exc)
    if getattr(required_result, "exit_code", 1) != 0:
        repair_status = _repair_status(config, "validate", "required Godot foundation files are missing")
        return {
            "status": "error",
            "phase": "validate",
            **repair_status,
            "errors": [f"Validation requires: {', '.join(required_files)}"],
        }

    try:
        validation_result = _run_godot_validation(sandbox, project_root)
    except Exception as exc:
        return _phase_error(config, "validate", _exception_reason(exc), exc=exc)

    output = _command_output(validation_result)
    errors = _godot_script_errors(output)
    if (
        candidate
        and (getattr(validation_result, "exit_code", 1) != 0 or errors)
        and _has_pure_formatting_error(errors, output)
    ):
        try:
            formatted_state = _repair_candidate_formatting(sandbox, thread_id, state)
            if formatted_state is not None:
                state = formatted_state
                validation_result = _run_godot_validation(sandbox, project_root)
                output = _command_output(validation_result)
                errors = _godot_script_errors(output)
        except (StructuralEditError, ValueError) as exc:
            print(
                f"[agent-progress] automatic_format_repair_skipped reason={_exception_reason(exc)}",
                flush=True,
            )
        except Exception as exc:
            if _is_infrastructure_error(exc):
                return _phase_error(config, "validate", _exception_reason(exc), exc=exc)
            print(
                f"[agent-progress] automatic_format_repair_failed reason={_exception_reason(exc)}",
                flush=True,
            )
    if getattr(validation_result, "exit_code", 1) != 0 or errors:
        reported_errors = errors or ["Godot stage validation found script or scene errors"]
        if candidate:
            return _candidate_validation_failure(
                config=config,
                sandbox=sandbox,
                state=state,
                errors=reported_errors,
                output=output,
                exit_code=getattr(validation_result, "exit_code", None),
            )
        repair_status = _repair_status(config, "validate", reported_errors[0])
        return {
            "status": "error",
            "phase": "validate",
            **repair_status,
            "exit_code": getattr(validation_result, "exit_code", None),
            "errors": reported_errors,
            "pitfall_guidance": guidance_for_errors(reported_errors),
            "output": output,
        }

    print(
        "[agent-progress] godot validation "
        f"exit_code={getattr(validation_result, 'exit_code', None)} "
        f"output_lines={len(output.splitlines())}"
    )
    if candidate:
        try:
            validated_revision = _promote_validated_candidate(sandbox, thread_id, state)
        except Exception as exc:
            return {
                "status": "error",
                "phase": "infrastructure",
                "terminal": False,
                "retryable": True,
                "errors": [f"Candidate promotion failed: {_exception_reason(exc)}"],
            }
    else:
        validated_revision = int(state.get("workspace_revision", 0))
        update_execution_state(
            thread_id,
            lambda current: current.update(
                {
                    "phase": "validated",
                    "last_valid_revision": validated_revision,
                    "validated_revision": validated_revision,
                }
            ),
        )
    return {
        "status": "success",
        "phase": "validate",
        "validated_revision": validated_revision,
        "candidate_promoted": candidate,
        "message": (
            "Current stage imports, parses, and starts headlessly. Review it against the "
            "stage's acceptance criteria, make targeted improvements if needed, then continue."
        ),
    }


def build_godot_game(*, config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Import changed resources, validate, and export the finished Godot project.

    Returns:
        A structured success or error result with Godot output.

    Usage:
        Call only after all Godot project files have been written and the game is
        ready for final export. Godot keeps its import cache in project/.godot.
    """
    if rejection := _guard_tool(config, "build_godot_game"):
        return rejection
    thread_id = _thread_id_from_config(config)

    try:
        sandbox = get_sandbox(config)
        binary_check = sandbox.commands.run(
            f"command -v {shlex.quote(GODOT_BINARY)}",
            timeout=30,
        )
    except Exception as exc:
        return _phase_error(config, "build", _exception_reason(exc), exc=exc)

    if getattr(binary_check, "exit_code", 1) != 0:
        reason = f"Godot editor binary '{GODOT_BINARY}' is not installed in the sandbox image."
        print(f"[agent-progress] phase=environment terminal=false reason={reason}")
        return {
            "status": "error",
            "phase": "environment",
            "terminal": False,
            "errors": [reason],
            "setup_required": "Build and select the pinned Godot E2B template.",
        }

    required_files = [
        f"{GODOT_PROJECT_ROOT}/project.godot",
        f"{GODOT_PROJECT_ROOT}/export_presets.cfg",
        f"{GODOT_PROJECT_ROOT}/game/main.tscn",
        f"{GODOT_PROJECT_ROOT}/game/main.gd",
    ]
    required_check = " && ".join(f"test -f {shlex.quote(path)}" for path in required_files)
    try:
        required_result = sandbox.commands.run(required_check, timeout=30)
    except Exception as exc:
        return _phase_error(config, "build", _exception_reason(exc), exc=exc)
    if getattr(required_result, "exit_code", 1) != 0:
        repair_status = _repair_status(config, "build", "required Godot project files are missing")
        return {
            "status": "error",
            "phase": "build",
            **repair_status,
            "errors": [f"Missing one or more required files: {', '.join(required_files)}"],
        }

    try:
        sandbox.commands.run(
            f"rm -rf {shlex.quote(GODOT_WEB_ROOT)} && mkdir -p {shlex.quote(GODOT_WEB_ROOT)}",
            timeout=30,
        )
        export_result = sandbox.commands.run(
            f"{shlex.quote(GODOT_BINARY)} --headless --path {shlex.quote(GODOT_PROJECT_ROOT)} "
            f"--export-release Web {shlex.quote(f'{GODOT_WEB_ROOT}/index.html')}",
            timeout=300,
        )
    except Exception as exc:
        return _phase_error(config, "build", _exception_reason(exc), exc=exc)

    print(
        "[agent-progress] godot export "
        f"exit_code={getattr(export_result, 'exit_code', None)} "
        f"output={_command_output(export_result) or '<empty>'}"
    )
    export_output = _command_output(export_result)
    export_errors = _godot_script_errors(export_output)
    if getattr(export_result, "exit_code", 1) != 0 or export_errors:
        reason = "Godot web export found script errors" if export_errors else "Godot web export failed"
        repair_status = _repair_status(config, "build", reason)
        return {
            "status": "error",
            "phase": "build",
            **repair_status,
            "exit_code": getattr(export_result, "exit_code", None),
            "errors": export_errors,
            "output": export_output,
        }

    artifact_command = (
        f"test -f {shlex.quote(f'{GODOT_WEB_ROOT}/index.html')} && "
        f"find {shlex.quote(GODOT_WEB_ROOT)} -maxdepth 1 -type f -name '*.wasm' | grep -q . && "
        f"find {shlex.quote(GODOT_WEB_ROOT)} -maxdepth 1 -type f -name '*.pck' | grep -q ."
    )
    try:
        artifact_check = sandbox.commands.run(f"bash -lc {shlex.quote(artifact_command)}", timeout=30)
    except Exception as exc:
        return _phase_error(config, "build", _exception_reason(exc), exc=exc)
    if getattr(artifact_check, "exit_code", 1) != 0:
        repair_status = _repair_status(config, "build", "Godot web artifacts are incomplete")
        return {
            "status": "error",
            "phase": "build",
            **repair_status,
            "errors": ["Godot export did not produce index.html, a .wasm file, and a .pck file."],
        }

    state = update_execution_state(
        thread_id,
        lambda current: current.update(
            {
                "phase": "built",
                "build_revision": int(current.get("workspace_revision", 0)),
            }
        ),
    )
    return {
        "status": "success",
        "phase": "build",
        "build_revision": state["build_revision"],
        "output": export_output,
    }


async def _run_playability_checks(page):
    return await page.evaluate(
        """
        async () => {
          const failures = []
          const canvas = document.querySelector('canvas')

          if (!canvas) {
            failures.push('No canvas element was found. The generated game must render through WebGL.')
          } else {
            const rect = canvas.getBoundingClientRect()
            if (rect.width < 320 || rect.height < 240) {
              failures.push(`Canvas is too small to be playable: ${Math.round(rect.width)}x${Math.round(rect.height)}.`)
            }

            const gl = canvas.getContext('webgl2') || canvas.getContext('webgl')
            if (!gl) {
              failures.push('Canvas does not expose a WebGL context.')
            }
          }

          if (!window.__GAMEGEN_READY__) {
            failures.push('window.__GAMEGEN_READY__ was not set after the first playable render.')
          }

          if (!window.__GAMEGEN_TEST__ || typeof window.__GAMEGEN_TEST__.smokeTest !== 'function') {
            failures.push('Missing window.__GAMEGEN_TEST__.smokeTest().')
          } else {
            try {
              const result = await window.__GAMEGEN_TEST__.smokeTest()
              if (!result || result.ok !== true) {
                failures.push(`smokeTest did not report ok=true: ${JSON.stringify(result)}`)
              }
            } catch (error) {
              failures.push(`smokeTest threw: ${error && error.message ? error.message : String(error)}`)
            }
          }

          return {
            ok: failures.length === 0,
            failures,
            title: document.title || 'Generated game',
            url: window.location.href,
          }
        }
        """
    )


def _start_preview_server(sandbox) -> str:
    """Own one sandbox preview server and wait until it serves the exported game locally."""
    pid_path = "/tmp/gamegen-preview.pid"
    log_path = "/tmp/gamegen-preview.log"
    cleanup_command = (
        f"if test -f {shlex.quote(pid_path)}; then "
        f"kill \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null || true; fi; "
        f"rm -f {shlex.quote(pid_path)}"
    )
    sandbox.commands.run(f"bash -lc {shlex.quote(cleanup_command)}", timeout=30)

    start_command = (
        f"nohup python3 -m http.server 3000 --bind 0.0.0.0 "
        f"--directory {shlex.quote(GODOT_WEB_ROOT)} "
        f"> {shlex.quote(log_path)} 2>&1 < /dev/null & "
        f"echo $! > {shlex.quote(pid_path)}"
    )
    start_result = sandbox.commands.run(f"bash -lc {shlex.quote(start_command)}", timeout=30)
    if getattr(start_result, "exit_code", 1) != 0:
        raise RuntimeError(f"Preview server failed to start: {_command_output(start_result)}")

    readiness_command = (
        "for attempt in $(seq 1 60); do "
        "curl --fail --silent --show-error http://127.0.0.1:3000/ >/dev/null && exit 0; "
        "sleep 0.25; "
        "done; "
        f"cat {shlex.quote(log_path)} >&2; exit 1"
    )
    ready_result = sandbox.commands.run(
        f"bash -lc {shlex.quote(readiness_command)}",
        timeout=30,
    )
    if getattr(ready_result, "exit_code", 1) != 0:
        raise RuntimeError(
            "Preview server did not become ready inside the sandbox: "
            f"{_command_output(ready_result) or '<no server log>'}"
        )
    return f"https://{sandbox.get_host(3000)}"


async def _wait_for_public_preview(page, url: str, *, attempts: int = 15):
    """Wait for the E2B port proxy without misclassifying transient 502s as game failures."""
    last_error = "no response"
    for attempt in range(1, attempts + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            status = getattr(response, "status", None)
            if status is not None and 200 <= status < 400:
                return response
            last_error = f"HTTP {status if status is not None else 'NO_RESPONSE'}"
        except Exception as exc:
            last_error = _exception_reason(exc)
        if attempt < attempts:
            await asyncio.sleep(1)
    raise RuntimeError(
        f"Public preview did not become reachable after {attempts} attempts: {last_error}"
    )


async def run_game(config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Runs the game for testing.

    Args: None

    Returns:
        A structured success result with a stored game URL, or the errors that must be fixed.

    Usage:
        Use this tool to test the game. A passing run also stores the playable game.
    """
    rejection = await sync_to_async(_guard_tool, thread_sensitive=True)(config, "run_game")
    if rejection:
        return rejection
    configuration = Configuration.from_runnable_config(config)
    thread_id = _thread_id_from_config(config)
    await sync_to_async(update_execution_state, thread_sensitive=True)(
        thread_id,
        lambda state: state.update({"phase": "runtime", "run_finished": False}),
    )

    try:
        sandbox = await sync_to_async(get_sandbox, thread_sensitive=True)(config)
        url = await sync_to_async(_start_preview_server, thread_sensitive=True)(sandbox)
    except Exception as exc:
        return _phase_error(config, "runtime", _exception_reason(exc), exc=exc)

    print('Server started at:', url)

    console_err = []
    page_err = []
    failed_requests = []

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Establish that the E2B proxy is reachable before collecting game diagnostics. A
            # short-lived 502 here is infrastructure propagation, not a defect in generated code.
            await _wait_for_public_preview(page, f"{url}/")

            def handle_console_errors(msg):
                nonlocal console_err
                if msg.type in {"error"}:
                    err_str = f"[{msg.type}] {msg.text}"
                    console_err.append(err_str)

            def handle_page_errors(exc):
                nonlocal page_err
                err_str = f"[PAGE ERROR] {exc.message}"
                page_err.append(err_str)

            def handle_response(response):
                if response.status >= 400 and not response.url.endswith("/favicon.ico"):
                    failed_requests.append(f"{response.status} {response.url}")

            page.on("console", handle_console_errors)
            page.on("pageerror", handle_page_errors)
            page.on("response", handle_response)

            response = await page.reload(wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                failed_requests.append(f"{getattr(response, 'status', 'NO_RESPONSE')} {url}/")

            try:
                await page.wait_for_function("window.__GAMEGEN_READY__ === true", timeout=30000)
            except PlaywrightTimeoutError:
                pass

            playability = await _run_playability_checks(page)
    except Exception as exc:
        return _phase_error(
            config,
            "runtime",
            _exception_reason(exc),
            exc=exc,
            extra={
                "test_report": {
                    "browser_url": f"{url}/",
                    "console_errors": console_err,
                    "page_errors": page_err,
                    "failed_requests": failed_requests,
                }
            },
        )
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    result = console_err + page_err + failed_requests + playability.get("failures", [])
    test_report = {
        "browser_url": f"{url}/",
        "console_errors": console_err,
        "page_errors": page_err,
        "failed_requests": failed_requests,
        "playability": playability,
    }

    if result:
        repair_status = _repair_status(config, "runtime", "playability checks failed")
        return {
            "status": "error",
            "phase": "runtime",
            **repair_status,
            "errors": result,
            "test_report": test_report,
        }

    try:
        game = await sync_to_async(store_sandbox_game, thread_sensitive=True)(
            sandbox,
            thread_id=thread_id,
            title=playability.get("title") or configuration.project_id or thread_id,
            test_report=test_report,
            source_root=GODOT_WEB_ROOT,
        )
    except Exception as exc:
        repair_status = _repair_status(config, "store", str(exc))
        return {
            "status": "error",
            "phase": "store",
            **repair_status,
            "errors": [str(exc)],
            "test_report": test_report,
        }

    await sync_to_async(update_execution_state, thread_sensitive=True)(
        thread_id,
        lambda state: state.update(
            {
                "phase": "complete",
                "run_finished": True,
                "run_succeeded": True,
                "terminal": True,
            }
        ),
    )
    return {
        "status": "success",
        "phase": "runtime",
        "game": game,
        "play_url": game["play_url"],
        "message": f"Game passed compile and playability checks. Stored as {game['id']}.",
    }

TOOL_LIST: List[Callable[..., Any]] = [
    research_godot_docs,
    create_game_workspace,
    read_game_files,
    plan_game_stage,
    implement_stage_step,
    validate_godot_project,
    build_godot_game,
    run_game,
]
tool_node = ToolNode(TOOL_LIST)
