"""Prompt HTTP routes share the native MCP asset lock and path boundary."""
import asyncio
import os
from pathlib import Path
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.api.dependencies import get_task_service
from src.services import prompt_assets as assets

router = APIRouter(prefix="/api/prompts", tags=["prompts"])
_PROMPTS_DIR = Path("prompts").absolute()


def _safe_prompt_path(filename: str) -> Path:
    # Validate tokens, never normalize traversal or dereference a symlink.
    name = assets.prompt_name(filename)
    with assets.directory(_PROMPTS_DIR):
        pass
    path = _PROMPTS_DIR / name
    if path.is_symlink():
        assets.reject("unsafe_prompt_path")
    return path


def _root(request):
    return assets.resolve_root(request.app)


class PromptUpdate(BaseModel):
    content: str


@router.get("")
async def list_prompts(request: Request):
    root = _root(request)
    if not root.is_dir():
        return []
    with assets.directory(root) as fd:
        return sorted(name for name in os.listdir(fd) if name.endswith(".txt") and not (root / name).is_symlink())


@router.get("/{filename}")
async def get_prompt(filename: str, request: Request):
    content = await asyncio.to_thread(assets.read_prompt, filename, _root(request), nonempty=False)
    return {"filename": assets.prompt_name(filename), "content": content}


@router.put("/{filename}")
async def update_prompt(filename: str, prompt_update: PromptUpdate, request: Request,
                        service=Depends(get_task_service)):
    # REST may edit an in-use prompt; the same lock serializes it against task CAS.
    await asyncio.to_thread(assets.mutate_prompt, service.repository, filename,
        root=_root(request), content=prompt_update.content, block_references=False)
    return {"message": f"Prompt 文件 '{assets.prompt_name(filename)}' 更新成功"}
