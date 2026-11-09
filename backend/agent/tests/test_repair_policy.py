from django.test import SimpleTestCase
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.component_library import ComponentRegistry
from agent.semantic_validation import (
    validate_generated_semantics,
    validate_replacement_text,
    validate_step_contract,
    validate_step_scope,
    validate_stage_implementation,
)
from agent.tools import (
    TOOL_LIST,
    _compose_created_step_file,
    _repair_status,
    _reject_step_output,
    _is_allowed_godot_demo_url,
    _is_allowed_godot_doc_url,
    _search_godot_47_demos,
    _validate_generated_file_content,
    _validate_generated_file_path,
    _validate_readable_file_path,
    edit_game_files,
    create_game_workspace,
    read_game_files,
    reset_repair_count,
    write_files,
)


class _FakeFiles:
    def __init__(self, files):
        self.files = dict(files)

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, content):
        self.files[path] = content


class _FakeSandbox:
    def __init__(self, files, command_stdout=""):
        self.files = _FakeFiles(files)
        def run(command, *args, **kwargs):
            if "cp -a /home/user/game_files /home/user/game_files_candidate" in command:
                source_prefix = "/home/user/game_files/"
                target_prefix = "/home/user/game_files_candidate/"
                for path, content in list(self.files.files.items()):
                    if path.startswith(source_prefix):
                        self.files.files[target_prefix + path.removeprefix(source_prefix)] = content
            if "mv /home/user/game_files_candidate /home/user/game_files" in command:
                candidate_prefix = "/home/user/game_files_candidate/"
                active_prefix = "/home/user/game_files/"
                for path in [path for path in self.files.files if path.startswith(active_prefix)]:
                    del self.files.files[path]
                for path, content in list(self.files.files.items()):
                    if path.startswith(candidate_prefix):
                        self.files.files[active_prefix + path.removeprefix(candidate_prefix)] = content
                        del self.files.files[path]
            return SimpleNamespace(exit_code=0, stdout=command_stdout, stderr="")

        self.commands = SimpleNamespace(run=run)


