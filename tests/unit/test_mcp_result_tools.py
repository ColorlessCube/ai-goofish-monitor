"""Native result tools exercise an isolated real SQLite database only."""
import asyncio
import importlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from src.services.result_storage_service import save_result_record


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in MCP result unit tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def result_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "results.sqlite3"))
    return {"app": FastAPI(), "principal": {"subject": "unit", "scopes": {
        "results:read", "results:write", "results:delete"}}}


def call(ctx, name, **arguments):
    module = importlib.import_module("src.mcp.result_tools")
    tool = next(t for t in module.build_tools() if t["name"] == name)
    return asyncio.run(tool["handler"](tool["input_model"](**arguments), ctx))


def seed(item_id="1", title="fixture camera", keyword="camera"):
    asyncio.run(save_result_record({
        "搜索关键字": keyword, "任务名称": "fixture", "爬取时间": "2026-01-01T01:00:00",
        "商品信息": {"商品ID": item_id, "商品标题": title, "当前售价": "100",
                     "商品链接": f"https://www.goofish.com/item?id={item_id}"},
        "cookie": "fixture-cookie-secret", "OPENAI_API_KEY": "fixture-key-secret",
        "ai_analysis": {"is_recommended": True, "analysis_source": "ai"},
    }, keyword))


def test_result_query_is_bounded_and_allowlisted(result_context):
    seed()
    seed("2")
    listed = call(result_context, "list_result_sets", limit=1)
    assert listed["total"] == 1
    assert listed["items"][0]["filename"] == "camera_full_data.jsonl"
    page = call(result_context, "query_results", filename="camera_full_data.jsonl", limit=1)
    assert page["total"] == 2 and page["has_more"] is True
    assert len(page["items"]) == 1
    assert "fixture-cookie-secret" not in json.dumps(page)
    assert "fixture-key-secret" not in json.dumps(page)
    item = call(result_context, "get_result", filename="camera_full_data.jsonl", item_id="1")
    assert item["item"]["title"] == "fixture camera"
    assert len(item["revision"]) == 64


def test_status_write_checks_revision_and_reads_back(result_context):
    seed()
    args = {"filename": "camera_full_data.jsonl", "item_id": "1"}
    before = call(result_context, "get_result", **args)
    updated = call(result_context, "set_result_status", **args,
                   expected_revision=before["revision"], status="hidden")
    assert updated["item"]["status"] == "hidden"
    assert updated["revision"] != before["revision"]
    assert updated["committed"] is True
    assert call(result_context, "get_result", **args) == {k: v for k, v in updated.items() if k != "committed"}
    with pytest.raises(HTTPException) as exc:
        call(result_context, "set_result_status", **args,
             expected_revision=before["revision"], status="expired")
    assert exc.value.status_code == 409
    assert updated["committed"] is True
    assert call(result_context, "get_result", **args) == {k: v for k, v in updated.items() if k != "committed"}


def test_blacklist_add_remove_replace_with_revision(result_context):
    seed()
    args = {"filename": "camera_full_data.jsonl"}
    before = call(result_context, "get_blacklist_rules", **args)
    assert before["keywords"] == []
    one = call(result_context, "update_blacklist_rules", **args, action="add",
               keywords=["camera", "lens"], expected_revision=before["revision"])
    assert one["keywords"] == ["camera", "lens"]
    assert call(result_context, "query_results", **args)["total"] == 0
    with pytest.raises(HTTPException) as exc:
        call(result_context, "update_blacklist_rules", **args, action="replace",
             keywords=[], expected_revision=before["revision"])
    assert exc.value.status_code == 409
    two = call(result_context, "update_blacklist_rules", **args, action="remove",
               keywords=["camera"], expected_revision=one["revision"])
    assert two["keywords"] == ["lens"]
    three = call(result_context, "update_blacklist_rules", **args, action="replace",
                 keywords=[], expected_revision=two["revision"])
    assert three["keywords"] == []
    assert three["committed"] is True
    assert call(result_context, "get_blacklist_rules", **args) == {k: v for k, v in three.items() if k != "committed"}


