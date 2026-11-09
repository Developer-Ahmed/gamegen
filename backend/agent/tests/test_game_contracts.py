from django.test import SimpleTestCase

from agent.game_contracts import (
    compact_game_contracts,
    record_validated_step_contract,
    validate_plan_against_contracts,
)


class GameContractTests(SimpleTestCase):
    def setUp(self):
        self.state = {"game_contracts": {}}
        self.source = (
            "extends Node\n"
            "func create_deck() -> Array[Dictionary]:\n"
            "\treturn [{\"id\": \"red_0\", \"color\": \"red\", \"rank\": \"0\", "
            "\"type\": \"number\"}]\n"
        )
        record_validated_step_contract(
            self.state,
            step={
                "id": "create_deck",
                "path": "project/game/main.gd",
                "target": "create_deck",
                "symbol_kind": "function",
                "purpose": "Create the canonical game deck.",
                "acceptance_criteria": ["Returns structured cards."],
                "required_calls": [],
                "required_dictionary_keys": ["id", "color", "rank", "type"],
            },
            stage_id="domain_model",
            revision=2,
            sources={"project/game/main.gd": self.source},
        )

    def test_records_validated_symbol_and_dictionary_shape(self):
        contracts = compact_game_contracts(self.state)
        self.assertEqual(contracts[0]["symbol"], "create_deck")
        self.assertEqual(contracts[0]["owner_stage"], "domain_model")
        self.assertEqual(contracts[0]["dictionary_keys"], ["color", "id", "rank", "type"])

    def test_later_stage_cannot_silently_rewrite_validated_symbol(self):
        errors = validate_plan_against_contracts(
            self.state,
            stage_id="initialization",
            steps=[
                {
                    "path": "project/game/main.gd",
                    "symbol": "create_deck",
                    "required_dictionary_keys": ["id", "color", "rank", "type"],
                }
            ],
        )
        self.assertIn("Reuse it instead of redefining it", errors[0])

    def test_compatible_amendment_must_preserve_established_keys(self):
        errors = validate_plan_against_contracts(
            self.state,
            stage_id="initialization",
            steps=[
                {
                    "path": "project/game/main.gd",
                    "symbol": "create_deck",
                    "amends_contract": True,
                    "amendment_reason": "Add deterministic metadata required by initialization.",
                    "required_dictionary_keys": ["id", "color"],
                }
            ],
        )
        self.assertIn("Preserve these keys", errors[0])

    def test_duplicate_factory_role_is_rejected(self):
        errors = validate_plan_against_contracts(
            self.state,
            stage_id="initialization",
            steps=[
                {
                    "path": "project/game/main.gd",
                    "symbol": "create_uno_deck",
                    "required_dictionary_keys": ["id", "color", "rank", "type"],
                }
            ],
        )
        self.assertIn("duplicates the established 'create_deck'", errors[0])
