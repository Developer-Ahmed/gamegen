from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from openai_harmony import (
    Author,
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    Message,
    ReasoningEffort,
    RenderConversationConfig,
    Role,
    SystemContent,
    ToolDescription,
    load_harmony_encoding,
)
from pydantic import Field

from .generation_control import raise_if_stopped


DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_TPM_LIMIT = 8_000
DEFAULT_TPM_BUDGET = 7_600
DEFAULT_CONTEXT_WINDOW = 7_600
DEFAULT_MAX_OUTPUT_TOKENS = 1_200


@dataclass
class _Reservation:
    created_at: float
    tokens: int


class PromptTokenCalibrator:
    """Apply a process-wide safety correction learned from provider usage."""

    def __init__(self, *, initial_ratio: float = 1.03, fixed_margin: int = 16):
        self._ratio = max(1.0, initial_ratio)
        self._fixed_margin = max(0, fixed_margin)
        self._lock = threading.Lock()

    def estimate(self, rendered_tokens: int) -> int:
        with self._lock:
            return math.ceil(rendered_tokens * self._ratio) + self._fixed_margin

    def observe(self, rendered_tokens: int, actual_tokens: int) -> None:
        if rendered_tokens <= 0 or actual_tokens <= 0:
            return
        observed_ratio = actual_tokens / rendered_tokens
        with self._lock:
            # Keep a 2% cushion and never lower the process high-water mark. Under-counting can
            # violate the provider quota; modest over-counting only triggers earlier compaction.
            self._ratio = max(self._ratio, observed_ratio * 1.02)

    @property
    def ratio(self) -> float:
        with self._lock:
            return self._ratio


class RollingTokenRateLimiter:
    """Process-wide rolling token limiter with post-request usage reconciliation."""

    def __init__(self, tokens_per_minute: int, budget: int, window_seconds: float = 60.0):
        if tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be positive.")
        if budget <= 0 or budget > tokens_per_minute:
            raise ValueError("budget must be positive and no greater than tokens_per_minute.")
        self.tokens_per_minute = tokens_per_minute
        self.budget = budget
        self.window_seconds = window_seconds
        self._condition = threading.Condition()
        self._reservations: deque[_Reservation] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._reservations and self._reservations[0].created_at <= cutoff:
            self._reservations.popleft()

    def acquire(self, requested_tokens: int, *, thread_id: str = "") -> _Reservation:
        if requested_tokens <= 0:
            raise ValueError("requested_tokens must be positive.")
        if requested_tokens > self.budget:
            raise RuntimeError(
                "The model request cannot fit within the configured Groq token-per-minute "
                f"budget: requested {requested_tokens}, budget {self.budget}. Compact the "
                "prompt or reduce the output allowance."
            )

        with self._condition:
            while True:
                if thread_id:
                    raise_if_stopped(thread_id)
                now = time.monotonic()
                self._prune(now)
                used = sum(item.tokens for item in self._reservations)
                if used + requested_tokens <= self.budget:
                    reservation = _Reservation(now, requested_tokens)
                    self._reservations.append(reservation)
                    print(
                        "[llm-rate-limit] "
                        f"reserved={requested_tokens} used={used + requested_tokens}/"
                        f"{self.budget} window_seconds={int(self.window_seconds)}",
                        flush=True,
                    )
                    return reservation

                oldest = self._reservations[0]
                wait_seconds = max(0.1, oldest.created_at + self.window_seconds - now)
                print(
                    "[llm-rate-limit] waiting "
                    f"seconds={wait_seconds:.1f} requested={requested_tokens} "
                    f"used={used}/{self.budget}",
                    flush=True,
                )
                self._condition.wait(timeout=min(wait_seconds, 1.0))

    def reconcile(self, reservation: _Reservation, actual_tokens: int | None) -> None:
        with self._condition:
            if actual_tokens is not None and actual_tokens > 0:
                reservation.tokens = actual_tokens
            self._condition.notify_all()


_SHARED_LIMITER = RollingTokenRateLimiter(
    tokens_per_minute=int(os.getenv("GROQ_TOKENS_PER_MINUTE", DEFAULT_TPM_LIMIT)),
    budget=int(os.getenv("GROQ_TOKEN_BUDGET", DEFAULT_TPM_BUDGET)),
)
_SHARED_PROMPT_CALIBRATOR = PromptTokenCalibrator()


