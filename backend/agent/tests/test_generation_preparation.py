from copy import deepcopy
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from agent.generation_preparation import prepare_generation_workspace


class GenerationPreparationTests(SimpleTestCase):
    def test_preparation_validates_then_researches_demos_then_reads(self):
        path = "project/game/main.gd"
        state = {
            "workspace_revision": 1,
            "validated_revision": -1,
            "research_completed": False,
            "preparation_complete": False,
            "components": ["godot.web_runtime"],
            "game_plan": "Build a small turn-based game.",
            "required_component_reads": [],
            "required_main_reads": [path],
            "read_coverage": {},
        }
        events = []

        def validate(*, config):
            events.append("validate")
            state["validated_revision"] = 1
            return {"status": "success"}

        async def research(query, include_demo_examples, *, config):
            events.append("research")
            self.assertTrue(include_demo_examples)
            state["research_completed"] = True
            return {"status": "success"}

        def read(requests=None, *, config):
            events.append("read")
            state["read_coverage"] = {
                path: {"sha256": "a" * 64, "line_count": 1, "ranges": [[1, 1]]}
            }
            return {"status": "success"}

        async def load(_thread_id):
            return deepcopy(state)

        async def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch(
                "agent.generation_preparation.aload_execution_state",
                side_effect=load,
            ),
            patch("agent.generation_preparation.aupdate_execution_state", side_effect=update),
            patch("agent.generation_preparation.validate_godot_project", side_effect=validate),
            patch("agent.generation_preparation.research_godot_docs", side_effect=research),
            patch("agent.generation_preparation.read_game_files", side_effect=read),
        ):
            result = async_to_sync(prepare_generation_workspace)(
                {"configurable": {"thread_id": "preparation-test"}}
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(events, ["validate", "research", "read"])
        self.assertTrue(state["preparation_complete"])

    def test_research_fallback_is_automatic_and_stops_after_success(self):
        state = {
            "workspace_revision": 1,
            "validated_revision": 1,
            "research_completed": False,
            "preparation_complete": False,
            "components": ["godot.web_runtime"],
            "game_plan": "Build a browser game.",
            "required_component_reads": [],
            "required_main_reads": [],
            "read_coverage": {},
        }
        queries = []

        async def research(query, include_demo_examples, *, config):
            queries.append(query)
            if len(queries) == 1:
                return {"status": "error", "errors": ["No focused result"]}
            state["research_completed"] = True
            return {"status": "success"}

        async def load(_thread_id):
            return deepcopy(state)

        async def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.generation_preparation.aload_execution_state", side_effect=load),
            patch("agent.generation_preparation.aupdate_execution_state", side_effect=update),
            patch("agent.generation_preparation.research_godot_docs", side_effect=research),
        ):
            result = async_to_sync(prepare_generation_workspace)(
                {"configurable": {"thread_id": "fallback-test"}}
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(state["research_attempts"]), 2)

    def test_failed_research_ends_preparation_without_model_routing(self):
        state = {
            "workspace_revision": 1,
            "validated_revision": 1,
            "research_completed": False,
            "preparation_complete": False,
            "components": [],
            "game_plan": "Build a game.",
            "required_component_reads": [],
            "required_main_reads": [],
            "read_coverage": {},
        }

        async def research(query, include_demo_examples, *, config):
            return {"status": "error", "errors": ["Research unavailable"]}

        async def load(_thread_id):
            return deepcopy(state)

        async def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.generation_preparation.aload_execution_state", side_effect=load),
            patch("agent.generation_preparation.aupdate_execution_state", side_effect=update),
            patch("agent.generation_preparation.research_godot_docs", side_effect=research),
        ):
            result = async_to_sync(prepare_generation_workspace)(
                {"configurable": {"thread_id": "failed-research-test"}}
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["step"], "research")
        self.assertEqual(state["phase"], "preparation_failed")
        self.assertFalse(state["preparation_complete"])
