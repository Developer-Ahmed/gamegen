from django.test import SimpleTestCase

from agent.prompts import (
    STAGE_PLANNING_PROMPT,
    WORKSPACE_PROMPT,
    system_prompt_for_execution,
)


class PhasePromptTests(SimpleTestCase):
    def test_workspace_prompt_contains_component_selection_contract(self):
        prompt = system_prompt_for_execution({"workspace_created": False})

        self.assertIn("create_game_workspace", prompt)
        self.assertIn("godot.card_system", prompt)
        self.assertIn(WORKSPACE_PROMPT, prompt)

    def test_stage_prompt_omits_workspace_registry(self):
        prompt = system_prompt_for_execution(
            {
                "workspace_created": True,
                "allowed_tools": ["plan_game_stage"],
            }
        )

        self.assertIn(STAGE_PLANNING_PROMPT, prompt)
        self.assertNotIn("recommended_sets", prompt)
        self.assertLess(len(prompt), 4_000)

    def test_implementation_prompt_is_scoped_to_active_step(self):
        prompt = system_prompt_for_execution(
            {
                "workspace_created": True,
                "allowed_tools": ["implement_stage_step"],
            }
        )

        self.assertIn("active_step_context", prompt)
        self.assertNotIn("plan_game_stage", prompt)
