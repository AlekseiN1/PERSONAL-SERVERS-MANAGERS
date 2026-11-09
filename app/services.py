"""Task orchestration across stacks (server + stack). Mongo is read-only.

The stack list is fetched live from Portainer (Mongo's `stacks` field is empty almost
everywhere): load_servers() gives the connection inventory, list_stacks_live() polls
each Portainer instance.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml

from app.config import settings
from app.db import get_servers_collection
from app.models import (
    BackupContent,
    BackupResult,
    BackupServerEntry,
    BackupStackEntry,
    ContainerLogs,
    ImageDeleteResult,
    ImageInfo,
    Server,
    ServerGroup,
    ServerImages,
    StackComparison,
    StackDetail,
    StackLogs,
    StackOnServer,
    StackTarget,
    TaskResult,
)
from app.portainer import (
    PortainerClient,
    PortainerError,
    align_env_keys,
    compose_project_name,
    demux_docker_log,
    env_keys,
    format_env_text,
    is_git_stack,
    parse_env_text,
    replace_env_var,
)

LIST_TIMEOUT = 10.0  # seconds to poll one Portainer instance when listing stacks


def parse_service_names(compose: str) -> list[str]:
    """Service names from a compose file (keys of the services block)."""
    try:
        data = yaml.safe_load(compose) or {}
    except Exception:  # noqa: BLE001
        return []
    services = data.get("services") if isinstance(data, dict) else None
    return list(services.keys()) if isinstance(services, dict) else []


async def load_servers() -> list[Server]:
    """Read the server inventory from Mongo (documents not matching our schema are skipped)."""
    docs = await get_servers_collection().find().to_list(length=None)
    servers: list[Server] = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        try:
            servers.append(Server.model_validate(doc))
        except Exception:  # noqa: BLE001
            continue
    return servers


def _creds(server: Server) -> tuple[str, str]:
    return (
        server.username or settings.portainer_user,
        server.password or settings.portainer_password,
    )


def _base_url(internal_ip: str, port: int) -> str:
    return f"https://{internal_ip}:{port or settings.default_portainer_port}"


def is_main(server: Server) -> bool:
    return server.name == settings.main_server_name


def _client(server: Server, timeout: float = 30.0) -> PortainerClient:
    username, password = _creds(server)
    return PortainerClient(
        _base_url(server.internal_ip, server.port), username, password,
        verify=settings.portainer_verify_tls, timeout=timeout,
    )


async def _list_one(server: Server) -> ServerGroup:
    """Poll one server's Portainer and return its group with the stack list."""
    base = dict(
        server_id=server.id or "", server_name=server.name,
        internal_ip=server.internal_ip, port=server.port, is_main=is_main(server),
    )
    username, password = _creds(server)
    if not username or not password:
        return ServerGroup(**base, reachable=False, error="no username/password")
    try:
        async with PortainerClient(
            _base_url(server.internal_ip, server.port), username, password,
            verify=settings.portainer_verify_tls, timeout=LIST_TIMEOUT,
        ) as client:
            stacks = await client.list_stacks()
    except Exception as exc:  # noqa: BLE001 — show an unreachable server as an error string
        return ServerGroup(**base, reachable=False, error=f"{type(exc).__name__}: {exc}")

    names = [s["Name"] for s in stacks if s.get("Name")]
    return ServerGroup(**base, reachable=True, stacks=names)


async def list_servers_grouped() -> list[ServerGroup]:
    """Poll all servers concurrently; return one group per server (stacks or error)."""
    servers = await load_servers()
    if not servers:
        return []
    return list(await asyncio.gather(*(_list_one(s) for s in servers)))


