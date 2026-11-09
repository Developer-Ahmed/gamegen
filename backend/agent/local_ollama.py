from __future__ import annotations

import json
import os
import uuid
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field
from tokenizers import Tokenizer

from .generation_control import raise_if_stopped


DEFAULT_TOKENIZER_PATH = Path(__file__).parent / "tokenizers" / "qwen3_8b" / "tokenizer.json"
DEFAULT_TOKENIZER_SHA256 = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})
DEFAULT_CONTEXT_WINDOW = 12_288


def _context_window_from_environment() -> int:
    raw_value = os.getenv("QWEN_CONTEXT_WINDOW", str(DEFAULT_CONTEXT_WINDOW)).strip()
    try:
        context_window = int(raw_value)
    except ValueError as exc:
        raise ValueError("QWEN_CONTEXT_WINDOW must be an integer.") from exc
    if context_window < 8_192:
        raise ValueError("QWEN_CONTEXT_WINDOW must be at least 8192 tokens.")
    return context_window


def _thinking_logging_enabled() -> bool:
    value = os.getenv("QWEN_LOG_THINKING", "1").strip().lower()
    return value not in _FALSE_ENV_VALUES


@lru_cache(maxsize=4)
def _load_tokenizer(path: str, expected_sha256: str) -> Tokenizer:
    tokenizer_path = Path(path)
    payload = tokenizer_path.read_bytes()
    actual_sha256 = sha256(payload).hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"Qwen tokenizer checksum mismatch for {tokenizer_path}: "
            f"expected {expected_sha256}, got {actual_sha256}."
        )
    return Tokenizer.from_file(str(tokenizer_path))


