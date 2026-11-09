class_name GamegenTurnOrder
extends RefCounted


signal turn_advanced(previous_player: String, current_player: String, turn_number: int)
signal direction_changed(direction: int)


var _players: Array[String] = []
var _active_index := 0
var _direction := 1
var _turn_number := 0


func configure(player_ids: Array, starting_index: int = 0) -> bool:
	var validation_error := _validate_players(player_ids)
	if not validation_error.is_empty():
		push_error(validation_error)
		return false

	_players.clear()
	for player_id in player_ids:
		_players.append(str(player_id))
	_active_index = posmod(starting_index, _players.size())
	_direction = 1
	_turn_number = 0
	return true


func is_configured() -> bool:
	return not _players.is_empty()


func player_count() -> int:
	return _players.size()


func current_player() -> String:
	return "" if _players.is_empty() else _players[_active_index]


func current_index() -> int:
	return _active_index if is_configured() else -1


func direction() -> int:
	return _direction


func turn_number() -> int:
	return _turn_number


func advance(steps: int = 1) -> String:
	if not is_configured():
		push_error("Turn order is not configured.")
		return ""
	if steps < 1:
		push_error("Advance steps must be positive.")
		return current_player()

	var previous_player := current_player()
	_active_index = posmod(_active_index + (_direction * steps), _players.size())
	_turn_number += 1
	var active_player := current_player()
	turn_advanced.emit(previous_player, active_player, _turn_number)
	return active_player


func skip(players_to_skip: int = 1) -> String:
	if players_to_skip < 0:
		push_error("Players to skip cannot be negative.")
		return current_player()
	return advance(players_to_skip + 1)


func reverse() -> bool:
	if not is_configured():
		push_error("Turn order is not configured.")
		return false
	_direction *= -1
	direction_changed.emit(_direction)
	return true


func restore(state: Dictionary) -> bool:
	var players = state.get("players", [])
	if not players is Array:
		push_error("Turn state players must be an array.")
		return false
	var validation_error := _validate_players(players)
	if not validation_error.is_empty():
		push_error(validation_error)
		return false

	var active_index := int(state.get("active_index", -1))
	var restored_direction := int(state.get("direction", 0))
	var restored_turn_number := int(state.get("turn_number", -1))
	if active_index < 0 or active_index >= players.size():
		push_error("Turn state active_index is outside the player list.")
		return false
	if restored_direction not in [-1, 1]:
		push_error("Turn state direction must be -1 or 1.")
		return false
	if restored_turn_number < 0:
		push_error("Turn state turn_number cannot be negative.")
		return false
	if state.has("active_player") and str(state.active_player) != str(players[active_index]):
		push_error("Turn state active_player does not match active_index.")
		return false

	_players.clear()
	for player_id in players:
		_players.append(str(player_id))
	_active_index = active_index
	_direction = restored_direction
	_turn_number = restored_turn_number
	return true


func snapshot() -> Dictionary:
	return {
		"players": _players.duplicate(),
		"active_player": current_player(),
		"active_index": current_index(),
		"direction": _direction,
		"turn_number": _turn_number,
	}


func _validate_players(player_ids: Array) -> String:
	if player_ids.is_empty():
		return "Turn order requires at least one player."
	var unique := {}
	for player_id in player_ids:
		if not player_id is String or player_id.is_empty():
			return "Player IDs must be non-empty strings."
		if unique.has(player_id):
			return "Player IDs must be unique."
		unique[player_id] = true
	return ""
