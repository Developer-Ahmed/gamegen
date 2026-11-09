import json
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.context_compaction import (
    COMPACTION_USER_PROMPT,
    MAX_CHECKPOINT_REQUEST_CHARS,
    SUMMARY_MAX_OUTPUT_TOKENS,
    SUMMARY_PREFIX,
    _normalize_intention_summary,
    prepare_model_context,
)
from agent.local_ollama import LocalOllamaChatModel, _load_tokenizer


class _FakeModel:
    def __init__(
        self,
        *,
        normal_prompt_tokens=500,
        context_window=8192,
        max_output_tokens=2048,
        tools=None,
        temperature=0.2,
        think=None,
        tracker=None,
        checkpoint_tokens=600,
    ):
        self.normal_prompt_tokens = normal_prompt_tokens
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.tools = [{"type": "function"}] if tools is None else tools
        self.temperature = temperature
        self.think = think
        self.tracker = tracker if tracker is not None else {"summary_calls": 0}
        self.checkpoint_tokens = checkpoint_tokens

    def model_copy(self, *, update):
        values = {
            "normal_prompt_tokens": self.normal_prompt_tokens,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "tools": self.tools,
            "temperature": self.temperature,
            "think": self.think,
            "tracker": self.tracker,
            "checkpoint_tokens": self.checkpoint_tokens,
        }
        values.update(update)
        return _FakeModel(**values)

    def count_prompt_tokens(self, messages):
        if messages and str(messages[-1].content) == COMPACTION_USER_PROMPT:
            return 1000
        if any(str(message.content).startswith(SUMMARY_PREFIX) for message in messages):
            return self.checkpoint_tokens
        return self.normal_prompt_tokens

    def invoke(self, messages):
        self.tracker["summary_calls"] += 1
        self.tracker["summary_messages"] = list(messages)
        self.tracker["summary_tools"] = self.tools
        self.tracker["summary_think"] = self.think
        self.tracker["summary_max_output_tokens"] = self.max_output_tokens
        return AIMessage(
            content=json.dumps({
                "user_goal": "Finish the requested UNO game.",
                "requirements": ["Local multiplayer"],
                "design_decisions": [],
                "next_intent": "Validate the next small edit.",
            }),
            response_metadata={"eval_count": 18},
        )


class _InvalidSummaryModel(_FakeModel):
    def model_copy(self, *, update):
        values = {
            "normal_prompt_tokens": self.normal_prompt_tokens,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "tools": self.tools,
            "temperature": self.temperature,
            "think": self.think,
            "tracker": self.tracker,
            "checkpoint_tokens": self.checkpoint_tokens,
        }
        values.update(update)
        return _InvalidSummaryModel(**values)

    def invoke(self, messages):
        self.tracker["summary_calls"] += 1
        return AIMessage(
            content='{"user_goal":"truncated',
            response_metadata={"eval_count": 320, "output_limit_reached": True},
        )


class _UnavailableSummaryModel(_FakeModel):
    def model_copy(self, *, update):
        values = {
            "normal_prompt_tokens": self.normal_prompt_tokens,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "tools": self.tools,
            "temperature": self.temperature,
            "think": self.think,
            "tracker": self.tracker,
            "checkpoint_tokens": self.checkpoint_tokens,
        }
        values.update(update)
        return _UnavailableSummaryModel(**values)

    def count_prompt_tokens(self, messages):
        raise ConnectionError("Ollama is unavailable")


class _RenderedPromptResponse:
    def __init__(self, rendered):
        self.rendered = rendered

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return {"_debug_info": {"rendered_template": self.rendered}}