async def _update_one(target: StackTarget, key: str, value: str, dry_run: bool) -> TaskResult:
    """Read-modify-write env on one stack. With dry_run, only GET is performed."""
    username, password = _creds_for_target(target)
    base = dict(server_name=target.server_name, stack_name=target.stack_name)
    if not username or not password:
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message="No login/password (neither in .env nor in the server document)")
    try:
        async with PortainerClient(
            _base_url(target.internal_ip, target.port), username, password,
            verify=settings.portainer_verify_tls,
        ) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(target.stack_name)
            env = stack.get("Env") or []
            new_env, old_value = replace_env_var(env, key, value)

            if is_git_stack(stack):
                return TaskResult(**base, ok=False, dry_run=dry_run, old_value=old_value,
                                  message="Git stack: needs POST /api/stacks/{id}/git/redeploy")

            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True, old_value=old_value,
                                  new_value=value,
                                  message=f"dry-run OK: stack {stack.get('Id')}, "
                                          f"endpoint {endpoint_id}, {len(env)} env var(s) — no PUT")

            await client.update_stack_env(int(stack["Id"]), endpoint_id, new_env)
            return TaskResult(**base, ok=True, dry_run=False, old_value=old_value,
                              new_value=value, message="updated (HTTP 200)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


def _creds_for_target(target: StackTarget) -> tuple[str, str]:
    return (
        target.username or settings.portainer_user,
        target.password or settings.portainer_password,
    )


async def _resolve_targets(targets: list[str] | Literal["all"]) -> list[StackTarget]:
    """Tokens '<server_id>::<stack_name>' (or 'all') -> expanded StackTargets.

    'all' pulls the live stack list across all reachable servers.
    """
    servers_by_id = {s.id: s for s in await load_servers() if s.id}

    if targets == "all":
        groups = await list_servers_grouped()
        tokens = [
            f"{g.server_id}::{name}"
            for g in groups if g.reachable for name in g.stacks
        ]
    else:
        tokens = list(targets)

    built: list[StackTarget] = []
    for tok in tokens:
        server_id, sep, stack_name = tok.partition("::")
        if not sep:
            continue
        server = servers_by_id.get(server_id)
        if server is None:
            continue
        built.append(StackTarget(
            server_id=server_id, server_name=server.name, internal_ip=server.internal_ip,
            port=server.port, stack_name=stack_name,
            username=server.username, password=server.password,
        ))
    return built


async def run_env_update(
    targets: list[str] | Literal["all"],
    key: str,
    value: str,
    dry_run: bool = True,
) -> list[TaskResult]:
    """Update one env variable across selected stacks (concurrently).

    targets — tokens like '<server_id>::<stack_name>', or 'all' (pulls the live list).
    """
    built = await _resolve_targets(targets)
    if not built:
        return []
    return await asyncio.gather(*(_update_one(t, key, value, dry_run) for t in built))


# ---------------------------------------------------------------------------
# Stack details (env + services + compose)
# ---------------------------------------------------------------------------

async def get_stack_detail(server_id: str, stack_name: str) -> StackDetail:
    """Fetch env, services (from compose) and the compose file itself for one stack."""
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_id=server_id, server_name=server.name if server else server_id,
                stack_name=stack_name)
    if server is None:
        return StackDetail(**base, reachable=False, error="server not found")
    try:
        async with _client(server) as client:
            stack = await client.get_stack_by_name(stack_name)
            compose = await client.get_stack_file(int(stack["Id"]))
            return StackDetail(
                **base, reachable=True, stack_id=int(stack["Id"]),
                env=stack.get("Env") or [], compose=compose,
                services=parse_service_names(compose),
            )
    except Exception as exc:  # noqa: BLE001
        return StackDetail(**base, reachable=False, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Manual edit and restart of one stack
# ---------------------------------------------------------------------------

async def save_stack(
    server_id: str,
    stack_name: str,
    compose: str,
    env: list[dict],
    pull_image: bool = False,
    dry_run: bool = True,
) -> TaskResult:
    """Save one stack's compose+env and redeploy it (same as "Update" in the Portainer UI).

    Read-modify-write: find the stack and endpoint; with dry_run, only read (no PUT).
    Git stacks aren't supported (they update via git/redeploy instead).
    """
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_name=server.name if server else server_id, stack_name=stack_name)
    if server is None:
        return TaskResult(**base, ok=False, dry_run=dry_run, message="server not found")
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(stack_name)
            if is_git_stack(stack):
                return TaskResult(**base, ok=False, dry_run=dry_run,
                                  message="Git stack: update via git/redeploy, not this PUT")
            summary = f"compose {len(compose)} chars, {len(env)} env var(s), pullImage={pull_image}"
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: stack {stack.get('Id')}, endpoint {endpoint_id}, "
                                          f"{summary} — no PUT")
            await client.update_stack_full(
                int(stack["Id"]), endpoint_id, compose, env, pull_image=pull_image,
            )
            return TaskResult(**base, ok=True, dry_run=False,
                              message=f"saved and redeployed (HTTP 200): {summary}")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def restart_stack(server_id: str, stack_name: str, dry_run: bool = True) -> TaskResult:
    """Restart a stack: stop -> start (containers get recreated with the current config).

    A failed stop on an already-stopped stack is ignored; what matters is a successful start.
    """
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_name=server.name if server else server_id, stack_name=stack_name)
    if server is None:
        return TaskResult(**base, ok=False, dry_run=dry_run, message="server not found")
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(stack_name)
            stack_id = int(stack["Id"])
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: stack {stack_id}, endpoint {endpoint_id} — "
                                          f"would stop->start (no action taken)")
            try:
                await client.stop_stack(stack_id, endpoint_id)
            except PortainerError:
                pass  # already stopped / stop unsupported — doesn't block starting
            await client.start_stack(stack_id, endpoint_id)
            return TaskResult(**base, ok=True, dry_run=False, message="restarted (stop->start)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Comparing a same-named stack across servers against the main one
# ---------------------------------------------------------------------------

async def compare_stacks(stack_name: str) -> StackComparison:
    """Compare stack_name across all servers against the reference (main server)."""
    servers = await load_servers()
    main_env: list[dict] = []
    main_keys: list[str] = []
    main_compose = ""
    main_present = False

    async def fetch(server: Server):
        """-> (server, present, env|None, compose|None, error|None)"""
        try:
            async with _client(server, timeout=LIST_TIMEOUT) as client:
                stacks = await client.list_stacks()
                match = next((s for s in stacks if s.get("Name") == stack_name), None)
                if match is None:
                    return server, False, None, None, None
                env = match.get("Env") or []
                compose = ""
                if is_main(server):
                    try:
                        compose = await client.get_stack_file(int(match["Id"]))
                    except Exception:  # noqa: BLE001
                        compose = ""
                return server, True, env, compose, None
        except Exception as exc:  # noqa: BLE001
            return server, False, None, None, f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(fetch(s) for s in servers))

    # the reference server first
    for server, present, env, compose, _err in results:
        if is_main(server) and present:
            main_present = True
            main_keys = env_keys(env or [])
            main_compose = compose or ""

    rows: list[StackOnServer] = []
    main_key_set = set(main_keys)
    for server, present, env, _compose, err in results:
        keys = env_keys(env or []) if present else []
        rows.append(StackOnServer(
            server_id=server.id or "", server_name=server.name, is_main=is_main(server),
            reachable=err is None, error=err, present=present, env_keys=keys,
            missing_keys=[k for k in main_keys if k not in set(keys)] if present else list(main_keys),
            extra_keys=[k for k in keys if k not in main_key_set],
        ))
    # main server first
    rows.sort(key=lambda r: (not r.is_main, r.server_name))
    return StackComparison(
        stack_name=stack_name, main_present=main_present, main_keys=main_keys,
        main_compose=main_compose, servers=rows,
    )