class LocalOllamaChatModel(BaseChatModel):
    """Small LangChain chat wrapper for Ollama's native streaming /api/chat endpoint."""

    model: str = "qwen3:8b"
    endpoint_url: str = "http://localhost:11434/api/chat"
    temperature: float = 0.2
    request_timeout: int = 600
    context_window: int = Field(default_factory=_context_window_from_environment)
    max_output_tokens: int = 2048
    think: bool | None = None
    thread_id: str = ""
    tokenizer_path: str = Field(
        default_factory=lambda: os.getenv("QWEN_TOKENIZER_PATH", str(DEFAULT_TOKENIZER_PATH))
    )
    tokenizer_sha256: str = Field(
        default_factory=lambda: os.getenv("QWEN_TOKENIZER_SHA256", DEFAULT_TOKENIZER_SHA256)
    )
    tools: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "local_ollama_chat"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "LocalOllamaChatModel":
        converted_tools = [convert_to_openai_tool(tool) for tool in tools]
        return self.model_copy(update={"tools": converted_tools})

    def count_prompt_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """Count the exact tokens in the prompt rendered by the local Ollama model."""
        payload = self._chat_payload(messages, stream=False)
        payload["_debug_render_only"] = True
        with requests.post(
            self.endpoint_url,
            json=payload,
            timeout=(10, 30),
        ) as response:
            response.raise_for_status()
            rendered = ((response.json().get("_debug_info") or {}).get("rendered_template"))
        if not isinstance(rendered, str) or not rendered:
            raise RuntimeError(
                "Ollama did not return a rendered prompt for exact token counting. "
                "The installed Ollama version must support _debug_render_only."
            )
        tokenizer = _load_tokenizer(self.tokenizer_path, self.tokenizer_sha256)
        return len(tokenizer.encode(rendered, add_special_tokens=False).ids)

    def _chat_payload(
        self,
        messages: Sequence[BaseMessage],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_to_ollama(message) for message in messages],
            "stream": stream,
            "truncate": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.context_window,
                "num_predict": self.max_output_tokens,
            },
        }
        if self.tools:
            payload["tools"] = self.tools
        if self.think is not None:
            payload["think"] = self.think
        return payload

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = self._chat_payload(messages, stream=True)

        if stop:
            payload["options"]["stop"] = stop

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        raw_tool_calls: list[dict[str, Any]] = []
        chunk_count = 0
        response_metadata: dict[str, Any] = {}
        print(f"[llm-stream] provider=ollama model={self.model} start")

        with requests.post(
            self.endpoint_url,
            json=payload,
            stream=True,
            timeout=(10, self.request_timeout),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if self.thread_id:
                    raise_if_stopped(self.thread_id)
                if not line:
                    continue

                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))

                message = event.get("message") or {}
                token = message.get("content") or ""
                if token:
                    content_parts.append(token)
                    if run_manager:
                        run_manager.on_llm_new_token(token)

                thinking = message.get("thinking") or ""
                if thinking:
                    thinking_parts.append(thinking)

                tool_calls = message.get("tool_calls") or []
                if tool_calls:
                    raw_tool_calls.extend(tool_calls)

                for key in ("prompt_eval_count", "eval_count", "done_reason"):
                    if key in event:
                        response_metadata[key] = event[key]

                if token or thinking or tool_calls or event.get("done"):
                    chunk_count += 1
                if chunk_count == 1 or chunk_count % 40 == 0:
                    print(f"[llm-stream] provider=ollama chunks={chunk_count}", flush=True)

                if event.get("done"):
                    break

        print(f"[llm-stream] provider=ollama model={self.model} done chunks={chunk_count}")
        content = "".join(content_parts)
        thinking_text = "".join(thinking_parts)
        normalized_tool_calls = self._normalize_tool_calls(raw_tool_calls)
        response_metadata["thinking_character_count"] = len(thinking_text)
        response_metadata["content_character_count"] = len(content)
        response_metadata["tool_call_count"] = len(normalized_tool_calls)
        response_metadata["output_limit_reached"] = response_metadata.get("done_reason") == "length"
        print(
            "[llm-usage] "
            f"prompt_tokens={response_metadata.get('prompt_eval_count', 'unknown')} "
            f"generated_tokens={response_metadata.get('eval_count', 'unknown')} "
            f"done_reason={response_metadata.get('done_reason', 'unknown')} "
            f"thinking_chars={len(thinking_text)} content_chars={len(content)} "
            f"tool_calls={len(normalized_tool_calls)}",
            flush=True,
        )
        print(
            "[llm-output] "
            f"provider=ollama model={self.model} visible_content="
            f"{json.dumps(content, ensure_ascii=True)}",
            flush=True,
        )
        print(
            "[llm-output] "
            f"provider=ollama model={self.model} tool_calls="
            f"{json.dumps(normalized_tool_calls, ensure_ascii=True, sort_keys=True)}",
            flush=True,
        )
        if _thinking_logging_enabled():
            print(
                "[llm-thinking] "
                f"provider=ollama model={self.model} thread={self.thread_id or 'unknown'} "
                f"content={json.dumps(thinking_text, ensure_ascii=True)}",
                flush=True,
            )
        message = AIMessage(
            content=content,
            tool_calls=normalized_tool_calls,
            response_metadata=response_metadata,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _message_to_ollama(self, message: BaseMessage) -> dict[str, Any]:
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        elif isinstance(message, ToolMessage):
            role = "tool"
        else:
            role = str(getattr(message, "type", "user"))

        result: dict[str, Any] = {"role": role, "content": self._string_content(message.content)}

        if isinstance(message, AIMessage) and message.tool_calls:
            result["tool_calls"] = [
                {"function": {"name": call["name"], "arguments": call.get("args", {})}}
                for call in message.tool_calls
            ]

        if isinstance(message, ToolMessage):
            result["tool_name"] = getattr(message, "name", "") or getattr(message, "tool_call_id", "")

        return result

    def _normalize_tool_calls(self, raw_tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for raw_call in raw_tool_calls:
            function = raw_call.get("function") or {}
            name = function.get("name") or raw_call.get("name")
            if not name:
                continue

            args = function.get("arguments", raw_call.get("arguments", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"__raw_arguments": args}

            normalized.append(
                {
                    "name": name,
                    "args": args or {},
                    "id": raw_call.get("id") or f"ollama_{uuid.uuid4().hex}",
                    "type": "tool_call",
                }
            )
        return normalized

    def _string_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, separators=(",", ":"))
