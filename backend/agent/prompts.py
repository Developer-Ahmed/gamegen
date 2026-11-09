"""System prompt for the Godot game-generation agent."""

import json

from .component_library import ComponentRegistry


_REGISTRY = ComponentRegistry()
AVAILABLE_COMPONENTS = [
    {
        "name": component.name,
        "description": component.description,
        "use_when": list(component.use_when),
    }
    for component in _REGISTRY.list_components()
]
AVAILABLE_COMPONENTS_PROMPT = json.dumps(
    {
        "components": AVAILABLE_COMPONENTS,
        "recommended_sets": _REGISTRY.recommended_sets(),
    },
    ensure_ascii=True,
    separators=(",", ":"),
)

SYSTEM_PROMPT_TEMPLATE = """
Build complete browser games in Godot 4.7. Application state is authoritative. Return exactly one
tool call per turn and use only `allowed_tools`.

## Workflow
1. Call `create_game_workspace` once with the minimal reusable components and a concise whole-game
   design. The application creates a universal blueprint for domain model, initialization, core
   gameplay, game flow, presentation/interaction, and verification.
   Select mechanisms, not finished games. For a turn-based card game, normally select
   `godot.turn_system` and `godot.card_system`; add inventory only when the game has a separate item
   inventory. You must author and connect the requested game's deck, rules, actions, effects, win
   conditions, tests, and presentation. No UNO or other game recipe is supplied by the backend.
2. The application researches pinned official docs/demos, reads exact component and foundation
   source, validates, commits, rolls back, and routes tools. Never repeat mechanical work.
3. When `plan_game_stage` is allowed, plan only `next_stage`. Backend stages come before UI. List
   the exact small ordered functions, declarations, scene sections, component extensions, or new
   files that stage needs. Choose any
   useful symbol names and project structure; there is no fixed function-category menu. Each step
   names one path, symbol, and structural `symbol_kind` (function, declaration, scene_node, or file),
   plus its dependencies, purpose, and testable acceptance criteria. New `.gd` files must begin with
   one named function or declaration; plan later symbols as later steps. Declare
   every exact call required to prove those criteria in `required_calls`. Put only quoted Dictionary
   keys in `required_dictionary_keys`; function, variable, constant, member, and component names do
   not belong there. The application parses current source and chooses insertion, replacement, and
   placement mechanics. Never provide edit operations or insertion anchors.
   Foundation hooks belong to their named stage: `initialize_game_state` to initialization,
   `handle_game_action` to core gameplay, `evaluate_game_state` to game flow, `render_game` to
   presentation, and `smoke_test_gameplay` to verification. Never use one as a helper in another
   stage. Each of those stages must plan its hook, with prerequisite helpers placed earlier.
   Components are copied into this project, so a plan may extend one after it has been read and
   understood; preserve existing public behavior and signatures and explain the additive extension.
   `established_game_contracts` are validated cross-stage decisions. Reuse their symbols and data
   shapes. Never create a second producer for the same role. A compatible extension requires
   `amends_contract=true`, a concrete `amendment_reason`, and preservation of existing keys/calls.
4. When `implement_stage_step` is allowed, implement only `active_step_context` and obey its
   `output_contract`. Send source text
   only. The application owns path, operation, target, SHA, stage metadata, and validation. It will
   reject code missing the step's required calls/state evidence or selected-component initialization,
   and will not advance until the candidate passes semantic checks and Godot 4.7 validation.
5. During `step_correction`, regenerate only the rejected active step from the exact errors; do not
   repeat the rejected strategy. After three rejected versions, the application discards the frozen
   stage plan and requests a materially different plan. During repair, send only the complete
   corrected scope requested by `repair_packet`. Always obey `stage_retry_directive`.
6. After every stage and step completes, build once and run. Claim success only from a real
   `run_game` play URL.

## GameGen component contract
{AVAILABLE_COMPONENTS}

- Every `Gamegen*` class is private code written by GameGen, not an official Godot API.
- Selected contracts define signatures, returns, preconditions, side effects, and non-effects.
  Existing component behavior and signatures are immutable. Reuse foundation members; only isolated
  tests may create local systems.
- Components provide generic mechanisms. Keep game-specific rules in generated game files unless a
  frozen stage plan explicitly extends the project-local component copy with reusable mechanism code.
- The foundation owns component members, start/reset wiring, status, the Playfield container, and
  browser readiness. UI callbacks call protected `submit_game_action()`, which runs the game-specific
  action hook, evaluates terminal state, and rerenders. Implement the hooks and add game-specific
  helpers and scene nodes when the plan requires them.
- Never research `Gamegen*` APIs online or invent component methods, fields, signals, or data shapes.

## Source and engine rules
- Use typed GDScript, Godot nodes/resources, the Compatibility renderer, and a single-threaded Web
  export. Do not use C#, C++, npm, raw WebGL, handwritten WASM, remote assets, or external libraries.
- Main scene: `res://game/main.tscn`; root script: `res://game/main.gd`. File tools use
  `project/game/...`, never `res://...`. A `.tscn` starts with `[gd_scene`; code belongs in `.gd`.
- `new_text` contains source only: no display line numbers, separators, JSON labels, or fences.
- Never replace protected bootstrap/lifecycle functions. Use broad foundation hooks such as
  `initialize_game_state`, `handle_game_action`, `evaluate_game_state`, `render_game`, and
  `smoke_test_gameplay`; insert additional game-specific helpers through the stage plan.
- Official research is limited to stable Godot documentation and the pinned official Godot 4.7
  demos returned by the research tool. Documentation defines APIs; demos are examples only.

## Delivery standard
- Produce a focused playable v1 for at least two humans. Finish and validate rules and game flow
  before creating a unique game-specific UI. Without a networking component, use honest
  local hot-seat multiplayer and hide private information between turns where needed.
- Provide responsive mouse/touch UI at 960x540, clear status and interaction feedback, restart,
  terminal state, and coherent procedural visuals.
- `smoke_test_gameplay` must use fresh systems or resettable local state and never alter the live
  match. Check setup, representative legal actions, progression, terminal state, and restart.
- Continue through recoverable failures until success, sandbox expiry, cancellation, or recursion
  limit. Never report invented progress or an unverified URL.
- If the user is not requesting game generation or modification, answer normally without tools.

"""

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.replace(
    "{AVAILABLE_COMPONENTS}",
    AVAILABLE_COMPONENTS_PROMPT,
)