class RepairPolicyTests(SimpleTestCase):
    def test_new_gdscript_step_receives_deterministic_file_header(self):
        source = "func create_rules() -> Dictionary:\n\treturn {}"
        self.assertEqual(
            _compose_created_step_file("project/game/rules.gd", source),
            "extends RefCounted\n\nfunc create_rules() -> Dictionary:\n\treturn {}\n",
        )

    def test_rejected_step_source_is_reduced_to_focused_correction_state(self):
        state = {
            "active_stage_id": "domain_model",
            "active_step_id": "create_rules",
            "game_blueprint": {
                "stages": [{
                    "id": "domain_model",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "create_rules",
                        "path": "project/game/rules.gd",
                        "operation": "create_file",
                        "target": "create_rules",
                        "symbol_kind": "function",
                        "required_calls": [],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "component_contracts": [],
            "rejected_step_signatures": [],
        }

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        rejected_source = "extends Godot\n[gd_scene format=3]\n"
        with patch("agent.tools.update_execution_state", side_effect=update):
            result = _reject_step_output(
                {"configurable": {"thread_id": "step-correction-test"}},
                execution=state,
                path="project/game/rules.gd",
                content=rejected_source,
                errors=["Invalid GDScript base type 'Godot'."],
                phase="write",
            )

        self.assertEqual(result["phase"], "step_correction")
        self.assertEqual(state["phase"], "step_correction")
        self.assertNotIn(rejected_source, str(state["step_correction"]))
        self.assertEqual(len(state["step_correction"]["rejected_sha256"]), 64)

    def test_three_rejected_step_versions_discard_plan_and_request_replan(self):
        state = {
            "active_stage_id": "domain_model",
            "active_step_id": "create_rules",
            "game_blueprint": {
                "stages": [{
                    "id": "domain_model",
                    "status": "in_progress",
                    "dependencies": [],
                    "plan_summary": "Create rules.",
                    "steps": [{
                        "id": "create_rules",
                        "path": "project/game/rules.gd",
                        "operation": "create_file",
                        "target": "create_rules",
                        "symbol_kind": "function",
                        "required_calls": [],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "component_contracts": [],
            "rejected_step_signatures": [],
            "rejected_step_strategy_fingerprints": [],
            "step_attempt_failures": 0,
        }

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        sources = [
            "func create_rules():\n\treturn []\n",
            "func create_rules():\n\tif true:\n\t\treturn {}\n",
            "func create_rules():\n\tmatch 1:\n\t\t1:\n\t\t\treturn {}\n",
        ]
        with patch("agent.tools.update_execution_state", side_effect=update):
            results = [
                _reject_step_output(
                    {"configurable": {"thread_id": "step-replan-test"}},
                    execution=state,
                    path="project/game/rules.gd",
                    content=source,
                    errors=["candidate did not satisfy the step"],
                    phase="write",
                )
                for source in sources
            ]

        self.assertEqual(results[-1]["phase"], "needs_stage_plan")
        self.assertEqual(results[-1]["allowed_tools"], ["plan_game_stage"])
        self.assertEqual(state["phase"], "needs_stage_plan")
        self.assertEqual(state["game_blueprint"]["stages"][0]["status"], "needs_plan")
        self.assertEqual(state["stage_replan_counts"], {"domain_model": 1})

    def test_repeated_workspace_creation_is_rejected_without_touching_sandbox(self):
        rejection = {
            "status": "rejected",
            "errors": ["create_game_workspace is not allowed during phase 'validated'."],
        }
        with (
            patch("agent.tools._guard_tool", return_value=rejection),
            patch("agent.tools.get_sandbox") as get_sandbox,
        ):
            result = create_game_workspace([], config={"configurable": {"thread_id": "ignored"}})

        self.assertEqual(result, rejection)
        get_sandbox.assert_not_called()

    def test_repair_failures_remain_recoverable(self):
        config = {"configurable": {"thread_id": "repair-policy-test"}}
        reset_repair_count("repair-policy-test")

        first = _repair_status(config, "write", "first failure")
        self.assertEqual(first["repair_attempts"], 1)
        self.assertEqual(first["failure_count"], 1)
        self.assertFalse(first["terminal"])

        second = _repair_status(config, "validate", "second failure")
        self.assertEqual(second["repair_attempts"], 2)
        self.assertEqual(second["failure_count"], 2)
        self.assertFalse(second["terminal"])

        third = _repair_status(config, "build", "third failure")
        self.assertEqual(third["repair_attempts"], 3)
        self.assertEqual(third["failure_count"], 3)
        self.assertFalse(third["terminal"])

        fourth = _repair_status(config, "build", "fourth failure")
        self.assertEqual(fourth["repair_attempts"], 4)
        self.assertEqual(fourth["failure_count"], 4)
        self.assertFalse(fourth["terminal"])

    def test_generated_file_contract_rejects_godot_resource_tool_paths(self):
        with self.assertRaisesRegex(ValueError, "project/game/main.gd"):
            _validate_generated_file_path("res://game/main.gd")

    def test_component_source_is_readable_but_not_editable(self):
        component_path = "project/addons/gamegen/card_system/gamegen_card_system.gd"
        _validate_readable_file_path(component_path)
        with self.assertRaisesRegex(ValueError, "immutable"):
            _validate_generated_file_path(component_path)

    def test_scene_files_must_use_scene_syntax(self):
        with self.assertRaisesRegex(ValueError, "GDScript belongs in a .gd file"):
            _validate_generated_file_content("project/game/main.tscn", "extends Node")

    def test_gdscript_rejects_scene_sections_and_fake_godot_namespace(self):
        with self.assertRaisesRegex(ValueError, "scene/resource section syntax"):
            _validate_generated_file_content(
                "project/game/state.gd",
                "extends RefCounted\n[gd_scene format=3]\n",
            )
        with self.assertRaisesRegex(ValueError, "Invalid GDScript base type"):
            _validate_generated_file_content(
                "project/game/state.gd",
                "extends Godot\n",
            )

    def test_every_registry_entry_is_a_contracted_component(self):
        components = ComponentRegistry().list_components()
        self.assertTrue(components)
        for component in components:
            self.assertTrue(component.api)

    def test_research_allows_only_stable_official_documentation(self):
        self.assertTrue(
            _is_allowed_godot_doc_url(
                "https://docs.godotengine.org/en/stable/classes/class_node.html"
            )
        )
        self.assertFalse(
            _is_allowed_godot_doc_url(
                "https://docs.godotengine.org/en/latest/classes/class_node.html"
            )
        )
        self.assertFalse(_is_allowed_godot_doc_url("https://example.com/godot-tutorial"))

    def test_research_allows_only_the_pinned_official_godot_4_7_demo_tag(self):
        self.assertTrue(
            _is_allowed_godot_demo_url(
                "https://github.com/godotengine/godot-demo-projects/"
                "blob/4.7-6ad6167/2d/finite_state_machine/player.gd"
            )
        )
        self.assertTrue(
            _is_allowed_godot_demo_url(
                "https://github.com/godotengine/godot-demo-projects/"
                "tree/4.7-6ad6167/gui/drag_and_drop"
            )
        )
        self.assertFalse(
            _is_allowed_godot_demo_url(
                "https://github.com/godotengine/godot-demo-projects/"
                "blob/master/2d/finite_state_machine/player.gd"
            )
        )
        self.assertFalse(
            _is_allowed_godot_demo_url(
                "https://github.com/godotengine/godot-demo-projects/"
                "tree/4.7-6ad6167-malicious/gui"
            )
        )
        self.assertFalse(
            _is_allowed_godot_demo_url(
                "https://github.com/another-owner/godot-demo-projects/"
                "blob/4.7-6ad6167/example.gd"
            )
        )

    def test_demo_research_returns_source_from_the_pinned_project(self):
        tree = (
            {"type": "blob", "path": "gui/drag_and_drop/project.godot"},
            {"type": "blob", "path": "gui/drag_and_drop/drag_control.gd"},
            {"type": "blob", "path": "gui/drag_and_drop/main.tscn"},
            {"type": "blob", "path": "2d/platformer/project.godot"},
        )
        with (
            patch("agent.tools._godot_47_demo_tree", return_value=tree),
            patch(
                "agent.tools._godot_47_demo_file",
                return_value="func _get_drag_data(_position: Vector2) -> Variant:\n\tpass\n",
            ),
        ):
            results = _search_godot_47_demos("Control drag and drop")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["project_path"], "gui/drag_and_drop")
        self.assertEqual(results[0]["source_file"], "gui/drag_and_drop/drag_control.gd")
        self.assertIn("/tree/4.7-6ad6167/", results[0]["url"])
        self.assertIn("_get_drag_data", results[0]["excerpt"])

    def test_every_registered_tool_has_a_valid_schema(self):
        for tool in TOOL_LIST:
            with self.subTest(tool=tool.__name__):
                schema = convert_to_openai_tool(tool)
                self.assertEqual(schema["function"]["name"], tool.__name__)
                if tool.__name__ in {"edit_game_files", "write_files"}:
                    self.assertNotIn(
                        "stage_name",
                        schema["function"]["parameters"].get("properties", {}),
                    )

    def test_read_then_edit_uses_optimistic_revision(self):
        thread_id = "file-edit-test"
        config = {"configurable": {"thread_id": thread_id}}
        reset_repair_count(thread_id)
        path = "/home/user/game_files/project/game/main.gd"
        sandbox = _FakeSandbox({path: "extends Node\nvar score := 0\n"})
        state = {
            "workspace_created": False,
            "initial_context_complete": False,
            "required_component_reads": [],
            "required_main_reads": [],
            "read_coverage": {},
            "read_snapshots": {},
        }

        def update_state(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", side_effect=lambda _thread_id: deepcopy(state)),
            patch("agent.tools.update_execution_state", side_effect=update_state),
        ):
            read_result = read_game_files(
                [{"path": "project/game/main.gd"}],
                config=config,
            )
            edit_result = edit_game_files(
                [
                    {
                        "path": "project/game/main.gd",
                        "expected_sha256": read_result["files"][0]["sha256"],
                        "operations": [
                            {
                                "operation": "replace_declaration",
                                "target": "score",
                                "new_text": "var score := 1",
                            }
                        ],
                    }
                ],
                config=config,
            )

        self.assertEqual(edit_result["status"], "success")
        self.assertIn("var score := 0", sandbox.files.files[path])
        self.assertIn(
            "var score := 1",
            sandbox.files.files[path.replace("/game_files/", "/game_files_candidate/")],
        )
        self.assertEqual(
            state["candidate_change_summary"],
            "Apply bounded source update for implement_score",
        )

    def test_required_reads_override_model_ranges_and_advance_coverage(self):
        thread_id = "ordered-read-test"
        component_path = "project/addons/gamegen/card_system/gamegen_card_system.gd"
        absolute_component_path = f"/home/user/game_files/{component_path}"
        source = "".join(
            f"var card_{index}: String = \"value_{index}\"\n" for index in range(300)
        )
        sandbox = _FakeSandbox({absolute_component_path: source})
        state = {
            "required_component_reads": [component_path],
            "required_main_reads": ["project/game/main.gd", "project/game/main.tscn"],
            "read_coverage": {},
            "read_snapshots": {},
            "read_revision": 0,
            "read_lines_covered": 0,
            "initial_context_complete": False,
        }

        def update_state(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.load_execution_state", side_effect=lambda _thread_id: deepcopy(state)),
            patch("agent.tools.update_execution_state", side_effect=update_state),
            patch("agent.tools.get_sandbox", return_value=sandbox),
        ):
            first = read_game_files(
                [{"path": "project/game/main.gd"}],
                config={"configurable": {"thread_id": thread_id}},
            )
            second = read_game_files(
                [{"path": "project/game/main.gd"}],
                config={"configurable": {"thread_id": thread_id}},
            )

        self.assertEqual(first["status"], "success")
        self.assertTrue(first["request_overridden"])
        self.assertEqual(first["files"][0]["path"], component_path)
        self.assertEqual(first["files"][0]["start_line"], 1)
        self.assertEqual(second["status"], "success")
        self.assertGreater(second["files"][0]["start_line"], 1)
        self.assertGreater(second["coverage_progress"]["read_revision"], 1)
        self.assertGreater(second["coverage_progress"]["lines_added"], 0)

    def test_read_persists_exact_raw_source_but_returns_structured_lines(self):
        thread_id = "exact-read-test"
        config = {"configurable": {"thread_id": thread_id}}
        relative_path = "project/addons/gamegen/card_system/gamegen_card_system.gd"
        absolute_path = f"/home/user/game_files/{relative_path}"
        source = "class_name GamegenCardSystem\nvar cards: Array = []\n"
        sandbox = _FakeSandbox({absolute_path: source})
        state = {
            "required_component_reads": [relative_path],
            "required_main_reads": [],
            "read_coverage": {},
            "read_snapshots": {},
            "initial_context_complete": False,
        }

        def update_state(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", side_effect=lambda _thread_id: deepcopy(state)),
            patch("agent.tools.update_execution_state", side_effect=update_state),
        ):
            result = read_game_files([{"path": relative_path}], config=config)

        self.assertEqual(result["status"], "success")
        self.assertNotIn("content", result["files"][0])
        self.assertEqual(
            result["files"][0]["source_lines"],
            [
                {"line": 1, "code": "class_name GamegenCardSystem"},
                {"line": 2, "code": "var cards: Array = []"},
            ],
        )
        self.assertEqual(state["read_snapshots"][relative_path][0]["content"], source)
        self.assertTrue(state["initial_context_complete"])

    def test_edit_rejects_lines_outside_saved_read_coverage(self):
        thread_id = "unread-edit-test"
        config = {"configurable": {"thread_id": thread_id}}
        relative_path = "project/game/main.gd"
        absolute_path = f"/home/user/game_files/{relative_path}"
        source = "extends Node\nvar score := 0\nfunc reset():\n"
        file_hash = sha256(source.encode()).hexdigest()
        sandbox = _FakeSandbox({absolute_path: source})
        state = {
            "initial_context_complete": True,
            "active_read": {
                "path": relative_path,
                "sha256": file_hash,
                "start_line": 1,
                "end_line": 2,
                "content": "extends Node\nvar score := 0\n",
            },
            "read_snapshots": {
                relative_path: [{
                    "sha256": file_hash,
                    "start_line": 1,
                    "end_line": 2,
                    "content": "extends Node\nvar score := 0\n",
                }]
            },
        }

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", return_value=state),
            patch(
                "agent.tools.update_execution_state",
                side_effect=lambda _thread_id, updater: (updater(state), deepcopy(state))[1],
            ),
        ):
            result = edit_game_files(
                [{
                    "path": relative_path,
                    "expected_sha256": file_hash,
                    "operations": [{
                        "operation": "replace_function_body",
                        "target": "missing_reset",
                        "new_text": "pass",
                    }],
                }],
                config=config,
            )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("was not found uniquely", result["errors"][0])
        self.assertEqual(sandbox.files.files[absolute_path], source)

    def test_edit_accepts_an_exact_saved_read_that_is_not_active(self):
        thread_id = "inactive-read-edit-test"
        config = {"configurable": {"thread_id": thread_id}}
        relative_path = "project/game/main.gd"
        absolute_path = f"/home/user/game_files/{relative_path}"
        source = "extends Node\nvar score := 0\n"
        file_hash = sha256(source.encode()).hexdigest()
        sandbox = _FakeSandbox({absolute_path: source})
        state = {
            "initial_context_complete": True,
            "active_read": {
                "path": "project/game/main.tscn",
                "sha256": "f" * 64,
                "start_line": 1,
                "end_line": 1,
                "content": "[gd_scene]\n",
            },
            "read_snapshots": {
                relative_path: [{
                    "sha256": file_hash,
                    "start_line": 1,
                    "end_line": 2,
                    "content": source,
                }]
            },
        }

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", return_value=state),
            patch(
                "agent.tools.update_execution_state",
                side_effect=lambda _thread_id, updater: (updater(state), deepcopy(state))[1],
            ),
        ):
            result = edit_game_files(
                [{
                    "path": relative_path,
                    "expected_sha256": file_hash,
                    "operations": [{
                        "operation": "replace_declaration",
                        "target": "score",
                        "new_text": "var score := 1",
                    }],
                }],
                config=config,
            )

        self.assertEqual(result["status"], "success")
        self.assertIn(
            "var score := 1",
            sandbox.files.files[absolute_path.replace("/game_files/", "/game_files_candidate/")],
        )

    def test_stale_edit_does_not_write(self):
        thread_id = "stale-edit-test"
        config = {"configurable": {"thread_id": thread_id}}
        reset_repair_count(thread_id)
        path = "/home/user/game_files/project/game/main.gd"
        original = "extends Node\n"
        sandbox = _FakeSandbox({path: original})
        state = {
            "initial_context_complete": True,
            "read_snapshots": {
                "project/game/main.gd": [{
                    "sha256": "0" * 64,
                    "start_line": 1,
                    "end_line": 1,
                    "content": original,
                }]
            },
        }

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", return_value=state),
            patch(
                "agent.tools.update_execution_state",
                side_effect=lambda _thread_id, updater: (updater(state), deepcopy(state))[1],
            ),
        ):
            result = edit_game_files(
                [
                    {
                        "path": "project/game/main.gd",
                        "expected_sha256": "0" * 64,
                        "operations": [
                            {
                                "operation": "insert_function",
                                "target": "",
                                "new_text": "func setup() -> void:\n\tpass",
                            }
                        ],
                    }
                ],
                config=config,
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(sandbox.files.files[path], original)

    def test_large_focused_edit_is_allowed(self):
        thread_id = "large-edit-test"
        config = {"configurable": {"thread_id": thread_id}}
        path = "/home/user/game_files/project/game/main.gd"
        original = "extends Node\nfunc implement() -> void:\n\tpass\n"
        sandbox = _FakeSandbox({path: original})
        file_hash = sha256(original.encode()).hexdigest()
        state = {
            "initial_context_complete": True,
            "active_read": {
                "path": "project/game/main.gd",
                "sha256": file_hash,
                "start_line": 1,
                "end_line": 2,
                "content": original,
            },
            "read_snapshots": {
                "project/game/main.gd": [{
                    "sha256": file_hash,
                    "start_line": 1,
                    "end_line": 2,
                    "content": original,
                }]
            },
        }

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", return_value=state),
            patch(
                "agent.tools.update_execution_state",
                side_effect=lambda _thread_id, updater: (updater(state), deepcopy(state))[1],
            ),
        ):
            result = edit_game_files(
                [{
                    "path": "project/game/main.gd",
                    "expected_sha256": file_hash,
                    "operations": [{
                        "operation": "replace_function_body",
                        "target": "implement",
                        "new_text": "\n".join(
                            f"var value_{index} := {index}" for index in range(21)
                        ),
                    }],
                }],
                config=config,
            )

        self.assertEqual(result["status"], "success")
        candidate_path = path.replace("/game_files/", "/game_files_candidate/")
        self.assertIn("var value_20 := 20", sandbox.files.files[candidate_path])

    def test_write_files_refuses_to_overwrite_existing_source(self):
        thread_id = "write-existing-test"
        config = {"configurable": {"thread_id": thread_id}}
        reset_repair_count(thread_id)
        path = "/home/user/game_files/project/game/main.gd"
        original = "extends Node\n"
        sandbox = _FakeSandbox({path: original}, command_stdout="project/game/main.gd\n")

        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.load_execution_state", return_value={"components": []}),
            patch("agent.tools.get_sandbox", return_value=sandbox),
        ):
            result = write_files(
                {"project/game/main.gd": "extends Control\n"},
                config=config,
            )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("only creates new files", result["errors"][0])
        self.assertEqual(sandbox.files.files[path], original)

    def test_replacement_rejects_display_line_prefixes(self):
        issues = validate_replacement_text("    22 | func start_game() -> void:\n\tpass")
        self.assertEqual([issue.code for issue in issues], ["SOURCE_PREFIX"])

    def test_replacement_rejects_unsupported_range_loop(self):
        issues = validate_replacement_text("for value in 0 to 9:\n\tpass")
        self.assertIn("UNSUPPORTED_LOOP_SYNTAX", [issue.code for issue in issues])

    def test_replacement_rejects_incomplete_block(self):
        issues = validate_replacement_text("if cards.is_empty():")
        self.assertIn("INCOMPLETE_BLOCK", [issue.code for issue in issues])

    def test_replacement_accepts_complete_nested_blocks(self):
        issues = validate_replacement_text(
            "for value in range(0, 10):\n\tif value > 2:\n\t\tprint(value)"
        )
        self.assertEqual(issues, [])

    def test_edit_with_display_prefix_never_reaches_the_sandbox(self):
        path = "project/game/main.gd"
        absolute_path = f"/home/user/game_files/{path}"
        source = "extends Control\nfunc start_game() -> void:\n\tpass\n"
        file_hash = sha256(source.encode()).hexdigest()
        sandbox = _FakeSandbox({absolute_path: source})
        state = {
            "initial_context_complete": True,
            "components": [],
            "read_snapshots": {
                path: [{
                    "sha256": file_hash,
                    "start_line": 1,
                    "end_line": 3,
                    "content": source,
                }]
            },
        }
        with (
            patch("agent.tools._guard_tool", return_value=None),
            patch("agent.tools.get_sandbox", return_value=sandbox),
            patch("agent.tools.load_execution_state", return_value=state),
        ):
            result = edit_game_files(
                [{
                    "path": path,
                    "expected_sha256": file_hash,
                    "operations": [{
                        "operation": "replace_function_body",
                        "target": "start_game",
                        "new_text": "    2 | func start_game() -> void:\n    3 | \tpass",
                    }],
                }],
                config={"configurable": {"thread_id": "prefix-test"}},
            )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("SOURCE_PREFIX", result["errors"][0])
        self.assertEqual(sandbox.files.files[absolute_path], source)

    def test_card_component_rejects_string_card_definitions(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func setup() -> void:
\tcard_system.configure([\"red 0\", \"blue 1\"], [\"p1\", \"p2\"], 42)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("COMPONENT_SHAPE", [issue.code for issue in issues])

    def test_direct_double_initial_discard_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func initialize_game_state() -> void:
\tcard_system.discard(card_system.start_discard())
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("DOUBLE_INITIAL_DISCARD", [issue.code for issue in issues])

    def test_assigned_double_initial_discard_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func initialize_game_state() -> void:
\tvar opening_card := card_system.start_discard()
\tcard_system.discard(opening_card)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("DOUBLE_INITIAL_DISCARD", [issue.code for issue in issues])

    def test_stage_placeholder_cannot_be_marked_complete(self):
        source = """extends Control
func create_deck() -> Array:
\treturn []
"""
        issues = validate_stage_implementation(source, ["create_deck"])
        self.assertEqual([issue.code for issue in issues], ["PLACEHOLDER_STAGE"])

    def test_real_stage_implementation_passes_placeholder_gate(self):
        source = """extends Control
func create_deck() -> Array:
\tvar cards: Array[Dictionary] = []
\tcards.append({"id": "red-0", "color": "red", "value": "0"})
\treturn cards
"""
        self.assertEqual(validate_stage_implementation(source, ["create_deck"]), [])

    def test_component_constructor_inside_function_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func setup() -> void:
\tvar another = GamegenCardSystem.new()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("LOCAL_COMPONENT", [issue.code for issue in issues])

    def test_component_constructor_inside_isolated_smoke_test_is_allowed(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func smoke_test_draw() -> Dictionary:
\tvar isolated_card_system := GamegenCardSystem.new()
\treturn {"ok": isolated_card_system != null, "details": "isolated"}
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertNotIn("LOCAL_COMPONENT", [issue.code for issue in issues])

    def test_undocumented_component_method_is_rejected(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
func setup() -> void:
\tturn_system.get_instance()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.turn_system"]
        )
        self.assertIn("INVENTED_COMPONENT_API", [issue.code for issue in issues])

    def test_undocumented_component_property_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func setup() -> void:
	var hands = card_system.hands
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("INVENTED_COMPONENT_PROPERTY", [issue.code for issue in issues])

    def test_documented_component_calls_are_not_properties(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
func setup() -> void:
\tvar advance_result = turn_system.advance(1)
\tvar skip_result = turn_system.skip(1)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.turn_system"]
        )
        self.assertNotIn(
            "INVENTED_COMPONENT_PROPERTY",
            [issue.code for issue in issues],
        )

    def test_undocumented_method_on_isolated_component_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func smoke_test_draw() -> Dictionary:
\tvar isolated_cards := GamegenCardSystem.new()
\tisolated_cards.hallucinated_method()
\treturn {"ok": false, "details": "invalid"}
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("INVENTED_COMPONENT_API", [issue.code for issue in issues])

    def test_non_void_component_result_cannot_be_ignored(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func setup() -> void:
\tcard_system.draw_card()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("IGNORED_COMPONENT_RESULT", [issue.code for issue in issues])

    def test_optional_component_command_result_may_be_ignored(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
func advance_turn() -> void:
\tturn_system.advance()
\tturn_system.skip()
\tturn_system.reverse()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.turn_system"]
        )
        self.assertNotIn(
            "IGNORED_COMPONENT_RESULT",
            [issue.code for issue in issues],
        )

    def test_literal_success_smoke_test_is_rejected(self):
        source = """extends Control
func smoke_test_setup() -> Dictionary:
\treturn {\"ok\": true, \"details\": \"pretend success\"}
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("FAKE_SMOKE_TEST", [issue.code for issue in issues])

    def test_live_component_smoke_test_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func smoke_test_draw() -> Dictionary:
	card_system.draw_to_hand("p1")
	return {"ok": true, "details": "drawn"}
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("LIVE_SMOKE_MUTATION", [issue.code for issue in issues])

    def test_live_game_state_smoke_test_is_rejected(self):
        source = """extends Control
var game_state := {}
func smoke_test_gameplay() -> Dictionary:
	game_state["winner"] = "p1"
	return {"ok": true, "details": "mutated live state"}
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("LIVE_SMOKE_MUTATION", [issue.code for issue in issues])

    def test_component_mutation_in_process_is_rejected(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
func _process(_delta: float) -> void:
	turn_system.advance()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.turn_system"]
        )
        self.assertIn("FRAME_STATE_MUTATION", [issue.code for issue in issues])

    def test_insufficient_literal_deck_is_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func setup() -> void:
	var cards := [{"id": "one"}, {"id": "two"}]
	var players := ["p1", "p2"]
	var configured := card_system.configure(cards, players, 42)
	var dealt := card_system.deal(7)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("INSUFFICIENT_DECK", [issue.code for issue in issues])

    def test_incrementally_built_named_deck_is_not_treated_as_empty_literal(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func create_cards() -> Array:
	var cards: Array = []
	cards.append({"id": "one", "color": "red"})
	return cards
func setup() -> void:
	var cards := create_cards()
	var configured := card_system.configure(cards, ["p1", "p2"], 42)
	var dealt := card_system.deal(7)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertNotIn("COMPONENT_SHAPE", [issue.code for issue in issues])
        self.assertNotIn("INSUFFICIENT_DECK", [issue.code for issue in issues])

    def test_integer_card_ids_in_generated_deck_are_rejected(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func create_card_definitions() -> Array:
	var cards: Array = []
	var id_counter: int = 0
	cards.append({"id": id_counter, "color": "red", "rank": "number"})
	return cards
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("CARD_ID_TYPE", [issue.code for issue in issues])

    def test_string_formatted_card_ids_are_accepted(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func create_card_definitions() -> Array:
	var cards: Array = []
	var id_counter: int = 0
	cards.append({"id": "card_%d" % id_counter, "color": "red", "rank": "number"})
	return cards
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertNotIn("CARD_ID_TYPE", [issue.code for issue in issues])

    def test_undeclared_assignment_is_rejected(self):
        source = """extends Control
func setup() -> void:
	result = true
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("UNDECLARED_IDENTIFIER", [issue.code for issue in issues])

    def test_top_level_constant_cannot_be_reassigned_inside_function(self):
        source = """extends Control
const STATE_HANDS := "hands"
var game_state: Dictionary = {}
func setup() -> void:
	STATE_HANDS = {"p1": []}
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("CONST_REASSIGNMENT", [issue.code for issue in issues])

    def test_top_level_constant_can_be_used_as_dictionary_key(self):
        source = """extends Control
const STATE_HANDS := "hands"
var game_state: Dictionary = {}
func setup() -> void:
	game_state[STATE_HANDS] = {"p1": []}
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("CONST_REASSIGNMENT", [issue.code for issue in issues])

    def test_state_key_constant_cannot_be_used_as_live_state_value(self):
        source = """extends Control
const STATE_CURRENT_COLOR := "current_color"
var game_state: Dictionary = {}
func color() -> String:
\tvar current_color: String = STATE_CURRENT_COLOR
\treturn current_color
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("STATE_KEY_AS_VALUE", [issue.code for issue in issues])

    def test_state_key_constant_can_index_live_state(self):
        source = """extends Control
const STATE_CURRENT_COLOR := "current_color"
var game_state: Dictionary = {}
func color() -> String:
\tvar current_color: String = game_state[STATE_CURRENT_COLOR]
\treturn current_color
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("STATE_KEY_AS_VALUE", [issue.code for issue in issues])

    def test_quoted_state_constant_name_is_not_the_dictionary_key(self):
        source = """extends Control
const STATE_CURRENT_COLOR := "current_color"
var game_state: Dictionary = {}
func color() -> String:
\tif "STATE_CURRENT_COLOR" in game_state:
\t\treturn game_state["STATE_CURRENT_COLOR"]
\treturn ""
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("STATE_KEY_AS_VALUE", [issue.code for issue in issues])

    def test_dictionary_comparison_must_use_generated_literal_domain(self):
        source = """extends Control
func create_card_definitions() -> Array:
\treturn [{"id": "1", "type": "wild"}, {"id": "2", "type": "number"}]
func can_play(card: Dictionary) -> bool:
\tvar card_type = card.get("type", "")
\treturn card_type == "Wild"
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("DICTIONARY_VALUE_DOMAIN", [issue.code for issue in issues])

    def test_dictionary_comparison_accepts_generated_literal_value(self):
        source = """extends Control
func create_card_definitions() -> Array:
\treturn [{"id": "1", "type": "wild"}, {"id": "2", "type": "number"}]
func can_play(card: Dictionary) -> bool:
\treturn card["type"] == "wild"
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("DICTIONARY_VALUE_DOMAIN", [issue.code for issue in issues])

    def test_play_action_must_remove_card_from_hand_before_discarding(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func handle_game_action(action: Dictionary) -> bool:
	var card: Dictionary = action.get("card", {})
	return card_system.discard(card)
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertIn("CARD_NOT_REMOVED_FROM_HAND", [issue.code for issue in issues])

    def test_atomic_discard_from_hand_is_accepted(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func handle_game_action(action: Dictionary) -> bool:
	var played := card_system.discard_from_hand("p1", str(action.get("card_id", "")))
	return not played.is_empty()
"""
        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )
        self.assertNotIn("CARD_NOT_REMOVED_FROM_HAND", [issue.code for issue in issues])

    def test_play_action_must_call_available_legality_predicate(self):
        source = """extends Control
func is_card_playable(_card: Dictionary) -> bool:
	return true
func handle_game_action(action: Dictionary) -> bool:
	if action.get("type") == "play_card":
		return true
	return false
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("PLAY_BYPASSES_LEGALITY", [issue.code for issue in issues])

    def test_play_action_using_legality_predicate_is_accepted(self):
        source = """extends Control
func is_card_playable(_card: Dictionary) -> bool:
	return true
func handle_game_action(action: Dictionary) -> bool:
	if action.get("type") == "play_card":
		return is_card_playable(action.get("card", {}))
	return false
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("PLAY_BYPASSES_LEGALITY", [issue.code for issue in issues])

    def test_card_access_must_use_generated_schema_key(self):
        source = """extends Control
func create_card_definitions() -> Array:
	return [{"id": "r1", "color": "red", "value": "skip"}]
func apply_card_effect(card: Dictionary) -> void:
	var effect = card.get("effect", "")
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("UNKNOWN_CARD_FIELD", [issue.code for issue in issues])

    def test_card_access_accepts_generated_schema_key(self):
        source = """extends Control
func create_card_definitions() -> Array:
	return [{"id": "r1", "color": "red", "value": "skip"}]
func apply_card_effect(card: Dictionary) -> void:
	var effect = card.get("value", "")
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("UNKNOWN_CARD_FIELD", [issue.code for issue in issues])

    def test_signal_emit_arity_must_match_declaration(self):
        source = """extends Control
signal game_reset
func reset_game() -> void:
	game_reset.emit("winner")
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertIn("SIGNAL_ARITY", [issue.code for issue in issues])

    def test_signal_emit_matching_arity_is_accepted(self):
        source = """extends Control
signal game_finished(winner)
func finish_game() -> void:
	game_finished.emit("p1")
"""
        issues = validate_generated_semantics("project/game/main.gd", source, [])
        self.assertNotIn("SIGNAL_ARITY", [issue.code for issue in issues])

    def test_step_contract_requires_declared_calls_and_dictionary_keys(self):
        source = """extends Control
func create_match() -> void:
	game_state = {"players": []}
"""
        step = {
            "id": "create_match",
            "operation": "insert_function",
            "target": "create_match",
            "required_calls": ["turn_system.configure"],
            "required_dictionary_keys": ["players", "winner"],
        }
        issues = validate_step_contract("project/game/main.gd", source, step, [])
        self.assertEqual(
            {issue.code for issue in issues},
            {"MISSING_REQUIRED_CALL", "MISSING_DICTIONARY_KEY"},
        )

    def test_step_contract_accepts_symbolic_dictionary_key_assignment(self):
        source = """extends Control
const STATE_HANDS := "hands"
var game_state: Dictionary = {}
func setup() -> void:
	game_state[STATE_HANDS] = {}
"""
        issues = validate_step_contract(
            "project/game/main.gd",
            source,
            {
                "id": "setup",
                "target": "setup",
                "operation": "replace_function_body",
                "required_dictionary_keys": ["STATE_HANDS"],
            },
            [],
        )
        self.assertNotIn("MISSING_DICTIONARY_KEY", [issue.code for issue in issues])

    def test_initialization_calls_are_enforced_from_frozen_step_contract(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
var card_system := GamegenCardSystem.new()
func initialize_game_state() -> void:
	game_state = {"players": []}
"""
        step = {
            "id": "initialize",
            "operation": "replace_function_body",
            "target": "initialize_game_state",
            "required_calls": ["turn_system.configure", "card_system.configure"],
        }
        issues = validate_step_contract(
            "project/game/main.gd",
            source,
            step,
            ["godot.turn_system", "godot.card_system"],
        )
        self.assertEqual(
            [issue.code for issue in issues].count("MISSING_REQUIRED_CALL"),
            2,
        )

    def test_initialization_component_contract_passes_with_shared_members(self):
        source = """extends Control
var turn_system := GamegenTurnOrder.new()
var card_system := GamegenCardSystem.new()
func initialize_game_state() -> void:
	var players := ["p1", "p2"]
	var cards := [{"id": "one"}]
	var turns_ok := turn_system.configure(players, 0)
	var cards_ok := card_system.configure(cards, players, 42)
	game_state = {"configured": turns_ok and cards_ok}
"""
        step = {
            "id": "initialize",
            "operation": "replace_function_body",
            "target": "initialize_game_state",
            "required_calls": ["turn_system.configure", "card_system.configure"],
            "required_dictionary_keys": ["configured"],
        }
        self.assertEqual(
            validate_step_contract(
                "project/game/main.gd",
                source,
                step,
                ["godot.turn_system", "godot.card_system"],
            ),
            [],
        )

    def test_smoke_step_requires_isolated_component_transitions(self):
        source = """extends Control
func smoke_test_gameplay() -> Dictionary:
	return {"ok": true, "details": "claimed success"}
"""
        step = {
            "id": "verify_gameplay",
            "operation": "replace_function_body",
            "target": "smoke_test_gameplay",
        }
        issues = validate_step_contract(
            "project/game/main.gd",
            source,
            step,
            ["godot.turn_system", "godot.card_system"],
        )
        self.assertEqual(
            [issue.code for issue in issues].count("MISSING_ISOLATED_COMPONENT_TEST"),
            2,
        )

    def test_smoke_step_accepts_isolated_configured_component_transitions(self):
        source = """extends Control
func smoke_test_gameplay() -> Dictionary:
	var test_cards := GamegenCardSystem.new()
	var test_turns := GamegenTurnOrder.new()
	var players := ["p1", "p2"]
	var cards := [
		{"id": "one"}, {"id": "two"}, {"id": "three"},
		{"id": "four"}, {"id": "five"}
	]
	var cards_ok := test_cards.configure(cards, players, 42)
	var dealt_ok := test_cards.deal(1)
	var turns_ok := test_turns.configure(players, 0)
	var next_player := test_turns.advance()
	return {
		"ok": cards_ok and dealt_ok and turns_ok and next_player == "p2",
		"details": "isolated card deal and turn advancement verified"
	}
"""
        step = {
            "id": "verify_gameplay",
            "operation": "replace_function_body",
            "target": "smoke_test_gameplay",
        }
        self.assertEqual(
            validate_step_contract(
                "project/game/main.gd",
                source,
                step,
                ["godot.turn_system", "godot.card_system"],
            ),
            [],
        )

    def test_new_function_file_cannot_smuggle_in_whole_game(self):
        source = """extends RefCounted
func create_rules() -> Dictionary:
	return {}
func _ready() -> void:
	pass
func render_game() -> void:
	pass
"""
        step = {
            "id": "create_rules",
            "operation": "create_file",
            "target": "create_rules",
            "symbol_kind": "function",
        }

        codes = {issue.code for issue in validate_step_scope("project/game/rules.gd", source, step)}

        self.assertIn("STEP_SCOPE", codes)
        self.assertIn("OUT_OF_SCOPE_LIFECYCLE", codes)

    def test_new_declaration_file_rejects_unrelated_functions(self):
        source = """extends RefCounted
const CARD_COLORS := ["red", "blue"]
func initialize_game_state() -> void:
	pass
"""
        step = {
            "id": "colors",
            "operation": "create_file",
            "target": "CARD_COLORS",
            "symbol_kind": "declaration",
        }

        codes = {issue.code for issue in validate_step_scope("project/game/state.gd", source, step)}

        self.assertIn("STEP_SCOPE", codes)

    def test_helper_file_does_not_require_every_foundation_component_member(self):
        source = """extends RefCounted
func can_play() -> bool:
	return true
"""
        issues = validate_generated_semantics(
            "project/game/rules.gd",
            source,
            ["godot.card_system"],
            require_component_members=False,
        )

        self.assertNotIn("MISSING_COMPONENT_MEMBER", {issue.code for issue in issues})

    def test_concatenated_hands_are_not_treated_as_nested_hands(self):
        source = """extends Control
var card_system := GamegenCardSystem.new()
func check_winner() -> bool:
	var player_hands = card_system.hand("p1") + card_system.hand("p2")
	for hand in player_hands:
		if hand.size() == 0:
			return true
	return false
"""

        issues = validate_generated_semantics(
            "project/game/main.gd", source, ["godot.card_system"]
        )

        self.assertIn("FLATTENED_HANDS", {issue.code for issue in issues})

    def test_generic_node_clear_method_is_rejected(self):
        source = """extends Control
func render_cards() -> void:
	var playfield := get_node("Playfield")
	playfield.clear()
"""

        issues = validate_generated_semantics("project/game/main.gd", source, [])

        self.assertIn("INVENTED_NODE_API", {issue.code for issue in issues})
