import ast
import json

from asgiref.sync import async_to_sync, sync_to_async
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from .graph import agent
from .tools import get_repair_count, initialize_repair_count


RECURSION_LIMIT = 300


def _parse_tool_content(content):
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


def _format_terminal_run_game_message(message: ToolMessage) -> str:
    payload = _parse_tool_content(message.content)
    if payload.get("status") == "success" and payload.get("play_url"):
        return f"Game is ready. Play it here: {payload['play_url']}"

    errors = payload.get("errors") or []
    if isinstance(errors, str):
        errors = [errors]

    report = payload.get("test_report") or {}
    playability = report.get("playability") or {}
    failures = playability.get("failures") or []
    if isinstance(failures, str):
        failures = [failures]

    details = [str(item) for item in [*errors, *failures] if item]
    if not details and payload.get("message"):
        details = [str(payload["message"])]
    if not details and payload.get("raw"):
        details = [str(payload["raw"])]

    detail_text = "\n".join(f"- {detail}" for detail in details[:8])
    if not detail_text:
        detail_text = "- run_game failed, but did not return a specific error."

    return (
        "Game generation stopped after the first runtime test failure.\n"
        "run_game failed with:\n"
        f"{detail_text}"
    )


def _format_terminal_tool_message(message: ToolMessage) -> str:
    payload = _parse_tool_content(message.content)
    errors = payload.get("errors") or [payload.get("message") or "Generation cannot continue."]
    if isinstance(errors, str):
        errors = [errors]
    details = "\n".join(f"- {item}" for item in errors if item)
    setup = payload.get("setup_required")
    if setup:
        details += f"\n- Required setup: {setup}"
    return f"Game generation stopped during {payload.get('phase', 'generation')}.\n{details}"


def _final_response_from_messages(messages, fallback):
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and getattr(message, "name", "") == "run_game":
            payload = _parse_tool_content(message.content)
            if payload.get("status") == "success" and payload.get("play_url"):
                return {"messages": [AIMessage(content=_format_terminal_run_game_message(message))]}

        if isinstance(message, ToolMessage):
            content = str(message.content or "").lower()
            if any(
                marker in content
                for marker in (
                    "sandbox was not found",
                    "sandbox timeout",
                    "sandbox is no longer available",
                )
            ):
                return {
                    "messages": [
                        AIMessage(content="Game generation stopped because the E2B sandbox expired.")
                    ]
                }

    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls and message.content:
            return {"messages": [message]}

    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            payload = _parse_tool_content(message.content)
            if payload.get("terminal") is True:
                return {"messages": [AIMessage(content=_format_terminal_tool_message(message))]}

    return fallback


async def _stream_chat_async(thread_id: str, message: str):
    final = ""
    recursion_count = 0
    model_calls = 0
    tool_batches = 0
    await sync_to_async(initialize_repair_count, thread_sensitive=True)(thread_id)

    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT},
            stream_mode="updates"
        ):
            recursion_count += 1
            node_names = list(chunk) if isinstance(chunk, dict) else []
            model_calls += int("agent" in node_names)
            tool_batches += int("tools" in node_names)
            print(
                f"[agent-progress] thread={thread_id} "
                f"graph_steps={recursion_count}/{RECURSION_LIMIT} "
                f"nodes={node_names} model_calls={model_calls} tool_batches={tool_batches} "
                f"repairs={get_repair_count(thread_id)}"
            )
            final = chunk
    except GraphRecursionError:
        print(
            f"[agent-progress] thread={thread_id} recursion_limit_reached "
            f"limit={RECURSION_LIMIT} model_calls={model_calls} tool_batches={tool_batches}",
            flush=True,
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"Game generation reached the {RECURSION_LIMIT}-step recursion limit "
                        "before producing a passing game."
                    )
                )
            ]
        }

    print(
        f"[agent-progress] thread={thread_id} finished "
        f"graph_steps={recursion_count}/{RECURSION_LIMIT} "
        f"model_calls={model_calls} tool_batches={tool_batches} "
        f"repairs={get_repair_count(thread_id)}"
    )

    if hasattr(agent, "aget_state"):
        state = await agent.aget_state(config={"configurable": {"thread_id": thread_id}})
    else:
        state = agent.get_state(config={"configurable": {"thread_id": thread_id}})

    messages = state.values.get("messages", [])
    return _final_response_from_messages(messages, final)

# django is sync so bridge betweeen sync django and async agent
def stream_chat(thread_id: str, message: str):
    return async_to_sync(_stream_chat_async)(thread_id, message)

def get_final_or_full(thread_id: str, final: bool = True):
    full_history = agent.get_state(config={"configurable": {"thread_id": thread_id}})
    print(f"full history: {full_history}")
    messages = full_history.values.get("messages", {})
    print(f"messages: {messages}")
    if not messages:
        return []
    if final:
        return messages[-1].content
    else:
        return messages


def delete_chat_history(thread_id: str) -> None:
    if getattr(agent, "checkpointer", None):
        agent.checkpointer.delete_thread(thread_id)
