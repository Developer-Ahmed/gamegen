from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_COMPONENT_ROOT = Path(__file__).with_name("components")
DEFAULT_COMPONENT_MANIFEST = DEFAULT_COMPONENT_ROOT / "components.json"

_GODOT_TITLE_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u00ad": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)


def _godot_title(value: str | None) -> str:
    """Normalize punctuation missing from some Godot web font builds."""
    title = (value or "Generated Multiplayer Game").translate(_GODOT_TITLE_TRANSLATION)
    title = " ".join(title.replace("\r", " ").replace("\n", " ").split())
    return title.replace("\\", "\\\\").replace('"', '\\"')


def normalize_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9_.]+", "_", (value or "").lower()).strip("_.")


def validate_component_path(path: str) -> str:
    rel_path = PurePosixPath(path)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError(f"Unsafe component path: {path}")
    return rel_path.as_posix()


@dataclass(frozen=True)
class ComponentFile:
    source: str
    target: str


@dataclass(frozen=True)
class GameComponent:
    name: str
    version: str
    engine: str
    category: str
    description: str
    use_when: tuple[str, ...]
    provides: tuple[str, ...]
    api: tuple[str, ...]
    method_contracts: dict[str, Any]
    data_contracts: dict[str, Any]
    invariants: tuple[str, ...]
    examples: tuple[str, ...]
    foundation_member: str
    dependencies: tuple[str, ...]
    files: tuple[ComponentFile, ...]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> GameComponent:
        required = ["name", "category", "description", "files"]
        missing = [
            key
            for key in required
            if key not in raw or (key != "files" and not raw.get(key))
        ]
        if missing:
            raise ValueError(f"Component manifest item is missing: {', '.join(missing)}")
        if not isinstance(raw["files"], list):
            raise ValueError(f"Component {raw['name']} files must be a list.")

        files = tuple(
            ComponentFile(
                source=validate_component_path(str(item["source"])),
                target=validate_component_path(str(item["target"])),
            )
            for item in raw["files"]
        )
        api = tuple(str(item) for item in raw.get("api", []))
        method_contracts = dict(raw.get("method_contracts") or {})
        api_methods = {
            match.group(1): signature
            for signature in api
            if not signature.endswith(".new()")
            and (match := re.search(r"(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)\(", signature))
        }
        if set(method_contracts) != set(api_methods):
            missing = sorted(set(api_methods) - set(method_contracts))
            extra = sorted(set(method_contracts) - set(api_methods))
            raise ValueError(
                f"Component {raw['name']} method contracts do not match its API; "
                f"missing={missing}, extra={extra}."
            )
        required_contract_fields = {
            "signature",
            "returns",
            "preconditions",
            "side_effects",
            "non_effects",
        }
        optional_contract_fields = {"result_policy"}
        for method_name, contract in method_contracts.items():
            if (
                not isinstance(contract, dict)
                or not required_contract_fields.issubset(contract)
                or set(contract) - (required_contract_fields | optional_contract_fields)
            ):
                raise ValueError(
                    f"Component {raw['name']} contract for {method_name} must contain "
                    f"{sorted(required_contract_fields)} and only supported optional fields."
                )
            if contract["signature"] != api_methods[method_name]:
                raise ValueError(
                    f"Component {raw['name']} contract signature for {method_name} "
                    "does not match its API signature."
                )
            if not isinstance(contract["returns"], str) or not all(
                isinstance(contract[field], list)
                and all(isinstance(item, str) for item in contract[field])
                for field in ("preconditions", "side_effects", "non_effects")
            ):
                raise ValueError(
                    f"Component {raw['name']} contract for {method_name} has invalid field types."
                )
            if contract.get("result_policy", "required") not in {"required", "optional"}:
                raise ValueError(
                    f"Component {raw['name']} contract for {method_name} has invalid "
                    "result_policy; use 'required' or 'optional'."
                )
        return cls(
            name=normalize_key(str(raw["name"])),
            version=str(raw.get("version", "0.0.0")),
            engine=str(raw.get("engine", "Godot 4")),
            category=normalize_key(str(raw["category"])),
            description=str(raw["description"]),
            use_when=tuple(str(item) for item in raw.get("use_when", [])),
            provides=tuple(str(item) for item in raw.get("provides", [])),
            api=api,
            method_contracts=method_contracts,
            data_contracts=dict(raw.get("data_contracts") or {}),
            invariants=tuple(str(item) for item in raw.get("invariants", [])),
            examples=tuple(str(item) for item in raw.get("examples", [])),
            foundation_member=str(raw.get("foundation_member") or "").strip(),
            dependencies=tuple(normalize_key(str(item)) for item in raw.get("dependencies", [])),
            files=files,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "engine": self.engine,
            "category": self.category,
            "description": self.description,
            "use_when": list(self.use_when),
            "provides": list(self.provides),
            "api": list(self.api),
            "method_contracts": self.method_contracts,
            "data_contracts": self.data_contracts,
            "invariants": list(self.invariants),
            "examples": list(self.examples),
            "foundation_member": self.foundation_member,
            "dependencies": list(self.dependencies),
            "files": [{"source": file.source, "target": file.target} for file in self.files],
        }

    def to_contract_dict(self) -> dict[str, Any]:
        """Return the exact, model-facing integration contract without file metadata."""
        return {
            "name": self.name,
            "private_gamegen_component": True,
            "api": list(self.api),
            "method_contracts": self.method_contracts,
            "data_contracts": self.data_contracts,
            "invariants": list(self.invariants),
            "examples": list(self.examples),
            "foundation_member": self.foundation_member,
        }


