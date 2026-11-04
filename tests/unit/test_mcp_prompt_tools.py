"""Prompt CRUD and task-isolated criteria against real files and SQLite."""
import importlib
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from test_mcp_task_tools import mcp_context, invoke, task_payload


async def prompt(name, payload, ctx):
    assert importlib.util.find_spec("src.mcp.prompt_tools") is not None, "MCP prompt tools not implemented"
    tools = {entry["name"]: entry for entry in importlib.import_module("src.mcp.prompt_tools").build_tools()}
    assert name in tools, f"missing prompt tool: {name}"
    entry = tools[name]
    return await entry["handler"](entry["input_model"].model_validate(payload), ctx)


@pytest.mark.asyncio
async def test_prompt_crud_revision_and_delete_preview(mcp_context):
    created = await prompt("create_prompt", {"filename": "new.txt", "content": "Accept normal devices"}, mcp_context)
    assert created["content"] == "Accept normal devices"
    assert created["revision"]
    assert await prompt("get_prompt", {"filename": "new.txt"}, mcp_context) == created
    listed = await prompt("list_prompts", {"offset": 0, "limit": 1}, mcp_context)
    assert listed["total"] == 3 and len(listed["items"]) == 1 and listed["has_more"] is True
    args = {"filename": "new.txt", "expected_revision": created["revision"], "content": "No repairs"}
    updated = await prompt("update_prompt", args, mcp_context)
    assert updated["revision"] != created["revision"]
    assert Path("prompts/new.txt").read_text() == "No repairs"
    with pytest.raises(HTTPException) as conflict:
        await prompt("update_prompt", args, mcp_context)
    assert conflict.value.detail == "revision_conflict"
    preview = await prompt("preview_prompt_deletion", {"filename": "new.txt"}, mcp_context)
    assert preview["can_delete"] is True and preview["references"] == []
    result = await prompt("delete_prompt", {"filename": "new.txt", "expected_revision": updated["revision"]}, mcp_context)
    assert result["deleted"] is True and not Path("prompts/new.txt").exists()


@pytest.mark.asyncio
async def test_referenced_prompt_cannot_change_or_delete_and_criteria_are_task_isolated(mcp_context):
    first = (await invoke("create_task", task_payload(task_name="first"), mcp_context))["task"]
    second = (await invoke("create_task", task_payload(task_name="second"), mcp_context))["task"]
    current = await prompt("get_prompt", {"filename": "criteria.txt"}, mcp_context)
    preview = await prompt("preview_prompt_deletion", {"filename": "criteria.txt"}, mcp_context)
    assert preview["can_delete"] is False and len(preview["references"]) == 2
    for action, extra in [("delete_prompt", {}), ("update_prompt", {"content": "dangerous shared overwrite"})]:
        with pytest.raises(HTTPException) as exc:
            await prompt(action, {"filename": "criteria.txt", "expected_revision": current["revision"], **extra}, mcp_context)
        assert exc.value.detail == "prompt_in_use"
    changed = (await prompt("set_task_criteria", {"task_id": first["id"], "expected_revision": first["revision"], "description": "Reject repairs", "content": "Reject repairs"}, mcp_context))["task"]
    assert changed["description"] == "Reject repairs"
    assert changed["ai_prompt_criteria_file"] != second["ai_prompt_criteria_file"]
    assert Path(changed["ai_prompt_criteria_file"]).read_text() == "Reject repairs"
    assert (await invoke("get_task", {"task_id": second["id"]}, mcp_context))["task"] == second
    assert Path("prompts/criteria.txt").read_text() == "Only normal functioning devices"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../outside.txt", "/tmp/outside.txt", "prompts/../outside.txt", "..\\outside.txt", "file.json", "nested/file.txt", "linked.txt"])
async def test_prompt_paths_never_escape_or_follow_symlinks(mcp_context, filename):
    outside = Path("outside.txt")
    outside.write_text("never modify")
    Path("prompts/linked.txt").symlink_to(outside.absolute())
    for action, extra in [("get_prompt", {}), ("create_prompt", {"content": "replacement"}), ("update_prompt", {"content": "replacement", "expected_revision": "0" * 64}), ("delete_prompt", {"expected_revision": "0" * 64})]:
        with pytest.raises((HTTPException, ValidationError)):
            await prompt(action, {"filename": filename, **extra}, mcp_context)
    assert outside.read_text() == "never modify"
    listing = await prompt("list_prompts", {}, mcp_context)
    assert "linked.txt" not in [item["filename"] for item in listing["items"]]
