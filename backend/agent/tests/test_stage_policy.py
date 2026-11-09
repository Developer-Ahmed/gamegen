from types import SimpleNamespace

from django.test import SimpleTestCase
from pydantic import ValidationError

from agent.tools import StagePlanStep, _sanitize_stage_plan_units
from agent.stage_policy import (
    advance_blueprint_after_commit,
    create_game_blueprint,
    current_step,
    dependency_errors,
    freeze_stage_plan,
    generation_is_complete,
    next_stage,
    operation_targets,
    reset_active_stage_for_replan,
    validate_stage_plan,
)


def _state():
    return {
        "workspace_created": True,
        "game_blueprint": create_game_blueprint(),
        "required_component_reads": [
            "project/addons/gamegen/card_system/gamegen_card_system.gd"
        ],
    }


def _step(**overrides):
    value = {
        "id": "create_rules",
        "path": "project/game/main.gd",
        "symbol": "create_rules",
        "symbol_kind": "function",
        "purpose": "Create deterministic game-specific rules.",
        "acceptance_criteria": ["Returns a non-empty typed rule set."],
        "depends_on": [],
        "required_calls": [],
        "required_dictionary_keys": [],
        "extends_component": False,
    }
    value.update(overrides)
    return value


class StagePolicyTests(SimpleTestCase):
    GAME_SOURCE = (
        "extends Control\n"
        "func initialize_game_state() -> void:\n\tpass\n"
        "func create_rules() -> Dictionary:\n\treturn {}\n"
    )

    def _validate(self, state, steps, summary="Define rules.", sources=None):
        return validate_stage_plan(
            state,
            plan_summary=summary,
            steps=steps,
            sources=sources or {"project/game/main.gd": self.GAME_SOURCE},
        )

    def test_every_game_starts_with_universal_domain_model_stage(self):
        self.assertEqual(next_stage(_state())["id"], "domain_model")

    def test_stage_plan_is_frozen_and_exposes_one_active_step(self):
        state = _state()
        steps = self._validate(state, [_step(symbol="new_rules")])
        freeze_stage_plan(state, plan_summary="Define rules.", steps=steps)
        self.assertEqual(current_step(state)["id"], "create_rules")
        self.assertEqual(current_step(state)["operation"], "insert_function")
        self.assertEqual(state["phase"], "ready_to_implement_step")

    def test_existing_function_is_resolved_to_body_replacement(self):
        steps = self._validate(
            _state(),
            [_step(symbol="create_rules")],
        )
        self.assertEqual(steps[0]["operation"], "replace_function_body")
        self.assertEqual(steps[0]["target"], "create_rules")

    def test_validated_cross_stage_symbol_requires_explicit_amendment(self):
        state = _state()
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_contracts"] = {
            "project/game/main.gd::create_rules": {
                "path": "project/game/main.gd",
                "symbol": "create_rules",
                "owner_stage": "domain_model",
                "last_changed_stage": "domain_model",
                "dictionary_keys": ["id"],
                "factory_role": "rules",
            }
        }
        with self.assertRaisesMessage(ValueError, "established contract"):
            self._validate(
                state,
                [
                    _step(
                        id="rewrite_rules",
                        symbol="create_rules",
                        required_dictionary_keys=["id"],
                    ),
                    _step(
                        id="initialize",
                        symbol="initialize_game_state",
                        purpose="Initialize the game from create_rules.",
                        acceptance_criteria=["Game state is initialized."],
                        required_calls=["create_rules"],
                    ),
                ],
            )

    def test_foundation_hook_cannot_be_planned_in_wrong_stage(self):
        with self.assertRaisesMessage(ValueError, "belongs to stage 'initialization'"):
            self._validate(
                _state(),
                [_step(symbol="initialize_game_state")],
            )

    def test_foundation_hook_cannot_be_disconnected_in_helper_file(self):
        state = _state()
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        with self.assertRaisesMessage(ValueError, "must update project/game/main.gd"):
            self._validate(
                state,
                [
                    _step(
                        path="project/game/setup.gd",
                        symbol="initialize_game_state",
                        required_calls=[],
                    )
                ],
                sources={},
            )

    def test_component_extension_must_be_explicit_and_selected(self):
        state = _state()
        component_step = _step(
            path="project/addons/gamegen/card_system/gamegen_card_system.gd",
            extends_component=False,
        )
        with self.assertRaisesMessage(ValueError, "extends_component=true"):
            validate_stage_plan(
                state,
                plan_summary="Extend cards.",
                steps=[component_step],
                sources={
                    "project/addons/gamegen/card_system/gamegen_card_system.gd":
                        "class_name GamegenCardSystem\n"
                },
            )

    def test_component_extension_requires_full_read_and_is_additive(self):
        state = _state()
        path = "project/addons/gamegen/card_system/gamegen_card_system.gd"
        state["read_coverage"] = {
            path: {"line_count": 20, "ranges": [[1, 20]], "sha256": "a" * 64}
        }
        destructive = _step(
            path=path,
            symbol="draw_card",
            extends_component=True,
        )
        component_source = "class_name GamegenCardSystem\nfunc draw_card() -> Dictionary:\n\treturn {}\n"
        with self.assertRaisesMessage(ValueError, "must add a new function"):
            validate_stage_plan(
                state,
                plan_summary="Extend cards.",
                steps=[destructive],
                sources={path: component_source},
            )

        additive = _step(
            path=path,
            symbol="draw_matching_card",
            extends_component=True,
        )
        steps = validate_stage_plan(
            state,
            plan_summary="Extend cards.",
            steps=[additive],
            sources={path: component_source},
        )
        self.assertTrue(steps[0]["extends_component"])
        self.assertEqual(steps[0]["operation"], "insert_function")

    def test_only_active_step_target_is_allowed(self):
        state = _state()
        steps = self._validate(state, [_step()])
        freeze_stage_plan(state, plan_summary="Define rules.", steps=steps)
        self.assertEqual(dependency_errors(["create_rules"], state), [])
        self.assertTrue(any("active step" in value for value in dependency_errors(["other"], state)))

    def test_commit_advances_step_then_stage(self):
        state = _state()
        steps = self._validate(
            state,
            [_step(), _step(id="rule_helper", symbol="rule_helper")],
        )
        freeze_stage_plan(state, plan_summary="Define rules.", steps=steps)
        advance_blueprint_after_commit(state)
        self.assertEqual(current_step(state)["id"], "rule_helper")
        advance_blueprint_after_commit(state)
        self.assertEqual(next_stage(state)["id"], "initialization")
        self.assertEqual(next_stage(state)["status"], "needs_plan")

    def test_generation_completes_only_after_all_universal_stages(self):
        state = _state()
        self.assertFalse(generation_is_complete(state))
        for stage in state["game_blueprint"]["stages"]:
            stage["status"] = "completed"
        self.assertTrue(generation_is_complete(state))

    def test_inserted_function_name_is_parsed_from_source(self):
        edit = SimpleNamespace(
            operations=[
                SimpleNamespace(
                    operation="insert_function",
                    target="",
                    new_text="func can_move() -> bool:\n\treturn true",
                )
            ]
        )
        self.assertEqual(operation_targets([edit]), ("can_move",))

    def test_dependencies_must_name_earlier_semantic_steps(self):
        with self.assertRaisesMessage(ValueError, "dependencies must name earlier steps"):
            self._validate(
                _state(),
                [_step(depends_on=["future_step"])],
            )

    def test_existing_foundation_function_dependency_is_already_satisfied(self):
        steps = self._validate(
            _state(),
            [_step(depends_on=["initialize_game_state"])],
        )
        self.assertEqual(steps[0]["depends_on"], [])

    def test_non_presentation_stage_rejects_ui_work(self):
        with self.assertRaisesMessage(ValueError, "presentation stage"):
            self._validate(
                _state(),
                [_step(purpose="Render a button for the player's action")],
            )

    def test_mixed_core_plan_defers_game_flow_step(self):
        state = _state()
        stages = state["game_blueprint"]["stages"]
        stages[0]["status"] = "completed"
        stages[1]["status"] = "completed"
        source = self.GAME_SOURCE + "func handle_game_action(_action: Dictionary) -> bool:\n\treturn false\n"
        steps = self._validate(
            state,
            [
                _step(
                    id="check_winner",
                    symbol="check_winner",
                    purpose="Detect the winner and terminal victory state.",
                ),
                _step(
                    id="handle_action",
                    symbol="handle_game_action",
                    purpose="Apply one legal gameplay action.",
                    depends_on=["check_winner"],
                ),
            ],
            sources={"project/game/main.gd": source},
        )
        self.assertEqual([step["target"] for step in steps], ["handle_game_action"])
        self.assertEqual(steps[0]["depends_on"], [])

    def test_function_dependency_becomes_required_call(self):
        steps = self._validate(
            _state(),
            [
                _step(id="create_data", symbol="create_data"),
                _step(
                    id="initialize",
                    symbol="initialize_data",
                    depends_on=["create_data"],
                ),
            ],
        )
        self.assertIn("create_data", steps[1]["required_calls"])

    def test_completed_stage_dependency_is_consumed_by_application_state(self):
        state = _state()
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_blueprint"]["stages"][1]["status"] = "completed"
        state["game_blueprint"]["stages"][2]["status"] = "completed"
        state["game_blueprint"]["stages"][3]["status"] = "completed"

        steps = self._validate(
            state,
            [_step(id="render", symbol="render_game", depends_on=["game_flow"])],
        )

        self.assertEqual(steps[0]["depends_on"], [])

    def test_presentation_defers_verification_and_application_transitions(self):
        state = _state()
        for stage in state["game_blueprint"]["stages"][:4]:
            stage["status"] = "completed"
        steps = self._validate(
            state,
            [
                _step(id="render", symbol="render_game"),
                _step(id="smoke", symbol="smoke_test_presentation", depends_on=["render"]),
                _step(id="run", symbol="run_game", depends_on=["smoke"]),
            ],
        )

        self.assertEqual([step["target"] for step in steps], ["render_game"])

    def test_new_gd_file_requires_one_named_structural_unit(self):
        with self.assertRaisesMessage(ValueError, "requires one named function or declaration"):
            self._validate(
                _state(),
                [_step(path="project/game/state.gd", symbol="", symbol_kind="function")],
                sources={},
            )

    def test_public_stage_schema_disallows_whole_gd_file_units(self):
        with self.assertRaises(ValidationError):
            StagePlanStep(
                **_step(
                    path="project/game/rules.gd",
                    symbol="",
                    symbol_kind="file",
                )
            )

    def test_public_stage_schema_names_new_scene_units_explicitly(self):
        step = StagePlanStep(
            **_step(
                path="project/game/board.tscn",
                symbol="",
                symbol_kind="new_scene",
            )
        )
        self.assertEqual(step.symbol_kind, "new_scene")

    def test_plan_sanitizer_keeps_valid_work_and_drops_malformed_optional_units(self):
        valid = _step(id="render", symbol="render_game")
        valid["depends_on"] = ["bad_scene", "bad_asset"]
        steps, omitted = _sanitize_stage_plan_units([
            valid,
            _step(
                id="bad_scene",
                path="project/game/main.tscn",
                symbol="",
                symbol_kind="scene_node",
            ),
            _step(
                id="bad_asset",
                path="project/assets",
                symbol="",
                symbol_kind="new_asset",
            ),
        ])

        self.assertEqual([step["id"] for step in steps], ["render"])
        self.assertEqual(steps[0]["depends_on"], [])
        self.assertEqual(omitted, ["bad_asset", "bad_scene"])

    def test_plan_sanitizer_rejects_plan_with_only_malformed_optional_units(self):
        with self.assertRaisesMessage(ValueError, "only malformed structural units"):
            _sanitize_stage_plan_units([
                _step(
                    id="bad_asset",
                    path="project/assets/",
                    symbol="",
                    symbol_kind="new_asset",
                )
            ])

    def test_public_stage_schema_canonicalizes_function_and_enum_notation(self):
        function = StagePlanStep(**_step(symbol="func create_deck() -> Array:"))
        declaration = StagePlanStep(
            **_step(symbol="enum CardColor { RED, BLUE }", symbol_kind="declaration")
        )

        self.assertEqual(function.symbol, "create_deck")
        self.assertEqual(declaration.symbol, "CardColor")

    def test_public_stage_schema_canonicalizes_empty_call_notation(self):
        step = StagePlanStep(
            **_step(
                required_calls=[
                    "turn_system.advance()",
                    "card_system.hand(player_id)",
                ]
            )
        )

        self.assertEqual(
            step.required_calls,
            ["turn_system.advance", "card_system.hand"],
        )

    def test_plan_sanitizer_rejects_unsupported_type_alias_notation(self):
        step = StagePlanStep(
            **_step(symbol="type Card = Dictionary", symbol_kind="declaration")
        )
        with self.assertRaisesMessage(ValueError, "only malformed structural units"):
            _sanitize_stage_plan_units([step.model_dump()])

    def test_domain_model_rejects_stateful_component_calls(self):
        state = _state()
        state["components"] = ["godot.card_system"]
        with self.assertRaisesMessage(ValueError, "pure data and rules"):
            self._validate(
                state,
                [
                    _step(
                        path="project/game/state.gd",
                        symbol="create_state",
                        required_calls=["card_system.configure"],
                    )
                ],
                sources={},
            )

    def test_function_step_cannot_hide_top_level_declaration_work(self):
        with self.assertRaisesMessage(ValueError, "top-level declarations"):
            self._validate(
                _state(),
                [
                    _step(
                        purpose="Define card constants and return the initial state.",
                    )
                ],
            )

    def test_grouped_declarations_are_split_into_ordered_steps(self):
        steps = self._validate(
            _state(),
            [
                _step(
                    symbol="CardColor",
                    symbol_kind="declaration",
                    purpose="Declare enum CardColor.",
                    acceptance_criteria=[
                        "Enum CardColor contains four colors.",
                        "Enum CardRank contains card ranks.",
                    ],
                )
            ],
        )

        self.assertEqual([step["target"] for step in steps], ["CardColor", "CardRank"])
        self.assertEqual(steps[1]["depends_on"], [steps[0]["id"]])

    def test_mixed_domain_plan_defers_pseudo_types_and_stateful_steps(self):
        state = _state()
        state["components"] = ["godot.card_system"]
        steps = self._validate(
            state,
            [
                _step(symbol="COLORS", symbol_kind="declaration"),
                _step(
                    id="card_alias",
                    symbol="Card",
                    symbol_kind="declaration",
                    purpose="Define a type alias for a typed Dictionary.",
                ),
                _step(
                    id="winner",
                    symbol="check_winner",
                    required_calls=["card_system.hand"],
                ),
            ],
        )

        self.assertEqual([step["target"] for step in steps], ["COLORS"])

    def test_grouped_constant_assignments_are_split(self):
        steps = self._validate(
            _state(),
            [
                _step(
                    symbol="WILD_CARDS",
                    symbol_kind="declaration",
                    purpose="Declare deck counts.",
                    acceptance_criteria=[
                        "Constant WILD_CARDS = 4, WILD_DRAW_FOUR_CARDS = 4."
                    ],
                )
            ],
        )

        self.assertEqual(
            [step["target"] for step in steps],
            ["WILD_CARDS", "WILD_DRAW_FOUR_CARDS"],
        )

    def test_large_collection_declaration_becomes_factory_function(self):
        steps = self._validate(
            _state(),
            [
                _step(
                    symbol="CARD_DEFINITIONS",
                    symbol_kind="declaration",
                    purpose="Create a constant Array describing all 108 cards.",
                    acceptance_criteria=[
                        "Constant CARD_DEFINITIONS is an Array of 108 card records."
                    ],
                    required_dictionary_keys=["id", "color"],
                )
            ],
        )

        self.assertEqual(steps[0]["target"], "create_card_definitions")
        self.assertEqual(steps[0]["operation"], "insert_function")
        self.assertEqual(steps[0]["symbol_kind"], "function")

    def test_declaration_prose_does_not_treat_array_as_a_symbol(self):
        steps = self._validate(
            _state(),
            [
                _step(
                    symbol="UNO_COLORS",
                    symbol_kind="declaration",
                    purpose="Declare a typed constant array of UNO colors.",
                    acceptance_criteria=[
                        'UNO_COLORS is a const Array[String] containing "red", "green", "blue", and "yellow".'
                    ],
                )
            ],
        )
        self.assertIn('"green"', steps[0]["acceptance_criteria"][0])

    def test_dictionary_property_examples_are_not_treated_as_calls(self):
        self._validate(
            _state(),
            [
                _step(
                    purpose="Compare card.color with top.color.",
                    acceptance_criteria=["Returns true when card.value equals top.value."],
                    required_dictionary_keys=["color", "value"],
                )
            ],
        )

    def test_invoked_method_in_acceptance_requires_declared_call(self):
        with self.assertRaisesMessage(ValueError, "mentions required calls"):
            self._validate(
                _state(),
                [
                    _step(
                        purpose="Advance with turn_system.advance().",
                    )
                ],
            )

    def test_declaration_step_rejects_gdscript_type_alias_plan(self):
        with self.assertRaisesMessage(ValueError, "does not support"):
            self._validate(
                _state(),
                [
                    _step(
                        symbol="Card",
                        symbol_kind="declaration",
                        purpose="Define a typed Dictionary Card alias.",
                    )
                ],
            )

    def test_unknown_component_method_is_rejected_during_planning(self):
        state = _state()
        state["components"] = ["godot.card_system"]
        with self.assertRaisesMessage(ValueError, "undocumented GameGen API"):
            self._validate(
                state,
                [_step(required_calls=["card_system.hallucinate_cards"])],
            )

    def test_unknown_component_method_reports_documented_alternatives(self):
        state = _state()
        state["components"] = ["godot.card_system"]
        with self.assertRaisesMessage(ValueError, "card_system.draw_many_to_hand()"):
            self._validate(
                state,
                [_step(required_calls=["card_system.draw_cards"])],
            )

    def test_plan_must_choose_one_of_two_alternative_component_calls(self):
        state = _state()
        state["components"] = ["godot.card_system"]
        with self.assertRaisesMessage(ValueError, "marks both required"):
            self._validate(
                state,
                [
                    _step(
                        acceptance_criteria=[
                            "Use card_system.draw_to_hand() or "
                            "card_system.draw_many_to_hand() for the draw."
                        ],
                        required_calls=[
                            "card_system.draw_to_hand",
                            "card_system.draw_many_to_hand",
                        ],
                    )
                ],
            )

    def test_missing_declaration_resolves_to_declaration_insertion(self):
        steps = self._validate(
            _state(),
            [_step(symbol="rules", symbol_kind="declaration")],
        )
        self.assertEqual(steps[0]["operation"], "insert_declaration")

    def test_later_step_can_extend_file_created_by_earlier_step(self):
        path = "project/game/rules.gd"
        steps = self._validate(
            _state(),
            [
                _step(id="can_play", path=path, symbol="can_play"),
                _step(
                    id="winner",
                    path=path,
                    symbol="find_winner",
                    depends_on=["can_play"],
                ),
            ],
            sources={},
        )

        self.assertEqual(steps[0]["operation"], "create_file")
        self.assertEqual(steps[1]["operation"], "insert_function")

    def test_initialization_plan_must_configure_selected_stateful_components(self):
        state = _state()
        state["components"] = ["godot.turn_system", "godot.card_system"]
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        with self.assertRaisesMessage(ValueError, "configure every selected stateful"):
            self._validate(
                state,
                [
                    _step(
                        id="initialize_match",
                        symbol="initialize_game_state",
                        purpose="Create deterministic starting state.",
                    )
                ],
            )

    def test_initialization_plan_requires_real_foundation_hook(self):
        state = _state()
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        with self.assertRaisesMessage(ValueError, "initialize_game_state"):
            self._validate(
                state,
                [_step(id="setup_helper", symbol="setup_helper")],
            )

    def test_later_stage_defers_repeated_component_initialization_helper(self):
        state = _state()
        state["components"] = ["godot.turn_system", "godot.card_system"]
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_blueprint"]["stages"][1]["status"] = "completed"

        steps = self._validate(
            state,
            [
                _step(
                    id="configure_systems",
                    symbol="setup_game",
                    purpose="Configure systems and create the starting state.",
                    required_calls=[
                        "turn_system.configure",
                        "card_system.configure",
                        "card_system.start_discard",
                    ],
                ),
                _step(
                    id="handle_actions",
                    symbol="handle_game_action",
                    purpose="Apply legal player actions and advance the turn.",
                    depends_on=["configure_systems"],
                    required_calls=[
                        "card_system.discard_from_hand",
                        "turn_system.advance",
                    ],
                ),
            ],
        )

        self.assertEqual([step["target"] for step in steps], ["handle_game_action"])
        self.assertEqual(steps[0]["depends_on"], [])
        self.assertNotIn("setup_game", steps[0]["required_calls"])

    def test_later_stage_rejects_component_initialization_inside_required_hook(self):
        state = _state()
        state["components"] = ["godot.turn_system"]
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_blueprint"]["stages"][1]["status"] = "completed"

        with self.assertRaisesMessage(ValueError, "belongs to the 'initialization' stage"):
            self._validate(
                state,
                [
                    _step(
                        id="handle_actions",
                        symbol="handle_game_action",
                        required_calls=["turn_system.configure", "turn_system.advance"],
                    )
                ],
            )

    def test_gameplay_step_splits_large_component_orchestration(self):
        state = _state()
        state["components"] = ["godot.turn_system", "godot.card_system"]
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_blueprint"]["stages"][1]["status"] = "completed"

        with self.assertRaisesMessage(ValueError, "distinct stateful component methods"):
            self._validate(
                state,
                [
                    _step(
                        id="effects",
                        symbol="apply_effects",
                        required_calls=[
                            "turn_system.advance",
                            "turn_system.skip",
                            "turn_system.reverse",
                            "card_system.draw_many_to_hand",
                            "card_system.discard_from_hand",
                        ],
                    ),
                    _step(id="handle", symbol="handle_game_action"),
                ],
                sources={
                    "project/game/main.gd": self.GAME_SOURCE
                    + "func handle_game_action(_action: Dictionary) -> bool:\n\treturn false\n"
                },
            )

    def test_replan_omits_already_promoted_stage_target(self):
        state = _state()
        state["game_blueprint"]["stages"][0]["status"] = "completed"
        state["game_blueprint"]["stages"][1]["status"] = "completed"
        state["completed_stages"] = [{"name": "core_gameplay:is_card_playable"}]
        source = self.GAME_SOURCE + (
            "func is_card_playable(_card: Dictionary) -> bool:\n\treturn true\n"
            "func handle_game_action(_action: Dictionary) -> bool:\n\treturn false\n"
        )

        steps = self._validate(
            state,
            [
                _step(
                    id="playable",
                    symbol="is_card_playable",
                ),
                _step(
                    id="handle",
                    symbol="handle_game_action",
                    depends_on=["playable"],
                    required_calls=["is_card_playable"],
                ),
            ],
            sources={"project/game/main.gd": source},
        )

        self.assertEqual([step["target"] for step in steps], ["handle_game_action"])
        self.assertEqual(steps[0]["depends_on"], [])

    def test_stage_plan_consumes_redundant_protected_foundation_steps(self):
        state = _state()
        for stage in state["game_blueprint"]["stages"][:3]:
            stage["status"] = "completed"
        source = self.GAME_SOURCE + (
            "signal game_reset\n"
            "func reset_game() -> void:\n\tpass\n"
            "func evaluate_game_state() -> Dictionary:\n\treturn {}\n"
        )

        steps = self._validate(
            state,
            [
                _step(
                    id="reset_signal",
                    symbol="game_reset",
                    symbol_kind="declaration",
                ),
                _step(
                    id="reset_function",
                    symbol="reset_game",
                    depends_on=["reset_signal"],
                ),
                _step(
                    id="outcome",
                    symbol="evaluate_game_state",
                    depends_on=["reset_function"],
                ),
            ],
            sources={"project/game/main.gd": source},
        )

        self.assertEqual([step["target"] for step in steps], ["evaluate_game_state"])
        self.assertEqual(steps[0]["depends_on"], [])

    def test_replan_preserves_files_and_resets_only_active_stage_plan(self):
        state = _state()
        state["files"] = {"project/game/rules.gd": "hash"}
        steps = self._validate(state, [_step()])
        freeze_stage_plan(state, plan_summary="Define rules.", steps=steps)

        reset_active_stage_for_replan(state, errors=["contradictory step"])

        stage = next_stage(state)
        self.assertEqual(stage["id"], "domain_model")
        self.assertEqual(stage["status"], "needs_plan")
        self.assertEqual(stage["steps"], [])
        self.assertEqual(state["files"], {"project/game/rules.gd": "hash"})
        self.assertEqual(state["phase"], "needs_stage_plan")
        self.assertEqual(state["stage_replan_counts"], {"domain_model": 1})
        with self.assertRaisesMessage(ValueError, "repeats a plan that already failed"):
            self._validate(state, [_step()])

    def test_uno_like_plan_can_reach_every_playable_hook_without_conflicts(self):
        state = _state()
        state["components"] = ["godot.turn_system", "godot.card_system"]
        main_source = """extends Control
func initialize_game_state() -> void:
	pass
func handle_game_action(_action: Dictionary) -> bool:
	return false
func evaluate_game_state() -> Dictionary:
	return {}
func render_game() -> void:
	pass
func smoke_test_gameplay() -> Dictionary:
	return {"ok": false}
"""

        domain_steps = validate_stage_plan(
            state,
            plan_summary="Define cards and matching rules.",
            steps=[
                _step(
                    id="create_deck",
                    path="project/game/uno_rules.gd",
                    symbol="create_deck",
                    required_dictionary_keys=["id", "color", "rank"],
                ),
                _step(
                    id="can_play_card",
                    path="project/game/uno_rules.gd",
                    symbol="can_play_card",
                    depends_on=["create_deck"],
                ),
            ],
            sources={},
        )
        freeze_stage_plan(state, plan_summary="Define cards and matching rules.", steps=domain_steps)
        advance_blueprint_after_commit(state)
        advance_blueprint_after_commit(state)

        stage_plans = (
            (
                "Initialize shared systems.",
                _step(
                    id="initialize_match",
                    symbol="initialize_game_state",
                    required_calls=[
                        "card_system.configure",
                        "card_system.deal",
                        "card_system.start_discard",
                        "turn_system.configure",
                    ],
                ),
            ),
            (
                "Implement legal actions.",
                _step(
                    id="handle_actions",
                    symbol="handle_game_action",
                    required_calls=[
                        "card_system.discard_from_hand",
                        "turn_system.advance",
                    ],
                ),
            ),
            (
                "Evaluate victory.",
                _step(id="evaluate_match", symbol="evaluate_game_state"),
            ),
            (
                "Render interactive controls.",
                _step(id="render_match", symbol="render_game"),
            ),
            (
                "Verify legal play and victory.",
                _step(id="smoke_test", symbol="smoke_test_gameplay"),
            ),
        )
        for summary, raw_step in stage_plans:
            normalized = validate_stage_plan(
                state,
                plan_summary=summary,
                steps=[raw_step],
                sources={"project/game/main.gd": main_source},
            )
            freeze_stage_plan(state, plan_summary=summary, steps=normalized)
            advance_blueprint_after_commit(state)

        self.assertTrue(generation_is_complete(state))
        self.assertEqual(state["phase"], "ready_to_build")
