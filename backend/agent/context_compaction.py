"""Token-aware, model-generated context checkpoints for the game agent."""

from __future__ import annotations

# import ast  # Legacy tool-history reconstruction only.
import json
from dataclasses import dataclass
# from hashlib import sha256  # Legacy tool-history reconstruction only.
from typing import Any, Protocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage



class ContextModel(Protocol):
    context_window: int
    max_output_tokens: int

    def count_prompt_tokens(self, messages: Sequence[BaseMessage]) -> int: ...
    def model_copy(self, *, update: dict[str, Any]) -> Any: ...
    def invoke(self, input: Any, *args: Any, **kwargs: Any) -> BaseMessage: ...


SUMMARY_MAX_OUTPUT_TOKENS = 256
TOKEN_SAFETY_MARGIN = 128
MIN_RESPONSE_TOKENS = 512
MAX_CHECKPOINT_REQUEST_CHARS = 1_200
# MAX_REHYDRATED_FILES = 20  # Legacy tool-history reconstruction only.

COMPACTION_SYSTEM_PROMPT = """Summarize durable user intent and design decisions in at most 150
tokens. Application state owns files, tools, validation, runtime, and progress. Return valid compact
JSON only, with no Markdown or tools."""

COMPACTION_USER_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION.
Create a handoff summary for the same agent to resume this game-generation task.

Return exactly these JSON fields:
{
  "user_goal": "string",
  "requirements": ["string"],
  "design_decisions": [{"decision": "string", "reason": "string"}]
}

