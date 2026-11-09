extends Node


func _ready() -> void:
	var turns := GamegenTurnOrder.new()
	assert(not turns.configure(["p1", "p1"]))
	assert(turns.configure(["p1", "p2", "p3"]))
	assert(turns.current_player() == "p1")
	assert(turns.advance() == "p2")
	assert(turns.skip() == "p1")
	assert(turns.reverse())
	assert(turns.advance() == "p3")
	var turn_state := turns.snapshot()
	assert(turns.advance(0) == turn_state.active_player)
	assert(turns.snapshot() == turn_state)
	assert(turns.skip(-1) == turn_state.active_player)
	assert(turns.snapshot() == turn_state)
	assert(not turns.configure(["p1", "p1"]))
	assert(turns.snapshot() == turn_state)
	assert(not turns.restore({
		"players": ["p1", "p2"],
		"active_player": "p2",
		"active_index": 0,
		"direction": 1,
		"turn_number": 0,
	}))
	assert(turns.snapshot() == turn_state)
	var restored_turns := GamegenTurnOrder.new()
	assert(restored_turns.restore(turn_state))
	assert(restored_turns.snapshot() == turn_state)

	var cards := GamegenCardSystem.new()
	assert(not cards.configure([{"id": "same"}, {"id": "same"}], ["p1"], 1))
	assert(cards.configure(
		[
			{"id": "red-1", "color": "red", "value": 1},
			{"id": "blue-1", "color": "blue", "value": 1},
			{"id": "green-1", "color": "green", "value": 1},
			{"id": "yellow-1", "color": "yellow", "value": 1},
			{"id": "red-2", "color": "red", "value": 2},
		],
		["p1", "p2"],
		42,
	))
	assert(cards.deal(1))
	var before_failed_deal := cards.snapshot(true)
	assert(not cards.deal(100))
	assert(cards.snapshot(true) == before_failed_deal)
	assert(cards.hand("p1").size() == 1)
	assert(cards.hand("p2").size() == 1)
	assert(not cards.start_discard().is_empty())
	assert(cards.draw_many_to_hand("p1", 2) == 2)
	var played_card: Dictionary = cards.hand("p1")[0]
	assert(not cards.discard_from_hand("p1", str(played_card["id"])).is_empty())
	assert(not cards.discard(played_card))
	assert(not cards.discard({"id": "not-configured"}))
	assert(cards.draw_to_hand("p2"))
	assert(cards.discard_count() == 1)
	var card_state := cards.snapshot(true)
	var invalid_card_state := card_state.duplicate(true)
	invalid_card_state["random_state"] = "invalid"
	assert(not cards.restore(invalid_card_state))
	assert(cards.snapshot(true) == card_state)
	var restored_cards := GamegenCardSystem.new()
	assert(restored_cards.restore(card_state))
	assert(restored_cards.snapshot(true) == card_state)
	var recycling_cards := GamegenCardSystem.new()
	assert(recycling_cards.configure([
		{"id": "a"}, {"id": "b"}, {"id": "c"},
	], ["p1"], 7))
	assert(not recycling_cards.start_discard().is_empty())
	var second_discard := recycling_cards.draw_card()
	assert(recycling_cards.discard(second_discard))
	var expected_top := recycling_cards.top_discard()
	assert(not recycling_cards.draw_card().is_empty())
	assert(not recycling_cards.draw_card().is_empty())
	assert(recycling_cards.top_discard() == expected_top)

	var inventory := GamegenInventory.new()
	assert(not inventory.configure(3, []))
	assert(inventory.configure(3, [
		{"id": "key", "max_stack": 2},
		{"id": "coin", "max_stack": 3},
	]))
	var configured_inventory_state := inventory.snapshot()
	assert(not inventory.configure(0, [{"id": "bad"}]))
	assert(inventory.snapshot() == configured_inventory_state)
	assert(inventory.apply_transaction({"key": 2, "coin": 1}))
	var before_failed_transaction := inventory.snapshot()
	assert(not inventory.apply_transaction({"key": 0.5}))
	assert(inventory.snapshot() == before_failed_transaction)
	assert(not inventory.add("key", 1))
	assert(not inventory.apply_transaction({"key": -3, "coin": 1}))
	assert(inventory.quantity("coin") == 1)
	assert(inventory.remove("key"))
	assert(inventory.quantity("key") == 1)
	var inventory_state := inventory.snapshot()
	var incompatible_inventory_state := inventory_state.duplicate(true)
	incompatible_inventory_state["capacity"] = 99
	assert(not inventory.restore(incompatible_inventory_state))
	assert(inventory.snapshot() == inventory_state)
	inventory.clear()
	assert(inventory.restore(inventory_state))
	assert(inventory.quantity("key") == 1)

	GamegenWebRuntime.publish_ready({
		"ok": true,
		"details": "Godot component smoke test passed.",
	})
	print("GAMEGEN_COMPONENT_SMOKE_OK")
