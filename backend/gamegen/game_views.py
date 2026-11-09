from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET
import re

from agent.game_storage import get_stored_game, get_stored_game_file, list_stored_games

GAME_REFERER_RE = re.compile(r"/games/(?P<game_id>[-\w]+)/")


def _public_game_record(game):
    if not game:
        return None

    return {
        "id": game["id"],
        "title": game.get("title", "Generated game"),
        "created_at": game.get("created_at"),
        "entry_path": game.get("entry_path"),
        "file_count": game.get("file_count", 0),
        "play_url": game.get("play_url"),
        "test_report": game.get("test_report", {}),
    }


@require_GET
def list_games(request):
    return JsonResponse({"games": [_public_game_record(game) for game in list_stored_games()]})


@require_GET
def game_detail(request, game_id):
    game = get_stored_game(game_id)
    if not game:
        raise Http404("Stored game was not found.")

    return JsonResponse({"game": _public_game_record(game)})


@require_GET
@xframe_options_exempt
def play_game(request, game_id):
    game = get_stored_game(game_id)
    if not game:
        raise Http404("Stored game was not found.")

    return redirect(f"/games/{game_id}/{game['entry_path']}")


@require_GET
@xframe_options_exempt
def game_file(request, game_id, path):
    game = get_stored_game(game_id)
    if not game:
        raise Http404("Stored game was not found.")

    try:
        game_file = get_stored_game_file(game_id, path)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    if not game_file:
        raise Http404("Stored game file was not found.")

    return HttpResponse(bytes(game_file.content), content_type=game_file.content_type)


@require_GET
@xframe_options_exempt
def game_asset_from_referer(request, path):
    referer = request.META.get("HTTP_REFERER", "")
    match = GAME_REFERER_RE.search(referer)
    if not match:
        raise Http404("Stored game asset was not found.")

    return game_file(request, match.group("game_id"), path)