Keep at most 4 requirements and 3 decisions. Use short phrases. Never report completed work, files,
tool/validation/build/runtime state, repairs, obsolete discussion, or source code."""

SUMMARY_PREFIX = """CONTEXT CHECKPOINT: intentions only; application state is authoritative."""

# GENERATION_TOOLS = {
#     "research_godot_docs",
#     "create_game_workspace",
#     "read_game_files",
#     "edit_game_files",
#     "write_files",
#     "validate_godot_project",
#     "build_godot_game",
#     "run_game",
# }

INTERNAL_HUMAN_PREFIXES = (
    "Internal validation failed:",
    "The previous model turn made no executable progress.",
    "CONTEXT CHECKPOINT SUMMARY",
    "AUTOMATIC PREPARATION RESULT:",
    "AUTOMATIC VALIDATION RESULT:",
)


@dataclass(frozen=True)
class ContextProjection:
    messages: list[BaseMessage]
    compacted: bool
    compacted_this_call: bool
    source_message_count: int
    projected_message_count: int
    projected_tokens: int
    compaction_threshold_tokens: int
    context_summary: str
    context_summary_through: int
    context_compaction_count: int


'''Legacy tool-history reconstruction helpers. Production state lives in execution_state.py.

def _parse_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "")
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"raw": text}


def _tool_call_index(messages: Sequence[BaseMessage]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = str(call.get("id") or "")
            if call_id:
                calls[call_id] = {
                    "name": str(call.get("name") or ""),
                    "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                }
    return calls


def _tool_events(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    calls = _tool_call_index(messages)
    events = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        call = calls.get(str(message.tool_call_id or ""), {})
        name = str(message.name or call.get("name") or "")
        if name in GENERATION_TOOLS:
            events.append(
                {
                    "name": name,
                    "args": call.get("args") or {},
                    "result": _parse_payload(message.content),
                }
            )
    return events
'''


def _latest_external_user_message(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        content = str(message.content or "").strip()
        if content and not content.startswith(INTERNAL_HUMAN_PREFIXES):
            return content
    return ""


def _contains_external_user_message(messages: Sequence[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage)
        and (content := str(message.content or "").strip())
        and not content.startswith(INTERNAL_HUMAN_PREFIXES)
        for message in messages
    )


'''Legacy tool-history state reconstruction. Production uses execution_state.py.

def build_authoritative_execution_state(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    """Legacy tool-history reconstruction; use execution_state.py in production."""
    events = _tool_events(messages)
    files: dict[str, str] = {}
    components: list[dict[str, str]] = []
    active_file_reads: list[dict[str, Any]] = []
    workspace_created = False
    validated = False
    validation_count = 0
    build_succeeded = False
    run_succeeded = False
    play_url = ""
    last_event: dict[str, Any] = {}

    for event in events:
        name = event["name"]
        args = event["args"]
        result = event["result"]
        status = str(result.get("status") or "unknown")
        success = status == "success"

        last_event = {
            "tool": name,
            "status": status,
            "phase": str(result.get("phase") or ""),
        }
        if not success:
            last_event["errors"] = (
                result.get("errors")
                or result.get("message")
                or result.get("raw")
                or ""
            )
            for key in ("repair_attempts", "max_repair_attempts", "terminal"):
                if key in result:
                    last_event[key] = result[key]

        if name == "create_game_workspace" and success:
            workspace_created = True
            components = [
                {
                    "name": str(component.get("name") or ""),
                    "version": str(component.get("version") or ""),
                }
                for component in result.get("components", [])
                if isinstance(component, dict)
            ]

        elif name == "write_files" and success:
            written = set(result.get("files_written") or [])
            for path, content in (args.get("files") or {}).items():
                if path in written and isinstance(content, str):
                    files[str(path)] = sha256(content.encode("utf-8")).hexdigest()
            active_file_reads = []
            validated = False
            build_succeeded = False
            run_succeeded = False
            play_url = ""

        elif name == "read_game_files" and success:
            active_file_reads = []
            for record in result.get("files", []):
                if not isinstance(record, dict) or not record.get("path"):
                    continue
                path = str(record["path"])
                revision = str(record.get("sha256") or "")
                if revision:
                    files[path] = revision
                active_file_reads.append(
                    {
                        "path": path,
                        "sha256": revision,
                        "start_line": record.get("start_line"),
                        "end_line": record.get("end_line"),
                        "content": str(record.get("content") or ""),
                    }
                )

        elif name == "edit_game_files":
            active_file_reads = []
            if success:
                for path, revision in (result.get("sha256") or {}).items():
                    files[str(path)] = str(revision)
                validated = False
                build_succeeded = False
                run_succeeded = False
                play_url = ""

        elif name == "validate_godot_project":
            validated = success
            if success:
                validation_count += 1

        elif name == "build_godot_game":
            build_succeeded = success
            if not success:
                run_succeeded = False
                play_url = ""

        elif name == "run_game":
            run_succeeded = success
            play_url = str(result.get("play_url") or "") if success else ""

    if run_succeeded:
        phase = "complete"
    elif build_succeeded:
        phase = "built"
    elif validated:
        phase = "validated_stage"
    elif workspace_created:
        phase = "implementation"
    elif events:
        phase = "research"
    else:
        phase = "not_started"

    return {
        "contract": (
            "This block is reconstructed from tool results. Sandbox files and SHA-256 revisions "
            "are authoritative; the LLM summary is not. Read files before depending on their code."
        ),
        "phase": phase,
        "selected_components": components,
        "generated_file_count": len(files),
        "generated_files": [
            {"path": path, "sha256": revision}
            for path, revision in sorted(files.items())[-MAX_REHYDRATED_FILES:]
        ],
        "active_file_reads": active_file_reads,
        "latest_files_validated": validated,
        "successful_validation_count": validation_count,
        "build_succeeded": build_succeeded,
        "run_succeeded": run_succeeded,
        "play_url": play_url,
        "last_tool_event": last_event,
    }
'''


def _checkpoint_message(
    summary: str,
    messages: Sequence[BaseMessage],
) -> HumanMessage:
    latest_request = _latest_external_user_message(messages)
    if len(latest_request) > MAX_CHECKPOINT_REQUEST_CHARS:
        latest_request = (
            latest_request[:MAX_CHECKPOINT_REQUEST_CHARS].rstrip()
            + "\n[request truncated; durable requirements are in the handoff summary]"
        )
    return HumanMessage(
        content=(
            f"{SUMMARY_PREFIX}\n\n"
            f"<handoff_summary>\n{summary.strip()}\n</handoff_summary>\n\n"
            f"<latest_user_request_verbatim>\n{latest_request}\n</latest_user_request_verbatim>"
        )
    )


def _active_messages(
    messages: Sequence[BaseMessage],
    summary: str,
    summary_through: int,
) -> list[BaseMessage]:
    if not summary or summary_through < 0 or summary_through > len(messages):
        return list(messages)
    return [
        _checkpoint_message(summary, messages),
        *messages[summary_through:],
    ]


def _normalize_intention_summary(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) >= 3 else lines).strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Context summary must be a JSON object.")
    required_fields = {
        "user_goal",
        "requirements",
        "design_decisions",
    }
    if not required_fields.issubset(payload) or set(payload) - (required_fields | {"next_intent"}):
        raise ValueError("Context summary has missing or unsupported fields.")
    if not isinstance(payload["user_goal"], str):
        raise ValueError("Context summary goal must be a string.")
    if not isinstance(payload["requirements"], list) or not all(
        isinstance(item, str) for item in payload["requirements"]
    ):
        raise ValueError("Context summary requirements must be an array of strings.")
    decisions = payload["design_decisions"]
    if not isinstance(decisions, list) or not all(
        isinstance(item, dict)
        and set(item) == {"decision", "reason"}
        and isinstance(item["decision"], str)
        and isinstance(item["reason"], str)
        for item in decisions
    ):
        raise ValueError("Context summary decisions must contain decision and reason strings.")
    def clipped(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    normalized: dict[str, Any] = {
        "user_goal": clipped(payload.get("user_goal"), 240),
        "requirements": [
            clipped(item, 140) for item in (payload.get("requirements") or [])[:4]
        ],
        "design_decisions": [
            {
                "decision": clipped(item.get("decision"), 140),
                "reason": clipped(item.get("reason"), 140),
            }
            for item in (payload.get("design_decisions") or [])[:3]
            if isinstance(item, dict)
        ],
    }
    return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))


def _summary_was_truncated(response: Any) -> bool:
    metadata = getattr(response, "response_metadata", {}) or {}
    return bool(metadata.get("output_limit_reached")) or str(
        metadata.get("done_reason") or metadata.get("finish_reason") or ""
    ).lower() in {"length", "max_tokens"}


def _count_normal_prompt(
    model: ContextModel,
    system_prompt: str,
    messages: Sequence[BaseMessage],
) -> int:
    return model.count_prompt_tokens([SystemMessage(content=system_prompt), *messages])


def _plain_text_summary_messages(messages: Sequence[BaseMessage]) -> list[HumanMessage]:
    """Flatten executable chat history into an inert transcript for summarization.

    Some providers require every native tool call in message history to reference a
    tool declared on the current request. Compaction intentionally disables tools,
    so forwarding AIMessage.tool_calls or ToolMessage objects produces an invalid
    request. The summary needs their meaning, not their execution metadata.
    """
    transcript: list[HumanMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        elif isinstance(message, ToolMessage):
            role = f"tool:{message.name or 'unknown'}"
        else:
            role = str(getattr(message, "type", "message") or "message")

        sections = [f"[{role}]", str(message.content or "")]
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                sections.append(
                    "[tool request] "
                    + json.dumps(
                        {
                            "name": str(call.get("name") or "unknown"),
                            "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        default=str,
                    )
                )
        transcript.append(HumanMessage(content="\n".join(sections).strip()))
    return transcript


def _fit_summary_input(
    model: ContextModel,
    messages: Sequence[BaseMessage],
) -> tuple[list[BaseMessage], int]:
    budget = model.context_window - SUMMARY_MAX_OUTPUT_TOKENS - TOKEN_SAFETY_MARGIN
    working: list[BaseMessage] = _plain_text_summary_messages(messages)
    removed = 0

    def request() -> list[BaseMessage]:
        prefix = []
        if removed:
            prefix.append(
                HumanMessage(
                    content=(
                        f"[Emergency compaction note: {removed} oldest transcript message(s) "
                        "were omitted to fit the summarization request.]"
                    )
                )
            )
        return [
            SystemMessage(content=COMPACTION_SYSTEM_PROMPT),
            *prefix,
            *working,
            HumanMessage(content=COMPACTION_USER_PROMPT),
        ]

    while True:
        summary_request = request()
        token_count = model.count_prompt_tokens(summary_request)
        if token_count <= budget:
            return summary_request, token_count
        if len(working) <= 1:
            raise RuntimeError(
                "The conversation cannot be compacted safely because one retained message "
                "exceeds the model context window. Reduce the tool output size."
            )
        first_is_checkpoint = (
            isinstance(working[0], HumanMessage)
            and str(working[0].content or "").startswith(SUMMARY_PREFIX)
        )
        remove_index = 1 if first_is_checkpoint and len(working) > 1 else 0
        working.pop(remove_index)
        removed += 1


def prepare_model_context(
    *,
    messages: Sequence[BaseMessage],
    system_prompt: str,
    model: ContextModel,
    summary_model: Any | None = None,
    context_summary: str = "",
    context_summary_through: int = 0,
    context_compaction_count: int = 0,
    force: bool = False,
) -> ContextProjection:
    """Return active history or create a model checkpoint when the token budget is reached."""
    active = _active_messages(messages, context_summary, context_summary_through)
    # Reserve the model's configured response allowance before inference. A request that leaves
    # only the absolute minimum technically fits, but cannot reliably emit a stage plan or bounded
    # code unit. Adaptive output remains a final safety valve after compaction.
    threshold = model.context_window - model.max_output_tokens - TOKEN_SAFETY_MARGIN
    hard_threshold = model.context_window - MIN_RESPONSE_TOKENS - TOKEN_SAFETY_MARGIN
    prompt_tokens = _count_normal_prompt(model, system_prompt, active)

    if prompt_tokens <= threshold and not force:
        return ContextProjection(
            messages=active,
            compacted=bool(context_summary),
            compacted_this_call=False,
            source_message_count=len(messages),
            projected_message_count=len(active),
            projected_tokens=prompt_tokens,
            compaction_threshold_tokens=threshold,
            context_summary=context_summary,
            context_summary_through=context_summary_through,
            context_compaction_count=context_compaction_count,
        )

    # Once a valid intent summary exists, tool traffic and application-owned transition messages
    # add no new subjective information. Advance the checkpoint without spending another model
    # call; objective progress is re-injected from execution_state.py on every turn.
    new_messages = list(messages[context_summary_through:])
    if context_summary and not _contains_external_user_message(new_messages):
        replacement = [_checkpoint_message(context_summary, messages)]
        replacement_tokens = _count_normal_prompt(model, system_prompt, replacement)
        if replacement_tokens > hard_threshold:
            raise RuntimeError(
                "The reused context checkpoint exceeds the reserved input budget: "
                f"{replacement_tokens} > {hard_threshold} tokens."
            )
        print(
            "[agent-context] compaction_complete source=reused_no_new_intent "
            f"replacement_tokens={replacement_tokens}",
            flush=True,
        )
        return ContextProjection(
            messages=replacement,
            compacted=True,
            compacted_this_call=True,
            source_message_count=len(messages),
            projected_message_count=1,
            projected_tokens=replacement_tokens,
            compaction_threshold_tokens=threshold,
            context_summary=context_summary,
            context_summary_through=len(messages),
            context_compaction_count=context_compaction_count + 1,
        )

    if summary_model is None:
        compaction_model = model.model_copy(
            update={
                "tools": [],
                "temperature": 0.1,
                "max_output_tokens": SUMMARY_MAX_OUTPUT_TOKENS,
                "think": False,
            }
        )
    else:
        compaction_model = summary_model.model_copy(
            update={
                "tools": [],
                "temperature": 0.1,
                "max_output_tokens": SUMMARY_MAX_OUTPUT_TOKENS,
                "think": False,
            }
        )
    summary_response: Any | None = None
    summary_error: Exception | None = None
    try:
        summary_request, summary_input_tokens = _fit_summary_input(compaction_model, active)
        print(
            "[agent-context] compaction_start "
            f"prompt_tokens={prompt_tokens} threshold={threshold} "
            f"summary_input_tokens={summary_input_tokens} model=local_qwen"
        )
        summary_response = compaction_model.invoke(summary_request)
        try:
            summary_payload = summary_response.model_dump(mode="json")
        except Exception:
            summary_payload = {
                "content": summary_response.content,
                "response_metadata": summary_response.response_metadata,
                "usage_metadata": getattr(summary_response, "usage_metadata", None),
                "additional_kwargs": summary_response.additional_kwargs,
                "id": summary_response.id,
            }
        print(
            "[llm-compaction-output] "
            f"response={json.dumps(summary_payload, ensure_ascii=True, default=str)}",
            flush=True,
        )
    except Exception as exc:
        summary_error = exc
        print(
            "[agent-context] compaction_model_failed fallback=true "
            f"reason={type(exc).__name__}: {exc}",
            flush=True,
        )
    raw_summary = str(summary_response.content or "").strip() if summary_response else ""
    summary_source = "model"
    try:
        if summary_error is not None:
            raise ValueError(f"Local Qwen summary failed: {summary_error}")
        if _summary_was_truncated(summary_response):
            raise ValueError("The context-compaction response was truncated.")
        if not raw_summary:
            raise ValueError("The model returned an empty context-compaction summary.")
        new_summary = _normalize_intention_summary(raw_summary)
    except (json.JSONDecodeError, ValueError) as exc:
        if context_summary:
            summary_source = "previous_valid"
            new_summary = context_summary
        else:
            summary_source = "deterministic_minimal"
            new_summary = json.dumps(
                {
                    "user_goal": _latest_external_user_message(messages)[:240],
                    "requirements": [],
                    "design_decisions": [],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        print(
            f"[agent-context] invalid_summary discarded=true source={summary_source} "
            f"reason={type(exc).__name__}: {exc}",
            flush=True,
        )

    summary_through = len(messages)
    replacement = [_checkpoint_message(new_summary, messages)]
    replacement_tokens = _count_normal_prompt(model, system_prompt, replacement)
    if replacement_tokens > hard_threshold:
        raise RuntimeError(
            "The compacted checkpoint still exceeds the reserved input budget: "
            f"{replacement_tokens} > {hard_threshold} tokens."
        )

    summary_tokens = (
        summary_response.response_metadata.get("eval_count", "unknown")
        if summary_response is not None
        else "unavailable"
    )
    print(
        "[agent-context] compaction_complete "
        f"source={summary_source} "
        f"summary_tokens={summary_tokens} "
        f"replacement_tokens={replacement_tokens}"
    )
    return ContextProjection(
        messages=replacement,
        compacted=True,
        compacted_this_call=True,
        source_message_count=len(messages),
        projected_message_count=len(replacement),
        projected_tokens=replacement_tokens,
        compaction_threshold_tokens=threshold,
        context_summary=new_summary,
        context_summary_through=summary_through,
        context_compaction_count=context_compaction_count + 1,
    )
