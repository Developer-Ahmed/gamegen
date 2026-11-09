from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from agent.tools import (
    CANDIDATE_GAME_FILES_ROOT,
    GAME_FILES_ROOT,
    PROMOTING_GAME_FILES_ROOT,
    _application_change_summary,
    _candidate_validation_failure,
    _promote_validated_candidate,
    _repair_candidate_formatting,
)


class _Files:
    def __init__(self, files):
        self.values = dict(files)

    def read(self, path):
        return self.values[path]

    def write(self, path, content):
        self.values[path] = content


class _Sandbox:
    def __init__(self, files):
        self.files = _Files(files)
        self.commands = SimpleNamespace(run=self.run)

    def run(self, command, **_kwargs):
        if command.startswith("rm -rf /home/user/game_files_candidate"):
            for path in [path for path in self.files.values if path.startswith(CANDIDATE_GAME_FILES_ROOT)]:
                del self.files.values[path]
        if "cp -a /home/user/game_files_candidate /home/user/game_files_promoting" in command:
            candidate_prefix = f"{CANDIDATE_GAME_FILES_ROOT}/"
            promoting_prefix = f"{PROMOTING_GAME_FILES_ROOT}/"
            for path, content in list(self.files.values.items()):
                if path.startswith(candidate_prefix):
                    self.files.values[promoting_prefix + path.removeprefix(candidate_prefix)] = content
        if "mv /home/user/game_files_promoting /home/user/game_files" in command:
            active_prefix = f"{GAME_FILES_ROOT}/"
            promoting_prefix = f"{PROMOTING_GAME_FILES_ROOT}/"
            for path in [path for path in self.files.values if path.startswith(active_prefix)]:
                del self.files.values[path]
            for path, content in list(self.files.values.items()):
                if path.startswith(promoting_prefix):
                    self.files.values[active_prefix + path.removeprefix(promoting_prefix)] = content
                    del self.files.values[path]
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


def _state(candidate_failure_count=0):
    return {
        "phase": "candidate_needs_validation",
        "workspace_revision": 3,
        "last_valid_revision": 3,
        "validated_revision": 3,
        "candidate_revision": "candidate-1",
        "candidate_base_revision": 3,
        "candidate_changed_paths": ["project/game/main.gd"],
        "candidate_files": {"project/game/main.gd": "candidate-hash"},
        "candidate_stage_name": "rules",
        "candidate_stage_targets": ["setup"],
        "candidate_change_summary": "implement rules",
        "candidate_failure_count": candidate_failure_count,
        "repair_packet": None,
        "last_validation_fingerprint": "",
        "read_snapshots": {},
        "read_coverage": {},
        "files": {"project/game/main.gd": "valid-hash"},
        "gameplay_edit_count": 0,
    }


class CandidateRevisionTests(SimpleTestCase):
    def test_failed_candidate_is_retained_and_valid_workspace_is_untouched(self):
        active_path = f"{GAME_FILES_ROOT}/project/game/main.gd"
        candidate_path = f"{CANDIDATE_GAME_FILES_ROOT}/project/game/main.gd"
        sandbox = _Sandbox(
            {
                active_path: "extends Node\nfunc setup() -> void:\n\tpass\n",
                candidate_path: "extends Node\nfunc setup() -> void:\n\tvar data := {\n",
            }
        )
        state = _state()

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.tools._repair_status", return_value={"repair_attempts": 1}),
            patch("agent.tools.update_execution_state", side_effect=update),
        ):
            result = _candidate_validation_failure(
                config={"configurable": {"thread_id": "chat"}},
                sandbox=sandbox,
                state=deepcopy(state),
                errors=["Parse Error at: res://game/main.gd:3:15"],
                output="",
                exit_code=1,
            )

        self.assertEqual(result["phase"], "repair")
        self.assertEqual(state["phase"], "repair")
        self.assertIn(candidate_path, sandbox.files.values)
        self.assertIn("\tpass", sandbox.files.values[active_path])
        self.assertEqual(result["repair_packet"]["scope"]["name"], "setup")

    def test_third_failure_discards_candidate_and_keeps_last_valid_revision(self):
        active_path = f"{GAME_FILES_ROOT}/project/game/main.gd"
        candidate_path = f"{CANDIDATE_GAME_FILES_ROOT}/project/game/main.gd"
        sandbox = _Sandbox({active_path: "valid", candidate_path: "broken"})
        state = _state(candidate_failure_count=2)

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with (
            patch("agent.tools._repair_status", return_value={"repair_attempts": 3}),
            patch("agent.tools.update_execution_state", side_effect=update),
        ):
            result = _candidate_validation_failure(
                config={"configurable": {"thread_id": "chat"}},
                sandbox=sandbox,
                state=deepcopy(state),
                errors=["Parse Error at: res://game/main.gd:1:1"],
                output="",
                exit_code=1,
            )

        self.assertEqual(result["phase"], "candidate_rolled_back")
        self.assertIsNone(state["candidate_revision"])
        self.assertEqual(state["workspace_revision"], 3)
        self.assertEqual(sandbox.files.values[active_path], "valid")
        self.assertEqual(state["stage_retry_directive"]["stage_name"], "rules")
        self.assertIn("materially different", result["instruction"])
        self.assertEqual(state["blocked_stage_strategies"][0]["target"], "setup")

    def test_formatting_repair_updates_candidate_content_and_hash(self):
        candidate_path = f"{CANDIDATE_GAME_FILES_ROOT}/project/game/main.gd"
        source = "extends Node\nfunc setup() -> void:\n\t    if true:\n\t        pass\n"
        sandbox = _Sandbox({candidate_path: source})
        state = _state()

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with patch("agent.tools.update_execution_state", side_effect=update):
            updated = _repair_candidate_formatting(sandbox, "chat", deepcopy(state))

        self.assertIsNotNone(updated)
        self.assertIn("\t\tif true:\n\t\t\tpass", sandbox.files.values[candidate_path])
        self.assertNotEqual(state["candidate_revision"], "candidate-1")
        self.assertNotEqual(state["candidate_files"]["project/game/main.gd"], "candidate-hash")

    def test_application_generates_repair_summary_from_diagnostic(self):
        state = {
            "phase": "repair",
            "repair_packet": {
                "scope": {"name": "create_deck"},
                "diagnostic": {"message": "Parse Error: expected statement"},
            },
        }

        summary = _application_change_summary(state, "implement_create_deck", ("create_deck",))

        self.assertEqual(summary, "Repair create_deck: Parse Error: expected statement")

    def test_promotion_commits_candidate_as_new_valid_revision(self):
        active_path = f"{GAME_FILES_ROOT}/project/game/main.gd"
        candidate_path = f"{CANDIDATE_GAME_FILES_ROOT}/project/game/main.gd"
        sandbox = _Sandbox({active_path: "valid", candidate_path: "candidate"})
        state = _state()

        def update(_thread_id, updater):
            updater(state)
            return deepcopy(state)

        with patch("agent.tools.update_execution_state", side_effect=update):
            revision = _promote_validated_candidate(sandbox, "chat", deepcopy(state))

        self.assertEqual(revision, 4)
        self.assertEqual(state["validated_revision"], 4)
        self.assertEqual(state["last_valid_revision"], 4)
        self.assertIsNone(state["candidate_revision"])
        self.assertEqual(sandbox.files.values[active_path], "candidate")
