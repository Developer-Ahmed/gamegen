from django.urls import path, re_path

from .auth import advanced_view as auth_view
from . import views
from . import chat
from . import game_views

urlpatterns = [

    # auth urls
    path('auth/signup/', auth_view.signup, name='auth-signup'),
    path('auth/login/', auth_view.sign_in_with_email, name='auth-login'),
    path('auth/logout/', auth_view.sign_out, name='auth-logout'),
    path('auth/session/', auth_view.get_session, name='get_session'),
    path('auth/session/refresh/', auth_view.refresh_session, name='refresh_session'),

    path("", views.chat, name="chat"),
    path('chat/', chat.get_chat_messages, name='chat'),
    path('chat/send/', chat.post_chat_message, name='chat-send'),
    path('api/chats/', chat.chats, name='chats'),
    path('api/chats/<uuid:chat_id>/', chat.chat_detail, name='chat-detail'),
    path('api/chats/<uuid:chat_id>/stop/', chat.stop_chat_generation, name='chat-stop'),
    path('api/games/', game_views.list_games, name='games-list'),
    path('api/games/<slug:game_id>/', game_views.game_detail, name='games-detail'),
    path('games/<slug:game_id>/', game_views.play_game, name='games-play'),
    re_path(r'^games/(?P<game_id>[-\w]+)/(?P<path>.+)$', game_views.game_file, name='games-file'),
    re_path(r'^(?P<path>(?:assets|build|dist|public|web)/.+)$', game_views.game_asset_from_referer, name='games-asset-from-referer'),
    # path('chat/messages', chat.stream_response, name='chat'),

]
