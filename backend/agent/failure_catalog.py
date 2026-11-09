"""Stable failure taxonomy shared by prompts and validation feedback."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class GenerationPitfall:
    code: str
    prevention: str
    repair: str
    error_patterns: tuple[str, ...] = ()


PITFALLS = (
    GenerationPitfall(
        "SOURCE_PREFIX",
        "Copy only source text; display line numbers and separators are metadata.",
        "Reread the clean source and resend replacement text with every '<number> |' prefix removed.",
        (r"display line", r"unexpected integer", r"expected statement"),
    ),
    GenerationPitfall(
        "STALE_REVISION",
        "Use the current file SHA-256 and only ranges present in editable_sources.",
        "Use the newest source revision; never reconstruct old_text from memory.",
        (r"stale edit", r"sha-?256", r"latest.*read"),
    ),
    GenerationPitfall(
        "SCENE_SCRIPT_MIXUP",
        "Put GDScript in .gd files and declarative [gd_scene] records in .tscn files.",
        "Move code into a .gd script and reference it from the scene with ExtResource.",
        (r"error parsing", r"failed loading resource", r"gd_scene"),
    ),
    GenerationPitfall(
        "STEP_SCOPE",
        "Implement only the one file and symbol selected by the active stage step.",
        "Regenerate the requested function or declaration alone; move every other symbol to a later planned step.",
        (r"step_scope", r"out_of_scope", r"only function", r"only declaration"),
    ),
    GenerationPitfall(
        "STAGE_OWNERSHIP",
        "Implement each foundation hook only in its owning blueprint stage and in project/game/main.gd.",
        "Move the behavior into a stage-local helper, or replan it under the hook's owning stage.",
        (r"belongs to stage", r"foundation hook", r"must update project/game/main.gd"),
    ),
    GenerationPitfall(
        "MISSING_DICTIONARY_KEY",
        "List only literal Dictionary keys in required_dictionary_keys and emit each as a quoted key.",
        "Add the exact missing quoted key to the active function's Dictionary without adding unrelated declarations.",
        (r"missing_dictionary_key", r"dictionary key"),
    ),
    GenerationPitfall(
        "INVALID_GDSCRIPT_BASE",
        "A .gd file extends a real Godot class or project script, never Godot or a component registry id.",
        "Use a valid base such as RefCounted, Node, Control, or an exact res:// script path.",
        (r"invalid gdscript base", r"extends Godot", r"Godot\."),
    ),
    GenerationPitfall(
        "GODOT3_API",
        "Use Godot 4.7 syntax: @export, instantiate(), await, and typed declarations.",
        "Replace the obsolete Godot 3 API with the documented Godot 4.7 equivalent.",
        (r"nonexistent function.*instance", r"unexpected.*export", r"yield"),
    ),
    GenerationPitfall(
        "PARSE_STRUCTURE",
        "Keep GDScript indentation, colons, quotes, and brackets balanced; edit complete logical blocks.",
        "Read the reported function, repair the smallest complete block, and preserve tab indentation.",
        (r"parse error", r"expected.*after", r"unexpected", r"indent"),
    ),
    GenerationPitfall(
        "INVENTED_COMPONENT_API",
        "Call only methods and fields listed in the selected GameGen component contract.",
        "Replace the invented call with the exact contract method; never use get_instance or undocumented fields.",
        (r"nonexistent function", r"invalid call", r"get_instance"),
    ),
    GenerationPitfall(
        "INVENTED_COMPONENT_PROPERTY",
        "Private GameGen components expose only their documented methods and signals.",
        "Replace direct property access with an exact documented query method from the component contract.",
        (r"invented_component_property", r"undocumented propert"),
    ),
    GenerationPitfall(
        "COMPONENT_SHAPE",
        "Follow component data_contracts exactly; card and item definitions are dictionaries, not labels.",
        "Rebuild the argument using the required dictionary fields and unique IDs from the contract.",
        (r"every card must be a dictionary", r"requires.*id", r"component contract"),
    ),
    GenerationPitfall(
        "LOCAL_COMPONENT",
        "Use the prewired foundation members such as card_system and turn_system.",
        "Remove the local constructor and use the persistent member owned by the scene root.",
        (r"local.*component", r"duplicate.*Gamegen", r"not declared in the current scope"),
    ),
    GenerationPitfall(
        "UNCONFIGURED_COMPONENT",
        "Check configure() success before calling component operations.",
        "Configure the existing member once with valid data before deal, draw, turn, or inventory calls.",
        (r"not configured", r"configuration failed"),
    ),
    GenerationPitfall(
        "DISCARD_NOT_STARTED",
        "Call card_system.start_discard() after dealing and before reading top_discard().",
        "Initialize the discard pile and handle an empty return before rendering or matching cards.",
        (r"top_discard", r"discard pile"),
    ),
    GenerationPitfall(
        "DOUBLE_INITIAL_DISCARD",
        "start_discard() already moves the opening card onto the discard pile and returns it.",
        "Store or inspect the returned card if needed, but never pass it to discard() again.",
        (r"double_initial_discard", r"start_discard.*already"),
    ),
    GenerationPitfall(
        "DUPLICATE_SYMBOL",
        "Replace only the intended range; do not paste surrounding functions into a narrow edit.",
        "Read the full affected functions and remove duplicate declarations while preserving one implementation.",
        (r"already has a function", r"member.*already exists", r"duplicate"),
    ),
    GenerationPitfall(
        "TYPE_MISMATCH",
        "Use explicit Godot types and match method parameter and return contracts.",
        "Read the reported declaration and convert or annotate the value with the exact expected type.",
        (r"cannot assign", r"cannot pass", r"type.*mismatch", r"cannot infer"),
    ),
    GenerationPitfall(
        "UNDECLARED_IDENTIFIER",
        "Declare every member, parameter, and local before assignment or use.",
        "Use the exact enclosing function and add the smallest correctly scoped declaration.",
        (r"identifier.*not declared", r"not declared in the current scope", r"undeclared"),
    ),
    GenerationPitfall(
        "CONST_REASSIGNMENT",
        "Top-level const declarations are read-only values, including constants used as dictionary keys.",
        "Assign writable state such as game_state[STATE_KEY] instead of assigning STATE_KEY itself.",
        (r"const_reassignment", r"assign.*constant", r"constant.*read-only"),
    ),
    GenerationPitfall(
        "STATE_KEY_AS_VALUE",
        "STATE_* constants identify keys in game_state; they are not the current runtime values.",
        "Read or write game_state[STATE_KEY] instead of assigning or comparing the key constant itself.",
        (r"state_key_as_value", r"dictionary key, not live game state"),
    ),
    GenerationPitfall(
        "IGNORED_COMPONENT_RESULT",
        "Check bool and Dictionary results from component operations before continuing.",
        "Capture the result and handle failure without mutating later state.",
        (r"ignored.*result", r"check the return value"),
    ),
    GenerationPitfall(
        "INSUFFICIENT_DECK",
        "Provide enough physical cards for every dealt hand plus the initial discard.",
        "Generate uniquely identified physical cards before configure and verify deal succeeds.",
        (r"insufficient.*deck", r"not enough cards"),
    ),
    GenerationPitfall(
        "CARD_ID_TYPE",
        "GamegenCardSystem requires every physical card id to be a non-empty String.",
        "Build stable unique string IDs, for example with str(counter) or string formatting.",
        (r"card_id_type", r"card.*id.*string", r"non-empty string id"),
    ),
    GenerationPitfall(
        "DICTIONARY_VALUE_DOMAIN",
        "Compare dictionary fields using the exact values emitted by the project's data factory.",
        "Read the generated definitions and replace the impossible literal with its exact stored representation.",
        (r"dictionary_value_domain", r"exact generated representation"),
    ),
    GenerationPitfall(
        "FRAME_STATE_MUTATION",
        "Do not configure systems or advance gameplay from _process every frame.",
        "Move the transition into an explicit input or game-action function.",
        (r"every frame", r"_process.*mutat"),
    ),
    GenerationPitfall(
        "COLLECTION_API",
        "Use Godot 4 Array methods deliberately: pop_at, remove_at, erase, append, and find have different contracts.",
        "Replace the invalid collection call and avoid removing the same card twice.",
        (r"nonexistent function.*pop", r"nonexistent function.*remove", r"invalid.*index"),
    ),
    GenerationPitfall(
        "FLATTENED_HANDS",
        "Keep each player's hand as its own card array; concatenation produces one flat card array.",
        "Check hand(player_id).is_empty() for each player instead of iterating concatenated cards as hands.",
        (r"flattened_hands", r"concatenates card arrays"),
    ),
    GenerationPitfall(
        "INVENTED_NODE_API",
        "Call only documented methods on explicitly typed Godot nodes.",
        "Replace the invented node method with a documented API; remove children with queue_free when clearing a container.",
        (r"invented_node_api", r"no generic clear"),
    ),
    GenerationPitfall(
        "STRING_FORMAT",
        "Use Godot formatting operators or str(); braces inside ordinary strings are not interpolation.",
        "Replace brace placeholders with a valid %, format(), or str() expression.",
    ),
    GenerationPitfall(
        "NODE_PATH",
        "Only reference nodes that exist in main.tscn; prefer unique-name access for foundation nodes.",
        "Align the scene node, unique_name_in_owner setting, and script lookup before using it.",
        (r"node not found", r"invalid get index", r"null instance"),
    ),
    GenerationPitfall(
        "SCENE_RESOURCE_REFERENCE",
        "Every ExtResource/SubResource ID and referenced res:// path must exist and match its declared type.",
        "Correct the resource declaration and reference together; do not invent imported asset paths.",
        (r"extresource", r"subresource", r"cannot open file", r"failed loading resource"),
    ),
    GenerationPitfall(
        "SIGNAL_WIRING",
        "Connect each signal once and use a callable with a compatible signature.",
        "Correct the callback signature and guard against duplicate signal connections.",
        (r"signal.*already connected", r"callable", r"argument count"),
    ),
    GenerationPitfall(
        "TURN_DOUBLE_ADVANCE",
        "One legal action advances the turn exactly once; skip() already includes advancement.",
        "Centralize end-of-turn logic and remove the second advance call.",
        (r"turn", r"advance"),
    ),
    GenerationPitfall(
        "ACTION_CARD_ORDER",
        "Apply card effects, choose required state such as wild color, then advance consistently.",
        "Define one transition function and test skip, reverse, draw penalties, and wild selection separately.",
    ),
    GenerationPitfall(
        "DECK_EXHAUSTION",
        "Handle an empty draw pile and preserve the top discard while recycling.",
        "Use the component recycle behavior and surface a terminal state if no draw is possible.",
    ),
    GenerationPitfall(
        "PRIVATE_HAND_LEAK",
        "For hot-seat games, hide the inactive hand and add an explicit pass-device state.",
        "Render only public counts until the active player confirms their private turn.",
    ),
    GenerationPitfall(
        "STALE_UI",
        "Render UI from authoritative game state after every transition instead of mutating labels ad hoc.",
        "Create one render_state() path and call it after setup, actions, restart, and terminal transitions.",
    ),
    GenerationPitfall(
        "FRAME_LIFECYCLE",
        "Wait for nodes to be ready and one rendered frame before browser readiness or size-dependent checks.",
        "Move access to _ready(), await the documented frame signal when necessary, then run the check.",
        (r"not set after the first playable render", r"before.*ready"),
    ),
    GenerationPitfall(
        "FAKE_SMOKE_TEST",
        "A smoke test must execute legal transitions and assert resulting state; never return literal success.",
        "Build isolated deterministic state, perform actions, verify a terminal path, and return those results.",
        (r"smoke", r"GAMEGEN_READY", r"publish_ready"),
    ),
    GenerationPitfall(
        "PLACEHOLDER_STAGE",
        "A stage is complete only when its required extension function has real behavior.",
        "Replace the foundation pass, empty return, constant false, or failing smoke-test stub with the bounded implementation.",
        (r"placeholder_stage", r"non-functional foundation placeholder"),
    ),
    GenerationPitfall(
        "LIVE_SMOKE_MUTATION",
        "Smoke tests use isolated systems and must not mutate the live match.",
        "Move test setup to local test instances or snapshot and restore all affected state.",
    ),
    GenerationPitfall(
        "MISSING_TERMINAL_STATE",
        "Disable gameplay after victory or exhaustion and provide restart.",
        "Set an explicit terminal state, block actions, announce the result, and reset all systems on restart.",
    ),
    GenerationPitfall(
        "WEB_READY_TOO_EARLY",
        "Publish browser readiness only after the first playable render and a passing smoke test.",
        "Await one rendered frame, run the deterministic test, then publish its real result.",
        (r"GAMEGEN_READY", r"publish_ready"),
    ),
    GenerationPitfall(
        "UNSUPPORTED_WEB_FEATURE",
        "Use the Compatibility renderer, single-threaded Web APIs, local assets, and browser-safe input.",
        "Replace unsupported desktop/threaded behavior with the documented Godot Web alternative.",
        (r"web", r"sharedarraybuffer", r"thread"),
    ),
)


def prompt_catalog() -> str:
    """Render a compact prevention checklist suitable for any model provider."""
    return "\n".join(
        f"- {pitfall.code}: {pitfall.prevention} Repair: {pitfall.repair}"
        for pitfall in PITFALLS
    )


def guidance_for_errors(errors: Iterable[str], *, limit: int = 5) -> list[dict[str, str]]:
    combined = "\n".join(str(error) for error in errors)
    matched: list[GenerationPitfall] = []
    for pitfall in PITFALLS:
        if pitfall.error_patterns and any(
            re.search(pattern, combined, flags=re.IGNORECASE)
            for pattern in pitfall.error_patterns
        ):
            matched.append(pitfall)
    if not matched:
        matched = [
            pitfall
            for pitfall in PITFALLS
            if pitfall.code in {"TYPE_MISMATCH", "SOURCE_PREFIX", "STALE_REVISION"}
        ]
    return [
        {"code": pitfall.code, "repair": pitfall.repair}
        for pitfall in matched[:limit]
    ]
