import re

from django.test import SimpleTestCase

from agent.component_library import ComponentRegistry


class ComponentQualityTests(SimpleTestCase):
    def setUp(self):
        self.registry = ComponentRegistry()

    def test_all_components_resolve_together_without_file_conflicts(self):
        components = self.registry.list_components()
        resolved = self.registry.resolve([component.name for component in components])
        files = self.registry.read_files(resolved, title="Quality check")

        self.assertEqual(len(resolved), len(components))
        self.assertIn("project/project.godot", files)
        self.assertIn("project/game/main.gd", files)
        self.assertIn("project/game/main.tscn", files)

    def test_manifest_public_methods_exist_in_component_sources(self):
        for component in self.registry.list_components():
            source = "\n".join(
                (self.registry.root / file.source).read_text(encoding="utf-8")
                for file in component.files
                if file.source.endswith(".gd")
            )
            for signature in component.api:
                method_match = re.match(r"(?:\w+\.)?(\w+)\(", signature)
                if not method_match or method_match.group(1) == "new":
                    continue
                method_name = method_match.group(1)
                self.assertIn(
                    f"func {method_name}(",
                    source,
                    f"{component.name} documents missing method {method_name}",
                )

    def test_foundation_is_rule_neutral_and_fails_closed(self):
        files = self.registry.read_files(
            self.registry.resolve(["godot.web_runtime"]),
            title="Broad game",
        )
        script = files["project/game/main.gd"]
        scene = files["project/game/main.tscn"]

        self.assertIn('"ok": false', script)
        self.assertIn("Gameplay smoke test is not implemented", script)
        self.assertIn("func handle_game_action", script)
        self.assertIn("func evaluate_game_state", script)
        self.assertIn("func submit_game_action", script)
        self.assertIn("handle_game_action(action)", script)
        self.assertIn("evaluate_game_state()", script)
        self.assertIn("render_game()", script)
        self.assertIn("func run_isolated_smoke_tests()", script)
        self.assertIn("smoke_test_gameplay()", script)
        self.assertNotRegex(script.lower(), r"\b(uno|blackjack|monopoly)\b")
        self.assertTrue(scene.startswith("[gd_scene"))
        self.assertIn('path="res://game/main.gd"', scene)

    def test_selected_components_are_persistent_foundation_members(self):
        files = self.registry.read_files(
            self.registry.resolve(["godot.turn_system", "godot.card_system"]),
            title="Card game",
        )
        script = files["project/game/main.gd"]
        self.assertIn("var turn_system := GamegenTurnOrder.new()", script)
        self.assertIn("var card_system := GamegenCardSystem.new()", script)
        self.assertNotIn("{{GAMEGEN_COMPONENT_MEMBERS}}", script)

    def test_foundation_normalizes_web_font_hostile_title_punctuation(self):
        files = self.registry.read_files(
            self.registry.resolve(["godot.web_runtime"]),
            title="UNO Hot\u2011Seat \u201cLocal\u201d",
        )

        expected = 'UNO Hot-Seat \\"Local\\"'
        self.assertIn(f'config/name="{expected}"', files["project/project.godot"])
        self.assertIn(f'text = "{expected}"', files["project/game/main.tscn"])

    def test_turn_based_card_set_composes_mechanisms_not_a_finished_game(self):
        recommended = {
            item["name"]: item for item in self.registry.recommended_sets()
        }
        self.assertEqual(
            recommended["turn_based_card"]["components"],
            ["godot.turn_system", "godot.card_system"],
        )
        self.assertNotIn(
            "godot.turn_card_runtime",
            {component.name for component in self.registry.list_components()},
        )

    def test_component_contracts_define_shapes_invariants_and_examples(self):
        for component in self.registry.list_components():
            with self.subTest(component=component.name):
                self.assertTrue(component.data_contracts)
                self.assertTrue(component.invariants)
                self.assertTrue(component.examples)
                documented_methods = {
                    match.group(1)
                    for signature in component.api
                    if (match := re.match(r"(?:\w+\.)?(\w+)\(", signature))
                    and match.group(1) != "new"
                }
                self.assertEqual(documented_methods, set(component.method_contracts))
                for contract in component.method_contracts.values():
                    self.assertIn("signature", contract)
                    self.assertIn("returns", contract)
                    self.assertIn("preconditions", contract)
                    self.assertIn("side_effects", contract)
                    self.assertIn("non_effects", contract)
