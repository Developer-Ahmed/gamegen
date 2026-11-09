class_name GamegenCardSystem
extends RefCounted


signal card_drawn(player_id: String, card_id: String)
signal card_discarded(card_id: String)
signal discard_recycled(card_count: int)


var _draw_pile: Array[Dictionary] = []
var _discard_pile: Array[Dictionary] = []
var _hands: Dictionary = {}
var _known_card_ids: Dictionary = {}
var _random := RandomNumberGenerator.new()
var _auto_recycle := true
var _configured := false


func configure(cards: Array, player_ids: Array, seed: int) -> bool:
	var validation_error := _validate_configuration(cards, player_ids)
	if not validation_error.is_empty():
		push_error(validation_error)
		return false

	_draw_pile.clear()
	_discard_pile.clear()
	_hands.clear()
	_known_card_ids.clear()
	for card in cards:
		var normalized_card := (card as Dictionary).duplicate(true)
		normalized_card["id"] = str(normalized_card["id"])
		_draw_pile.append(normalized_card)
		_known_card_ids[normalized_card.id] = true
	for player_id in player_ids:
		_hands[str(player_id)] = []

	_random.seed = seed
	_shuffle(_draw_pile)
	_configured = true
	return true


func is_configured() -> bool:
	return _configured


func set_auto_recycle(enabled: bool) -> void:
	_auto_recycle = enabled


func deal(cards_per_player: int) -> bool:
	if not _require_configured() or cards_per_player < 0:
		if cards_per_player < 0:
			push_error("Cards per player cannot be negative.")
		return false
	if cards_per_player * _hands.size() > _draw_pile.size():
		return false

	for _round in range(cards_per_player):
		for player_id in _hands:
			if not draw_to_hand(player_id):
				return false
	return true


func draw_card() -> Dictionary:
	if not _require_configured():
		return {}
	if _draw_pile.is_empty() and _auto_recycle:
		recycle_discards()
	if _draw_pile.is_empty():
		return {}
	return _draw_pile.pop_back()


func draw_to_hand(player_id: String) -> bool:
	if not _require_player(player_id):
		return false
	var card := draw_card()
	if card.is_empty():
		return false
	var player_hand: Array = _hands[player_id]
	player_hand.append(card)
	card_drawn.emit(player_id, str(card.get("id", "")))
	return true


func draw_many_to_hand(player_id: String, count: int) -> int:
	if not _require_player(player_id) or count < 0:
		if count < 0:
			push_error("Draw count cannot be negative.")
		return 0
	var drawn := 0
	for _index in range(count):
		if not draw_to_hand(player_id):
			break
		drawn += 1
	return drawn


func start_discard() -> Dictionary:
	if not _discard_pile.is_empty():
		return top_discard()
	var card := draw_card()
	if not card.is_empty():
		discard(card)
	return card.duplicate(true)


func remove_from_hand(player_id: String, card_id: String) -> Dictionary:
	if not _require_player(player_id) or card_id.is_empty():
		return {}
	var player_hand: Array = _hands[player_id]
	for index in range(player_hand.size()):
		if str(player_hand[index].get("id", "")) == card_id:
			return player_hand.pop_at(index)
	return {}


func discard_from_hand(player_id: String, card_id: String) -> Dictionary:
	var card := remove_from_hand(player_id, card_id)
	if not card.is_empty():
		discard(card)
	return card


func discard(card: Dictionary) -> bool:
	if not _configured or card.is_empty() or not (card.get("id") is String) or card.id.is_empty():
		push_error("A configured card system can only discard cards with IDs.")
		return false
	if not _known_card_ids.has(card.id):
		push_error("Cannot discard a card that was not part of the configured deck.")
		return false
	if _contains_card_id(card.id):
		push_error("Cannot discard a card that is already in a pile or hand.")
		return false
	var stored_card := card.duplicate(true)
	_discard_pile.append(stored_card)
	card_discarded.emit(str(stored_card["id"]))
	return true


func recycle_discards() -> bool:
	if _discard_pile.size() <= 1:
		return false
	var top_card: Dictionary = _discard_pile.pop_back()
	var recycled_count := _discard_pile.size()
	for card in _discard_pile:
		_draw_pile.append((card as Dictionary).duplicate(true))
	_discard_pile.clear()
	_discard_pile.append(top_card)
	_shuffle(_draw_pile)
	discard_recycled.emit(recycled_count)
	return true


func hand(player_id: String) -> Array:
	if not _require_player(player_id):
		return []
	return (_hands[player_id] as Array).duplicate(true)


func top_discard() -> Dictionary:
	return {} if _discard_pile.is_empty() else _discard_pile.back().duplicate(true)


func draw_count() -> int:
	return _draw_pile.size()


func discard_count() -> int:
	return _discard_pile.size()


