"""Inventory routes: live stack listing (grouped by server) + stack detail/edit. Mongo read-only."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.models import ServerGroup
from app.portainer import format_env_text
from app.services import get_stack_detail, list_servers_grouped
from app.templating import templates

router = APIRouter(prefix="/servers", tags=["servers"])


def _dom_id(server_id: str, stack: str) -> str:
    """Safe DOM id from server_id+stack (non-alnum -> '-')."""
    return re.sub(r"[^A-Za-z0-9]+", "-", f"{server_id}-{stack}").strip("-")


@router.get("", response_model=list[ServerGroup])
async def get_servers() -> list[ServerGroup]:
    """All servers from Mongo, each with its live stacks from Portainer (or an access error)."""
    return await list_servers_grouped()


@router.get("/detail", response_class=HTMLResponse)
async def stack_detail(request: Request, server_id: str, stack: str) -> HTMLResponse:
    """HTMX partial: env vars, services and compose file of one stack."""
    detail = await get_stack_detail(server_id, stack)
    return templates.TemplateResponse(request, "partials/detail.html", {"d": detail})


@router.get("/edit", response_class=HTMLResponse)
async def stack_edit(request: Request, server_id: str, stack: str) -> HTMLResponse:
    """HTMX partial: editable compose + env of one stack, with save/restart actions."""
    detail = await get_stack_detail(server_id, stack)
    return templates.TemplateResponse(
        request, "partials/edit.html",
        {"d": detail, "env_text": format_env_text(detail.env),
         "dom_id": _dom_id(server_id, stack)},
    )