# ---------------------------------------------------------------------------
# Normalizing env keys to match the reference server
# ---------------------------------------------------------------------------

async def _get_main_env(stack_name: str) -> list[dict]:
    """Env of the reference stack on the main server (or an error)."""
    servers = await load_servers()
    main = next((s for s in servers if is_main(s)), None)
    if main is None:
        raise PortainerError(f"Main server {settings.main_server_name!r} not found in Mongo")
    async with _client(main) as client:
        stack = await client.get_stack_by_name(stack_name)
        return stack.get("Env") or []


async def _normalize_one(server: Server, stack_name: str, main_env: list[dict],
                         dry_run: bool) -> TaskResult:
    base = dict(server_name=server.name, stack_name=stack_name)
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(stack_name)
            if is_git_stack(stack):
                return TaskResult(**base, ok=False, dry_run=dry_run,
                                  message="Git stack: updated separately")
            new_env, added, removed = align_env_keys(stack.get("Env") or [], main_env)
            msg = f"+{len(added)} key(s) {added or ''}, -{len(removed)} {removed or ''}".strip()
            if not added and not removed:
                return TaskResult(**base, ok=True, dry_run=dry_run, message="already aligned")
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True, message=f"dry-run: {msg}")
            await client.update_stack_env(int(stack["Id"]), endpoint_id, new_env)
            return TaskResult(**base, ok=True, dry_run=False, message=f"applied: {msg}")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def normalize_keys(stack_name: str, server_ids: list[str],
                         dry_run: bool = True) -> list[TaskResult]:
    """Align the stack's env KEYS to the reference on selected servers (values are kept)."""
    servers = {s.id: s for s in await load_servers() if s.id}
    targets = [servers[sid] for sid in server_ids if sid in servers and not is_main(servers[sid])]
    if not targets:
        return []
    main_env = await _get_main_env(stack_name)
    return await asyncio.gather(
        *(_normalize_one(s, stack_name, main_env, dry_run) for s in targets)
    )


