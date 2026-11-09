from django.test import SimpleTestCase

from agent.source_structure import (
    StructuralEditError,
    canonicalize_gdscript_indentation,
    enclosing_scope,
    insert_function,
    insert_declaration,
    replace_declaration,
    replace_function_body,
    replace_scene_node,
)


class SourceStructureTests(SimpleTestCase):
    def test_function_body_replacement_preserves_neighbors_and_indentation(self):
        source = """extends Node

func first() -> void:
	pass

func second() -> int:
	return 2
"""
        updated, span = replace_function_body(
            source,
            "first",
            "var value := 1\nreturn",
        )

        self.assertEqual(span.name, "first")
        self.assertIn("\tvar value := 1\n\treturn\n", updated)
        self.assertIn("func second() -> int:\n\treturn 2", updated)

    def test_function_body_replacement_canonicalizes_space_indentation(self):
        source = "extends Node\nfunc build() -> void:\n\tpass\n"
        updated, _ = replace_function_body(
            source,
            "build",
            "    if true:\n        var value := 1\n        print(value)",
        )

        self.assertIn("\tif true:\n\t\tvar value := 1\n\t\tprint(value)\n", updated)
        self.assertNotRegex(updated, r"(?m)^\t +\S")

    def test_function_body_replacement_accepts_matching_complete_function(self):
        source = "extends Node\nfunc build() -> void:\n\tpass\n"
        updated, _ = replace_function_body(
            source,
            "build",
            "func build() -> void:\n    return",
        )
        self.assertIn("func build() -> void:\n\treturn\n", updated)

    def test_function_body_replacement_rejects_signature_change(self):
        source = "extends Node\nfunc build() -> void:\n\tpass\n"
        with self.assertRaisesRegex(StructuralEditError, "preserve its exact signature"):
            replace_function_body(
                source,
                "build",
                "func build(value: int) -> void:\n    return",
            )

    def test_insert_function_canonicalizes_complete_function(self):
        updated, _ = insert_function(
            "extends Node\n",
            "",
            "func build() -> void:\n    if true:\n        pass",
        )

        self.assertIn("func build() -> void:\n\tif true:\n\t\tpass\n", updated)

    def test_whole_file_format_repair_converts_mixed_indentation(self):
        source = "extends Node\nfunc build() -> void:\n\t    if true:\n\t        pass\n"
        normalized = canonicalize_gdscript_indentation(source)

        self.assertEqual(
            normalized,
            "extends Node\nfunc build() -> void:\n\t\tif true:\n\t\t\tpass\n",
        )

    def test_two_space_model_indentation_is_canonicalized(self):
        updated, _ = replace_function_body(
            "extends Node\nfunc build() -> void:\n\tpass\n",
            "build",
            "if true:\n  pass",
        )

        self.assertIn("\tif true:\n\t\tpass\n", updated)

    def test_protected_lifecycle_function_cannot_be_replaced(self):
        with self.assertRaisesRegex(StructuralEditError, "protected"):
            replace_function_body("func _ready() -> void:\n\tpass\n", "_ready", "pass")

    def test_declaration_replacement_requires_same_symbol(self):
        with self.assertRaisesRegex(StructuralEditError, "same name"):
            replace_declaration("var score := 0\n", "score", "var lives := 3")

    def test_insert_declaration_places_one_named_declaration_before_functions(self):
        updated, span = insert_declaration(
            "extends Node\n\nfunc play() -> void:\n\tpass\n",
            "colors",
            'const colors := ["red", "blue"]',
        )

        self.assertEqual(span.name, "colors")
        self.assertLess(updated.index("const colors"), updated.index("func play"))

    def test_insert_declaration_supports_named_godot_enum(self):
        updated, span = insert_declaration(
            "extends Node\n\nfunc play() -> void:\n\tpass\n",
            "CardColor",
            "enum CardColor { RED, GREEN, BLUE, YELLOW }",
        )

        self.assertEqual(span.name, "CardColor")
        self.assertLess(updated.index("enum CardColor"), updated.index("func play"))

    def test_foundation_declaration_cannot_be_replaced(self):
        with self.assertRaisesRegex(StructuralEditError, "protected"):
            replace_declaration(
                "var card_system := GamegenCardSystem.new()\n",
                "card_system",
                "var card_system := null",
            )

    def test_scene_node_replacement_is_bounded_to_named_section(self):
        source = """[gd_scene format=3]

[node name="Root" type="Control"]
visible = true

[node name="Label" type="Label" parent="."]
text = "old"
"""
        updated, _ = replace_scene_node(
            source,
            "Root/Label",
            "[node name=\"Label\" type=\"Label\" parent=\".\"]\ntext = \"new\"",
        )
        self.assertIn('text = "new"', updated)
        self.assertIn("visible = true", updated)

    def test_error_line_maps_to_enclosing_function(self):
        source = """extends Node
func setup() -> void:
	var data := {
		"ok": false,
	}
"""
        scope = enclosing_scope(source, 4, path="project/game/main.gd")
        self.assertEqual(scope.kind, "function")
        self.assertEqual(scope.name, "setup")
