"""Native MCP task tools; schemas deliberately reject unknown fields."""
from pydantic import BaseModel, ConfigDict, Field
from src.domain.models.task import TaskCreate, TaskUpdate, TaskGenerateRequest
from src.services import mcp_task_operations as ops


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTask(TaskCreate):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class GenerateTask(TaskGenerateRequest):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    ai_prompt_base_file: str = "prompts/base_prompt.txt"
    reference_file: str = "prompts/macbook_criteria.txt"


class GenerationJob(StrictModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class TaskId(StrictModel):
    task_id: int = Field(ge=0)


class TaskWrite(TaskId):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskChanges(TaskUpdate):
    model_config = ConfigDict(extra="forbid")


class UpdateTask(TaskWrite):
    changes: TaskChanges
    criteria_content: str | None = Field(default=None, max_length=200000)


class CloneTask(TaskWrite):
    task_name: str = Field(min_length=1, max_length=200)
    enabled: bool = False


class ListTasks(StrictModel):
    keyword: str | None = None
    enabled: bool | None = None
    is_running: bool | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class SetTaskSchedule(TaskWrite):
    cron: str | None


class SetTaskEnabled(TaskWrite):
    enabled: bool


class DeleteTask(TaskWrite):
    purge: bool = False


class ValidateTaskConfig(StrictModel):
    config: CreateTask


async def get_task(model, ctx):
    return await ops.task_response(ctx, model.task_id)


def tool(name, description, model, scope, handler, *, read_only=False, destructive=False):
    async def guarded(validated_model, ctx):
        if read_only:
            return await handler(validated_model, ctx)
        async with ops.mutation_lock():
            return await handler(validated_model, ctx)
    return {"name": name, "description": description, "input_model": model, "scope": scope, "read_only": read_only, "destructive": destructive, "handler": guarded}


async def set_task_schedule(model, ctx):
    return await ops.update_task(UpdateTask(task_id=model.task_id, expected_revision=model.expected_revision, changes=TaskChanges(cron=model.cron)), ctx)


async def set_task_enabled(model, ctx):
    return await ops.update_task(UpdateTask(task_id=model.task_id, expected_revision=model.expected_revision, changes=TaskChanges(enabled=model.enabled)), ctx)


def build_tools():
    return [
        tool("generate_task", "Explicit AI generation: returns status_code=202 and a job, not a completed task. Poll get_generation_job. New task disabled by default; isolated criteria.", GenerateTask, "tasks:write", ops.generate_task),
        tool("get_generation_job", "Read a durable generation receipt with immutable created-task snapshot. partial_success means persisted task but scheduler repair required; unknown is not proof of non-execution and must not be automatically retried.", GenerationJob, "tasks:read", ops.get_generation_job, read_only=True),
        tool("set_task_schedule", "Set or remove cron after checking revision; reload scheduler.", SetTaskSchedule, "tasks:write", set_task_schedule),
        tool("set_task_enabled", "Enable/disable future scheduling; does not start or stop a running task.", SetTaskEnabled, "tasks:write", set_task_enabled),
        tool("start_task", "Start an enabled task through ProcessService; may crawl and notify.", TaskWrite, "tasks:run", ops.start_task),
        tool("stop_task", "Stop a running task through ProcessService and verify.", TaskWrite, "tasks:run", ops.stop_task),
        tool("get_task_runtime_status", "Read runtime/scheduler state without starting anything.", TaskId, "tasks:read", ops.get_task_runtime_status, read_only=True),
        tool("preview_task_deletion", "Preview configuration-only deletion and shared-keyword tasks; no deletion.", TaskId, "tasks:read", ops.preview_task_deletion, read_only=True),
        tool("delete_task", "Stop and delete configuration only. History/logs/prompts preserved. Purge is unsupported and rejected.", DeleteTask, "tasks:delete", ops.delete_task, destructive=True),
        tool("create_task", "Create a task, disabled by default; never starts a crawler.", CreateTask, "tasks:write", ops.create_task),
        tool("list_tasks", "List/filter tasks with bounded pagination.", ListTasks, "tasks:read", ops.list_tasks, read_only=True),
        tool("update_task", "Update configuration, no implicit AI; description and criteria must be synchronized.", UpdateTask, "tasks:write", ops.update_task),
        tool("preview_task_change", "Validate changes and show a diff without writing or invoking AI.", UpdateTask, "tasks:read", ops.preview_task_change, read_only=True),
        tool("clone_task", "Clone a task with isolated criteria; disabled by default.", CloneTask, "tasks:write", ops.clone_task),
        tool("validate_task_config", "Validate a complete config, prices, cron, account strategy and prompt assets without writing.", ValidateTaskConfig, "tasks:read", ops.validate_task_config, read_only=True),
        tool("get_task", "Read task configuration and its stable revision.", TaskId, "tasks:read", get_task, read_only=True),
    ]
