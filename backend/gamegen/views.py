from django.shortcuts import render

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods

@require_http_methods(["GET", "POST"])
def chat(request):
    if request.method == "GET":
        # fetch messages
        return JsonResponse({"messages": []})
    elif request.method == "POST":
        # langgraph stream func
        # pass in a function and content type
        return StreamingHttpResponse("Message sent")
