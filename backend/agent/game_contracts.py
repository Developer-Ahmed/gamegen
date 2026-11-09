"""Durable contracts for model-authored game code.

The language model chooses game design and source.  The application records the
validated result so later stages cannot accidentally redefine earlier decisions.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
from typing import Any, Iterable

from .source_structure import declaration_spans, function_spans, scene_node_spans


_FACTORY_PREFIXES = ("create_", "build_", "make_")
_QUOTED_DICTIONARY_KEY_RE = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*:')


def contract_key(path: str, symbol: str) -> str:
    return f"{path}::{symbol}"


def factory_role(symbol: str) -> str:
    """Return a conservative semantic role for factory-like function names."""
    lowered = symbol.lower()
    for prefix in _FACTORY_PREFIXES:
        if lowered.startswith(prefix):
            remainder = lowered[len(prefix):]
            return remainder.rsplit("_", 1)[-1] if remainder else ""
    return ""


def symbol_source(content: str, *, kind: str, symbol: str) -> str:
    spans = (
        function_spans(content)
        if kind == "function"
        else declaration_spans(content)
        if kind == "declaration"
        else scene_node_spans(content)
        if kind == "scene_node"
        else {}
    )
    span = spans.get(symbol)
    if span is None:
        return ""
    return "\n".join(content.splitlines()[span.start_line - 1:span.end_line])


def dictionary_keys(source: str) -> list[str]:
    return sorted(set(_QUOTED_DICTIONARY_KEY_RE.findall(source)))


def record_validated_step_contract(
    state: dict[str, Any],
    *,
    step: dict[str, Any],
    stage_id: str,
    revision: int,
    sources: dict[str, str],
) -> None:
    """Upsert the contract for one successfully validated implementation step."""
    path = str(step.get("path") or "")
    symbol = str(step.get("target") or step.get("symbol") or "")
    kind = str(step.get("symbol_kind") or "")
    if not path or not symbol:
        return
    source = symbol_source(sources.get(path, ""), kind=kind, symbol=symbol)
    declared_keys = [str(value) for value in step.get("required_dictionary_keys") or []]
    keys = sorted(set([*declared_keys, *dictionary_keys(source)]))
    key = contract_key(path, symbol)
    contracts = dict(state.get("game_contracts") or {})
    previous = contracts.get(key) or {}
    history = list(previous.get("history") or [])
    if previous:
        history.append(
            {
                "revision": previous.get("revision"),
                "source_sha256": previous.get("source_sha256"),
                "amendment_reason": previous.get("amendment_reason", ""),
            }
        )
    contracts[key] = {
        "path": path,
        "symbol": symbol,
        "symbol_kind": kind,
        "owner_stage": str(previous.get("owner_stage") or stage_id),
        "last_changed_stage": stage_id,
        "step_id": str(step.get("id") or ""),
        "purpose": str(step.get("purpose") or "")[:500],
        "acceptance_criteria": [
            str(value)[:300] for value in step.get("acceptance_criteria") or []
        ][:6],
        "required_calls": sorted(
            set(str(value) for value in step.get("required_calls") or [])
        ),
        "dictionary_keys": keys,
        "factory_role": factory_role(symbol),
        "revision": revision,
        "source_sha256": sha256(source.encode("utf-8")).hexdigest() if source else "",
        "amendment_reason": str(step.get("amendment_reason") or "")[:500],
        "history": history[-8:],
    }
    state["game_contracts"] = contracts


def validate_plan_against_contracts(
    state: dict[str, Any],
    *,
    stage_id: str,
    steps: Iterable[dict[str, Any]],
) -> list[str]:
    """Reject silent cross-stage rewrites and duplicate semantic producers."""
    contracts = state.get("game_contracts") or {}
    errors: list[str] = []
    for raw in steps:
        path = str(raw.get("path") or "").strip()
        symbol = str(raw.get("symbol") or raw.get("target") or "").strip()
        existing = contracts.get(contract_key(path, symbol))
        if existing and str(existing.get("last_changed_stage") or "") != stage_id:
            amendment = bool(raw.get("amends_contract"))
            reason = " ".join(str(raw.get("amendment_reason") or "").split())
            if not amendment or len(reason) < 20:
                errors.append(
                    f"Symbol '{symbol}' is an established contract owned by stage "
                    f"'{existing.get('owner_stage')}'. Reuse it instead of redefining it. "
                    "A compatible change requires amends_contract=true and a concrete "
                    "amendment_reason."
                )
                continue
            old_keys = set(existing.get("dictionary_keys") or [])
            new_keys = set(raw.get("required_dictionary_keys") or [])
            missing_keys = sorted(old_keys - new_keys)
            if missing_keys:
                errors.append(
                    f"Amendment of '{symbol}' would weaken its established dictionary contract. "
                    "Preserve these keys: " + ", ".join(missing_keys)
                )

        role = factory_role(symbol)
        if not role or existing:
            continue
        for contract in contracts.values():
            if contract.get("factory_role") != role:
                continue
            if contract.get("path") != path:
                continue
            errors.append(
                f"Factory '{symbol}' duplicates the established '{contract.get('symbol')}' "
                f"contract for role '{role}'. Reuse {contract.get('symbol')}() and declare it "
                "in required_calls/depends_on instead of creating another producer."
            )
            break
    return errors


def compact_game_contracts(
    state: dict[str, Any],
    *,
    path: str | None = None,
    symbols: Iterable[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    wanted = set(symbols or [])
    compact: list[dict[str, Any]] = []
    for contract in (state.get("game_contracts") or {}).values():
        if path and contract.get("path") != path:
            continue
        if wanted and contract.get("symbol") not in wanted:
            continue
        compact.append(
            {
                key: deepcopy(contract.get(key))
                for key in (
                    "path",
                    "symbol",
                    "symbol_kind",
                    "owner_stage",
                    "last_changed_stage",
                    "purpose",
                    "required_calls",
                    "dictionary_keys",
                    "factory_role",
                    "revision",
                )
            }
        )
    return sorted(compact, key=lambda item: (str(item["path"]), str(item["symbol"])))[:limit]
