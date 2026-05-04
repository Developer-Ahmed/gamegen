from urllib import response
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.request import Request
from rest_framework.response import Response
from django.core.cache import cache
import hashlib
import logging
import re

# Import custom throttling classes
from gamegen.auth.throttling import IPRateThrottle, IPBasedUserRateThrottle
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes

logger = logging.getLogger(__name__)

from agent.streaming import stream_chat, get_final_or_full

# everytime the server is started, all the messages are lost. Deployment can be used to prevent that but unless self hosting it costs money.

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
@permission_classes([AllowAny])
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
# for testing, it's fine but it would be better to only allow authenticated users
@permission_classes([AllowAny])
def post_chat_message(request: Request) -> Response:
    """
    Post a new chat message.
    """
    message = request.data.get("message", "")
    thread_id = request.data.get("thread_id", "")
    if not message or not thread_id:
        return Response({"error": "Message content and thread id are required."}, status=status.HTTP_400_BAD_REQUEST)

    res = stream_chat(thread_id, message)
    content = _extract_message_content(res)

    return Response(
        {"message": {"role": "assistant", "content": content}},
        status=status.HTTP_200_OK,
    )
