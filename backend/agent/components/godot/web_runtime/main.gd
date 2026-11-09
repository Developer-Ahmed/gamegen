extends Control


signal game_started
signal game_reset


@onready var title_label: Label = %TitleLabel
@onready var status_label: Label = %StatusLabel
@onready var playfield: Control = %Playfield
@onready var primary_action: Button = %PrimaryAction

var game_is_active := false
var session_revision := 0
var game_state: Dictionary = {}
{{GAMEGEN_COMPONENT_MEMBERS}}


func _ready() -> void:
	primary_action.pressed.connect(start_game)
	set_status("Ready")
	initialize_game_state()
	render_game()
	call_deferred("_publish_ready_after_render")


func _publish_ready_after_render() -> void:
	await get_tree().process_frame
	publish_smoke_result(run_isolated_smoke_tests())


func start_game() -> void:
	if game_is_active:
		return
	game_is_active = true
	session_revision += 1
	primary_action.disabled = true
	primary_action.text = "Game in progress"
	set_status("Game started")
	initialize_game_state()
	render_game()
	game_started.emit()


func reset_game() -> void:
	game_is_active = false
	primary_action.disabled = false
	primary_action.text = "Start game"
	set_status("Ready")
	initialize_game_state()
	render_game()
	game_reset.emit()


func set_game_title(value: String) -> void:
	title_label.text = value.strip_edges() if not value.strip_edges().is_empty() else "Game"


func set_status(value: String) -> void:
	status_label.text = value


func clear_playfield() -> void:
	for child in playfield.get_children():
		child.queue_free()


func submit_game_action(action: Dictionary) -> bool:
	if not game_is_active or not handle_game_action(action):
		return false
	var outcome := evaluate_game_state()
	if outcome.has("status"):
		set_status(str(outcome["status"]))
	if bool(outcome.get("finished", false)):
		game_is_active = false
		primary_action.disabled = false
		primary_action.text = "Play again"
	render_game()
	return true


func publish_smoke_result(result: Dictionary) -> void:
	GamegenWebRuntime.publish_ready(result)


func initialize_game_state() -> void:
	pass


func handle_game_action(_action: Dictionary) -> bool:
	return false


func evaluate_game_state() -> Dictionary:
	return {
		"finished": false,
		"winner": "",
	}


func render_game() -> void:
	pass


func run_isolated_smoke_tests() -> Dictionary:
	return smoke_test_gameplay()


func smoke_test_gameplay() -> Dictionary:
	return {"ok": false, "details": "Gameplay smoke test is not implemented."}
