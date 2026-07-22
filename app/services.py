"""Оркестрация задач по стекам (сервер + стек). Mongo — только чтение.

Список стеков берётся live из Portainer (в Mongo поле stacks почти везде пустое):
load_servers() даёт инвентарь подключений, list_stacks_live() опрашивает каждый Portainer.
"""
from __future__ import annotations

import asyncio
from typing import Literal

import yaml

from app.config import settings
from app.db import get_servers_collection
from app.models import (
    Server,
    ServerGroup,
    StackComparison,
    StackDetail,
    StackOnServer,
    StackTarget,
    TaskResult,
)
from app.portainer import (
    PortainerClient,
    PortainerError,
    align_env_keys,
    env_keys,
    is_git_stack,
    replace_env_var,
)

LIST_TIMEOUT = 10.0  # сек на опрос одного Portainer при перечислении стеков


def parse_service_names(compose: str) -> list[str]:
    """Имена сервисов из compose-файла (ключи блока services)."""
    try:
        data = yaml.safe_load(compose) or {}
    except Exception:  # noqa: BLE001
        return []
    services = data.get("services") if isinstance(data, dict) else None
    return list(services.keys()) if isinstance(services, dict) else []


async def load_servers() -> list[Server]:
    """Прочитать инвентарь серверов из Mongo (документы не нашей схемы пропускаются)."""
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
    """Опросить Portainer одного сервера и вернуть его группу со списком стеков."""
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
    except Exception as exc:  # noqa: BLE001 — недоступный сервер показываем строкой
        return ServerGroup(**base, reachable=False, error=f"{type(exc).__name__}: {exc}")

    names = [s["Name"] for s in stacks if s.get("Name")]
    return ServerGroup(**base, reachable=True, stacks=names)


async def list_servers_grouped() -> list[ServerGroup]:
    """Конкурентно опросить все серверы; вернуть по группе на сервер (со стеками/ошибкой)."""
    servers = await load_servers()
    if not servers:
        return []
    return list(await asyncio.gather(*(_list_one(s) for s in servers)))


