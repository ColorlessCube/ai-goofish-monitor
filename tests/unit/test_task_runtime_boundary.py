"""Exercise real runtime entry points with a stub crawler, never spawn/network."""
import asyncio
import importlib
import json
import sys
import types

import pytest

from src.domain.models.task import TaskCreate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


def runtime(monkeypatch):
    fake = types.ModuleType("src.scraper")
    captured = []
    async def scrape(task_config, debug_limit):
        captured.append(task_config)
        return 0
    fake.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake)
    sys.modules.pop("spider_v2", None)
    return importlib.import_module("spider_v2"), captured


def test_spider_rejects_symlink_prompt_at_consumption(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/base_prompt.txt").write_text("Base {{CRITERIA_SECTION}}")
    (tmp_path / "outside.txt").write_text("PRIVATE FILE MUST NOT BE READ")
    (tmp_path / "prompts/criteria.txt").symlink_to(tmp_path / "outside.txt")
    config = [{"task_name": "test", "keyword": "camera", "enabled": True, "account_strategy": "ass", "ai_prompt_base_file": "prompts/base_prompt.txt", "ai_prompt_criteria_file": "prompts/criteria.txt"}]
    (tmp_path / "config.json").write_text(json.dumps(config))
    spider, captured = runtime(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(tmp_path / "config.json")])
    asyncio.run(spider.main())
    assert captured == [], "Invalid AI prompt must fail closed, not read arbitrary content or crawl without screening"


def test_process_dispatches_id_and_instance_not_name(tmp_path, monkeypatch):
    from src.services.process_service import ProcessService
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "db.sqlite"))
    task = asyncio.run(TaskService(SqliteTaskRepository(legacy_config_file=None)).create_task(TaskCreate(task_name="label", keyword="camera", decision_mode="keyword", keyword_rules=["ok"])))
    service = ProcessService()
    command = service._build_spawn_command(task.task_name, task_id=task.id, instance_id=task.instance_id, config_version=task.config_version)
    assert "--task-name" not in command
    assert command[command.index("--task-id") + 1] == str(task.id)
    assert command[command.index("--task-instance") + 1] == task.instance_id
    assert command[command.index("--expected-config-version") + 1] == str(task.config_version)
    with pytest.raises(ValueError, match="requires config_version"):
        service._build_spawn_command(task.task_name, task_id=task.id, instance_id=task.instance_id)


def test_spider_immutable_instance_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = {"id": 0, "instance_id": "new-instance", "task_name": "same", "enabled": True, "account_strategy": "ass", "decision_mode": "keyword", "keyword_rules": ["ok"]}
    (tmp_path / "config.json").write_text(json.dumps([task]))
    spider, captured = runtime(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(tmp_path / "config.json"), "--task-id", "0", "--task-instance", "old-instance"])
    # No argparse error: a stale instance is a valid command but must not run.
    asyncio.run(spider.main())
    assert captured == []
