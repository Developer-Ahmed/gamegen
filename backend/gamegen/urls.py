from django.urls import path

from . import views
from . import chat

urlpatterns = [

    path("", views.index, name="index"),
    # path('chat', chat.get_messages, name='chat'),
    # path('chat/messages', chat.stream_response, name='chat'),

]