async def _update_one(target: StackTarget, key: str, value: str, dry_run: bool) -> TaskResult:
    """Read-modify-write env на одном стеке. При dry_run — только GET."""
    username, password = _creds_for_target(target)
    base = dict(server_name=target.server_name, stack_name=target.stack_name)
    if not username or not password:
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message="Нет логина/пароля (ни в .env, ни в документе сервера)")
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
                                  message="Git-стек: нужен POST /api/stacks/{id}/git/redeploy")

            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True, old_value=old_value,
                                  new_value=value,
                                  message=f"dry-run OK: стек {stack.get('Id')}, "
                                          f"endpoint {endpoint_id}, env {len(env)} перем. — без PUT")

            await client.update_stack_env(int(stack["Id"]), endpoint_id, new_env)
            return TaskResult(**base, ok=True, dry_run=False, old_value=old_value,
                              new_value=value, message="обновлено (HTTP 200)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


def _creds_for_target(target: StackTarget) -> tuple[str, str]:
    return (
        target.username or settings.portainer_user,
        target.password or settings.portainer_password,
    )


async def run_env_update(
    targets: list[str] | Literal["all"],
    key: str,
    value: str,
    dry_run: bool = True,
) -> list[TaskResult]:
    """Обновить одну env-переменную по выбранным стекам (конкурентно).

    targets — токены вида '<server_id>::<stack_name>' или 'all' (тогда тянем live-список).
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

    if not built:
        return []
    return await asyncio.gather(*(_update_one(t, key, value, dry_run) for t in built))


# ---------------------------------------------------------------------------
# Детали стека (env + сервисы + compose)
# ---------------------------------------------------------------------------

async def get_stack_detail(server_id: str, stack_name: str) -> StackDetail:
    """Подтянуть env, сервисы (из compose) и сам compose для одного стека."""
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_id=server_id, server_name=server.name if server else server_id,
                stack_name=stack_name)
    if server is None:
        return StackDetail(**base, reachable=False, error="сервер не найден")
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
# Ручное редактирование и перезапуск одного стека
# ---------------------------------------------------------------------------

async def save_stack(
    server_id: str,
    stack_name: str,
    compose: str,
    env: list[dict],
    pull_image: bool = False,
    dry_run: bool = True,
) -> TaskResult:
    """Сохранить compose+env одного стека и передеплоить (как «Update» в UI Portainer).

    Read-modify-write: находим стек и endpoint, при dry_run только читаем (без PUT).
    Git-стеки не поддержаны (обновляются через git/redeploy).
    """
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_name=server.name if server else server_id, stack_name=stack_name)
    if server is None:
        return TaskResult(**base, ok=False, dry_run=dry_run, message="сервер не найден")
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(stack_name)
            if is_git_stack(stack):
                return TaskResult(**base, ok=False, dry_run=dry_run,
                                  message="Git-стек: правится через git/redeploy, не этим PUT")
            summary = f"compose {len(compose)} симв., {len(env)} env-перем., pullImage={pull_image}"
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: стек {stack.get('Id')}, endpoint {endpoint_id}, "
                                          f"{summary} — без PUT")
            await client.update_stack_full(
                int(stack["Id"]), endpoint_id, compose, env, pull_image=pull_image,
            )
            return TaskResult(**base, ok=True, dry_run=False,
                              message=f"сохранено и передеплоено (HTTP 200): {summary}")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def restart_stack(server_id: str, stack_name: str, dry_run: bool = True) -> TaskResult:
    """Перезапустить стек: stop → start (контейнеры пересоздаются с текущей конфигурацией).

    stop на уже остановленном стеке игнорируется; главное — успешный start.
    """
    servers = {s.id: s for s in await load_servers() if s.id}
    server = servers.get(server_id)
    base = dict(server_name=server.name if server else server_id, stack_name=stack_name)
    if server is None:
        return TaskResult(**base, ok=False, dry_run=dry_run, message="сервер не найден")
    try:
        async with _client(server) as client:
            endpoint_id = await client.get_endpoint_id()
            stack = await client.get_stack_by_name(stack_name)
            stack_id = int(stack["Id"])
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: стек {stack_id}, endpoint {endpoint_id} — "
                                          f"будет stop→start (без действия)")
            try:
                await client.stop_stack(stack_id, endpoint_id)
            except PortainerError:
                pass  # уже остановлен / stop не поддержан — не мешает старту
            await client.start_stack(stack_id, endpoint_id)
            return TaskResult(**base, ok=True, dry_run=False, message="перезапущено (stop→start)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Сравнение одноимённого стека по серверам относительно главного
# ---------------------------------------------------------------------------

async def compare_stacks(stack_name: str) -> StackComparison:
    """Сравнить стек stack_name на всех серверах с эталоном (главным сервером)."""
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

    # сначала эталон
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
    # главный — первым
    rows.sort(key=lambda r: (not r.is_main, r.server_name))
    return StackComparison(
        stack_name=stack_name, main_present=main_present, main_keys=main_keys,
        main_compose=main_compose, servers=rows,
    )


# ---------------------------------------------------------------------------
# Нормализация ключей env под эталон
# ---------------------------------------------------------------------------

async def _get_main_env(stack_name: str) -> list[dict]:
    """Env эталонного стека с главного сервера (или ошибка)."""
    servers = await load_servers()
    main = next((s for s in servers if is_main(s)), None)
    if main is None:
        raise PortainerError(f"Главный сервер {settings.main_server_name!r} не найден в Mongo")
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
                                  message="Git-стек: обновляется отдельно")
            new_env, added, removed = align_env_keys(stack.get("Env") or [], main_env)
            msg = f"+{len(added)} ключ(ей) {added or ''}, -{len(removed)} {removed or ''}".strip()
            if not added and not removed:
                return TaskResult(**base, ok=True, dry_run=dry_run, message="уже выровнен")
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True, message=f"dry-run: {msg}")
            await client.update_stack_env(int(stack["Id"]), endpoint_id, new_env)
            return TaskResult(**base, ok=True, dry_run=False, message=f"применено: {msg}")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def normalize_keys(stack_name: str, server_ids: list[str],
                         dry_run: bool = True) -> list[TaskResult]:
    """Выровнять ключи env стека под эталон на выбранных серверах (значения сохраняются)."""
    servers = {s.id: s for s in await load_servers() if s.id}
    targets = [servers[sid] for sid in server_ids if sid in servers and not is_main(servers[sid])]
    if not targets:
        return []
    main_env = await _get_main_env(stack_name)
    return await asyncio.gather(
        *(_normalize_one(s, stack_name, main_env, dry_run) for s in targets)
    )


# ---------------------------------------------------------------------------
# Распространение стека на серверы, где его нет (create)
# ---------------------------------------------------------------------------

async def get_main_stack_source(stack_name: str) -> tuple[str, list[dict]]:
    """Compose-файл и env эталонного стека (для предзаполнения формы распространения)."""
    servers = await load_servers()
    main = next((s for s in servers if is_main(s)), None)
    if main is None:
        raise PortainerError(f"Главный сервер {settings.main_server_name!r} не найден")
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
                                  message="стек уже существует — пропущен")
            if dry_run:
                return TaskResult(**base, ok=True, dry_run=True,
                                  message=f"dry-run: создать стек ({len(env)} env) — без записи")
            await client.create_stack_standalone(stack_name, compose, env, endpoint_id)
            return TaskResult(**base, ok=True, dry_run=False, message="создан (HTTP 200/201)")
    except Exception as exc:  # noqa: BLE001
        return TaskResult(**base, ok=False, dry_run=dry_run,
                          message=f"{type(exc).__name__}: {exc}")


async def propagate_stack(stack_name: str, compose: str, env: list[dict],
                          server_ids: list[str], dry_run: bool = True) -> list[TaskResult]:
    """Создать стек stack_name (из compose+env) на выбранных серверах, где его ещё нет."""
    servers = {s.id: s for s in await load_servers() if s.id}
    targets = [servers[sid] for sid in server_ids if sid in servers and not is_main(servers[sid])]
    if not targets:
        return []
    return await asyncio.gather(
        *(_propagate_one(s, stack_name, compose, env, dry_run) for s in targets)
    )
