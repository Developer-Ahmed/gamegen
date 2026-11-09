from __future__ import annotations

import mimetypes
import posixpath
import re
import uuid
from pathlib import PurePosixPath
from typing import Any

from django.db import close_old_connections, transaction

from gamegen.models import GameChat, StoredGame, StoredGameFile


PLAYABLE_EXTENSIONS = {
    ".bin",
    ".css",
    ".data",
    ".gif",
    ".glb",
    ".gltf",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".ogg",
    ".pck",
    ".png",
    ".svg",
    ".wasm",
    ".wav",
    ".webp",
    ".woff",
    ".woff2",
}

EXCLUDED_PARTS = {
    ".cache",
    ".git",
    "__pycache__",
    "node_modules",
}

TEXT_REWRITE_EXTENSIONS = {".css", ".html", ".js", ".json"}
ABSOLUTE_ASSET_ROOTS = ("assets", "build", "dist", "public", "web")


def _safe_game_id(label: str | None = None) -> str:
    prefix = re.sub(r"[^a-z0-9-]+", "-", (label or "game").lower()).strip("-")
    prefix = prefix[:32] or "game"
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _relative_sandbox_path(path: str, root: str) -> str | None:
    cleaned_root = root.rstrip("/")
    cleaned_path = path.replace("\\", "/")

    if cleaned_path == cleaned_root:
        return None

    if cleaned_path.startswith(f"{cleaned_root}/"):
        cleaned_path = cleaned_path[len(cleaned_root) + 1:]

    rel_path = PurePosixPath(cleaned_path)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        return None

    if any(part in EXCLUDED_PARTS for part in rel_path.parts):
        return None

    if rel_path.suffix.lower() not in PLAYABLE_EXTENSIONS:
        return None

    return rel_path.as_posix()


def _is_file_entry(entry: Any) -> bool:
    entry_type = getattr(entry, "type", None)
    return getattr(entry_type, "value", entry_type) == "file"


def _entry_path(entry: Any) -> str:
    return str(getattr(entry, "path", entry))


def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, memoryview):
        return content.tobytes()
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content)


def _find_entry_path(files: list[str]) -> str:
    preferred = ["web/index.html", "index.html", "dist/index.html", "public/index.html"]
    file_set = set(files)

    for path in preferred:
        if path in file_set:
            return path

    html_files = sorted(path for path in files if path.endswith(".html"))
    if html_files:
        return html_files[0]

    raise ValueError("No HTML entry file was found in the generated game files.")


def _content_type_for(path: str) -> str:
    if path.endswith(".wasm"):
        return "application/wasm"

    content_type, _ = mimetypes.guess_type(path)
    return content_type or "application/octet-stream"


def _relative_prefix(from_path: str, target_root: str) -> str:
    parent = PurePosixPath(from_path).parent.as_posix()
    if parent == ".":
        parent = "."

    prefix = posixpath.relpath(target_root, parent)
    return "." if prefix == "." else prefix


def _rewrite_absolute_asset_paths(path: str, content: bytes) -> bytes:
    if PurePosixPath(path).suffix.lower() not in TEXT_REWRITE_EXTENSIONS:
        return content

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    for root in ABSOLUTE_ASSET_ROOTS:
        prefix = _relative_prefix(path, root)
        replacements = {
            f'"/{root}/': f'"{prefix}/',
            f"'/{root}/": f"'{prefix}/",
            f"`/{root}/": f"`{prefix}/",
            f"url(/{root}/": f"url({prefix}/",
            f"url('/{root}/": f"url('{prefix}/",
            f'url("/{root}/': f'url("{prefix}/',
        }
        for old, new in replacements.items():
            text = text.replace(old, new)

    return text.encode("utf-8")


def store_sandbox_game(
    sandbox: Any,
    *,
    thread_id: str | None = None,
    title: str | None = None,
    test_report: dict[str, Any] | None = None,
    source_root: str = "/home/user/game_files",
) -> dict[str, Any]:
    try:
        entries = sandbox.files.list(source_root, depth=10)
    except Exception:
        entries = []

    files: list[dict[str, Any]] = []
    seen_paths: list[str] = []
    for entry in entries:
        seen_paths.append(_entry_path(entry))
        if not _is_file_entry(entry):
            continue

        entry_path = _entry_path(entry)
        rel_path = _relative_sandbox_path(entry_path, source_root)
        if not rel_path:
            continue

        content = _as_bytes(sandbox.files.read(entry_path, format="bytes"))
        content = _rewrite_absolute_asset_paths(rel_path, content)
        files.append(
            {
                "path": rel_path,
                "content": content,
                "content_type": _content_type_for(rel_path),
                "size": len(content),
            }
        )

    if not files:
        sample = ", ".join(seen_paths[:12]) or "no entries"
        raise ValueError(
            f"No playable files were copied from sandbox game root {source_root}. "
            f"Seen {len(seen_paths)} entries: {sample}"
        )

    copied_paths = [file["path"] for file in files]
    entry_path = _find_entry_path(copied_paths)
    game_id = _safe_game_id(title or thread_id)

    # Storage runs after a long browser test, so the thread's previous connection may have aged
    # out while no database work was occurring.
    close_old_connections()
    with transaction.atomic():
        if thread_id:
            try:
                GameChat.objects.select_for_update().get(id=thread_id)
            except (GameChat.DoesNotExist, ValueError) as exc:
                raise ValueError(f"Cannot store a game for unknown chat {thread_id}.") from exc
            if StoredGame.objects.filter(thread_id=thread_id).exists():
                raise ValueError("This chat already has a stored game.")
        game = StoredGame.objects.create(
            id=game_id,
            title=title or "Generated game",
            thread_id=thread_id,
            entry_path=entry_path,
            file_count=len(files),
            test_report=test_report or {},
        )
        StoredGameFile.objects.bulk_create(
            [
                StoredGameFile(
                    game=game,
                    path=file["path"],
                    content_type=file["content_type"],
                    content=file["content"],
                    size=file["size"],
                )
                for file in files
            ]
        )

    return game.to_public_dict()


def list_stored_games() -> list[dict[str, Any]]:
    close_old_connections()
    return [game.to_public_dict() for game in StoredGame.objects.all()]


def get_stored_game(game_id: str) -> dict[str, Any] | None:
    close_old_connections()
    try:
        return StoredGame.objects.get(id=game_id).to_public_dict()
    except StoredGame.DoesNotExist:
        return None


def get_latest_game_for_thread(thread_id: str | None) -> dict[str, Any] | None:
    if not thread_id:
        return None

    close_old_connections()
    game = StoredGame.objects.filter(thread_id=thread_id).first()
    return game.to_public_dict() if game else None


def get_stored_game_file(game_id: str, path: str) -> StoredGameFile | None:
    rel_path = PurePosixPath(path)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError("Requested game file is outside the stored game.")

    close_old_connections()
    try:
        return StoredGameFile.objects.get(game_id=game_id, path=rel_path.as_posix())
    except StoredGameFile.DoesNotExist:
        return None
