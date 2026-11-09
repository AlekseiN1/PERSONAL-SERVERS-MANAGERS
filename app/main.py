"""FastAPI app: API + server-side UI (Jinja2/HTMX)."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import close_client
from app.routers import servers, tasks
from app.services import (
    backup_lookup_map,
    backup_server_summary,
    compare_stacks,
    format_backup_ts,
    list_backups,
    list_images_grouped,
    list_servers_grouped,
    safe_path_part,
)
from app.templating import templates

templates.env.globals["safe_path_part"] = safe_path_part
templates.env.globals["fmt_ts"] = format_backup_ts


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_client()


app = FastAPI(title="Personal Servers Manager", lifespan=lifespan)

app.include_router(servers.router)
app.include_router(tasks.router)

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    groups = await list_servers_grouped()
    stack_names = sorted({s for g in groups if g.reachable for s in g.stacks})
    return templates.TemplateResponse(
        request, "index.html", {"groups": groups, "stack_names": stack_names}
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare(request: Request, stack: str | None = None) -> HTMLResponse:
    groups = await list_servers_grouped()
    stack_names = sorted({s for g in groups if g.reachable for s in g.stacks})
    comparison = await compare_stacks(stack) if stack else None
    return templates.TemplateResponse(
        request, "compare.html",
        {"stack_names": stack_names, "stack": stack, "cmp": comparison},
    )


@app.get("/backups", response_class=HTMLResponse)
async def backups(request: Request) -> HTMLResponse:
    groups = await list_servers_grouped()
    stack_names = sorted({s for g in groups if g.reachable for s in g.stacks})
    return templates.TemplateResponse(
        request, "backups.html",
        {
            "groups": groups, "stack_names": stack_names, "backups": list_backups(),
            "backups_map": backup_lookup_map(), "server_backups": backup_server_summary(),
            "backup_dir_abs": str(Path(settings.backup_dir).resolve()),
        },
    )


@app.get("/images", response_class=HTMLResponse)
async def images(request: Request) -> HTMLResponse:
    groups = await list_images_grouped()
    return templates.TemplateResponse(request, "images.html", {"groups": groups})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    groups = await list_servers_grouped()
    stack_names = sorted({s for g in groups if g.reachable for s in g.stacks})
    return templates.TemplateResponse(
        request, "logs.html", {"groups": groups, "stack_names": stack_names}
    )