class ContextCompactionTests(SimpleTestCase):
    def test_intention_summary_rejects_operational_status_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            _normalize_intention_summary(json.dumps({
                "user_goal": "Create UNO",
                "requirements": [],
                "design_decisions": [],
                "next_intent": "Implement turns",
                "validation_passed": True,
                "files_created": ["main.gd"],
            }))

    def test_history_below_exact_token_threshold_is_unchanged(self):
        messages = [HumanMessage(content="Create a simple UNO game.")]
        model = _FakeModel(normal_prompt_tokens=6016)

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
        )

        self.assertFalse(projection.compacted)
        self.assertFalse(projection.compacted_this_call)
        self.assertEqual(projection.messages, messages)
        self.assertEqual(projection.projected_tokens, 6016)
        self.assertEqual(projection.compaction_threshold_tokens, 6016)
        self.assertEqual(model.tracker["summary_calls"], 0)

    def test_qwen_creates_checkpoint_after_token_threshold(self):
        messages = [
            HumanMessage(content="Create a two-player UNO game."),
            AIMessage(content="I will begin with the rules."),
        ]
        model = _FakeModel(normal_prompt_tokens=6017)

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
        )

        self.assertTrue(projection.compacted)
        self.assertTrue(projection.compacted_this_call)
        self.assertEqual(projection.context_summary_through, len(messages))
        self.assertEqual(projection.context_compaction_count, 1)
        self.assertEqual(len(projection.messages), 1)
        checkpoint = str(projection.messages[0].content)
        self.assertIn(SUMMARY_PREFIX, checkpoint)
        self.assertIn("Finish the requested UNO game", checkpoint)
        self.assertIn("Create a two-player UNO game.", checkpoint)
        self.assertEqual(model.tracker["summary_calls"], 1)
        self.assertEqual(model.tracker["summary_tools"], [])
        self.assertFalse(model.tracker["summary_think"])
        self.assertEqual(
            model.tracker["summary_max_output_tokens"],
            SUMMARY_MAX_OUTPUT_TOKENS,
        )

    def test_compaction_flattens_native_tool_history_before_summary(self):
        messages = [
            HumanMessage(content="Create UNO."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_game_workspace",
                        "args": {"title": "UNO"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"status":"success"}',
                tool_call_id="call-1",
                name="create_game_workspace",
            ),
        ]
        model = _FakeModel(normal_prompt_tokens=8_000)

        prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
        )

        summary_messages = model.tracker["summary_messages"]
        self.assertFalse(any(isinstance(message, ToolMessage) for message in summary_messages))
        self.assertFalse(
            any(
                isinstance(message, AIMessage) and message.tool_calls
                for message in summary_messages
            )
        )
        rendered = "\n".join(str(message.content) for message in summary_messages)
        self.assertIn('"name":"create_game_workspace"', rendered)
        self.assertIn('tool:create_game_workspace', rendered)
        self.assertEqual(model.tracker["summary_tools"], [])

    def test_checkpoint_bounds_large_verbatim_user_request(self):
        request = "Create a game. " + ("detailed requirement " * 2_000)
        model = _FakeModel(normal_prompt_tokens=8_000)

        projection = prepare_model_context(
            messages=[HumanMessage(content=request), AIMessage(content="Planning.")],
            system_prompt="system",
            model=model,
        )

        checkpoint = str(projection.messages[0].content)
        self.assertIn("request truncated", checkpoint)
        self.assertLess(len(checkpoint), MAX_CHECKPOINT_REQUEST_CHARS + 1_000)

    def test_larger_context_accepts_checkpoint_that_cannot_fit_in_8k(self):
        model = _FakeModel(
            normal_prompt_tokens=12_000,
            context_window=12_288,
            checkpoint_tokens=7_774,
        )

        projection = prepare_model_context(
            messages=[HumanMessage(content="Create UNO."), AIMessage(content="Planning.")],
            system_prompt="large authoritative prompt",
            model=model,
        )

        self.assertTrue(projection.compacted_this_call)
        self.assertEqual(projection.projected_tokens, 7_774)
        self.assertEqual(projection.compaction_threshold_tokens, 10_112)

    def test_summary_is_bounded_to_durable_information(self):
        summary = _normalize_intention_summary(json.dumps({
            "user_goal": "Create UNO " * 100,
            "requirements": [f"requirement {index}" for index in range(20)],
            "design_decisions": [
                {"decision": f"decision {index}", "reason": "because"}
                for index in range(10)
            ],
            "next_intent": "Continue " * 100,
        }))
        payload = json.loads(summary)
        self.assertLessEqual(len(payload["requirements"]), 4)
        self.assertLessEqual(len(payload["design_decisions"]), 3)
        self.assertLessEqual(len(payload["user_goal"]), 240)

    def test_checkpoint_is_reused_with_only_new_messages(self):
        original = [HumanMessage(content="Create UNO."), AIMessage(content="Planning.")]
        model = _FakeModel(normal_prompt_tokens=7800)
        first = prepare_model_context(
            messages=original,
            system_prompt="system",
            model=model,
        )
        expanded = [*original, HumanMessage(content="Keep it local hot-seat multiplayer.")]

        second = prepare_model_context(
            messages=expanded,
            system_prompt="system",
            model=model,
            context_summary=first.context_summary,
            context_summary_through=first.context_summary_through,
            context_compaction_count=first.context_compaction_count,
        )

        self.assertTrue(second.compacted)
        self.assertFalse(second.compacted_this_call)
        self.assertEqual(len(second.messages), 2)
        self.assertTrue(str(second.messages[0].content).startswith(SUMMARY_PREFIX))
        self.assertEqual(second.messages[1], expanded[-1])
        self.assertEqual(model.tracker["summary_calls"], 1)

    def test_invalid_summary_is_discarded_in_favor_of_previous_valid_summary(self):
        previous = json.dumps(
            {
                "user_goal": "Create UNO",
                "requirements": ["Two players"],
                "design_decisions": [],
                "next_intent": "Implement card rules",
            }
        )
        model = _InvalidSummaryModel(normal_prompt_tokens=8_000)
        messages = [
            HumanMessage(content="Create UNO"),
            HumanMessage(content="Keep it local multiplayer."),
        ]

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
            context_summary=previous,
            context_summary_through=1,
            context_compaction_count=1,
            force=True,
        )

        self.assertEqual(projection.context_summary, previous)
        self.assertEqual(projection.context_summary_through, len(messages))

    def test_unavailable_local_summary_model_uses_deterministic_checkpoint(self):
        messages = [
            HumanMessage(content="Create a local two-player UNO game."),
            AIMessage(content="I will plan the implementation."),
        ]

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=_FakeModel(normal_prompt_tokens=8_000),
            summary_model=_UnavailableSummaryModel(),
        )

        self.assertTrue(projection.compacted_this_call)
        self.assertEqual(projection.context_summary_through, len(messages))
        summary = json.loads(projection.context_summary)
        self.assertEqual(summary["user_goal"], messages[0].content)
        self.assertEqual(summary["requirements"], [])
        self.assertEqual(summary["design_decisions"], [])

    def test_existing_summary_advances_over_tool_traffic_without_model_call(self):
        previous = json.dumps(
            {
                "user_goal": "Create UNO",
                "requirements": ["Two players"],
                "design_decisions": [],
            }
        )
        model = _FakeModel(normal_prompt_tokens=8_000, checkpoint_tokens=600)
        messages = [
            HumanMessage(content="Create UNO"),
            AIMessage(content="Planning"),
            ToolMessage(content='{"status":"success"}', tool_call_id="call-1", name="tool"),
        ]

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
            context_summary=previous,
            context_summary_through=2,
            context_compaction_count=1,
            force=True,
        )

        self.assertEqual(model.tracker["summary_calls"], 0)
        self.assertEqual(projection.context_summary, previous)
        self.assertEqual(projection.context_summary_through, len(messages))
        self.assertEqual(projection.projected_message_count, 1)

    def test_reused_summary_may_use_adaptive_output_headroom(self):
        previous = json.dumps(
            {
                "user_goal": "Create UNO",
                "requirements": ["Two players"],
                "design_decisions": [],
            }
        )
        model = _FakeModel(normal_prompt_tokens=8_000, checkpoint_tokens=6_200)
        messages = [HumanMessage(content="Create UNO"), AIMessage(content="Planning")]

        projection = prepare_model_context(
            messages=messages,
            system_prompt="system",
            model=model,
            context_summary=previous,
            context_summary_through=2,
            context_compaction_count=1,
            force=True,
        )

        self.assertEqual(model.tracker["summary_calls"], 0)
        self.assertEqual(projection.projected_tokens, 6_200)
        self.assertGreater(projection.projected_tokens, projection.compaction_threshold_tokens)

    def test_local_counter_tokenizes_exact_ollama_rendered_prompt(self):
        rendered = "<|im_start|>system\nRules<|im_end|>\n<|im_start|>user\nHello<|im_end|>\n"
        requests_seen = []

        def fake_post(url, **kwargs):
            requests_seen.append((url, kwargs))
            return _RenderedPromptResponse(rendered)

        model = LocalOllamaChatModel(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "test_tool", "description": "test", "parameters": {}},
                }
            ]
        )
        tokenizer = _load_tokenizer(model.tokenizer_path, model.tokenizer_sha256)
        expected = len(tokenizer.encode(rendered, add_special_tokens=False).ids)

        with patch("agent.local_ollama.requests.post", side_effect=fake_post):
            actual = model.count_prompt_tokens(
                [SystemMessage(content="Rules"), HumanMessage(content="Hello")]
            )

        self.assertEqual(actual, expected)
        payload = requests_seen[0][1]["json"]
        self.assertTrue(payload["_debug_render_only"])
        self.assertFalse(payload["truncate"])
        self.assertEqual(payload["options"]["num_ctx"], 12_288)
        self.assertEqual(payload["options"]["num_predict"], 2048)
        self.assertEqual(payload["tools"], model.tools)