# ---------------------------------------------------------------------------
# Propagating a stack to servers that don't have it yet (create)
# ---------------------------------------------------------------------------

async def get_main_stack_source(stack_name: str) -> tuple[str, list[dict]]:
    """Compose file and env of the reference stack (to prefill the propagate form)."""
    servers = await load_servers()
    main = next((s for s in servers if is_main(s)), None)
    if main is None:
        raise PortainerError(f"Main server {settings.main_server_name!r} not found")
    async with _client(main) as client:
        stack = await client.get_stack_by_name(stack_name)
        compose = await client.get_stack_file(int(stack["Id"]))
        return compose, (stack.get("Env") or [])


async def _propagate_one(server: Server, stack_name: str, compose: str,
                         env: list[dict], dry_run: bool) -> TaskResult:
    base = dict(server_name=server.name, stack_name=stack_name)
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stacks = await client.list_stacks()
            if any(s.get("Name") == stack_name for s in stacks):
                return TaskResult(**base, ok=False, dry_run=dry_run,
                                  message="stack already exists — skipped")
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: would create the stack ({len(env)} env var(s)) — no write")
            await client.create_stack_standalone(stack_name, compose, env, endpoint_id)
            return TaskResult(**base, ok=True, dry_run=False, message="created (HTTP 200/201)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def propagate_stack(stack_name: str, compose: str, env: list[dict],
                          server_ids: list[str], dry_run: bool = True) -> list[TaskResult]:
    """Create stack_name (from compose+env) on selected servers that don't have it yet."""
    servers = {s.id: s for s in await load_servers() if s.id}
    targets = [servers[sid] for sid in server_ids if sid in servers and not is_main(servers[sid])]
    if not targets:
        return []
    return await asyncio.gather(
        *(_propagate_one(s, stack_name, compose, env, dry_run) for s in targets)
    )


# ---------------------------------------------------------------------------
# Backing up a stack (compose + env) to disk
# ---------------------------------------------------------------------------

def safe_path_part(name: str) -> str:
    """Server/stack name -> a filesystem-safe path fragment."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "_"


def _backup_paths(server_name: str, stack_name: str, ts: str) -> tuple[Path, Path]:
    stack_dir = Path(settings.backup_dir) / safe_path_part(server_name) / safe_path_part(stack_name)
    return stack_dir / f"{ts}.compose.yml", stack_dir / f"{ts}.env"


async def _backup_one(target: StackTarget, dry_run: bool) -> BackupResult:
    """Download one stack's compose+env and save it to disk (or just count it if dry_run)."""
    base = dict(server_name=target.server_name, stack_name=target.stack_name)
    username, password = _creds_for_target(target)
    if not username or not password:
        return BackupResult(**base, ok=False, dry_run=dry_run,
                            message="No login/password (neither in .env nor in the server document)")
    try:
        async with PortainerClient(
            _base_url(target.internal_ip, target.port), username, password,
            verify=settings.portainer_verify_tls,
        ) as client:
            stack = await client.get_stack_by_name(target.stack_name)
            compose = await client.get_stack_file(int(stack["Id"]))
            env = stack.get("Env") or []
            env_text = format_env_text(env)

            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            compose_path, env_path = _backup_paths(target.server_name, target.stack_name, ts)

            if dry_run:
                return BackupResult(**base, ok=True, dry_run=True, path=str(compose_path.parent),
                                    message=f"dry-run: {len(compose)} char(s) compose, "
                                            f"{len(env)} env var(s) — no disk write")

            compose_path.parent.mkdir(parents=True, exist_ok=True)
            compose_path.write_text(compose, encoding="utf-8")
            env_path.write_text(env_text, encoding="utf-8")
            return BackupResult(**base, ok=True, dry_run=False, path=str(compose_path.parent),
                                message=f"saved: {compose_path.name} + {env_path.name}")
    except Exception as exc:  # noqa: BLE001
        return BackupResult(**base, ok=False, dry_run=dry_run,
                            message=f"{type(exc).__name__}: {exc}")