def test_cleanup_preview_requires_exact_unchanged_target(result_context):
    seed()
    seed("other", keyword="other")
    args = {"filename": "camera_full_data.jsonl"}
    preview = call(result_context, "preview_result_cleanup", **args)
    assert preview["record_count"] == 1
    seed("2")
    with pytest.raises(HTTPException) as exc:
        call(result_context, "delete_result_set", **args, expected_revision=preview["revision"])
    assert exc.value.status_code == 409
    latest = call(result_context, "preview_result_cleanup", **args)
    deleted = call(result_context, "delete_result_set", **args, expected_revision=latest["revision"])
    assert deleted["deleted_records"] == 2 and deleted["remaining_records"] == 0
    assert call(result_context, "list_result_sets")["items"] == [{"filename": "other_full_data.jsonl"}]


@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_export_is_bounded_safe_and_never_writes_paths(result_context, tmp_path, format):
    seed("1", "=2+2")
    seed("2", "@SUM(A1:A3)")
    exported = call(result_context, "export_results", filename="camera_full_data.jsonl",
                    format=format, max_records=1)
    assert exported["total_matching"] == 2
    assert exported["exported_records"] == 1 and exported["truncated"] is True
    assert exported["complete"] is False
    assert "fixture-cookie-secret" not in exported["content"]
    if format == "csv":
        assert "'@SUM" in exported["content"]
    else:
        assert len(exported["content"].splitlines()) == 1
    assert not list(tmp_path.glob("*.csv")) and not list(tmp_path.glob("*.jsonl"))
    with pytest.raises(ValidationError):
        call(result_context, "export_results", filename="camera_full_data.jsonl", path="/tmp/stolen")


def test_prices_use_existing_snapshots_without_scraping(result_context):
    from src.services.price_history_service import record_market_snapshots
    seed()
    empty = call(result_context, "get_price_insights", filename="camera_full_data.jsonl")
    assert empty["latest_snapshot_at"] is None
    record_market_snapshots(keyword="camera", task_name="fixture", run_id="fixture-run",
        snapshot_time="2026-01-01T00:00:00", items=[
            {"商品ID": "1", "当前售价": "100"}, {"商品ID": "not-visible", "当前售价": "9999"}])
    result = call(result_context, "get_price_insights", filename="camera_full_data.jsonl")
    assert result["history_summary"]["unique_items"] == 1
    assert result["market_summary"]["avg_price"] == 100
    assert result["source"] == "stored_snapshots"


def test_export_respects_transport_budget_and_remains_parseable(result_context):
    for i in range(250):
        seed(str(i), "中" * 512)
    result = call(result_context, "export_results", filename="camera_full_data.jsonl", max_records=1000)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 110000
    assert result["truncated"] is True and result["complete"] is False
    parsed = [json.loads(line) for line in result["content"].splitlines()]
    assert len(parsed) == result["exported_records"] > 0


@pytest.mark.parametrize("filename", ["../x.jsonl", "/x.jsonl", "a\\\\b.jsonl", "bad\u0000.jsonl"])
def test_result_paths_are_rejected(result_context, filename):
    with pytest.raises(ValidationError):
        call(result_context, "query_results", filename=filename)


def test_status_concurrency_allows_only_one_revision_winner(result_context):
    seed()
    args = {"filename": "camera_full_data.jsonl", "item_id": "1"}
    rev = call(result_context, "get_result", **args)["revision"]
    module = importlib.import_module("src.mcp.result_tools")
    tool = next(t for t in module.build_tools() if t["name"] == "set_result_status")
    async def race():
        return await asyncio.gather(*(tool["handler"](tool["input_model"](**args, expected_revision=rev, status=status), result_context)
                                     for status in ("hidden", "expired")), return_exceptions=True)
    outcomes = asyncio.run(race())
    assert sum(isinstance(v, dict) for v in outcomes) == 1
    assert sum(isinstance(v, HTTPException) and v.status_code == 409 for v in outcomes) == 1


def test_csv_neutralizes_control_prefixed_formulas(result_context):
    import csv
    import io
    seed("1", "\u0000=SUM(A1:A2)")
    result = call(result_context, "export_results", filename="camera_full_data.jsonl", format="csv")
    row = next(csv.DictReader(io.StringIO(result["content"])))
    assert row["title"].startswith("'")
    assert "\u0000" not in result["content"]
