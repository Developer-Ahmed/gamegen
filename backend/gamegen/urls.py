from django.urls import path

from .auth import advanced_view as auth_view
from . import views
from . import chat

urlpatterns = [

    # auth urls
    path('auth/signup/', auth_view.signup, name='auth-signup'),
    path('auth/login/', auth_view.sign_in_with_email, name='auth-login'),
    path('auth/logout/', auth_view.sign_out, name='auth-logout'),
    path('auth/session/', auth_view.get_session, name='get_session'),
    path('auth/session/refresh/', auth_view.refresh_session, name='refresh_session'),

    path("", views.chat, name="chat"),
    # path('chat', chat.get_messages, name='chat'),
    # path('chat/messages', chat.stream_response, name='chat'),

]