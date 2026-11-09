import ast
import json
import re
from typing import Any, Callable, Dict, List, Sequence

from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END, START
from langgraph.prebuilt import ToolNode
from typing_extensions import Literal, cast

# from langchain_google_genai import ChatGoogleGenerativeAI

from .configuration import Configuration
from . import prompts
from .context_compaction import MIN_RESPONSE_TOKENS, TOKEN_SAFETY_MARGIN, prepare_model_context
from .groq_model import GroqChatModel
from .local_ollama import LocalOllamaChatModel
from .state import InputState, State

from langgraph.checkpoint.memory import InMemorySaver
from .execution_state import (
    allowed_tools,
    load_execution_state,
    public_execution_state,
    update_execution_state,
)
from .generation_preparation import (
    prepare_generation_workspace,
    validate_and_refresh_workspace,
)
from .generation_control import get_job_sandbox


PREMATURE_FINAL_RE = re.compile(r"^/(home|tmp|workspace)/[^\s]+$")
UNVERIFIED_GAME_DELIVERY_RE = re.compile(
    r"(example\.com|/games/|[\"']play_url[\"']|\bplay\s+(?:it\s+)?(?:at|here)\b)",
    re.IGNORECASE,
)
GENERATION_TOOL_NAMES = {
    "create_game_workspace",
    "read_game_files",
    "plan_game_stage",
    "implement_stage_step",
    "validate_godot_project",
    "build_godot_game",
    "run_game",
}
MECHANICAL_SINGLE_TOOLS = {
    "create_game_workspace",
    "read_game_files",
    "validate_godot_project",
    "run_game",
}
REPAIR_SYSTEM_PROMPT = """You are repairing one failed Godot 4.7 candidate. The supplied Godot
diagnostic and repair packet are authoritative. Return exactly one implement_stage_step call with
only the complete corrected source for repair_packet.scope. Do not change
bootstrap or lifecycle functions, diagnose syntax from memory, research, read, validate, or discuss
the error. For a function repair, return its complete corrected body (or the complete same-named
function), not a fragment. Do not invent repair metadata such as stage_name or change_summary; the
application derives it from persisted state. The application validates and commits the candidate
automatically."""
STEP_CORRECTION_SYSTEM_PROMPT = """You are correcting one rejected source submission before it
reaches the sandbox. Application state supplies the exact active step, rejection errors, output
contract, and relevant private GameGen component contracts. Return exactly one
implement_stage_step call containing only a materially different implementation for that step.
Correct every listed error. Do not add unrelated symbols, lifecycle functions, scene syntax to a
.gd file, invented APIs, routing metadata, explanations, or Markdown. The rejected source is
deliberately absent so you cannot repeat or patch around it; regenerate the bounded unit from the
authoritative step contract."""
TOOL_JSON_RECOVERY_SYSTEM_PROMPT = """Your previous tool call was rejected by the provider before
execution because its arguments were malformed or did not match the declared schema. Return exactly
one permitted tool call with concise, schema-valid JSON. Respect all array and string limits, use
double-quoted keys and strings, no comments, no trailing commas, and no prose. Preserve the current
application phase and do not broaden the requested work."""


# Gemini response logging fallback:
# def _log_model_response(response: AIMessage) -> None:
#     payload = response.model_dump(mode="json")
#     print(
#         "[llm-output] provider=gemini model=gemini-2.5-flash-lite "
#         f"response={json.dumps(payload, ensure_ascii=True, default=str)}",
#         flush=True,
#     )


def _tool_name(message: BaseMessage) -> str:
    return str(getattr(message, "name", "") or "").strip()


def _tool_content(message: BaseMessage) -> str:
    return str(getattr(message, "content", "") or "")


def _tool_payload(message: BaseMessage) -> dict[str, Any]:
    content = getattr(message, "content", "")
    if isinstance(content, dict):
        return content
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(str(content or ""))
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_malformed_tool_call_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "failed to parse tool call arguments as json" in text
        or ("tool call" in text and "invalid json" in text)
        or (
            "parsing failed" in text
            and "model generated output" in text
            and "could not be parsed" in text
        )
        or (
            "tool call validation failed" in text
            and "did not match schema" in text
        )
    )


