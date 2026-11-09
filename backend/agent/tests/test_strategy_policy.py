from django.test import SimpleTestCase

from agent.strategy_policy import blocked_strategy_error, strategy_fingerprint


class StrategyPolicyTests(SimpleTestCase):
    def test_fingerprint_ignores_formatting_comments_and_literal_values(self):
        first = """func create_deck() -> Array[Dictionary]:
\tvar cards := []
\tfor value in range(10):
\t\tcards.append({"value": value})
\treturn cards
"""
        second = """func create_deck() -> Array[Dictionary]:
    var cards := [] # Same structure, different formatting.
    for value in range(20):
        cards.append({"rank": value})
    return cards
"""

        self.assertEqual(
            strategy_fingerprint(first, "create_deck"),
            strategy_fingerprint(second, "create_deck"),
        )

    def test_blocked_strategy_is_rejected_for_same_stage(self):
        content = "func create_deck() -> Array:\n\treturn []\n"
        state = {
            "blocked_stage_strategies": [{
                "target": "create_deck",
                "fingerprint": strategy_fingerprint(content, "create_deck"),
            }]
        }

        error = blocked_strategy_error(state, target="create_deck", content=content)

        self.assertIn("materially different", error)

    def test_hash_character_inside_string_is_not_treated_as_comment(self):
        first = 'func color() -> String:\n\treturn "#ffffff"\n'
        second = 'func color() -> String:\n\treturn "#000000"\n'

        self.assertEqual(
            strategy_fingerprint(first, "color"),
            strategy_fingerprint(second, "color"),
        )