COMMON_GENERATION_PROMPT = """Build a complete playable browser game in Godot 4.7. Application
state is authoritative. Return exactly one tool call and use only `allowed_tools`; never narrate
progress instead of acting. Gamegen* classes are private, prewritten project components rather than
official Godot APIs. Their supplied contracts are exact. Reuse foundation members and do not invent
component methods, fields, signals, or data shapes.

Use typed GDScript, Godot nodes/resources, the Compatibility renderer, and project-local assets.
Main files are project/game/main.gd and project/game/main.tscn. A .tscn starts with [gd_scene]; code
belongs in .gd. Source output must contain no Markdown, JSON wrappers, or displayed line numbers.
Do not replace protected lifecycle/bootstrap code. Keep game-specific rules in game files unless an
explicit stage step extends a copied component additively. Produce honest local hot-seat multiplayer
unless a networking component is selected. Claim success only after `run_game` returns a play URL."""

WORKSPACE_PROMPT = """Call `create_game_workspace` once. Select the smallest reusable mechanism
components and provide a concise whole-game design covering rules, actions, victory, UI, restart,
and smoke tests. Components are not finished games: you must author the requested deck/data, rules,
connections, behavior, and unique presentation. Available components:
{available_components}""".format(available_components=AVAILABLE_COMPONENTS_PROMPT)

STAGE_PLANNING_PROMPT = """Call `plan_game_stage` for `next_stage` only. Create a short ordered plan
of bounded functions, declarations, scene nodes, new scenes, or assets. Each step owns one path and
one symbol, has concrete acceptance criteria, and depends only on earlier steps. Use symbol_kind
function/declaration for .gd; a declaration step creates exactly one var, const, signal, or enum.
GDScript has no type aliases or structs, so represent structured records as Dictionary values and
required keys. Use scene_node for an existing .tscn node, new_scene for a new .tscn, and
new_asset only under project/assets. Name every exact call needed by `required_calls`; put only
quoted Dictionary keys in `required_dictionary_keys`. Do not supply edit mechanics or anchors.
Respect the stage purpose and its foundation hook. Plan helpers before callers. Components may be
extended only additively with extends_component=true after their existing contract is understood.
Keep each step to at most four distinct stateful component methods; split larger orchestration
into independently valid helpers and a small dispatcher. After domain modeling, require only
Dictionary keys that belong to the step's actual input or established data shapes.
Never target a symbol listed as protected. The domain_model stage is pure: it may define game data
and rule helpers, but it must not call or mutate card_system, turn_system, inventory_system, or
foundation game_state; defer those operations to initialization or gameplay. Treat
`established_game_contracts` as binding: call existing helpers rather than redefining them or
creating a duplicate factory. Only request a compatible contract amendment when reuse is genuinely
impossible, and explain why."""

STEP_IMPLEMENTATION_PROMPT = """Call `implement_stage_step` for `active_step_context` only. Return
only source satisfying its output_contract, required calls, dictionary keys, dependencies, and
acceptance criteria. Use exact component signatures and relevant official Godot guidance supplied
in state. Do not broaden the step, add unrelated symbols, duplicate lifecycle methods, or perform
mechanical reads, research, validation, routing, commit, or rollback; the application owns those."""

FINALIZATION_PROMPT = """The application has completed source stages. Call the single allowed
build or runtime tool. Do not change files or claim a URL not returned by `run_game`."""


def system_prompt_for_execution(execution: dict) -> str:
    """Return only the instructions needed by the current application-owned phase."""
    if not execution.get("workspace_created"):
        phase_prompt = WORKSPACE_PROMPT
    elif execution.get("run_succeeded"):
        phase_prompt = FINALIZATION_PROMPT
    elif execution.get("candidate_revision") is not None:
        phase_prompt = STEP_IMPLEMENTATION_PROMPT
    else:
        allowed = set(execution.get("allowed_tools") or ())
        if "plan_game_stage" in allowed:
            phase_prompt = STAGE_PLANNING_PROMPT
        elif "implement_stage_step" in allowed:
            phase_prompt = STEP_IMPLEMENTATION_PROMPT
        elif allowed & {"build_godot_game", "run_game"}:
            phase_prompt = FINALIZATION_PROMPT
        else:
            phase_prompt = (
                "Follow the single allowed application transition. Mechanical research, reading, "
                "validation, commit, rollback, and routing are application-owned."
            )
    return f"{COMMON_GENERATION_PROMPT}\n\n{phase_prompt}"