def _bounded_tool_result(value: Any, *, depth: int = 0) -> Any:
    """Keep tool-result logs useful without dumping full source files or binary payloads."""
    if depth >= 5:
        return "<nested value omitted>"
    if isinstance(value, dict):
        return {
            str(key): (
                f"<{len(item)} chars omitted>"
                if key in {"content", "source", "data", "blob"}
                and isinstance(item, str)
                and len(item) > 500
                else _bounded_tool_result(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_bounded_tool_result(item, depth=depth + 1) for item in value[:20]] + (
            [f"<{len(value) - 20} additional items omitted>"] if len(value) > 20 else []
        )
    if isinstance(value, str) and len(value) > 4_000:
        return value[:4_000] + f"... <{len(value) - 4_000} chars omitted>"
    return value


def _log_tool_result(message: BaseMessage) -> None:
    name = _tool_name(message) or "unknown"
    payload = _tool_payload(message)
    rendered: Any = _bounded_tool_result(payload) if payload else _tool_content(message)
    print(
        "[agent-tool-result] "
        f"name={name} status={payload.get('status', 'unknown') if payload else 'unknown'} "
        f"payload={json.dumps(rendered, ensure_ascii=True, separators=(',', ':'), default=str)}",
        flush=True,
    )


def _tool_result_error(message: BaseMessage) -> str:
    """Return bounded rejection text, including ToolNode argument-validation failures."""
    payload = _tool_payload(message)
    status = str(payload.get("status") or "").lower()
    if status in {"error", "rejected", "failed"}:
        errors = payload.get("errors") or payload.get("message") or _tool_content(message)
        return " ".join(str(errors).split())[:1_500]
    content = _tool_content(message).strip()
    if not payload and content.lower().startswith("error:"):
        return " ".join(content.split())[:1_500]
    return ""


def _automatic_transition_for_tool(message: BaseMessage) -> str | None:
    """Select application-owned work that must happen before another model call."""
    payload = _tool_payload(message)
    if payload.get("status") != "success":
        return None
    tool_name = _tool_name(message)
    if tool_name == "create_game_workspace":
        return "prepare_workspace"
    if tool_name == "plan_game_stage":
        return "prepare_workspace"
    if tool_name in {"implement_stage_step", "edit_game_files", "write_files"}:
        return "validate_stage"
    if tool_name in {"research_godot_docs", "read_game_files"}:
        return "prepare_workspace"
    return None


def _requested_tool_names(message: BaseMessage) -> set[str]:
    names = set()
    for tool_call in getattr(message, "tool_calls", []) or []:
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
        else:
            name = getattr(tool_call, "name", None)

        if name:
            names.add(str(name))

    return names


def _tool_call_signature(tool_call: dict[str, Any]) -> str:
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    normalized_args = {
        key: value
        for key, value in args.items()
        if key not in {"stage_name", "change_summary"}
    }
    return json.dumps(
        {
            "name": str(tool_call.get("name") or ""),
            "args": normalized_args,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _execution_progress_token(execution: dict[str, Any]) -> str:
    """Identify meaningful application progress independently of model/tool-call count."""
    fields = (
        "phase",
        "workspace_revision",
        "candidate_revision",
        "candidate_failure_count",
        "read_revision",
        "validated_revision",
        "build_revision",
        "gameplay_edit_count",
        "stage_plan_revision",
        "active_stage_id",
        "active_step_id",
        "run_succeeded",
        "terminal",
    )
    return json.dumps(
        {field: execution.get(field) for field in fields},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _has_generation_started(messages: Sequence[BaseMessage]) -> bool:
    return any(
        _tool_name(message) in GENERATION_TOOL_NAMES
        or bool(_requested_tool_names(message) & GENERATION_TOOL_NAMES)
        for message in messages
    )


def _has_successful_game_run(messages: Sequence[BaseMessage]) -> bool:
    for message in messages:
        if isinstance(message, AIMessage):
            continue

        tool_name = _tool_name(message)
        if not isinstance(message, ToolMessage) and not tool_name:
            continue

        if tool_name != "run_game":
            continue

        content = _tool_content(message).lower()
        has_success = '"status": "success"' in content or "'status': 'success'" in content
        has_play_url = '"play_url"' in content or "'play_url'" in content
        if has_success and has_play_url:
            return True

    return False


def _has_expired_sandbox_result(messages: Sequence[BaseMessage]) -> bool:
    """Stop only when E2B reports that the generation sandbox has expired."""
    expiration_markers = (
        "sandbox was not found",
        "sandbox timeout",
        "sandbox is no longer available",
    )
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = _tool_content(message).lower()
        if any(marker in content for marker in expiration_markers):
            return True
    return False


def _is_premature_final(state: State, message: AIMessage) -> bool:
    if getattr(state, "is_last_step", False):
        return False

    if message.tool_calls:
        return False

    content = str(message.content or "").strip()
    if PREMATURE_FINAL_RE.match(content):
        return True

    if UNVERIFIED_GAME_DELIVERY_RE.search(content) and not _has_successful_game_run(state.messages):
        return True

    if _has_generation_started(state.messages) and not _has_successful_game_run(state.messages):
        return True

    return False


def create_react_agent(agent_name: str, tool_list: List[Callable[..., Any]], tool_node: ToolNode) -> StateGraph:
    """Create a React agent.

    Args:
        agent_name (str): The name of the agent.

    Returns:
        StateGraph: The compiled graph.
    """

    def call_model(state: State, config: RunnableConfig) -> Dict[str, Any]:
        """Call the LLM powering our "agent"."""
        configuration = Configuration.from_runnable_config(config)

        thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
        execution = load_execution_state(thread_id)
        if execution.get("workspace_created"):
            get_job_sandbox(thread_id)
            execution = load_execution_state(thread_id)
        permitted_names = allowed_tools(execution)
        tools_by_name = {tool.__name__: tool for tool in tool_list}
        permitted_tools = [tools_by_name[name] for name in permitted_names if name in tools_by_name]

        model = GroqChatModel.create(thread_id=thread_id, temperature=0.2)

        # Gemini fallback:
        # model = ChatGoogleGenerativeAI(
        #     model="gemini-2.5-flash-lite",
        #     temperature=0.2,
        #     max_retries=2,
        #     google_api_key=os.getenv("GOOGLE_API_KEY"),
        # )

        model = model.bind_tools(permitted_tools)
        summary_model = LocalOllamaChatModel(
            temperature=0.1,
            max_output_tokens=512,
            think=False,
            thread_id=thread_id,
            tools=[],
        )
        authoritative = public_execution_state(execution)
        repair_mode = execution.get("phase") == "repair" and bool(execution.get("repair_packet"))
        correction_mode = execution.get("phase") == "step_correction" and bool(
            execution.get("step_correction")
        )
        focused_mode = repair_mode or correction_mode
        phase_system_prompt = prompts.system_prompt_for_execution(
            {**execution, "allowed_tools": list(permitted_names)}
        )
        prompt_base = (
            REPAIR_SYSTEM_PROMPT
            if repair_mode
            else STEP_CORRECTION_SYSTEM_PROMPT
            if correction_mode
            else (
                phase_system_prompt
                if configuration.system_prompt == prompts.SYSTEM_PROMPT
                else configuration.system_prompt
            )
        )
        effective_system_prompt = (
            f"{prompt_base}\n\n## Authoritative execution state\n"
            f"{json.dumps(authoritative, ensure_ascii=True, separators=(',', ':'))}"
        )
        source_messages = (
            [
                HumanMessage(
                    content=(
                        "Apply the single scoped repair now."
                        if repair_mode
                        else "Regenerate only the rejected active step now."
                    )
                )
            ]
            if focused_mode
            else state.messages
        )

        projection = prepare_model_context(
            messages=source_messages,
            system_prompt=effective_system_prompt,
            model=model,
            summary_model=summary_model,
            context_summary="" if focused_mode else state.context_summary,
            context_summary_through=0 if focused_mode else state.context_summary_through,
            context_compaction_count=state.context_compaction_count,
        )
        print(
            "[agent-context] "
            f"compacted={str(projection.compacted).lower()} "
            f"compacted_now={str(projection.compacted_this_call).lower()} "
            f"messages={projection.source_message_count}->{projection.projected_message_count} "
            f"tokens={projection.projected_tokens}/{projection.compaction_threshold_tokens} "
            f"checkpoints={projection.context_compaction_count}"
        )
        def invoke(projected_context, *, invocation_system_prompt=effective_system_prompt):
            available_output = (
                model.context_window
                - projected_context.projected_tokens
                - TOKEN_SAFETY_MARGIN
            )
            output_budget = min(
                model.max_output_tokens,
                max(MIN_RESPONSE_TOKENS, available_output),
            )
            adaptive_model = model.model_copy(update={"max_output_tokens": output_budget})
            print(
                "[agent-context] adaptive_output "
                f"prompt_tokens={projected_context.projected_tokens} "
                f"output_budget={output_budget} context_window={model.context_window}",
                flush=True,
            )
            response = cast(
                AIMessage,
                adaptive_model.invoke(
                    [
                        {"role": "system", "content": invocation_system_prompt},
                        *projected_context.messages,
                    ],
                    config,
                ),
            )
            return response

        try:
            response = invoke(projection)
        except Exception as exc:
            if not _is_malformed_tool_call_error(exc):
                raise
            print(
                "[agent-progress] malformed_tool_json retry=1/1 "
                f"reason={type(exc).__name__}: {exc}",
                flush=True,
            )
            recovery_state = {
                key: authoritative.get(key)
                for key in (
                    "phase",
                    "game_plan",
                    "next_stage",
                    "active_step_context",
                    "stage_retry_directive",
                    "source_index",
                    "component_contracts",
                    "established_game_contracts",
                    "research_context",
                    "allowed_tools",
                    "contract",
                )
                if key in authoritative
            }
            recovery_system_prompt = (
                f"{TOOL_JSON_RECOVERY_SYSTEM_PROMPT}\n\n"
                "## Authoritative recovery state\n"
                f"{json.dumps(recovery_state, ensure_ascii=True, separators=(',', ':'))}"
            )
            recovery_projection = prepare_model_context(
                messages=[
                    HumanMessage(
                        content=(
                            "Retry the current action now. Keep arguments as short as the tool "
                            "contract permits. Provider rejection: "
                            + " ".join(str(exc).split())[:1_000]
                        )
                    )
                ],
                system_prompt=recovery_system_prompt,
                model=model,
                summary_model=summary_model,
                context_compaction_count=projection.context_compaction_count,
            )
            response = invoke(
                recovery_projection,
                invocation_system_prompt=recovery_system_prompt,
            )
        if response.response_metadata.get("output_limit_reached"):
            print("[agent-context] output_limit_reached compacting_before_retry", flush=True)
            projection = prepare_model_context(
                messages=source_messages,
                system_prompt=effective_system_prompt,
                model=model,
                summary_model=summary_model,
                context_summary="" if focused_mode else state.context_summary,
                context_summary_through=0 if focused_mode else state.context_summary_through,
                context_compaction_count=state.context_compaction_count,
                force=True,
            )
            response = invoke(projection)
            if response.response_metadata.get("output_limit_reached"):
                print(
                    "[agent-progress] output_limit_reached_twice continuing_generation",
                    flush=True,
                )
                response = AIMessage(
                    content="",
                    response_metadata={
                        **response.response_metadata,
                        "rejected_reason": "Output was incomplete after compaction.",
                    },
                )

        if len(response.tool_calls) > 1:
            print(
                "[agent-progress] rejected_multiple_tool_calls "
                f"count={len(response.tool_calls)}",
                flush=True,
            )
            response = AIMessage(
                content="",
                response_metadata={
                    **response.response_metadata,
                    "rejected_reason": "Only one tool call is allowed per model turn.",
                },
            )

        if len(response.tool_calls) == 1:
            signature = _tool_call_signature(response.tool_calls[0])
            progress_token = _execution_progress_token(execution)
            repeated_without_progress = (
                signature == str(execution.get("last_tool_signature") or "")
                and progress_token == str(execution.get("last_tool_progress_token") or "")
            )
            if repeated_without_progress:
                repeated_count = int(execution.get("repeated_tool_call_count", 0)) + 1
                print(
                    "[agent-progress] rejected_repeated_tool_call "
                    f"count={repeated_count} signature={signature}",
                    flush=True,
                )
                update_execution_state(
                    thread_id,
                    lambda current: current.update(
                        {"repeated_tool_call_count": repeated_count}
                    ),
                )
                response = AIMessage(
                    content="",
                    response_metadata={
                        **response.response_metadata,
                        "rejected_reason": (
                            "Identical tool call repeated without application progress."
                        ),
                    },
                )
            else:
                update_execution_state(
                    thread_id,
                    lambda current: current.update(
                        {
                            "last_tool_signature": signature,
                            "last_tool_progress_token": progress_token,
                            "repeated_tool_call_count": 0,
                        }
                    ),
                )

        generation_started = bool(execution.get("workspace_created"))
        rejected_response = bool(response.response_metadata.get("rejected_reason"))
        empty_response = not str(response.content or "").strip() and not response.tool_calls
        no_progress = (empty_response or generation_started or rejected_response) and not response.tool_calls
        no_progress_count = state.no_progress_count
        if no_progress:
            no_progress_count += 1
            update_execution_state(
                thread_id,
                lambda current: current.update({"no_progress_count": no_progress_count}),
            )
            print(
                f"[agent-progress] no_progress count={no_progress_count} continuing_generation",
                flush=True,
            )
        elif response.tool_calls:
            no_progress_count = 0
            update_execution_state(
                thread_id,
                lambda current: current.update({"no_progress_count": 0}),
            )

        # Handle the case when it's the last step and the model still wants to use a tool
        if state.is_last_step and response.tool_calls:
            return {
                "messages": [
                    AIMessage(
                        id=response.id,
                        content="Sorry, I could not find an answer to your question in the specified number of steps.",
                    )
                ],
                "context_summary": state.context_summary if focused_mode else projection.context_summary,
                "context_summary_through": (
                    state.context_summary_through
                    if focused_mode
                    else projection.context_summary_through
                ),
                "context_compaction_count": projection.context_compaction_count,
            }

        # Return the model's response as a list to be added to existing messages
        return {
            "messages": [response],
            "context_summary": state.context_summary if focused_mode else projection.context_summary,
            "context_summary_through": (
                state.context_summary_through if focused_mode else projection.context_summary_through
            ),
            "context_compaction_count": projection.context_compaction_count,
            "no_progress_count": no_progress_count,
        }

    def route_model_output(state: State) -> Literal["__end__", "tools", "continue_generation"]:
        """Determine the next node based on the model's output.

        This function checks if the model's last message contains tool calls.

        Args:
            state (State): The current state of the conversation.

        Returns:
            str: The name of the next node to call ("__end__" or "tools").
        """
        last_message = state.messages[-1]
        if not isinstance(last_message, AIMessage):
            raise ValueError(
                f"Expected AIMessage in output edges, but got {type(last_message).__name__}"
            )
        # A non-tool response is recoverable until the graph recursion limit.
        if state.no_progress_count and not last_message.tool_calls:
            return "continue_generation"
        if _is_premature_final(state, last_message):
            return "continue_generation"

        if not last_message.tool_calls:
            return "continue_generation"
        # Otherwise we execute the requested actions
        print(
            "[agent-progress] dispatching_tool_calls="
            f"{last_message.tool_calls}",
            flush=True,
        )
        return "tools"

    def route_after_tools(
        state: State,
        config: RunnableConfig,
    ) -> Literal["__end__", "agent", "prepare_workspace", "validate_stage"]:
        last_message = state.messages[-1]
        _log_tool_result(last_message)
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
        payload = _tool_payload(last_message)
        tool_error = _tool_result_error(last_message)
        retryable_infrastructure = (
            str(payload.get("phase") or "") == "infrastructure"
            and bool(payload.get("retryable"))
        )
        if retryable_infrastructure:
            update_execution_state(
                thread_id,
                lambda current: current.update(
                    {
                        "last_tool_error": tool_error,
                        "last_tool_signature": "",
                        "last_tool_progress_token": "",
                        "repeated_tool_call_count": 0,
                    }
                ),
            )
        else:
            update_execution_state(
                thread_id,
                lambda current: current.update({"last_tool_error": tool_error}),
            )
        if _has_successful_game_run(state.messages) or _has_expired_sandbox_result(state.messages):
            return END
        if transition := _automatic_transition_for_tool(last_message):
            return transition
        return "agent"

    # Define a new graph
    workflow = StateGraph(State, input=InputState, config_schema=Configuration)

    # Define the two nodes we will cycle between
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)

    async def prepare_workspace(state: State, config: RunnableConfig):
        result = await prepare_generation_workspace(config)
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "AUTOMATIC PREPARATION RESULT: "
                        f"{json.dumps(result, ensure_ascii=True, separators=(',', ':'))}"
                    )
                )
            ]
        }

    def validate_stage(state: State, config: RunnableConfig):
        result = validate_and_refresh_workspace(config)
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "AUTOMATIC VALIDATION RESULT: "
                        f"{json.dumps(result, ensure_ascii=True, separators=(',', ':'))}"
                    )
                )
            ]
        }

    workflow.add_node("prepare_workspace", prepare_workspace)
    workflow.add_node("validate_stage", validate_stage)

    def continue_generation(state: State, config: RunnableConfig):
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
        execution = public_execution_state(load_execution_state(thread_id))
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "The previous model turn made no executable progress. Use the "
                        "authoritative state below and return exactly one currently allowed "
                        "tool call. Do not describe work as completed.\n"
                        f"{json.dumps(execution, ensure_ascii=True, separators=(',', ':'))}"
                    )
                )
            ]
        }

    workflow.add_node("continue_generation", continue_generation)

    def route_start(
        state: State,
        config: RunnableConfig,
    ) -> Literal["agent", "prepare_workspace", "validate_stage"]:
        """Resume application-owned transitions without spending a model call."""
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
        execution = load_execution_state(thread_id)
        if (
            execution.get("candidate_revision") is not None
            and execution.get("phase") != "repair"
        ):
            return "validate_stage"
        if execution.get("workspace_created") and not execution.get("preparation_complete"):
            return "prepare_workspace"
        return "agent"

    workflow.add_conditional_edges(START, route_start)

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        "agent",
        # Next, we pass in the function that will determine which node is called next.
        route_model_output,
    )

    workflow.add_conditional_edges("tools", route_after_tools)

    def route_after_preparation(
        state: State,
        config: RunnableConfig,
    ) -> Literal["agent", "__end__"]:
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "default")
        execution = load_execution_state(thread_id)
        return "agent" if execution.get("preparation_complete") else END

    workflow.add_conditional_edges("prepare_workspace", route_after_preparation)
    def route_after_validation(state: State, config: RunnableConfig):
        return "agent"

    workflow.add_conditional_edges("validate_stage", route_after_validation)
    workflow.add_edge("continue_generation", "agent")

    checkpointer = InMemorySaver()

    # Now we can compile and visualize our graph
    graph = workflow.compile(checkpointer=checkpointer)
    graph.name = agent_name

    return graph
