"""Native tools over stored result data; never start a crawler."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import result_storage_service as storage
from src.mcp.result_admin_helpers import Input, redact, revision, require_revision, tool
from src.mcp.outcomes import MutationOutcomeError


class Page(Input):
    page: int = Field(default=1, ge=1, le=100000)
    limit: int = Field(default=50, ge=1, le=100)


class ResultSet(Input):
    filename: str = Field(min_length=7, max_length=255)

    @field_validator("filename")
    @classmethod
    def filename_safe(cls, value):
        if (not value.endswith(".jsonl") or ".." in value or
                any(c in value for c in "/\\\x00") or any(ord(c) < 32 for c in value)):
            raise ValueError("invalid_result_filename")
        return value


class Query(ResultSet, Page):
    include_hidden: bool = False
    ai_recommended_only: bool = False
    keyword_recommended_only: bool = False
    sort_by: Literal["crawl_time", "publish_time", "price", "keyword_hit_count"] = "crawl_time"
    sort_order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def exclusive_filter(self):
        if self.ai_recommended_only and self.keyword_recommended_only:
            raise ValueError("conflicting_filters")
        return self


class ResultItem(ResultSet):
    item_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def safe_item(record):
    product = record.get("商品信息") or {}
    analysis = record.get("ai_analysis") or {}
    return {
        "item_id": redact(product.get("商品ID"), 128),
        "title": redact(product.get("商品标题"), 512),
        "price": redact(product.get("当前售价"), 64),
        "crawl_time": redact(record.get("爬取时间"), 64),
        "status": record.get("_status", "active"),
        "is_recommended": analysis.get("is_recommended") is True,
        "analysis_source": analysis.get("analysis_source") if analysis.get("analysis_source") in {"ai", "keyword"} else None,
        "hidden_reason": record.get("_hidden_reason"),
        "effective_hidden": record.get("_effective_hidden", False),
    }


def item_rows(conn, args):
    rows = conn.execute("SELECT id, raw_json, status FROM result_items WHERE result_filename=? AND item_id=? ORDER BY id",
                        (args.filename, args.item_id)).fetchall()
    if not rows:
        raise HTTPException(404, "result_not_found")
    return [dict(row) for row in rows]


def item_response(conn, args):
    rows = item_rows(conn, args)
    record = json.loads(rows[0]["raw_json"])
    record = storage._decorate_record_visibility(record, rows[0]["status"],
                storage._load_blacklist_keywords_from_conn(conn, args.filename))
    return {"filename": args.filename, "item": safe_item(record), "revision": revision(rows)}


async def list_sets(args, ctx):
    names = await storage.list_result_filenames()
    start = (args.page - 1) * args.limit
    return {"items": [{"filename": name} for name in names[start:start + args.limit]],
            "total": len(names), "page": args.page, "limit": args.limit,
            "has_more": start + args.limit < len(names)}


async def stored_page(filename, *, page, limit, include_hidden, ai_recommended_only,
                      keyword_recommended_only, sort_by, sort_order):
    """Stream the existing visibility rules; retain only the bounded safe page."""
    def read():
        bootstrap_sqlite_storage()
        where, params = storage._build_query_conditions(filename=filename,
            ai_recommended_only=ai_recommended_only, keyword_recommended_only=keyword_recommended_only)
        order = storage._sort_expression(sort_by, sort_order)
        with sqlite_connection() as conn:
            conn.execute("BEGIN")
            if conn.execute("SELECT 1 FROM result_items WHERE result_filename=? LIMIT 1", (filename,)).fetchone() is None:
                raise HTTPException(404, "result_set_not_found")
            rules = storage._load_blacklist_keywords_from_conn(conn, filename)
            total, selected = 0, []
            offset = (page - 1) * limit
            for row in conn.execute(f"SELECT raw_json, status FROM result_items WHERE {where} ORDER BY {order}", tuple(params)):
                record = storage._decorate_record_visibility(json.loads(row["raw_json"]), row["status"], rules)
                if not include_hidden and not storage._is_record_visible(record):
                    continue
                if offset <= total < offset + limit:
                    selected.append(safe_item(record))
                total += 1
            return total, selected
    return await asyncio.to_thread(read)


async def query(args, ctx):
    total, items = await stored_page(**args.model_dump())
    return {"items": items, "total": total,
            "page": args.page, "limit": args.limit, "has_more": args.page * args.limit < total}


async def get_item(args, ctx):
    def read():
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            return item_response(conn, args)
    return await asyncio.to_thread(read)


class Versioned(Input):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatusWrite(ResultItem, Versioned):
    status: Literal["active", "hidden", "expired"]


async def set_status(args, ctx):
    def write():
        bootstrap_sqlite_storage()
        with mutation_connection() as (conn, finish):
            conn.execute("BEGIN IMMEDIATE")
            require_revision(args.expected_revision, item_rows(conn, args))
            conn.execute("UPDATE result_items SET status=? WHERE result_filename=? AND item_id=?",
                         (args.status, args.filename, args.item_id))
            response = item_response(conn, args)
            return finish(response)
    return await asyncio.to_thread(write)


class BlacklistWrite(ResultSet, Versioned):
    action: Literal["add", "remove", "replace"]
    keywords: list[str] = Field(max_length=200)

    @field_validator("keywords")
    @classmethod
    def bounded_keywords(cls, values):
        if any(not v.strip() or len(v) > 128 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("invalid_keyword")
        return values


@contextmanager
def mutation_connection():
    committed = False
    try:
        with sqlite_connection() as conn:
            def finish(response):
                nonlocal committed
                result = commit_response(conn, response)
                committed = True
                return result
            yield conn, finish
    except MutationOutcomeError:
        raise
    except Exception:
        if committed:
            raise MutationOutcomeError("unknown", {"success": False, "commit_attempted": True,
                "committed": True, "outcome": "unknown", "retry_allowed": False,
                "error_code": "postcommit_cleanup_failed"}) from None
        raise


def commit_response(conn, response):
    """Freeze readback inside the write transaction, then report that commit."""
    try:
        conn.commit()
    except Exception:
        raise MutationOutcomeError("unknown", {"success": False, "commit_attempted": True,
            "committed": None, "outcome": "unknown", "retry_allowed": False,
            "error_code": "commit_confirmation_unavailable"}) from None
    return {**response, "committed": True}


def blacklist_state(conn, filename):
    # AUTOINCREMENT row identity is never reused after deleting/recreating a set.
    row = conn.execute("SELECT MIN(id) AS incarnation FROM result_items WHERE result_filename=?", (filename,)).fetchone()
    if row["incarnation"] is None:
        raise HTTPException(404, "result_set_not_found")
    return {"filename": filename, "incarnation": row["incarnation"],
            "keywords": storage._load_blacklist_keywords_from_conn(conn, filename)}


def blacklist_response(conn, filename):
    state = blacklist_state(conn, filename)
    return {"filename": filename, "keywords": [redact(k, 128) for k in state["keywords"]],
            "revision": revision(state)}


async def get_blacklist(args, ctx):
    def read():
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            conn.execute("BEGIN")
            return blacklist_response(conn, args.filename)
    return await asyncio.to_thread(read)


async def update_blacklist(args, ctx):
    from src.services.result_blacklist_service import normalize_blacklist_keywords

    def write():
        bootstrap_sqlite_storage()
        with mutation_connection() as (conn, finish):
            conn.execute("BEGIN IMMEDIATE")
            state = blacklist_state(conn, args.filename)
            require_revision(args.expected_revision, state)
            current = state["keywords"]
            incoming = normalize_blacklist_keywords(args.keywords)
            if args.action == "add":
                incoming = normalize_blacklist_keywords(current + incoming)
            elif args.action == "remove":
                incoming = [k for k in current if k not in incoming]
            if len(incoming) > 200:
                raise HTTPException(422, "too_many_rules")
            conn.execute("INSERT INTO result_blacklist_rules(result_filename, blacklist_keywords_json, updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(result_filename) DO UPDATE SET blacklist_keywords_json=excluded.blacklist_keywords_json, updated_at=excluded.updated_at",
                         (args.filename, json.dumps(incoming, ensure_ascii=False)))
            response = blacklist_response(conn, args.filename)
            return finish(response)
    return await asyncio.to_thread(write)


class DeleteSet(ResultSet, Versioned):
    pass


def cleanup_snapshot(conn, filename):
    import hashlib
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute("SELECT * FROM result_items WHERE result_filename=? ORDER BY id", (filename,)):
        digest.update(revision(dict(row)).encode())
        count += 1
    return {"filename": filename, "record_count": count, "records_digest": digest.hexdigest(),
            "blacklist": storage._load_blacklist_keywords_from_conn(conn, filename)}


async def preview_cleanup(args, ctx):
    def read():
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            conn.execute("BEGIN")
            snapshot = cleanup_snapshot(conn, args.filename)
            if not snapshot["record_count"]:
                raise HTTPException(404, "result_set_not_found")
            return {"filename": args.filename, "record_count": snapshot["record_count"],
                    "blacklist_rule_count": len(snapshot["blacklist"]),
                    "price_history_retained": True, "revision": revision(snapshot)}
    return await asyncio.to_thread(read)


async def delete_set(args, ctx):
    def write():
        bootstrap_sqlite_storage()
        with mutation_connection() as (conn, finish):
            conn.execute("BEGIN IMMEDIATE")
            snapshot = cleanup_snapshot(conn, args.filename)
            require_revision(args.expected_revision, snapshot)
            if not snapshot["record_count"]:
                raise HTTPException(404, "result_set_not_found")
            deleted = conn.execute("DELETE FROM result_items WHERE result_filename=?", (args.filename,)).rowcount
            conn.execute("DELETE FROM result_blacklist_rules WHERE result_filename=?", (args.filename,))
            after = cleanup_snapshot(conn, args.filename)
            return finish({"filename": args.filename, "deleted_records": deleted,
                    "remaining_records": after["record_count"], "price_history_retained": True,
                    "revision": revision(after)})
    return await asyncio.to_thread(write)


class Export(ResultSet):
    format: Literal["csv", "jsonl"] = "jsonl"
    max_records: int = Field(default=100, ge=1, le=1000)
    include_hidden: bool = False


async def export(args, ctx):
    import csv
    import io

    total, items = await stored_page(args.filename, page=1,
        limit=args.max_records, include_hidden=args.include_hidden,
        ai_recommended_only=False, keyword_recommended_only=False,
        sort_by="crawl_time", sort_order="desc")
    output, size, count = [], 0, 0
    for item in items:
        if args.format == "jsonl":
            part = json.dumps(item, ensure_ascii=False) + "\n"
        else:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=list(item))
            if count == 0:
                writer.writeheader()
            # Spreadsheet formula neutralization, including whitespace prefixes.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for k, v in item.items()})
            part = stream.getvalue()
        part_size = len(part.encode("utf-8"))
        if size + part_size > 49152:
            break
        output.append(part)
        size += part_size
        count += 1
    return {"format": args.format, "content": "".join(output), "bytes": size,
            "exported_records": count, "total_matching": total, "complete": count == total,
            "truncated": count < total, "max_records": args.max_records, "max_bytes": 49152}


class Insights(ResultSet):
    window_days: int = Field(default=30, ge=1, le=365)


async def price_insights(args, ctx):
    from src.services.price_history_service import build_price_history_insights
    if not await storage.result_file_exists(args.filename):
        raise HTTPException(404, "result_set_not_found")
    def read():
        visible = storage.load_visible_result_item_ids(args.filename)
        value = build_price_history_insights(args.filename.removesuffix("_full_data.jsonl"),
                    window_days=args.window_days, visible_item_ids=visible)
        # Service only supplies numerical aggregate fields and observation dates.
        return {"source": "stored_snapshots", **value}
    return await asyncio.to_thread(read)


def build_tools():
    return [
        tool("get_price_insights", "Read existing price snapshot aggregates for visible results; does not crawl or call AI.", Insights, "results:read", price_insights),
        tool("export_results", "Return bounded safe CSV/JSONL inline (at most 1000 records/48 KiB); never writes a path; reports truncation.", Export, "results:read", export),
        tool("preview_result_cleanup", "Preview deletion of one result set; price history is retained.", ResultSet, "results:read", preview_cleanup),
        tool("delete_result_set", "Delete exactly the previewed set and its rules, only at the expected revision; retain price history.", DeleteSet, "results:delete", delete_set, read_only=False, destructive=True),
        tool("get_blacklist_rules", "Read blacklist rules and revision for a stored result set.", ResultSet, "results:read", get_blacklist),
        tool("update_blacklist_rules", "Add, remove or replace bounded blacklist rules; requires revision.", BlacklistWrite, "results:write", update_blacklist, read_only=False),
        tool("set_result_status", "Change an item status with a required current revision and readback.", StatusWrite, "results:write", set_status, read_only=False),
        tool("list_result_sets", "List stored result sets with bounded pagination.", Page, "results:read", list_sets),
        tool("query_results", "Query stored results, never crawl; safe output fields only.", Query, "results:read", query),
        tool("get_result", "Read a stored item and its status revision.", ResultItem, "results:read", get_item),
    ]
