"""Flat .txt prompt assets; references are always resolved through TaskService."""
import asyncio
from pydantic import Field
from src.services import prompt_assets as assets

from src.api import dependencies as deps
from src.mcp.task_tools import StrictModel, TaskWrite, UpdateTask, TaskChanges, tool
from src.services import mcp_task_operations as ops


class PromptName(StrictModel):
    filename: str = Field(min_length=1, max_length=240)


class PromptCreate(PromptName):
    content: str = Field(min_length=1, max_length=200000)


class PromptWrite(PromptName):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromptUpdate(PromptWrite):
    content: str = Field(min_length=1, max_length=200000)


class SetTaskCriteria(TaskWrite):
    description: str = Field(min_length=1, max_length=200000)
    content: str = Field(min_length=1, max_length=200000)


class ListPrompts(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


async def references(ctx, filename):
    service = await ops.dependency(ctx, deps.get_task_service)
    return assets.references_in_tasks(await service.get_all_tasks(), filename)


async def get_prompt(model, ctx):
    content = ops.read_prompt(ctx, model.filename)
    return {"filename": ops.prompt_path(ctx, model.filename).name, "content": content, "revision": ops.revision(content), "references": await references(ctx, model.filename)}


async def list_prompts(model, ctx):
    root = ops.prompt_root(ctx)
    names = sorted(path.name for path in root.glob("*.txt") if path.is_file() and not path.is_symlink())
    selected = names[model.offset:model.offset + model.limit]
    items = [{"filename": name} for name in selected]
    return {"items": items, "total": len(names), "offset": model.offset, "limit": model.limit, "has_more": model.offset + len(items) < len(names)}


async def create_prompt(model, ctx):
    ops.check_content(model.content)
    await asyncio.to_thread(assets.write_prompt, model.filename, model.content, ops.prompt_root(ctx), create=True)
    return await get_prompt(model, ctx)


async def update_prompt(model, ctx):
    ops.check_content(model.content)
    existing = await get_prompt(model, ctx)
    if existing["revision"] != model.expected_revision:
        ops.fail("revision_conflict", 409)
    if existing["references"]:
        ops.fail("prompt_in_use", 409)
    service = await ops.dependency(ctx, deps.get_task_service)
    await asyncio.to_thread(assets.mutate_prompt, service.repository, model.filename,
        root=ops.prompt_root(ctx), content=model.content, expected_revision=model.expected_revision)
    return await get_prompt(model, ctx)


async def preview_prompt_deletion(model, ctx):
    existing = await get_prompt(model, ctx)
    return {"filename": existing["filename"], "revision": existing["revision"], "references": existing["references"], "can_delete": not existing["references"]}


async def delete_prompt(model, ctx):
    preview = await preview_prompt_deletion(model, ctx)
    if preview["revision"] != model.expected_revision:
        ops.fail("revision_conflict", 409)
    if not preview["can_delete"]:
        ops.fail("prompt_in_use", 409)
    service = await ops.dependency(ctx, deps.get_task_service)
    await asyncio.to_thread(assets.mutate_prompt, service.repository, model.filename,
        root=ops.prompt_root(ctx), delete=True, expected_revision=model.expected_revision)
    return {"filename": assets.prompt_name(model.filename), "deleted": True}


async def set_task_criteria(model, ctx):
    change = UpdateTask(task_id=model.task_id, expected_revision=model.expected_revision, changes=TaskChanges(description=model.description), criteria_content=model.content)
    return await ops.update_task(change, ctx)


def build_tools():
    return [
        tool("set_task_criteria", "Set description and criteria together using a unique task-specific file. Never overwrite shared keyword criteria.", SetTaskCriteria, "tasks:write", set_task_criteria),
        tool("list_prompts", "List .txt prompt assets with bounded pagination.", ListPrompts, "prompts:read", list_prompts, read_only=True),
        tool("get_prompt", "Read prompt content, content revision and task references.", PromptName, "prompts:read", get_prompt, read_only=True),
        tool("create_prompt", "Create a new nonempty .txt prompt; never overwrite an existing file.", PromptCreate, "prompts:write", create_prompt),
        tool("update_prompt", "Update a prompt after checking its content revision.", PromptUpdate, "prompts:write", update_prompt),
        tool("preview_prompt_deletion", "Preview prompt deletion and blocking task references.", PromptName, "prompts:read", preview_prompt_deletion, read_only=True),
        tool("delete_prompt", "Delete an unreferenced prompt after checking its content revision.", PromptWrite, "prompts:delete", delete_prompt, destructive=True),
    ]
