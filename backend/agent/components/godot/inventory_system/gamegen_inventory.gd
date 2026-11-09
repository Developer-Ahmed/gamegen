class_name GamegenInventory
extends RefCounted


signal changed(snapshot: Dictionary)


var _capacity := 0
var _definitions: Dictionary = {}
var _quantities: Dictionary = {}


func configure(capacity: int, item_definitions: Array) -> bool:
	if capacity <= 0:
		push_error("Inventory capacity must be positive.")
		return false
	if item_definitions.is_empty():
		push_error("Inventory requires at least one item definition.")
		return false

	var definitions := {}
	for raw_definition in item_definitions:
		if not (raw_definition is Dictionary):
			push_error("Every item definition must be a dictionary.")
			return false
		var definition: Dictionary = raw_definition
		if not (definition.get("id") is String) or definition.id.is_empty():
			push_error("Every item definition requires a non-empty string id.")
			return false
		if not (definition.get("max_stack", 1) is int):
			push_error("Item max_stack must be an integer.")
			return false
		var item_id: String = definition.id
		var max_stack: int = definition.get("max_stack", 1)
		if max_stack <= 0:
			push_error("Item max_stack must be positive.")
			return false
		if definitions.has(item_id):
			push_error("Item IDs must be unique.")
			return false
		var normalized := definition.duplicate(true)
		normalized["id"] = item_id
		normalized["max_stack"] = max_stack
		definitions[item_id] = normalized

	_capacity = capacity
	_definitions = definitions
	_quantities.clear()
	return true


func is_configured() -> bool:
	return _capacity > 0


func capacity() -> int:
	return _capacity


func remaining_capacity() -> int:
	return maxi(_capacity - total_items(), 0)


func has_definition(item_id: String) -> bool:
	return _definitions.has(item_id)


func definition(item_id: String) -> Dictionary:
	if not has_definition(item_id):
		return {}
	return (_definitions[item_id] as Dictionary).duplicate(true)


func quantity(item_id: String) -> int:
	return int(_quantities.get(item_id, 0))


func can_add(item_id: String, amount: int = 1) -> bool:
	if not has_definition(item_id) or amount <= 0:
		return false
	var max_stack := int((_definitions[item_id] as Dictionary)["max_stack"])
	return quantity(item_id) + amount <= max_stack and amount <= remaining_capacity()


func can_remove(item_id: String, amount: int = 1) -> bool:
	return has_definition(item_id) and amount > 0 and quantity(item_id) >= amount


func add(item_id: String, amount: int = 1) -> bool:
	return apply_transaction({item_id: amount})


func remove(item_id: String, amount: int = 1) -> bool:
	if amount <= 0:
		return false
	return apply_transaction({item_id: -amount})


func apply_transaction(changes: Dictionary) -> bool:
	if not is_configured() or changes.is_empty():
		return false

	var next_quantities := _quantities.duplicate(true)
	for item_id_variant in changes:
		if not (item_id_variant is String):
			return false
		var item_id: String = item_id_variant
		if not has_definition(item_id):
			return false
		if not (changes[item_id_variant] is int):
			return false
		var delta: int = changes[item_id_variant]
		if delta == 0:
			continue
		var next_quantity := int(next_quantities.get(item_id, 0)) + delta
		var max_stack := int((_definitions[item_id] as Dictionary)["max_stack"])
		if next_quantity < 0 or next_quantity > max_stack:
			return false
		if next_quantity == 0:
			next_quantities.erase(item_id)
		else:
			next_quantities[item_id] = next_quantity

	var next_total := 0
	for amount in next_quantities.values():
		next_total += int(amount)
	if next_total > _capacity:
		return false

	_quantities = next_quantities
	changed.emit(snapshot())
	return true


func clear() -> void:
	if _quantities.is_empty():
		return
	_quantities.clear()
	changed.emit(snapshot())


func restore(state: Dictionary) -> bool:
	if not is_configured():
		return false
	if int(state.get("capacity", -1)) != _capacity:
		return false
	var quantities = state.get("quantities", {})
	if not (quantities is Dictionary):
		return false

	var restored := {}
	var restored_total := 0
	for item_id_variant in quantities:
		if not (item_id_variant is String):
			return false
		var item_id: String = item_id_variant
		if not has_definition(item_id):
			return false
		if not (quantities[item_id_variant] is int):
			return false
		var amount: int = quantities[item_id_variant]
		var max_stack := int((_definitions[item_id] as Dictionary)["max_stack"])
		if amount <= 0 or amount > max_stack:
			return false
		restored[item_id] = amount
		restored_total += amount
	if restored_total > _capacity:
		return false

	_quantities = restored
	changed.emit(snapshot())
	return true


func total_items() -> int:
	var total := 0
	for amount in _quantities.values():
		total += int(amount)
	return total


func snapshot() -> Dictionary:
	return {
		"capacity": _capacity,
		"remaining_capacity": remaining_capacity(),
		"total_items": total_items(),
		"quantities": _quantities.duplicate(true),
	}
