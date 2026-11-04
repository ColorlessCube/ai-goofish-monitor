"""Safe MCP business operations. Never delegate to side-effectful REST handlers."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
import hashlib
import inspect
import json
from pathlib import Path
from typing import NoReturn
from uuid import uuid4
from src.domain.models.task import TaskCreate, TaskUpdate

from fastapi import HTTPException

from src.api import dependencies as deps
from src.services.task_payloads import serialize_task
from src.prompt_utils import generate_criteria
from src.services import prompt_assets as assets
from src.services.generation_receipts import GenerationReceipts, settle


def fail(code: str, status: int = 400) -> NoReturn:
    raise HTTPException(status_code=status, detail=code)


async def dependency(ctx, factory):
    provider = ctx["app"].dependency_overrides.get(factory, factory)
    result = provider()
    result = await result if inspect.isawaitable(result) else result
    if factory is deps.get_task_service and hasattr(result.repository, "prompts_dir"):
        result.repository.prompts_dir = prompt_root(ctx)
    return result


def revision(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_WRITE_LOCK = threading.Lock()


@asynccontextmanager
async def mutation_lock():
    """Serialize native MCP writes, including generation's worker event loop.

    REST/external filesystem writers are outside this process-local lock.
    """
    while not _WRITE_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.005)
    try:
        yield
    finally:
        _WRITE_LOCK.release()


def task_revision(task, ctx) -> str:
    config = task.model_dump(mode="json", exclude={"is_running"})
    assets = {}
    for field in (("ai_prompt_base_file", "ai_prompt_criteria_file") if task.decision_mode == "ai" else ()):
        filename = getattr(task, field)
        try:
            assets[field] = revision(read_prompt(ctx, filename))
        except (HTTPException, OSError):
            assets[field] = "unavailable"
    return revision({"config": config, "assets": assets})


async def require_task(service, task_id):
    task = await service.get_task(task_id)
    if task is None:
        fail("task_not_found", 404)
    return task


async def task_response(ctx, task_id):
    service = await dependency(ctx, deps.get_task_service)
    scheduler = await dependency(ctx, deps.get_scheduler_service)
    task = await require_task(service, task_id)
    payload = serialize_task(task, scheduler)
    payload["revision"] = task_revision(task, ctx)
    return {"task": payload, "revision": payload["revision"]}


async def reload_scheduler(ctx, service):
    scheduler = await dependency(ctx, deps.get_scheduler_service)
    await scheduler.reload_jobs(await service.get_all_tasks())


def prompt_root(ctx):
    root = assets.resolve_root(ctx["app"])
    if root.is_symlink() or any(parent.is_symlink() for parent in root.absolute().parents):
        fail("unsafe_prompt_path")
    return root.absolute()


def prompt_path(ctx, filename):
    if not isinstance(filename, str) or not filename or "\\" in filename or "\x00" in filename:
        fail("unsafe_prompt_path")
    # Flat filenames only; accept the persisted prompts/ prefix, never normalize '..'.
    name = filename[8:] if filename.startswith("prompts/") else filename
    if "/" in name or name in {".", ".."} or not name.endswith(".txt"):
        fail("unsafe_prompt_path")
    path = prompt_root(ctx) / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        fail("unsafe_prompt_path")
    return path


def read_prompt(ctx, filename):
    return assets.read_prompt(filename, prompt_root(ctx))


def validate_config(model, ctx):
    from decimal import Decimal, InvalidOperation
    from src.domain.models.task import TaskCreate
    config = TaskCreate.model_validate(model.model_dump())
    if not config.task_name.strip() or not config.keyword.strip() or config.max_pages < 1:
        fail("invalid_task_config")
    prices = []
    for value in (config.min_price, config.max_price):
        try:
            price = Decimal(value) if value is not None else None
        except (InvalidOperation, ValueError):
            fail("invalid_price_range")
        if price is not None and (not price.is_finite() or price < 0):
            fail("invalid_price_range")
        prices.append(price)
    if all(price is not None for price in prices) and prices[0] > prices[1]:
        fail("invalid_price_range")
    for field in ("ai_prompt_base_file", "ai_prompt_criteria_file"):
        setattr(config, field, assets.canonical_reference(getattr(config, field)))
    if config.decision_mode == "ai":
        read_prompt(ctx, config.ai_prompt_base_file)
        read_prompt(ctx, config.ai_prompt_criteria_file)
    else:
        # Keyword mode does not consume prompt files. Validate path syntax only;
        # switching to AI later must validate actual nonempty assets.
        for filename in (config.ai_prompt_base_file, config.ai_prompt_criteria_file):
            if filename:
                prompt_path(ctx, filename)
    return config


async def ensure_unique_name(service, name, exclude_id=None):
    if any(task.task_name == name and task.id != exclude_id for task in await service.get_all_tasks()):
        fail("task_name_in_use", 409)


async def create_task(model, ctx):
    model = validate_config(model, ctx)
    service = await dependency(ctx, deps.get_task_service)
    await ensure_unique_name(service, model.task_name)
    task = await service.create_task(model)
    await reload_scheduler(ctx, service)
    return await task_response(ctx, task.id)


def check_revision(task, expected, ctx):
    if task_revision(task, ctx) != expected:
        fail("revision_conflict", 409)


def check_content(content):
    if not content or not content.strip():
        fail("empty_prompt")


def write_unique_criteria(ctx, content, prefix="task"):
    check_content(content)
    name = f"{prefix}_{uuid4().hex}_criteria.txt"
    assets.write_prompt(name, content, prompt_root(ctx), create=True)
    return f"prompts/{name}"


async def list_tasks(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    tasks = await service.get_all_tasks()
    if model.keyword is not None:
        needle = model.keyword.casefold()
        tasks = [task for task in tasks if needle in task.keyword.casefold() or needle in task.task_name.casefold()]
    if model.enabled is not None:
        tasks = [task for task in tasks if task.enabled == model.enabled]
    if model.is_running is not None:
        tasks = [task for task in tasks if task.is_running == model.is_running]
    total = len(tasks)
    items = [(await task_response(ctx, task.id))["task"] for task in tasks[model.offset:model.offset + model.limit]]
    return {"items": items, "total": total, "offset": model.offset, "limit": model.limit, "has_more": model.offset + len(items) < total}


async def prepare_update(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    existing = await require_task(service, model.task_id)
    check_revision(existing, model.expected_revision, ctx)
    changes = model.changes.model_dump(exclude_unset=True)
    if "is_running" in changes:
        fail("runtime_field_not_writable")
    # Same final-account semantics as REST, without invoking REST's AI side effects.
    from src.services.account_strategy_service import normalize_account_strategy
    if "account_state_file" in changes and "account_strategy" not in changes and existing.account_strategy != "ass":
        changes["account_strategy"] = normalize_account_strategy(None, changes["account_state_file"])
    merged = existing.model_dump(exclude={"id", "is_running"}) | changes
    await ensure_unique_name(service, merged["task_name"], existing.id)
    if merged.get("decision_mode") == "ai":
        changed_description = merged.get("description") != existing.description
        changed_mode = existing.decision_mode != "ai"
        changed_criteria = "ai_prompt_criteria_file" in changes and changes["ai_prompt_criteria_file"] != existing.ai_prompt_criteria_file
        if (changed_description or changed_mode) and not (model.criteria_content is not None or changed_criteria):
            fail("criteria_sync_required")
        if model.criteria_content is not None or changed_criteria:
            if "description" not in changes:
                fail("criteria_sync_required")
    if model.criteria_content is not None:
        check_content(model.criteria_content)
        # Validate everything before creating an isolated file. The existing criteria
        # reference is validated separately; a new AI-mode task may have no old asset.
        candidate = TaskCreate.model_validate(merged)
        original = candidate.ai_prompt_criteria_file
        candidate.ai_prompt_criteria_file = candidate.ai_prompt_base_file
        config = validate_config(candidate, ctx)
        config.ai_prompt_criteria_file = original
    else:
        config = validate_config(TaskCreate.model_validate(merged), ctx)
    return service, existing, config


async def preview_task_change(model, ctx):
    _, existing, config = await prepare_update(model, ctx)
    before = existing.model_dump()
    after = config.model_dump()
    changes = {key: {"before": before.get(key), "after": value} for key, value in after.items() if before.get(key) != value}
    if model.criteria_content is not None:
        changes["criteria_content"] = {"before": None, "after": model.criteria_content}
    return {"task_id": existing.id, "revision": task_revision(existing, ctx), "changes": changes, "will_generate_ai": False, "will_start": False}


async def reduce_task_risk(model, ctx, changes):
    service = await dependency(ctx, deps.get_task_service)
    existing = await require_task(service, model.task_id)
    await service.update_task(existing.id, TaskUpdate(**changes), expected=existing,
        guard=lambda current: check_revision(current, model.expected_revision, ctx))
    await reload_scheduler(ctx, service)
    return await task_response(ctx, existing.id)


async def update_task(model, ctx):
    changes = model.changes.model_dump(exclude_unset=True)
    if changes and all((key == "enabled" and value is False) or (key == "cron" and value is None) for key, value in changes.items()) and model.criteria_content is None:
        return await reduce_task_risk(model, ctx, changes)
    service, existing, config = await prepare_update(model, ctx)
    if model.criteria_content is not None:
        config.ai_prompt_criteria_file = await asyncio.to_thread(write_unique_criteria, ctx, model.criteria_content, f"task_{existing.id}")
    try:
        await service.update_task(existing.id, TaskUpdate.model_validate(config.model_dump()), expected=existing, guard=lambda current: check_revision(current, model.expected_revision, ctx))
    except Exception:
        if model.criteria_content is not None:
            readback = await service.get_task(existing.id)
            if readback is None or readback.ai_prompt_criteria_file != config.ai_prompt_criteria_file:
                await asyncio.to_thread(assets.cleanup_unreferenced, service.repository,
                    config.ai_prompt_criteria_file, prompt_root(ctx))
        raise
    await reload_scheduler(ctx, service)
    return await task_response(ctx, existing.id)


async def clone_task(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    existing = await require_task(service, model.task_id)
    check_revision(existing, model.expected_revision, ctx)
    config = TaskCreate.model_validate(existing.model_dump(exclude={"id", "is_running"}) | {"task_name": model.task_name, "enabled": model.enabled})
    validate_config(config, ctx)
    await ensure_unique_name(service, config.task_name)
    task = await service.clone_task(existing, task_name=model.task_name, enabled=model.enabled,
        guard=lambda current: check_revision(current, model.expected_revision, ctx))
    await reload_scheduler(ctx, service)
    return await task_response(ctx, task.id)


async def validate_task_config(model, ctx):
    config = validate_config(model.config, ctx)
    return {"valid": True, "config": config.model_dump(mode="json"), "will_generate_ai": False}


async def get_task_runtime_status(model, ctx):
    task = (await task_response(ctx, model.task_id))["task"]
    process = await dependency(ctx, deps.get_process_service)
    running = process.is_running(model.task_id) if hasattr(process, "is_running") else task["is_running"]
    return {"task_id": model.task_id, "enabled": task["enabled"], "is_running": running, "next_run_at": task["next_run_at"], "revision": task["revision"]}


async def start_task(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    task = await require_task(service, model.task_id)
    check_revision(task, model.expected_revision, ctx)
    if not task.enabled:
        fail("task_disabled", 409)
    if (await get_task_runtime_status(model, ctx))["is_running"]:
        fail("task_already_running", 409)
    validate_config(task, ctx)
    await ensure_unique_name(service, task.task_name, task.id)
    process = await dependency(ctx, deps.get_process_service)
    if not await process.start_task(task.id, task.task_name, instance_id=task.instance_id, config_version=task.config_version):
        fail("task_start_failed", 409)
    return await task_response(ctx, task.id)


async def stop_task(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    task = await require_task(service, model.task_id)
    check_revision(task, model.expected_revision, ctx)
    process = await dependency(ctx, deps.get_process_service)
    await process.stop_task(task.id)
    if (await get_task_runtime_status(model, ctx))["is_running"]:
        fail("task_stop_failed", 409)
    return await task_response(ctx, task.id)


async def preview_task_deletion(model, ctx):
    service = await dependency(ctx, deps.get_task_service)
    task = await require_task(service, model.task_id)
    shared = [other.id for other in await service.get_all_tasks() if other.id != task.id and other.keyword.strip() == task.keyword.strip()]
    return {"task_id": task.id, "revision": task_revision(task, ctx), "deletes": ["task_configuration"], "preserves": ["results", "price_history", "logs", "prompts"], "shared_keyword_task_ids": shared, "purge_supported": False}


async def delete_task(model, ctx):
    if model.purge:
        # Existing result/history deleters use global storage. Never guess at scope.
        fail("purge_not_supported", 409)
    service = await dependency(ctx, deps.get_task_service)
    task = await require_task(service, model.task_id)
    check_revision(task, model.expected_revision, ctx)
    await stop_task(model, ctx)
    if not await service.delete_task(task.id, expected=task, guard=lambda current: check_revision(current, model.expected_revision, ctx)):
        fail("task_not_found", 404)
    await reload_scheduler(ctx, service)
    if await service.get_task(task.id) is not None:
        fail("task_delete_verification_failed", 500)
    return {"task_id": task.id, "deleted": True, "purged": False, "preserves": ["results", "price_history", "logs", "prompts"]}


async def get_generation_job(model, ctx):
    service = await dependency(ctx, deps.get_task_generation_service)
    tasks = await dependency(ctx, deps.get_task_service)
    return {"job": await GenerationReceipts(tasks.repository).observe(model.job_id, service)}


async def run_generation(model, ctx, job_id, generation):
    filename = None
    task = None
    content = None
    task_service = await dependency(ctx, deps.get_task_service)
    receipts = GenerationReceipts(task_service.repository)
    generation.begin_work(job_id)
    try:
        await receipts.change(job_id, status="running", current_step="prepare")
        await generation.advance(job_id, "prepare", "Preparing isolated criteria")
        if model.decision_mode == "ai":
            async def progress(step_key, message):
                await generation.advance(job_id, step_key, step_key)
            content = await generate_criteria(user_description=model.description, reference_file_path=model.reference_file, progress_callback=progress, prompts_dir=prompt_root(ctx))
        async with mutation_lock():
            task_service = await dependency(ctx, deps.get_task_service)
            await ensure_unique_name(task_service, model.task_name)
            if model.decision_mode == "ai":
                filename = await asyncio.to_thread(write_unique_criteria, ctx, content, "generation")
            config = TaskCreate.model_validate(model.model_dump(exclude={"reference_file"}) | {"ai_prompt_criteria_file": filename or ""})
            validate_config(config, ctx)
            def record_created(conn, created):
                snapshot = serialize_task(created, None)
                snapshot["revision"] = task_revision(created, ctx)
                receipts.change_in_transaction(conn, job_id, {"task": snapshot, "task_committed": True, "status": "running", "current_step": "scheduler"})
            task = await settle(task_service.create_task(config, on_saved=record_created))
            await reload_scheduler(ctx, task_service)
            outcome = await receipts.change(job_id, status="completed", current_step=None, error=None, retry_safe=False)
            await generation.publish(job_id, outcome)
    except BaseException as exc:
        outcome = await receipts.recover(job_id, generation, uncertain=not isinstance(exc, Exception))
        if filename is not None and outcome.get("task_committed") is False and outcome["status"] == "failed":
            await asyncio.to_thread(assets.cleanup_unreferenced, task_service.repository, filename, prompt_root(ctx))
        if not isinstance(exc, Exception):
            raise
    finally:
        generation.end_work(job_id)


async def generate_task(model, ctx):
    # Validate all user-controlled fields before enqueuing work or calling an LLM.
    probe = TaskCreate.model_validate(model.model_dump(exclude={"reference_file"}) | {"ai_prompt_criteria_file": model.ai_prompt_base_file if model.decision_mode == "ai" else ""})
    validate_config(probe, ctx)
    if model.decision_mode == "ai":
        read_prompt(ctx, model.reference_file)
    task_service = await dependency(ctx, deps.get_task_service)
    await ensure_unique_name(task_service, model.task_name)
    generation = await dependency(ctx, deps.get_task_generation_service)
    job = await generation.create_job(model.task_name)
    await GenerationReceipts(task_service.repository).create(job)
    generation.track(run_generation(model, ctx, job.job_id, generation), job_id=job.job_id)
    readback = await generation.get_job(job.job_id)
    return {"status_code": 202, "job": {"job_id": readback.job_id, "status": readback.status, "task_name": readback.task_name}}
