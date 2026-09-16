"""Minimal backend context response for unrelated Manager lifecycle tests."""


async def manager_context_response(action, params, **kwargs):
    assert action == "get_manager_context"
    key = params["context_key"]
    data = {}
    if key.startswith("workstream:"):
        data = {"workstream_id": key.split(":", 1)[1], "workstream_name": "Test workstream"}
    if params["include_history"]:
        data["chat_history"] = ""
    return {"context_key": key, "context_data": data}
