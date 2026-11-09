from django.test import SimpleTestCase
from langchain_core.messages import ToolMessage

from agent.react_agent import (
    _automatic_transition_for_tool,
    _is_malformed_tool_call_error,
    _execution_progress_token,
    _tool_call_signature,
    _tool_result_error,
)


class ReactAgentPolicyTests(SimpleTestCase):
    def test_workspace_and_edits_have_application_owned_follow_up(self):
        workspace = ToolMessage(
            content='{"status":"success"}',
            name="create_game_workspace",
            tool_call_id="workspace",
        )
        plan = ToolMessage(
            content='{"status":"success"}',
            name="plan_game_stage",
            tool_call_id="plan",
        )
        implementation = ToolMessage(
            content='{"status":"success"}',
            name="implement_stage_step",
            tool_call_id="implementation",
        )
        failed_implementation = ToolMessage(
            content='{"status":"error"}',
            name="implement_stage_step",
            tool_call_id="failed-implementation",
        )
        self.assertEqual(_automatic_transition_for_tool(workspace), "prepare_workspace")
        self.assertEqual(_automatic_transition_for_tool(plan), "prepare_workspace")
        self.assertEqual(_automatic_transition_for_tool(implementation), "validate_stage")
        self.assertIsNone(_automatic_transition_for_tool(failed_implementation))

    def test_tool_signature_ignores_argument_order(self):
        first = {"name": "read_game_files", "args": {"b": 2, "a": 1}}
        second = {"name": "read_game_files", "args": {"a": 1, "b": 2}}
        self.assertEqual(_tool_call_signature(first), _tool_call_signature(second))

    def test_read_revision_changes_progress_token(self):
        state = {"phase": "context_ready", "read_revision": 4, "workspace_revision": 1}
        before = _execution_progress_token(state)
        state["read_revision"] = 5
        self.assertNotEqual(before, _execution_progress_token(state))

    def test_only_malformed_tool_json_provider_error_is_retryable(self):
        self.assertTrue(
            _is_malformed_tool_call_error(
                RuntimeError("Failed to parse tool call arguments as JSON")
            )
        )
        self.assertTrue(
            _is_malformed_tool_call_error(
                RuntimeError(
                    "Parsing failed. The model generated output that could not be parsed. "
                    "See 'failed_generation' for more details."
                )
            )
        )
        self.assertTrue(
            _is_malformed_tool_call_error(
                RuntimeError(
                    "Tool call validation failed: parameters for tool plan_game_stage "
                    "did not match schema: /steps/2/acceptance_criteria maxItems"
                )
            )
        )
        self.assertFalse(_is_malformed_tool_call_error(RuntimeError("connection failed")))

    def test_toolnode_argument_validation_error_is_preserved(self):
        message = ToolMessage(
            content="Error: symbol must be a bare identifier. Please fix your mistakes.",
            name="plan_game_stage",
            tool_call_id="bad-plan",
        )

        self.assertIn("bare identifier", _tool_result_error(message))
