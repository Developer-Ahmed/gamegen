from hashlib import sha256

from django.test import SimpleTestCase

from agent.repair_packets import build_repair_packet


class RepairPacketTests(SimpleTestCase):
    def test_packet_contains_exact_diagnostic_raw_source_and_scope(self):
        source = """extends Node

func setup() -> void:
	var result := {
		"ok": false,
	}
"""
        packet = build_repair_packet(
            state={
                "workspace_revision": 4,
                "candidate_base_revision": 4,
                "candidate_changed_paths": ["project/game/main.gd"],
                "components": [],
            },
            errors=[
                "SCRIPT ERROR: Parse Error: Expected expression as dictionary key. at: res://game/main.gd:5:2"
            ],
            output="",
            read_text=lambda _path: source,
        )

        self.assertEqual(packet["diagnostic"]["line"], 5)
        self.assertEqual(packet["scope"]["name"], "setup")
        self.assertEqual(packet["candidate"]["sha256"], sha256(source.encode()).hexdigest())
        self.assertEqual(packet["allowed_operation"], "replace_function_body")
        self.assertEqual(packet["source_excerpt"]["lines"][0], "func setup() -> void:")
        self.assertNotIn("3 |", "\n".join(packet["source_excerpt"]["lines"]))
        self.assertIn("Dictionary", packet["godot_syntax_reference"]["relevant_fact"])
