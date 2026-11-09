class_name GamegenWebRuntime
extends RefCounted


static var _published := false


static func publish_ready(smoke_result: Dictionary) -> void:
	if _published:
		push_warning("GamegenWebRuntime.publish_ready ignored a duplicate publication.")
		return
	var normalized := smoke_result.duplicate(true)
	if not normalized.has("ok"):
		normalized["ok"] = false
		normalized["details"] = "The game did not provide an explicit smoke-test result."

	print("[gamegen] ready smoke_result=", JSON.stringify(normalized))
	_published = true
	if not OS.has_feature("web"):
		return

	var payload := JSON.stringify(normalized)
	JavaScriptBridge.eval(
		"window.__GAMEGEN_READY__ = true;" +
		"window.__GAMEGEN_TEST__ = { smokeTest: async function () { return %s; } };" % payload,
		true,
	)
