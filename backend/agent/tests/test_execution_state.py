import asyncio
import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from agent.execution_state import (
    aload_execution_state,
    aupdate_execution_state,
    allowed_tools,
    first_unread_line,
    next_required_read,
    public_execution_state,
    unread_pending_paths,
    unread_required_paths,
)
from agent.stage_policy import (
    create_game_blueprint,
    freeze_stage_plan,
    required_stage_targets,
    validate_stage_plan,
)


class ExecutionStateTests(SimpleTestCase):
    def test_async_state_adapters_run_sync_storage_off_event_loop(self):
        def load(thread_id):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {"thread_id": thread_id}

        def update(thread_id, updater):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            state = {"thread_id": thread_id}
            updater(state)
            return state

        with (
            patch("agent.execution_state.load_execution_state", side_effect=load),
            patch("agent.execution_state.update_execution_state", side_effect=update),
        ):
            loaded = async_to_sync(aload_execution_state)("chat-id")
            updated = async_to_sync(aupdate_execution_state)(
                "chat-id",
                lambda state: state.update({"ready": True}),
            )

        self.assertEqual(loaded, {"thread_id": "chat-id"})
        self.assertEqual(updated, {"thread_id": "chat-id", "ready": True})

    def test_workspace_creation_is_the_only_initial_action(self):
        self.assertEqual(allowed_tools({"workspace_created": False}), ("create_game_workspace",))

    def test_blueprint_routes_planning_then_one_bounded_step(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 1,
            "validated_revision": 1,
            "build_revision": -1,
            "research_completed": True,
            "initial_context_complete": True,
            "game_blueprint": create_game_blueprint(),
            "required_component_reads": [],
        }
        self.assertEqual(allowed_tools(state), ("plan_game_stage",))
        steps = validate_stage_plan(
            state,
            plan_summary="Define state helpers.",
            steps=[
                {
                    "id": "create_rules",
                    "path": "project/game/main.gd",
                    "symbol": "create_rules",
                    "symbol_kind": "function",
                    "purpose": "Create rule data.",
                    "acceptance_criteria": ["Returns rule data."],
                }
            ],
            sources={"project/game/main.gd": "extends Control\n"},
        )
        freeze_stage_plan(state, plan_summary="Define state helpers.", steps=steps)
        self.assertEqual(allowed_tools(state), ("implement_stage_step",))

    def test_unvalidated_edit_requires_validation_before_another_edit(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 2,
            "validated_revision": 1,
            "build_revision": -1,
        }
        tools = allowed_tools(state)
        self.assertEqual(tools, ("validate_godot_project",))
        self.assertNotIn("edit_game_files", tools)
        self.assertNotIn("build_godot_game", tools)

    def test_failed_validation_enables_targeted_repair(self):
        state = {
            "workspace_created": True,
            "phase": "repair",
            "workspace_revision": 2,
            "validated_revision": 1,
            "build_revision": -1,
        }
        tools = allowed_tools(state)
        self.assertEqual(tools, ("edit_game_files",))

    def test_validated_revision_can_be_edited_or_built(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 2,
            "validated_revision": 2,
            "build_revision": -1,
            "gameplay_edit_count": 1,
            "research_completed": True,
            "initial_context_complete": True,
            "completed_stage_targets": list(required_stage_targets([])),
        }
        tools = allowed_tools(state)
        self.assertIn("edit_game_files", tools)
        self.assertIn("build_godot_game", tools)
        self.assertNotIn("validate_godot_project", tools)

    def test_required_context_reads_are_enforced_before_editing(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 1,
            "validated_revision": 1,
            "build_revision": -1,
            "required_component_reads": ["project/addons/gamegen/card.gd"],
            "required_main_reads": ["project/game/main.gd", "project/game/main.tscn"],
            "read_coverage": {},
            "initial_context_complete": False,
            "research_completed": True,
        }
        self.assertEqual(allowed_tools(state), ("read_game_files",))

    def test_full_read_coverage_requires_no_line_gaps(self):
        path = "project/game/main.gd"
        state = {
            "required_main_reads": [path],
            "read_coverage": {
                path: {"line_count": 10, "ranges": [[1, 4], [6, 10]]}
            },
        }
        self.assertEqual(unread_required_paths(state, "required_main_reads"), [path])
        state["read_coverage"][path]["ranges"] = [[1, 4], [5, 10]]
        self.assertEqual(unread_required_paths(state, "required_main_reads"), [])

    def test_promoted_file_remains_readable_until_every_chunk_is_ingested(self):
        path = "project/game/rules.gd"
        state = {
            "workspace_created": True,
            "workspace_revision": 2,
            "validated_revision": 2,
            "build_revision": -1,
            "research_completed": True,
            "initial_context_complete": True,
            "pending_changed_paths": [path],
            "read_coverage": {
                path: {"line_count": 100, "ranges": [[1, 40]], "sha256": "a" * 64}
            },
            "game_blueprint": create_game_blueprint(),
        }

        self.assertEqual(unread_pending_paths(state), [path])
        self.assertEqual(allowed_tools(state), ("read_game_files",))

        state["read_coverage"][path]["ranges"] = [[1, 100]]
        self.assertEqual(unread_pending_paths(state), [])
        self.assertEqual(allowed_tools(state), ("plan_game_stage",))

    def test_next_required_read_selects_first_gap_in_component_order(self):
        card_path = "project/addons/gamegen/card.gd"
        turn_path = "project/addons/gamegen/turn.gd"
        state = {
            "required_component_reads": [card_path, turn_path],
            "required_main_reads": ["project/game/main.gd"],
            "read_coverage": {
                card_path: {"line_count": 20, "ranges": [[1, 7], [10, 20]]},
            },
        }

        self.assertEqual(first_unread_line(state, card_path), 8)
        self.assertEqual(
            next_required_read(state),
            {"kind": "component", "path": card_path, "start_line": 8},
        )

    def test_public_state_hides_raw_snapshots_and_structures_active_read(self):
        state = {
            "read_snapshots": {
                "project/game/main.gd": [{"content": "private raw source\n"}]
            },
            "active_read": {
                "path": "project/game/main.gd",
                "start_line": 3,
                "content": "var score := 0\nfunc reset():\n",
            },
        }

        public = public_execution_state(state)

        self.assertNotIn("read_snapshots", public)
        self.assertNotIn("read_coverage", public)
        self.assertNotIn("files", public)
        self.assertNotIn("content", public["active_read"])
        self.assertNotIn("source", public["active_read"])

    def test_research_is_required_before_automatic_source_ingestion(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 1,
            "validated_revision": 1,
            "research_completed": False,
            "initial_context_complete": False,
        }
        self.assertEqual(allowed_tools(state), ("research_godot_docs",))
        state["research_completed"] = True
        self.assertEqual(allowed_tools(state), ("read_game_files",))

    def test_public_state_indexes_source_without_rehydrating_it(self):
        path = "project/game/main.gd"
        state = {
            "read_snapshots": {
                path: [{
                    "sha256": "a" * 64,
                    "start_line": 1,
                    "end_line": 2,
                    "content": "extends Control\nvar score := 0\n",
                }]
            },
            "read_coverage": {
                path: {"sha256": "a" * 64, "line_count": 2, "ranges": [[1, 2]]}
            },
        }
        public = public_execution_state(state)
        source = public["source_index"][0]
        self.assertEqual(source["path"], path)
        self.assertEqual(source["functions"], [])
        self.assertNotIn("sha256", source)
        self.assertNotIn("source", source)
        self.assertNotIn("editable_sources", public)

    def test_non_presentation_stage_omits_scene_inventory(self):
        path = "project/game/main.tscn"
        state = {
            "game_blueprint": create_game_blueprint(),
            "read_snapshots": {
                path: [{
                    "sha256": "c" * 64,
                    "start_line": 1,
                    "end_line": 2,
                    "content": '[gd_scene]\n[node name="Game" type="Control"]\n',
                }]
            },
            "read_coverage": {
                path: {"sha256": "c" * 64, "line_count": 2, "ranges": [[1, 2]]}
            },
        }

        self.assertEqual(public_execution_state(state)["source_index"], [])

    def test_stage_planning_uses_contracts_without_duplicate_component_source(self):
        component_path = "project/addons/gamegen/component.gd"
        state = {
            "phase": "needs_stage_plan",
            "game_blueprint": {
                "stages": [{"id": "domain_model", "status": "needs_plan"}],
            },
            "component_contracts": [{
                "name": "godot.card_system",
                "api": ["deal()"],
                "method_contracts": {
                    "deal": {
                        "signature": "deal()",
                        "returns": "bool",
                        "preconditions": [],
                        "side_effects": [],
                        "non_effects": [],
                    },
                },
            }],
            "project_component_contracts": {
                component_path: {"methods": {"deal": "func deal():"}},
            },
            "research_context": {"documentation": [{"excerpt": "Godot guidance"}]},
            "required_component_reads": [component_path],
            "read_snapshots": {
                component_path: [{
                    "sha256": "b" * 64,
                    "start_line": 1,
                    "end_line": 1,
                    "content": "class_name Component\n",
                }],
            },
            "read_coverage": {
                component_path: {
                    "sha256": "b" * 64,
                    "line_count": 1,
                    "ranges": [[1, 1]],
                },
            },
        }

        public = public_execution_state(state)

        self.assertIn("component_contracts", public)
        self.assertEqual(public["component_contracts"][0]["api"], ["deal()"])
        self.assertEqual(public["component_contracts"][0]["method_contracts"], {})
        self.assertNotIn("project_component_contracts", public)
        self.assertIn("research_context", public)
        self.assertNotIn("planning_component_sources", public)
        self.assertNotIn("class_name Component", json.dumps(public))

    def test_active_implementation_receives_component_contracts(self):
        state = {
            "phase": "ready_to_implement_step",
            "game_blueprint": {
                "stages": [{
                    "id": "domain_model",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "rules",
                        "target": "create_rules",
                        "required_calls": ["card_system.hand"],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "component_contracts": [{
                "name": "godot.card_system",
                "foundation_member": "var card_system := GamegenCardSystem.new()",
                "api": ["hand(player_id: String) -> Array", "draw_card() -> Dictionary"],
                "method_contracts": {
                    "hand": {
                        "signature": "hand(player_id: String) -> Array",
                        "returns": "copy of hand",
                        "preconditions": ["configure first"],
                        "side_effects": [],
                        "non_effects": ["does not mutate"],
                    },
                    "draw_card": {
                        "signature": "draw_card() -> Dictionary",
                        "returns": "card",
                        "preconditions": ["configure first"],
                        "side_effects": ["removes one card"],
                        "non_effects": ["does not add to a hand"],
                    },
                },
            }],
        }

        public = public_execution_state(state)

        contract = public["component_contracts"][0]
        self.assertEqual(contract["name"], "godot.card_system")
        self.assertEqual(set(contract["method_contracts"]), {"hand"})

    def test_active_presentation_step_receives_foundation_and_scene_contracts(self):
        script_path = "project/game/main.gd"
        scene_path = "project/game/main.tscn"
        script = (
            "extends Control\n"
            "@onready var playfield: Control = %Playfield\n"
            "var game_state: Dictionary = {}\n"
            "func clear_playfield() -> void:\n\tpass\n"
            "func render_game() -> void:\n\tpass\n"
        )
        scene = (
            "[gd_scene]\n"
            "[node name=\"Root\" type=\"Control\"]\n"
            "[node name=\"Playfield\" type=\"Control\" parent=\".\"]\n"
        )
        state = {
            "phase": "ready_to_implement_step",
            "required_main_reads": [script_path, scene_path],
            "game_blueprint": {
                "stages": [{
                    "id": "presentation",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "render",
                        "path": script_path,
                        "target": "render_game",
                        "operation": "replace_function_body",
                        "symbol_kind": "function",
                        "required_calls": ["clear_playfield"],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "read_snapshots": {
                script_path: [{
                    "sha256": "a" * 64,
                    "start_line": 1,
                    "end_line": len(script.splitlines()),
                    "content": script,
                }],
                scene_path: [{
                    "sha256": "b" * 64,
                    "start_line": 1,
                    "end_line": len(scene.splitlines()),
                    "content": scene,
                }],
            },
            "read_coverage": {
                script_path: {
                    "sha256": "a" * 64,
                    "line_count": len(script.splitlines()),
                    "ranges": [[1, len(script.splitlines())]],
                },
                scene_path: {
                    "sha256": "b" * 64,
                    "line_count": len(scene.splitlines()),
                    "ranges": [[1, len(scene.splitlines())]],
                },
            },
        }

        context = public_execution_state(state)["active_step_context"]

        self.assertIn("@onready var playfield", context["foundation_declarations"])
        self.assertIn("func clear_playfield", context["required_local_helpers"]["clear_playfield"])
        self.assertIn("Root/Playfield", context["scene_nodes"][scene_path])

    def test_active_implementation_omits_application_owned_planning_history(self):
        state = {
            "phase": "ready_to_implement_step",
            "game_plan": "large original plan",
            "game_blueprint": {
                "stages": [{
                    "id": "core_gameplay",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "play_card",
                        "path": "project/game/main.gd",
                        "target": "play_card",
                        "operation": "insert_function",
                        "symbol_kind": "function",
                        "required_calls": [],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "read_snapshots": {},
            "read_coverage": {},
        }

        public = public_execution_state(state)

        self.assertIn("active_step_context", public)
        self.assertNotIn("game_plan", public)
        self.assertNotIn("source_index", public)
        self.assertNotIn("remaining_read_counts", public)

    def test_step_correction_exposes_errors_but_not_rejected_source(self):
        state = {
            "phase": "step_correction",
            "workspace_revision": 1,
            "game_blueprint": {
                "stages": [{
                    "id": "domain_model",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "create_rules",
                        "path": "project/game/rules.gd",
                        "target": "create_rules",
                        "operation": "create_file",
                        "symbol_kind": "function",
                        "required_calls": [],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "active_stage_id": "domain_model",
            "active_step_id": "create_rules",
            "step_correction": {
                "errors": ["GDScript file contains [gd_scene] syntax"],
                "rejected_sha256": "a" * 64,
                "relevant_component_contracts": [{"large": "x" * 10_000}],
            },
            "component_contracts": [],
        }

        public = public_execution_state(state)

        self.assertEqual(public["allowed_tools"], ["implement_stage_step"])
        self.assertIn("errors", public["correction"])
        self.assertNotIn("relevant_component_contracts", public["correction"])
        self.assertNotIn("research_context", public)
        self.assertNotIn("rejected_source", json.dumps(public))

    def test_step_correction_includes_contracts_named_only_by_diagnostics(self):
        state = {
            "phase": "step_correction",
            "workspace_revision": 1,
            "game_blueprint": {
                "stages": [{
                    "id": "core_gameplay",
                    "status": "in_progress",
                    "dependencies": [],
                    "steps": [{
                        "id": "handle_action",
                        "target": "handle_game_action",
                        "required_calls": ["turn_system.advance"],
                    }],
                    "completed_step_ids": [],
                }],
            },
            "active_stage_id": "core_gameplay",
            "active_step_id": "handle_action",
            "step_correction": {
                "errors": [
                    "Unknown component method card_system.draw(). Use draw_to_hand().",
                    "card_system.discard() does not remove a card from a hand.",
                ],
            },
            "component_contracts": [{
                "name": "godot.card_system",
                "foundation_member": "var card_system := GamegenCardSystem.new()",
                "api": [
                    "draw_to_hand(player_id: String) -> Dictionary",
                    "discard(card: Dictionary) -> bool",
                ],
                "method_contracts": {
                    "draw_to_hand": {"side_effects": ["adds to hand"]},
                    "discard": {"non_effects": ["does not remove from hand"]},
                },
            }, {
                "name": "godot.turn_system",
                "foundation_member": "var turn_system := GamegenTurnOrder.new()",
                "api": ["advance(steps: int = 1) -> Dictionary"],
                "method_contracts": {
                    "advance": {"side_effects": ["moves current player"]},
                },
            }],
        }

        public = public_execution_state(state)

        contracts = {item["name"]: item for item in public["component_contracts"]}
        self.assertEqual(
            contracts["godot.card_system"]["api"],
            [
                "draw_to_hand(player_id: String) -> Dictionary",
                "discard(card: Dictionary) -> bool",
            ],
        )
        self.assertIn("discard", contracts["godot.card_system"]["method_contracts"])
        self.assertIn("advance", contracts["godot.turn_system"]["method_contracts"])

    def test_untouched_foundation_cannot_be_built(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 1,
            "validated_revision": 1,
            "build_revision": -1,
            "gameplay_edit_count": 0,
        }
        self.assertNotIn("build_godot_game", allowed_tools(state))

    def test_built_revision_can_only_run(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 3,
            "validated_revision": 3,
            "build_revision": 3,
        }
        self.assertEqual(allowed_tools(state), ("run_game",))

    def test_failed_run_remains_recoverable(self):
        state = {
            "workspace_created": True,
            "workspace_revision": 3,
            "validated_revision": 3,
            "build_revision": -1,
            "run_finished": False,
            "run_succeeded": False,
            "phase": "repair",
        }
        self.assertIn("edit_game_files", allowed_tools(state))

    def test_successful_run_is_terminal_for_the_graph(self):
        self.assertEqual(allowed_tools({"run_succeeded": True}), ())