async def backup_stacks(
    targets: list[str] | Literal["all"], dry_run: bool = True,
) -> list[BackupResult]:
    """Back up compose+env of selected stacks (concurrently) into settings.backup_dir on disk."""
    built = await _resolve_targets(targets)
    if not built:
        return []
    return await asyncio.gather(*(_backup_one(t, dry_run) for t in built))


# ---------------------------------------------------------------------------
# Browsing and restoring existing backups from disk
# ---------------------------------------------------------------------------

_BACKUP_SUFFIX = ".compose.yml"


def list_backups() -> list[BackupServerEntry]:
    """Scan settings.backup_dir for saved server/stack/timestamp backups (newest first)."""
    root = Path(settings.backup_dir)
    if not root.exists():
        return []
    servers: list[BackupServerEntry] = []
    for server_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        stacks: list[BackupStackEntry] = []
        for stack_dir in sorted(p for p in server_dir.iterdir() if p.is_dir()):
            timestamps = sorted(
                (p.name[: -len(_BACKUP_SUFFIX)] for p in stack_dir.glob(f"*{_BACKUP_SUFFIX}")),
                reverse=True,
            )
            if timestamps:
                stacks.append(BackupStackEntry(stack_dir=stack_dir.name, timestamps=timestamps))
        if stacks:
            servers.append(BackupServerEntry(server_dir=server_dir.name, stacks=stacks))
    return servers


def format_backup_ts(ts: str) -> str:
    """Backup timestamp ('%Y%m%dT%H%M%S') -> human-readable text; unparseable input is
    returned as-is (so a malformed directory name never crashes the page)."""
    try:
        return datetime.strptime(ts, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def backup_lookup_map() -> dict[str, BackupStackEntry]:
    """'<sanitized server dir>::<sanitized stack dir>' -> its BackupStackEntry.

    Lets the servers/stacks table look up "how many backups / when was the last one"
    for a given live server+stack in O(1), by sanitizing the live names the same way
    backup directories are named (see safe_path_part).
    """
    return {
        f"{srv.server_dir}::{st.stack_dir}": st
        for srv in list_backups()
        for st in srv.stacks
    }


def backup_server_summary() -> dict[str, tuple[int, str | None]]:
    """'<sanitized server dir>' -> (total backup count across its stacks, most recent timestamp)."""
    return {
        srv.server_dir: (
            sum(len(st.timestamps) for st in srv.stacks),
            max((st.timestamps[0] for st in srv.stacks if st.timestamps), default=None),
        )
        for srv in list_backups()
    }


def read_backup_content(server_dir: str, stack_dir: str, timestamp: str) -> BackupContent:
    """Read a saved backup's compose+env straight off disk."""
    base = dict(server_name=server_dir, stack_name=stack_dir, timestamp=timestamp)
    compose_path, env_path = _backup_paths(server_dir, stack_dir, timestamp)
    if not compose_path.exists():
        return BackupContent(**base, error="backup not found on disk")
    compose = compose_path.read_text(encoding="utf-8")
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    return BackupContent(**base, compose=compose, env_text=env_text)


async def _resolve_backup_target(server_dir: str, stack_dir: str) -> tuple[Server, str] | None:
    """Map sanitized backup directory names back to a live server + its exact stack name.

    Backup directories are named via safe_path_part, which is lossy (spaces etc. become
    '_'), so we re-derive the mapping by sanitizing the live inventory the same way instead
    of trusting the directory name as an exact server/stack name.
    """
    servers = await load_servers()
    server = next((s for s in servers if safe_path_part(s.name) == server_dir), None)
    if server is None:
        return None
    try:
        async with _client(server, timeout=LIST_TIMEOUT) as client:
            stacks = await client.list_stacks()
    except Exception:  # noqa: BLE001
        return None
    stack_name = next(
        (s["Name"] for s in stacks if s.get("Name") and safe_path_part(s["Name"]) == stack_dir),
        None,
    )
    if stack_name is None:
        return None
    return server, stack_name


async def restore_backup(
    server_dir: str, stack_dir: str, timestamp: str, dry_run: bool = True,
) -> TaskResult:
    """Redeploy a stack from a saved backup (compose+env) — reuses save_stack's
    read-modify-write + redeploy, and its dry-run/git-stack safety checks."""
    base = dict(server_name=server_dir, stack_name=stack_dir)
    compose_path, env_path = _backup_paths(server_dir, stack_dir, timestamp)
    if not compose_path.exists():
        return TaskResult(**base, ok=False, dry_run=dry_run, message="backup not found on disk")
    compose = compose_path.read_text(encoding="utf-8")
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env = parse_env_text(env_text)

    resolved = await _resolve_backup_target(server_dir, stack_dir)
    if resolved is None:
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message="server or stack not found live (renamed, removed, or unreachable?)")
    server, stack_name = resolved
    return await save_stack(server.id or "", stack_name, compose, env, dry_run=dry_run)