func restore(state: Dictionary) -> bool:
	for required_key in ["draw_pile", "discard_pile", "hands", "auto_recycle", "random_state"]:
		if not state.has(required_key):
			push_error("Card restore is missing required field: %s" % required_key)
			return false
	var draw_pile = state.get("draw_pile", null)
	var discard_pile = state.get("discard_pile", null)
	var hands = state.get("hands", null)
	if not (draw_pile is Array) or not (discard_pile is Array) or not (hands is Dictionary):
		push_error("Card restore requires a full snapshot containing draw_pile, discard_pile, and hands.")
		return false

	var card_ids := {}
	var validation_error := _validate_card_collection(draw_pile, card_ids)
	if validation_error.is_empty():
		validation_error = _validate_card_collection(discard_pile, card_ids)
	if not validation_error.is_empty():
		push_error(validation_error)
		return false

	var restored_hands := {}
	for player_id_variant in hands:
		if not (player_id_variant is String):
			push_error("Card restore requires string player IDs.")
			return false
		var player_id: String = player_id_variant
		var player_hand = hands[player_id_variant]
		if player_id.is_empty() or not (player_hand is Array):
			push_error("Card restore requires non-empty player IDs with array hands.")
			return false
		validation_error = _validate_card_collection(player_hand, card_ids)
		if not validation_error.is_empty():
			push_error(validation_error)
			return false
		restored_hands[player_id] = player_hand.duplicate(true)
	if restored_hands.is_empty():
		push_error("Card restore requires at least one player hand.")
		return false
	if not (state.auto_recycle is bool):
		push_error("Card restore auto_recycle must be a boolean.")
		return false
	if not (state.random_state is int):
		push_error("Card restore random_state must be an integer.")
		return false

	_draw_pile.clear()
	for card in draw_pile:
		_draw_pile.append((card as Dictionary).duplicate(true))
	_discard_pile.clear()
	for card in discard_pile:
		_discard_pile.append((card as Dictionary).duplicate(true))
	_hands = restored_hands
	_known_card_ids = card_ids.duplicate()
	_auto_recycle = state.auto_recycle
	_random.state = state.random_state
	_configured = true
	return true


func snapshot(include_hands: bool = false) -> Dictionary:
	var hand_sizes := {}
	var hands := {}
	for player_id in _hands:
		hand_sizes[player_id] = (_hands[player_id] as Array).size()
		if include_hands:
			hands[player_id] = (_hands[player_id] as Array).duplicate(true)
	var state := {
		"draw_count": draw_count(),
		"discard_count": discard_count(),
		"top_discard": top_discard(),
		"hand_sizes": hand_sizes,
	}
	if include_hands:
		state["draw_pile"] = _draw_pile.duplicate(true)
		state["discard_pile"] = _discard_pile.duplicate(true)
		state["hands"] = hands
		state["auto_recycle"] = _auto_recycle
		state["random_state"] = _random.state
	return state


func _shuffle(cards: Array[Dictionary]) -> void:
	for index in range(cards.size() - 1, 0, -1):
		var swap_index := _random.randi_range(0, index)
		var temporary := cards[index]
		cards[index] = cards[swap_index]
		cards[swap_index] = temporary


func _require_configured() -> bool:
	if _configured:
		return true
	push_error("Card system is not configured.")
	return false


func _require_player(player_id: String) -> bool:
	if not _require_configured():
		return false
	if _hands.has(player_id):
		return true
	push_error("Unknown player ID: %s" % player_id)
	return false


func _contains_card_id(card_id: String) -> bool:
	for card in _draw_pile:
		if card.id == card_id:
			return true
	for card in _discard_pile:
		if card.id == card_id:
			return true
	for player_hand in _hands.values():
		for card in player_hand:
			if card.id == card_id:
				return true
	return false


func _validate_configuration(cards: Array, player_ids: Array) -> String:
	if cards.is_empty():
		return "Card system requires at least one card."
	if player_ids.is_empty():
		return "Card system requires at least one player."

	var card_ids := {}
	for card in cards:
		if not card is Dictionary:
			return "Every card must be a dictionary."
		if not (card.get("id") is String) or card.id.is_empty():
			return "Every card requires a non-empty string id."
		var card_id: String = card.id
		if card_ids.has(card_id):
			return "Card IDs must be unique."
		card_ids[card_id] = true

	var unique_players := {}
	for player_id in player_ids:
		if not player_id is String or player_id.is_empty():
			return "Player IDs must be non-empty strings."
		if unique_players.has(player_id):
			return "Player IDs must be unique."
		unique_players[player_id] = true
	return ""


func _validate_card_collection(cards: Array, card_ids: Dictionary) -> String:
	for card in cards:
		if not (card is Dictionary):
			return "Every restored card must be a dictionary."
		if not (card.get("id") is String) or card.id.is_empty():
			return "Every restored card requires a non-empty string id."
		var card_id: String = card.id
		if card_ids.has(card_id):
			return "Restored card IDs must be unique across all piles and hands."
		card_ids[card_id] = true
	return ""
