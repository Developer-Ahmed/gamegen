"""Thread-safe cancellation and E2B sandbox ownership for generation jobs."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
import shlex
import threading
import time
from typing import Any

from e2b import Sandbox
from django.db import close_old_connections

from gamegen.models import GameChat


SANDBOX_TIMEOUT_SECONDS = 60 * 40
GAME_FILES_ROOT = "/home/user/game_files"
CANDIDATE_GAME_FILES_ROOT = "/home/user/game_files_candidate"
LAST_VALID_GAME_FILES_ROOT = "/home/user/game_files_last_valid"
PROMOTING_GAME_FILES_ROOT = "/home/user/game_files_promoting"


class GenerationCancelled(RuntimeError):
    pass


_lock = threading.RLock()
_cancelled: set[str] = set()
_sandboxes: dict[str, Any] = {}
_last_status_checks: dict[str, float] = {}


def begin_generation(thread_id: str) -> None:
    with _lock:
        _cancelled.discard(thread_id)
        _last_status_checks.pop(thread_id, None)


def request_stop(thread_id: str) -> None:
    with _lock:
        _cancelled.add(thread_id)
        sandbox = _sandboxes.pop(thread_id, None)

    if sandbox is None:
        try:
            close_old_connections()
            sandbox_id = GameChat.objects.only("sandbox_id").get(id=thread_id).sandbox_id
            if sandbox_id:
                sandbox = Sandbox.connect(sandbox_id, api_key=os.getenv("E2B_API_KEY"))
        except Exception:
            sandbox = None

    if sandbox is not None:
        try:
            sandbox.kill()
        except Exception:
            pass


def is_stop_requested(thread_id: str) -> bool:
    with _lock:
        if thread_id in _cancelled:
            return True
        now = time.monotonic()
        if now - _last_status_checks.get(thread_id, 0.0) < 0.5:
            return False
        _last_status_checks[thread_id] = now

    try:
        close_old_connections()
        status = GameChat.objects.only("status").get(id=thread_id).status
        stopped = status == GameChat.Status.STOPPED
    except (GameChat.DoesNotExist, ValueError):
        stopped = True
    if stopped:
        with _lock:
            _cancelled.add(thread_id)
    return stopped


def raise_if_stopped(thread_id: str) -> None:
    if is_stop_requested(thread_id):
        raise GenerationCancelled("Generation was stopped by the user.")


def _safe_workspace_path(root: str, relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"Unsafe persisted workspace path: {relative_path}")
    return f"{root}/{path.as_posix()}"


def _recoverable_workspace_sources(state: dict[str, Any]) -> dict[str, str]:
    """Reconstruct one committed revision from durable sources or legacy read snapshots."""
    from .component_library import ComponentRegistry
    from .execution_state import snapshot_source

    registry = ComponentRegistry()
    selected = registry.resolve(state.get("components") or [])
    sources = registry.read_files(selected, title=str(state.get("game_title") or "Game"))
    sources.update(
        {
            str(path): str(content)
            for path, content in (state.get("workspace_sources") or {}).items()
        }
    )
    for path in state.get("files") or {}:
        snapshot = snapshot_source(state, str(path))
        if snapshot is not None:
            sources[str(path)] = snapshot[0]

    missing = sorted(set(state.get("files") or {}) - set(sources))
    if missing:
        raise RuntimeError(
            "Cannot recover expired sandbox because committed sources are missing: "
            + ", ".join(missing)
        )
    return sources


def _materialize_workspace(sandbox: Any, state: dict[str, Any]) -> None:
    sources = _recoverable_workspace_sources(state)
    roots = (
        GAME_FILES_ROOT,
        CANDIDATE_GAME_FILES_ROOT,
        LAST_VALID_GAME_FILES_ROOT,
        PROMOTING_GAME_FILES_ROOT,
    )
    sandbox.commands.run(
        "rm -rf " + " ".join(shlex.quote(root) for root in roots),
        timeout=30,
    )
    sandbox.commands.run(f"mkdir -p {shlex.quote(GAME_FILES_ROOT)}", timeout=30)
    for relative_path, content in sources.items():
        absolute_path = _safe_workspace_path(GAME_FILES_ROOT, relative_path)
        parent = str(PurePosixPath(absolute_path).parent)
        sandbox.commands.run(f"mkdir -p {shlex.quote(parent)}", timeout=30)
        sandbox.files.write(absolute_path, content)

    sandbox.commands.run(
        f"cp -a {shlex.quote(GAME_FILES_ROOT)} {shlex.quote(LAST_VALID_GAME_FILES_ROOT)}",
        timeout=30,
    )
    candidate_sources = state.get("candidate_sources") or {}
    if state.get("candidate_revision") is not None:
        if not candidate_sources:
            raise RuntimeError(
                "Cannot recover an in-flight candidate because its durable sources are missing."
            )
        sandbox.commands.run(
            f"cp -a {shlex.quote(GAME_FILES_ROOT)} {shlex.quote(CANDIDATE_GAME_FILES_ROOT)}",
            timeout=30,
        )
        for relative_path, content in candidate_sources.items():
            absolute_path = _safe_workspace_path(CANDIDATE_GAME_FILES_ROOT, str(relative_path))
            parent = str(PurePosixPath(absolute_path).parent)
            sandbox.commands.run(f"mkdir -p {shlex.quote(parent)}", timeout=30)
            sandbox.files.write(absolute_path, str(content))


def get_job_sandbox(thread_id: str):
    raise_if_stopped(thread_id)
    with _lock:
        cached = _sandboxes.get(thread_id)
    if cached is not None:
        return cached

    close_old_connections()
    chat = GameChat.objects.only("sandbox_id", "execution_state").get(id=thread_id)
    api_key = os.getenv("E2B_API_KEY")
    recovered = False
    if chat.sandbox_id:
        try:
            sandbox = Sandbox.connect(chat.sandbox_id, api_key=api_key)
            sandbox.set_timeout(SANDBOX_TIMEOUT_SECONDS)
        except Exception:
            sandbox = None
            if not bool((chat.execution_state or {}).get("workspace_created")):
                raise
            recovered = True
    else:
        sandbox = None

    if sandbox is None:
        template_id = os.getenv("E2B_TEMPLATE_ID")
        if not template_id:
            raise RuntimeError("E2B_TEMPLATE_ID is required.")
        if not api_key:
            raise RuntimeError("E2B_API_KEY is required.")
        sandbox = Sandbox(
            template_id,
            api_key=api_key,
            timeout=SANDBOX_TIMEOUT_SECONDS,
            metadata={"gamegen_chat_id": thread_id},
        )
        chat.sandbox_id = sandbox.sandbox_id
        chat.save(update_fields=["sandbox_id", "updated_at"])
        if recovered:
            _materialize_workspace(sandbox, chat.execution_state or {})
            state = dict(chat.execution_state or {})
            state["build_revision"] = -1
            state["run_finished"] = False
            state["run_succeeded"] = False
            state["last_tool_error"] = ""
            chat.execution_state = state
            chat.save(update_fields=["execution_state", "updated_at"])
            print(
                f"[agent-progress] sandbox_recovered thread={thread_id} "
                f"revision={state.get('workspace_revision', 0)} files="
                f"{len(_recoverable_workspace_sources(state))}",
                flush=True,
            )

    raise_if_stopped(thread_id)
    with _lock:
        existing = _sandboxes.setdefault(thread_id, sandbox)
    if existing is not sandbox:
        try:
            sandbox.kill()
        except Exception:
            pass
    return existing


def forget_generation(thread_id: str) -> None:
    request_stop(thread_id)
    with _lock:
        _cancelled.discard(thread_id)
        _last_status_checks.pop(thread_id, None)