def delete_backup(server_dir: str, stack_dir: str, timestamp: str, dry_run: bool = True) -> TaskResult:
    """Delete one saved backup's compose+env files from disk. Irreversible — dry-run first."""
    base = dict(server_name=server_dir, stack_name=stack_dir)
    compose_path, env_path = _backup_paths(server_dir, stack_dir, timestamp)
    if not compose_path.exists() and not env_path.exists():
        return TaskResult(**base, ok=False, dry_run=dry_run, message="backup not found on disk")
    if dry_run:
        return TaskResult(**base, ok=True, dry_run=True,
                          message=f"dry-run: would delete {compose_path.name} + {env_path.name} — no disk write")
    compose_path.unlink(missing_ok=True)
    env_path.unlink(missing_ok=True)
    return TaskResult(**base, ok=True, dry_run=False,
                      message=f"deleted {compose_path.name} + {env_path.name}")


# ---------------------------------------------------------------------------
# Docker images: listing (in-use/unused, dangling) and deletion
# ---------------------------------------------------------------------------

async def _list_images_one(server: Server) -> ServerImages:
    """Poll one server: images + containers, to figure out what's unused."""
    base = dict(server_id=server.id or "", server_name=server.name, is_main=is_main(server))
    username, password = _creds(server)
    if not username or not password:
        return ServerImages(**base, reachable=False, error="no username/password")
    try:
        async with _client(server, timeout=LIST_TIMEOUT) as client:
            endpoint_id = await client.get_endpoint_id()
            images, containers = await asyncio.gather(
                client.list_images(endpoint_id), client.list_containers(endpoint_id),
            )
    except Exception as exc:  # noqa: BLE001
        return ServerImages(**base, reachable=False, error=f"{type(exc).__name__}: {exc}")

    used_ids = {c.get("ImageID") for c in containers if c.get("ImageID")}
    infos = []
    for img in images:
        tags = [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]
        infos.append(ImageInfo(
            id=img.get("Id", ""), tags=tags,
            size_mb=round((img.get("Size") or 0) / (1024 * 1024), 1),
            created=img.get("Created") or 0,
            in_use=img.get("Id") in used_ids,
            dangling=not tags,
        ))
    infos.sort(key=lambda i: (i.in_use, i.tags[0] if i.tags else ""))
    return ServerImages(**base, reachable=True, images=infos)


