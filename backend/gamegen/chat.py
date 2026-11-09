import logging
import re

from django.db import close_old_connections, transaction
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.request import Request
from rest_framework.response import Response

from agent.game_storage import get_latest_game_for_thread
from agent.generation_control import (
    GenerationCancelled,
    begin_generation,
    forget_generation,
    request_stop,
)
from agent.streaming import delete_chat_history, get_final_or_full, stream_chat
from gamegen.auth.throttling import IPRateThrottle
from gamegen.models import GameChat, StoredGame


logger = logging.getLogger(__name__)

SANDBOX_PATH_RE = re.compile(r"^/(home|tmp|workspace)/[^\s]+$")
GAME_DELIVERY_RE = re.compile(
    r"(example\.com|/games/|[\"']play_url[\"']|\bplay\s+(?:it\s+)?(?:at|here)\b)",
    re.IGNORECASE,
)


def _owned_chats(request: Request):
    return GameChat.objects.filter(owner=request.user)


@api_view(["GET", "POST"])
@permission_classes([permissions.IsAuthenticated])
def chats(request: Request) -> Response:
    if request.method == "GET":
        return Response({"chats": [chat.to_public_dict() for chat in _owned_chats(request)]})

    title = str(request.data.get("title") or "New game").strip()[:120] or "New game"
    chat = GameChat.objects.create(owner=request.user, title=title)
    return Response({"chat": chat.to_public_dict()}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([permissions.IsAuthenticated])
def chat_detail(request: Request, chat_id) -> Response:
    try:
        chat = _owned_chats(request).get(id=chat_id)
    except GameChat.DoesNotExist:
        return Response({"error": "Chat was not found."}, status=status.HTTP_404_NOT_FOUND)

    thread_id = str(chat.id)
    request_stop(thread_id)
    with transaction.atomic():
        StoredGame.objects.filter(thread_id=thread_id).delete()
        chat.delete()
    delete_chat_history(thread_id)
    forget_generation(thread_id)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def stop_chat_generation(request: Request, chat_id) -> Response:
    try:
        chat = _owned_chats(request).get(id=chat_id)
    except GameChat.DoesNotExist:
        return Response({"error": "Chat was not found."}, status=status.HTTP_404_NOT_FOUND)

    if chat.status == GameChat.Status.GENERATING:
        chat.status = GameChat.Status.STOPPED
        chat.last_error = "Generation was stopped by the user."
        chat.save(update_fields=["status", "last_error", "updated_at"])
        request_stop(str(chat.id))
    return Response({"chat": chat.to_public_dict()}, status=status.HTTP_202_ACCEPTED)

def _extract_message_content(payload):
    if payload is None:
        return ""

    if isinstance(payload, str):
        return payload

    if isinstance(payload, (int, float, bool)):
        return str(payload)

    if isinstance(payload, (list, tuple)):
        for item in reversed(payload):
            if isinstance(item, dict):
                role_value = item.get("role", "") or item.get("type", "") or item.get("name", "")
            else:
                role_value = getattr(item, "role", "") or getattr(item, "type", "") or getattr(item, "name", "")

            role = str(role_value).lower()
            if any(hint in role for hint in ("assistant", "ai", "bot", "model")):
                content = _extract_message_content(item)
                if content:
                    return content

        for item in reversed(payload):
            content = _extract_message_content(item)
            if content:
                return content
        return ""

    content = getattr(payload, "content", None)
    if content is not None:
        return _extract_message_content(content)

    if isinstance(payload, dict):
        if "messages" in payload:
            return _extract_message_content(payload["messages"])

        for key in ("content", "text", "output", "response", "message"):
            if key in payload:
                content = _extract_message_content(payload[key])
                if content:
                    return content

        for key, value in reversed(payload.items()):
            if key in {
                "id",
                "name",
                "role",
                "type",
                "additional_kwargs",
                "response_metadata",
                "usage_metadata",
                "tool_calls",
                "invalid_tool_calls",
            }:
                continue

            content = _extract_message_content(value)
            if content:
                return content

    return ""

@api_view(["GET"])
@throttle_classes([IPRateThrottle])
@permission_classes([permissions.IsAuthenticated])
def get_chat_messages(request: Request) -> Response:
    """
    Access previous chat messages.
    """
    thread_id = request.data.get("thread_id", "")
    if not thread_id:
        return Response({"error": "Thread id is required."}, status=status.HTTP_400_BAD_REQUEST)

    res = get_final_or_full(thread_id, final=False)
    return Response({"messages": res}, status=status.HTTP_200_OK)

@api_view(["POST"])
@throttle_classes([IPRateThrottle])
@permission_classes([permissions.IsAuthenticated])
def post_chat_message(request: Request) -> Response:
    """
    Post a new chat message.
    """
    message = request.data.get("message", "")
    thread_id = request.data.get("thread_id", "")
    if not message or not thread_id:
        return Response({"error": "Message content and thread id are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            chat = _owned_chats(request).select_for_update().get(id=thread_id)
            has_stored_game = StoredGame.objects.filter(thread_id=thread_id).exists()
            if has_stored_game and chat.status != GameChat.Status.COMPLETED:
                chat.status = GameChat.Status.COMPLETED
                chat.save(update_fields=["status", "updated_at"])
            if chat.status == GameChat.Status.COMPLETED or has_stored_game:
                return Response(
                    {"error": "This chat already generated a game. Create a new chat."},
                    status=status.HTTP_409_CONFLICT,
                )
            if chat.status == GameChat.Status.GENERATING:
                return Response(
                    {"error": "Generation is already running for this chat."},
                    status=status.HTTP_409_CONFLICT,
                )
            if chat.status == GameChat.Status.STOPPED:
                return Response(
                    {"error": "This generation is closed. Create a new chat to start again."},
                    status=status.HTTP_409_CONFLICT,
                )
            chat.status = GameChat.Status.GENERATING
            if chat.title == "New game":
                chat.title = str(message).strip()[:80] or chat.title
            chat.last_error = ""
            chat.save(update_fields=["status", "title", "last_error", "updated_at"])
    except (GameChat.DoesNotExist, ValueError):
        return Response({"error": "Chat was not found."}, status=status.HTTP_404_NOT_FOUND)

    previous_game = get_latest_game_for_thread(thread_id)
    begin_generation(thread_id)
    try:
        res = stream_chat(thread_id, message)
    except GenerationCancelled:
        res = {"messages": [{"role": "assistant", "content": "Generation stopped."}]}
    except Exception as exc:
        logger.exception("Game generation failed for chat %s", thread_id)
        res = {
            "messages": [
                {"role": "assistant", "content": f"Generation failed: {type(exc).__name__}: {exc}"}
            ]
        }
    finally:
        # async_to_sync may spend many minutes outside the request thread. Retire the connection
        # used before generation before querying the final game and chat state.
        close_old_connections()
    content = _extract_message_content(res)
    game = get_latest_game_for_thread(thread_id)
    new_game_created = bool(game and game.get("id") != (previous_game or {}).get("id"))

    try:
        chat = _owned_chats(request).get(id=thread_id)
        if new_game_created:
            chat.status = GameChat.Status.COMPLETED
        elif chat.status == GameChat.Status.GENERATING:
            execution = chat.execution_state or {}
            chat.status = (
                GameChat.Status.STOPPED
                if execution.get("terminal") or execution.get("run_finished")
                else GameChat.Status.ACTIVE
            )
        chat.save(update_fields=["status", "updated_at"])
    except GameChat.DoesNotExist:
        chat = None

    if SANDBOX_PATH_RE.match(content.strip()) and not game:
        content = (
            "I started writing the game files, but generation did not complete yet. "
            "Please try again; the next successful run should compile, test, store, "
            "and return a playable game link."
        )

    if GAME_DELIVERY_RE.search(content) and not new_game_created:
        content = (
            "Generation did not complete successfully, so no playable game link was stored. "
            "Please try again; a real game link will appear only after compile, browser validation, "
            "and storage all succeed."
        )

    if new_game_created:
        content = f"Game is ready. Play it here: {game.get('play_url')}"

    payload = {
        "message": {"role": "assistant", "content": content},
        "chat": chat.to_public_dict() if chat else None,
    }
    if new_game_created:
        payload["game"] = {
            "id": game["id"],
            "title": game.get("title", "Generated game"),
            "play_url": game.get("play_url"),
            "created_at": game.get("created_at"),
        }

    return Response(payload, status=status.HTTP_200_OK)