class ComponentRegistry:
    def __init__(self, root: Path = DEFAULT_COMPONENT_ROOT, manifest_path: Path = DEFAULT_COMPONENT_MANIFEST):
        self.root = root
        self.manifest_path = manifest_path

    def _raw_manifest(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("manifest_version") != 1:
            raise ValueError("Unsupported component manifest version.")
        if not isinstance(manifest.get("components"), list):
            raise ValueError("Component manifest requires a components list.")
        return manifest

    def list_components(self) -> list[GameComponent]:
        components = [GameComponent.from_raw(item) for item in self._raw_manifest()["components"]]
        names = [component.name for component in components]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate component names: {', '.join(duplicates)}")
        return components

    def recommended_sets(self) -> list[dict[str, Any]]:
        return list(self._raw_manifest().get("recommended_sets", []))

    def get(self, name: str) -> GameComponent | None:
        normalized = normalize_key(name)
        for component in self.list_components():
            if component.name == normalized:
                return component
        return None

    def resolve(self, names: list[str]) -> list[GameComponent]:
        resolved: list[GameComponent] = []
        seen: set[str] = set()
        visiting: set[str] = set()

        def add(name: str) -> None:
            normalized = normalize_key(name)
            if normalized in seen:
                return
            if normalized in visiting:
                raise ValueError(f"Circular component dependency involving '{normalized}'.")

            component = self.get(normalized)
            if component is None:
                available = ", ".join(component.name for component in self.list_components())
                raise ValueError(f"Unknown component '{name}'. Available components: {available}")

            visiting.add(normalized)
            try:
                for dependency in component.dependencies:
                    add(dependency)
            finally:
                visiting.remove(normalized)
            seen.add(component.name)
            resolved.append(component)

        for name in names:
            add(name)

        return resolved

    def read_files(self, components: list[GameComponent], *, title: str | None = None) -> dict[str, str]:
        game_title = _godot_title(title)
        member_lines = [component.foundation_member for component in components if component.foundation_member]
        component_members = (
            "\n".join(member_lines)
            if member_lines
            else "# No optional gameplay systems were selected."
        )
        replacements = {
            "{{GAME_TITLE}}": game_title,
            "{{GAMEGEN_COMPONENT_MEMBERS}}": component_members,
        }
        files: dict[str, str] = {}
        for component in components:
            for file in component.files:
                source_path = self.root / file.source
                if not source_path.is_file():
                    raise ValueError(f"Component {component.name} is missing file: {file.source}")
                if self.root.resolve() not in source_path.resolve().parents:
                    raise ValueError(f"Component source escapes the component root: {file.source}")

                content = source_path.read_text(encoding="utf-8")
                for old, new in replacements.items():
                    content = content.replace(old, new)
                if file.target in files and files[file.target] != content:
                    raise ValueError(
                        f"Components provide conflicting content for target: {file.target}"
                    )
                files[file.target] = content
        return files
