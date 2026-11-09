"""Validate the shipped Godot foundation and component edge-case suite in E2B."""

from __future__ import annotations

import os
from pathlib import Path

from e2b import Sandbox

from agent.component_library import ComponentRegistry


PROJECT_ROOT = "/home/user/component_test/project"
FIXTURE_ROOT = Path(__file__).parents[1] / "agent" / "tests" / "fixtures" / "godot_smoke"


def _run(sandbox, command: str, timeout: int = 180) -> str:
    result = sandbox.commands.run(command, timeout=timeout)
    output = "\n".join(
        value for value in (getattr(result, "stdout", ""), getattr(result, "stderr", "")) if value
    )
    if getattr(result, "exit_code", 1) != 0:
        raise RuntimeError(f"Command failed ({result.exit_code}): {command}\n{output}")
    return output


def main() -> None:
    registry = ComponentRegistry()
    components = registry.resolve(
        ["godot.turn_system", "godot.card_system", "godot.inventory_system"]
    )
    files = registry.read_files(components, title="Component validation")
    sandbox = Sandbox(
        os.environ["E2B_TEMPLATE_ID"],
        api_key=os.environ["E2B_API_KEY"],
        timeout=600,
        metadata={"purpose": "gamegen-component-validation"},
    )
    try:
        _run(sandbox, f"mkdir -p {PROJECT_ROOT}/game")
        for path, content in files.items():
            target = f"/home/user/component_test/{path}"
            parent = target.rsplit("/", 1)[0]
            _run(sandbox, f"mkdir -p '{parent}'")
            sandbox.files.write(target, content)

        foundation_output = _run(
            sandbox,
            f"godot --headless --editor --path {PROJECT_ROOT} --quit && "
            f"godot --headless --path {PROJECT_ROOT} --quit-after 2",
        )
        if "SCRIPT ERROR" in foundation_output or "Parse Error" in foundation_output:
            raise RuntimeError(f"Foundation validation failed:\n{foundation_output}")

        for filename in ("main.gd", "main.tscn"):
            sandbox.files.write(
                f"{PROJECT_ROOT}/game/{filename}",
                (FIXTURE_ROOT / filename).read_text(encoding="utf-8"),
            )
        component_output = _run(
            sandbox,
            f"godot --headless --editor --path {PROJECT_ROOT} --quit && "
            f"godot --headless --path {PROJECT_ROOT} --quit-after 3",
        )
        if "GAMEGEN_COMPONENT_SMOKE_OK" not in component_output:
            raise RuntimeError(f"Component smoke marker was not produced:\n{component_output}")
        if "SCRIPT ERROR" in component_output or "Parse Error" in component_output:
            raise RuntimeError(f"Component validation failed:\n{component_output}")
        print("Godot foundation and component edge-case validation passed.")
    finally:
        sandbox.kill()


if __name__ == "__main__":
    main()
