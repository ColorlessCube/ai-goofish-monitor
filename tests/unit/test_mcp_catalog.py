"""Accepted capability list and machine permission/schema contracts."""
from src.mcp.authentication import ALL_SCOPES
from src.mcp.remote import get_tools, tool_schema

EXPECTED = {
    "get_invocation",
    "list_tasks", "get_task", "create_task", "generate_task", "get_generation_job",
    "clone_task", "update_task", "set_task_schedule", "set_task_enabled",
    "set_task_criteria", "start_task", "stop_task", "get_task_runtime_status",
    "validate_task_config", "preview_task_change", "preview_task_deletion", "delete_task",
    "list_prompts", "get_prompt", "create_prompt", "update_prompt",
    "preview_prompt_deletion", "delete_prompt",
    "list_result_sets", "query_results", "get_result", "get_price_insights",
    "set_result_status", "get_blacklist_rules", "update_blacklist_rules",
    "export_results", "preview_result_cleanup", "delete_result_set",
    "get_health", "get_dashboard_summary", "get_system_status", "get_task_logs",
    "get_account_source_status", "get_failure_guard_status", "list_account_metadata",
    "get_ai_settings", "update_ai_settings", "test_ai_connection",
    "get_notification_settings", "update_notification_settings", "test_notification",
    "get_rotation_settings", "update_rotation_settings",
}


def test_catalog_covers_approved_list_without_duplicates():
    tools = get_tools()
    assert {t["name"] for t in tools} == EXPECTED
    assert len(tools) == len(EXPECTED)


def test_all_tools_have_explicit_scope_and_closed_input_schema():
    for tool in get_tools():
        assert tool["scope"] in ALL_SCOPES, tool["name"]
        schema = tool_schema(tool)
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"].get("additionalProperties") is False, tool["name"]
        assert schema["annotations"]["readOnlyHint"] is tool["read_only"]
        if not tool["read_only"]:
            assert "request_id" in schema["inputSchema"]["required"], tool["name"]
            assert schema["inputSchema"]["properties"]["request_id"]["format"] == "uuid"


def test_external_effect_tools_are_not_readonly_and_have_separate_grants():
    tools = {t["name"]: t for t in get_tools()}
    for name in ("start_task", "stop_task", "generate_task", "test_ai_connection", "test_notification"):
        assert tools[name]["read_only"] is False, name
    assert tools["start_task"]["scope"] == "tasks:run"
    assert tools["test_notification"]["scope"] == "notifications:send"
    assert tools["test_ai_connection"]["scope"] == "settings:test"