def _string_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=True, separators=(",", ":"), default=str)


def _harmony_tools(tools: Sequence[dict[str, Any]]) -> list[ToolDescription]:
    descriptions: list[ToolDescription] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        descriptions.append(
            ToolDescription.new(
                str(function["name"]),
                str(function.get("description") or ""),
                parameters=(
                    function.get("parameters")
                    if isinstance(function.get("parameters"), dict)
                    else {"type": "object", "properties": {}}
                ),
            )
        )
    return descriptions


def _harmony_conversation(
    messages: Sequence[BaseMessage],
    tools: Sequence[dict[str, Any]],
    *,
    reasoning_effort: str,
) -> Conversation:
    effort = {
        "high": ReasoningEffort.HIGH,
        "medium": ReasoningEffort.MEDIUM,
        "low": ReasoningEffort.LOW,
    }.get(reasoning_effort.lower(), ReasoningEffort.LOW)
    rendered: list[Message] = [
        Message.from_role_and_content(
            Role.SYSTEM,
            SystemContent.new()
            .with_reasoning_effort(effort)
            .with_conversation_start_date(date.today().isoformat()),
        )
    ]

    instructions = "\n\n".join(
        _string_content(message.content)
        for message in messages
        if isinstance(message, SystemMessage)
    )
    developer = DeveloperContent.new().with_instructions(instructions)
    function_tools = _harmony_tools(tools)
    if function_tools:
        developer.with_function_tools(function_tools)
    rendered.append(Message.from_role_and_content(Role.DEVELOPER, developer))

    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, HumanMessage):
            rendered.append(
                Message.from_role_and_content(Role.USER, _string_content(message.content))
            )
            continue
        if isinstance(message, ToolMessage):
            tool_name = str(message.name or "unknown")
            rendered.append(
                Message.from_author_and_content(
                    Author.new(Role.TOOL, f"functions.{tool_name}"),
                    _string_content(message.content),
                )
                .with_channel("commentary")
                .with_recipient("assistant")
            )
            continue
        if isinstance(message, AIMessage):
            content = _string_content(message.content)
            if content:
                rendered.append(
                    Message.from_role_and_content(Role.ASSISTANT, content).with_channel("final")
                )
            for call in message.tool_calls or []:
                rendered.append(
                    Message.from_role_and_content(
                        Role.ASSISTANT,
                        json.dumps(
                            call.get("args") if isinstance(call.get("args"), dict) else {},
                            ensure_ascii=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                    )
                    .with_channel("commentary")
                    .with_recipient(f"functions.{call.get('name') or 'unknown'}")
                    .with_content_type("<|constrain|> json")
                )
            continue
        rendered.append(
            Message.from_role_and_content(Role.USER, _string_content(message.content))
        )
    return Conversation.from_messages(rendered)


def _api_key() -> str:
    value = os.getenv("GROQ_API_KEY", "").strip()
    if not value:
        raise RuntimeError(
            "GROQ_API_KEY is required to use openai/gpt-oss-120b. Add it to backend/.env "
            "and restart the backend."
        )
    return value


class GroqChatModel(ChatOpenAI):
    """Groq-hosted GPT-OSS model with tool binding, logging, and TPM enforcement."""

    context_window: int = Field(
        default_factory=lambda: int(os.getenv("GROQ_CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW))
    )
    max_output_tokens: int = Field(
        default_factory=lambda: int(
            os.getenv("GROQ_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)
        )
    )
    thread_id: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)
    token_limiter: Any = Field(default_factory=lambda: _SHARED_LIMITER, exclude=True)
    prompt_calibrator: Any = Field(
        default_factory=lambda: _SHARED_PROMPT_CALIBRATOR,
        exclude=True,
    )

    @classmethod
    def create(cls, *, thread_id: str, temperature: float = 0.2) -> "GroqChatModel":
        model = cls(
            model=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            api_key=_api_key(),
            base_url=os.getenv("GROQ_BASE_URL", DEFAULT_BASE_URL),
            temperature=temperature,
            timeout=float(os.getenv("GROQ_REQUEST_TIMEOUT", "300")),
            # Hidden SDK retries would bypass one-request-per-reservation accounting.
            max_retries=0,
            streaming=True,
            stream_usage=True,
            tiktoken_model_name="gpt-4o",
            reasoning_effort="low",
            thread_id=thread_id,
        )
        if model.context_window > model.token_limiter.budget:
            raise ValueError(
                "GROQ_CONTEXT_WINDOW cannot exceed GROQ_TOKEN_BUDGET; a single request "
                "must fit inside the minute budget."
            )
        if model.max_output_tokens < 512 or model.max_output_tokens >= model.context_window:
            raise ValueError(
                "GROQ_MAX_OUTPUT_TOKENS must be at least 512 and smaller than "
                "GROQ_CONTEXT_WINDOW."
            )
        return model

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "GroqChatModel":
        converted = [convert_to_openai_tool(tool) for tool in tools]
        return self.model_copy(update={"tools": converted})

    def count_prompt_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """Count the official GPT-OSS Harmony rendering, then apply learned safety calibration."""
        rendered_tokens = self._count_harmony_tokens(messages)
        return self.prompt_calibrator.estimate(rendered_tokens)

    def _count_harmony_tokens(self, messages: Sequence[BaseMessage]) -> int:
        conversation = _harmony_conversation(
            messages,
            self.tools,
            reasoning_effort=str(self.reasoning_effort or "low"),
        )
        encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
        return len(
            encoding.render_conversation_for_completion(
                conversation,
                Role.ASSISTANT,
                RenderConversationConfig(auto_drop_analysis=True),
            )
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        rendered_prompt_tokens = self._count_harmony_tokens(messages)
        prompt_tokens = self.prompt_calibrator.estimate(rendered_prompt_tokens)
        requested_tokens = prompt_tokens + self.max_output_tokens
        reservation = self.token_limiter.acquire(requested_tokens, thread_id=self.thread_id)
        print(f"[llm-stream] provider=groq model={self.model_name} start", flush=True)
        provider_kwargs = {
            "max_completion_tokens": self.max_output_tokens,
            **kwargs,
        }
        if self.tools:
            provider_kwargs["tools"] = self.tools
            provider_kwargs["parallel_tool_calls"] = False
        try:
            result = super()._generate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **provider_kwargs,
            )
        except Exception:
            # Provider errors can occur after inference (for example malformed generated tool
            # JSON). Usage is unavailable in that case, so retain the full reservation before a
            # caller retries instead of incorrectly refunding the completion allowance.
            self.token_limiter.reconcile(reservation, None)
            raise

        message = result.generations[0].message
        usage = getattr(message, "usage_metadata", None) or {}
        actual_total = usage.get("total_tokens")
        actual_prompt = usage.get("input_tokens")
        if actual_total is None:
            token_usage = (result.llm_output or {}).get("token_usage") or {}
            actual_total = token_usage.get("total_tokens")
            actual_prompt = actual_prompt or token_usage.get("prompt_tokens")
        if actual_prompt:
            self.prompt_calibrator.observe(rendered_prompt_tokens, int(actual_prompt))
        self.token_limiter.reconcile(reservation, actual_total)

        finish_reason = message.response_metadata.get("finish_reason")
        message.response_metadata["output_limit_reached"] = finish_reason == "length"
        content = str(message.content or "")
        tool_calls = list(getattr(message, "tool_calls", []) or [])
        print(f"[llm-stream] provider=groq model={self.model_name} done", flush=True)
        print(
            "[llm-usage] "
            f"prompt_tokens={usage.get('input_tokens', prompt_tokens)} "
            f"harmony_tokens={rendered_prompt_tokens} "
            f"calibration={self.prompt_calibrator.ratio:.3f} "
            f"generated_tokens={usage.get('output_tokens', 'unknown')} "
            f"total_tokens={actual_total or 'unknown'} finish_reason={finish_reason or 'unknown'}",
            flush=True,
        )
        print(
            f"[llm-output] provider=groq model={self.model_name} "
            f"visible_content={json.dumps(content, ensure_ascii=True)}",
            flush=True,
        )
        print(
            f"[llm-output] provider=groq model={self.model_name} "
            f"tool_calls={json.dumps(tool_calls, ensure_ascii=True, sort_keys=True, default=str)}",
            flush=True,
        )
        reasoning = message.additional_kwargs.get("reasoning_content")
        if reasoning:
            print(
                f"[llm-reasoning] provider=groq model={self.model_name} "
                f"content={json.dumps(reasoning, ensure_ascii=True)}",
                flush=True,
            )
        return result
