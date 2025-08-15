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

@api_view(["GET"])
@throttle_classes([IPRateThrottle])
@permission_classes([AllowAny])
def get_chat_messages(request: Request) -> Response:
    """
    Access previous chat messages.
    """
    thread_id = "1234"
    res = get_final_or_full(thread_id, final=False)
    return Response({"messages": res}, status=status.HTTP_200_OK)

@api_view(["POST"])
@throttle_classes([IPRateThrottle])
@permission_classes([AllowAny])
def post_chat_message(request: Request) -> Response:
    """
    Post a new chat message.
    """
    thread_id = "1234"
    message = request.data.get("message", "")
    if not message:
        return Response({"error": "Message content is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Here you would typically add the message to the chat thread
    # For example:
    # add_message_to_thread(thread_id, message)
    res = stream_chat(thread_id, message)
    
    return Response({"messages": res}, status=status.HTTP_200_OK)
    