"""Task routes: env update, normalize keys, propagate stack (JSON + HTMX partials)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.models import EnvUpdateRequest, TaskResult
from app.portainer import parse_env_text
from app.services import (
    normalize_keys,
    propagate_stack,
    restart_stack,
    run_env_update,
    save_stack,
)
from app.templating import templates

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/env-update", response_model=list[TaskResult])
async def env_update(req: EnvUpdateRequest) -> list[TaskResult]:
    """JSON API: update one env variable across selected stacks."""
    return await run_env_update(req.targets, req.key, req.value, req.dry_run)


@router.post("/env-update/ui", response_class=HTMLResponse)
async def env_update_ui(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    dry_run: bool = Form(False),
    targets: list[str] = Form(default=[]),
) -> HTMLResponse:
    """HTMX form submit: returns per-stack results partial."""
    selection: list[str] | str = targets if targets else "all"
    results = await run_env_update(selection, key, value, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": results})


@router.post("/normalize/ui", response_class=HTMLResponse)
async def normalize_ui(
    request: Request,
    stack: str = Form(...),
    dry_run: bool = Form(False),
    server_ids: list[str] = Form(default=[]),
) -> HTMLResponse:
    """HTMX: align env KEYS of the stack to the main server on selected servers."""
    results = await normalize_keys(stack, server_ids, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": results})


@router.post("/stack-save/ui", response_class=HTMLResponse)
async def stack_save_ui(
    request: Request,
    server_id: str = Form(...),
    stack: str = Form(...),
    compose: str = Form(...),
    env_text: str = Form(default=""),
    pull_image: bool = Form(False),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: save edited compose + env of one stack and redeploy it."""
    env = parse_env_text(env_text)
    result = await save_stack(server_id, stack, compose, env, pull_image, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": [result]})


@router.post("/stack-restart/ui", response_class=HTMLResponse)
async def stack_restart_ui(
    request: Request,
    server_id: str = Form(...),
    stack: str = Form(...),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: restart one stack (stop then start)."""
    result = await restart_stack(server_id, stack, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": [result]})


@router.post("/propagate/ui", response_class=HTMLResponse)
async def propagate_ui(
    request: Request,
    stack: str = Form(...),
    compose: str = Form(...),
    dry_run: bool = Form(False),
    server_ids: list[str] = Form(default=[]),
) -> HTMLResponse:
    """HTMX: create the stack (from edited compose + main's env) on selected servers."""
    from app.services import get_main_stack_source
    _, env = await get_main_stack_source(stack)  # env берём с главного как есть
    results = await propagate_stack(stack, compose, env, server_ids, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": results})
