"""Task routes: env update, normalize keys, propagate stack (JSON + HTMX partials)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.models import BackupRequest, BackupResult, EnvUpdateRequest, TaskResult
from app.portainer import parse_env_text
from app.services import (
    backup_stacks,
    delete_backup,
    delete_image,
    delete_unused_images,
    get_stack_logs,
    normalize_keys,
    propagate_stack,
    read_backup_content,
    restart_stack,
    restore_backup,
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


@router.post("/backup", response_model=list[BackupResult])
async def backup(req: BackupRequest) -> list[BackupResult]:
    """JSON API: backup compose+env of selected stacks to disk."""
    return await backup_stacks(req.targets, req.dry_run)


@router.post("/backup/ui", response_class=HTMLResponse)
async def backup_ui(
    request: Request,
    dry_run: bool = Form(False),
    targets: list[str] = Form(default=[]),
) -> HTMLResponse:
    """HTMX form submit: backup compose+env of selected stacks, returns per-stack results partial."""
    selection: list[str] | str = targets if targets else "all"
    results = await backup_stacks(selection, dry_run)
    return templates.TemplateResponse(request, "partials/backup_results.html", {"results": results})


@router.get("/backup-view", response_class=HTMLResponse)
async def backup_view(
    request: Request, server_dir: str, stack_dir: str, timestamp: str,
) -> HTMLResponse:
    """HTMX: read one saved backup's compose+env content off disk."""
    content = read_backup_content(server_dir, stack_dir, timestamp)
    return templates.TemplateResponse(request, "partials/backup_view.html", {"c": content})


@router.post("/backup-restore/ui", response_class=HTMLResponse)
async def backup_restore_ui(
    request: Request,
    server_dir: str = Form(...),
    stack_dir: str = Form(...),
    timestamp: str = Form(...),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: redeploy a stack from a saved backup (compose+env)."""
    result = await restore_backup(server_dir, stack_dir, timestamp, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": [result]})


@router.post("/backup-delete/ui", response_class=HTMLResponse)
async def backup_delete_ui(
    request: Request,
    server_dir: str = Form(...),
    stack_dir: str = Form(...),
    timestamp: str = Form(...),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: delete one saved backup (compose+env) from disk."""
    result = delete_backup(server_dir, stack_dir, timestamp, dry_run)
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
    _, env = await get_main_stack_source(stack)  # env is taken from the main server as-is
    results = await propagate_stack(stack, compose, env, server_ids, dry_run)
    return templates.TemplateResponse(request, "partials/results.html", {"results": results})


@router.post("/image-delete/ui", response_class=HTMLResponse)
async def image_delete_ui(
    request: Request,
    server_id: str = Form(...),
    image_id: str = Form(...),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: delete one docker image on a server."""
    result = await delete_image(server_id, image_id, dry_run)
    return templates.TemplateResponse(request, "partials/image_results.html", {"results": [result]})


@router.post("/images-delete-unused/ui", response_class=HTMLResponse)
async def images_delete_unused_ui(
    request: Request,
    server_id: str = Form(...),
    dry_run: bool = Form(False),
) -> HTMLResponse:
    """HTMX: delete every image on a server that isn't used by any container."""
    results = await delete_unused_images(server_id, dry_run)
    return templates.TemplateResponse(request, "partials/image_results.html", {"results": results})


@router.post("/logs/ui", response_class=HTMLResponse)
async def logs_ui(
    request: Request,
    tail: int = Form(200),
    targets: list[str] = Form(default=[]),
) -> HTMLResponse:
    """HTMX: logs of containers for ONLY the selected stacks — no 'all', by design
    nothing loads until the user has explicitly checked some stacks."""
    tail = max(10, min(tail, 2000))
    results = await get_stack_logs(targets, tail)
    return templates.TemplateResponse(
        request, "partials/log_results.html", {"results": results, "selected": bool(targets)},
    )