async def list_images_grouped() -> list[ServerImages]:
    """Poll all servers concurrently; return one image group per server."""
    servers = await load_servers()
    if not servers:
        return []
    return list(await asyncio.gather(*(_list_images_one(s) for s in servers)))


async def delete_image(server_id: str, image_id: str, dry_run: bool = True) -> ImageDeleteResult:
    """Delete one image on a server (DELETE .../docker/images/{id})."""
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    if server is None:
        return ImageDeleteResult(server_name=server_id, image_id=image_id, ok=False,
                                 dry_run=dry_run, message="server not found")
    base = dict(server_name=server.name, image_id=image_id)
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            if dry_run:
                return ImageDeleteResult(**base, ok=True, dry_run=True,
                                         message=f"dry-run: endpoint {endpoint_id} — no deletion")
            await client.delete_image(endpoint_id, image_id)
            return ImageDeleteResult(**base, ok=True, dry_run=False, message="deleted")
    except Exception as exc:  # noqa: BLE001
        return ImageDeleteResult(**base, ok=False, dry_run=dry_run,
                                 message=f"{type(exc).__name__}: {exc}")


async def delete_unused_images(server_id: str, dry_run: bool = True) -> list[ImageDeleteResult]:
    """Delete every image on a server not referenced by any container (concurrently)."""
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    if server is None:
        return []
    group = await _list_images_one(server)
    if not group.reachable:
        return [ImageDeleteResult(server_name=server.name, image_id="", ok=False, dry_run=dry_run,
                                  message=group.error or "unreachable")]
    unused = [img for img in group.images if not img.in_use]
    if not unused:
        return []
    return await asyncio.gather(*(delete_image(server_id, img.id, dry_run) for img in unused))


# ---------------------------------------------------------------------------
# Stack container logs (only for explicitly selected targets — no 'all')
# ---------------------------------------------------------------------------

async def _one_container_log(
    client: PortainerClient, endpoint_id: int, container: dict, tail: int,
) -> ContainerLogs:
    cid = container.get("Id", "")
    name = (container.get("Names") or [f"/{cid[:12]}"])[0].lstrip("/")
    try:
        raw = await client.get_container_logs(endpoint_id, cid, tail=tail)
        return ContainerLogs(container_name=name, container_id=cid, text=demux_docker_log(raw))
    except Exception as exc:  # noqa: BLE001
        return ContainerLogs(container_name=name, container_id=cid,
                             error=f"{type(exc).__name__}: {exc}")


async def _stack_logs_one(target: StackTarget, tail: int) -> StackLogs:
    """Logs of every container of one stack: containers are found by their compose-project label."""
    base = dict(server_name=target.server_name, stack_name=target.stack_name)
    username, password = _creds_for_target(target)
    if not username or not password:
        return StackLogs(**base, ok=False,
                         message="No login/password (neither in .env nor in the server document)")
    try:
        async with PortainerClient(
            _base_url(target.internal_ip, target.port), username, password,
            verify=settings.portainer_verify_tls,
        ) as client:
            endpoint_id = await client.get_endpoint_id()
            containers = await client.list_containers(endpoint_id)
            project = compose_project_name(target.stack_name)
            matched = [
                c for c in containers
                if (c.get("Labels") or {}).get("com.docker.compose.project", "").lower() == project
            ]
            if not matched:
                return StackLogs(**base, ok=False,
                                 message="no containers found for this stack (compose.project label didn't match)")
            logs = await asyncio.gather(
                *(_one_container_log(client, endpoint_id, c, tail) for c in matched)
            )
            return StackLogs(**base, ok=True, containers=list(logs))
    except Exception as exc:  # noqa: BLE001
        return StackLogs(**base, ok=False, message=f"{type(exc).__name__}: {exc}")


async def get_stack_logs(targets: list[str], tail: int = 200) -> list[StackLogs]:
    """Logs of the containers of SELECTED stacks (concurrently). No 'all' by design — logs are
    only fetched for what's explicitly checked, never for every server at once."""
    if not targets:
        return []
    built = await _resolve_targets(targets)
    if not built:
        return []
    return await asyncio.gather(*(_stack_logs_one(t, tail) for t in built))
