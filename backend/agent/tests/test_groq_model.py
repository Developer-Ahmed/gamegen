import json
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from agent.groq_model import GroqChatModel, PromptTokenCalibrator, RollingTokenRateLimiter
from agent.component_library import ComponentRegistry
from agent.execution_state import public_execution_state
from agent.prompts import system_prompt_for_execution
from agent.stage_policy import create_game_blueprint
from agent.tools import plan_game_stage


class GroqModelTests(SimpleTestCase):
    def _model(self, limiter=None) -> GroqChatModel:
        return GroqChatModel(
            model="openai/gpt-oss-120b",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            context_window=7_600,
            max_output_tokens=512,
            token_limiter=limiter or RollingTokenRateLimiter(8_000, 7_600),
            prompt_calibrator=PromptTokenCalibrator(initial_ratio=1.0, fixed_margin=0),
        )

    def test_limiter_reconciles_reserved_tokens_to_actual_usage(self):
        limiter = RollingTokenRateLimiter(100, 100, window_seconds=1)
        first = limiter.acquire(60)
        limiter.reconcile(first, 20)
        second = limiter.acquire(70)
        self.assertEqual(second.tokens, 70)

    def test_limiter_rejects_request_larger_than_budget(self):
        limiter = RollingTokenRateLimiter(100, 90)
        with self.assertRaisesRegex(RuntimeError, "cannot fit"):
            limiter.acquire(91)

    def test_tool_binding_returns_countable_model_copy(self):
        model = self._model().bind_tools([])
        self.assertIsInstance(model, GroqChatModel)
        self.assertGreater(model.count_prompt_tokens([HumanMessage(content="hello")]), 0)

    def test_generation_passes_tools_and_output_limit_to_provider(self):
        result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="done",
                        usage_metadata={
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                        response_metadata={"finish_reason": "stop"},
                    )
                )
            ]
        )
        model = self._model()
        with patch.object(ChatOpenAI, "_generate", return_value=result) as generate:
            response = model._generate([HumanMessage(content="hello")])
        self.assertEqual(response.generations[0].message.content, "done")
        self.assertEqual(generate.call_args.kwargs["max_completion_tokens"], 512)
        self.assertNotIn("parallel_tool_calls", generate.call_args.kwargs)
        self.assertGreaterEqual(model.prompt_calibrator.ratio, 1.0)

    def test_harmony_count_includes_bound_tool_contract(self):
        model = self._model()
        messages = [HumanMessage(content="Create UNO")]
        without_tool = model._count_harmony_tokens(messages)
        with_tool = model.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "create_game",
                        "description": "Create a game workspace.",
                        "parameters": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                            "required": ["title"],
                        },
                    },
                }
            ]
        )._count_harmony_tokens(messages)
        self.assertGreater(with_tool, without_tool)

    def test_stage_planning_request_fits_free_tier_tpm_budget_after_calibration(self):
        registry = ComponentRegistry()
        components = registry.resolve(
            ["godot.web_runtime", "godot.turn_system", "godot.card_system"]
        )
        state = {
            "phase": "needs_stage_plan",
            "workspace_created": True,
            "components": [component.name for component in components],
            "component_contracts": [
                component.to_contract_dict() for component in components
            ],
            "game_plan": "Create a complete local multiplayer UNO game with rules, UI, and tests.",
            "game_blueprint": create_game_blueprint(),
            "workspace_revision": 1,
            "validated_revision": 1,
            "research_completed": True,
            "initial_context_complete": True,
            "preparation_complete": True,
        }
        public = public_execution_state(state)
        public["allowed_tools"] = ["plan_game_stage"]
        prompt = (
            system_prompt_for_execution(public)
            + "\n\n## Authoritative execution state\n"
            + json.dumps(public, ensure_ascii=True, separators=(",", ":"))
        )
        model = self._model().model_copy(
            update={
                "prompt_calibrator": PromptTokenCalibrator(
                    initial_ratio=2.1,
                    fixed_margin=16,
                )
            }
        ).bind_tools([plan_game_stage])

        tokens = model.count_prompt_tokens(
            [SystemMessage(content=prompt), HumanMessage(content="Create a simple UNO game.")]
        )

        self.assertLessEqual(tokens, 6_960)
