"""Клиент Portainer API + чистая логика правки env.

Ядро фичи: read-modify-write всего массива Env.
PUT перезаписывает весь список env — точечного обновления в API нет.
Протокол и нюансы — в .claude/skills/portainer-env-update/.
"""
from __future__ import annotations

from typing import Any

import httpx


class PortainerError(RuntimeError):
    """Ошибка взаимодействия с Portainer API."""


def replace_env_var(
    env: list[dict[str, Any]],
    key: str,
    value: str,
    add_if_missing: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """Чистая read-modify-write правка ОДНОЙ переменной в массиве Env.

    Возвращает (новый_массив, старое_значение). Остальные элементы не трогаются.
    Если переменной нет и add_if_missing=True — добавляет её в конец.
    """
    old_value: str | None = None
    found = False
    new_env: list[dict[str, Any]] = []
    for item in env:
        if item.get("name") == key:
            found = True
            old_value = item.get("value")
            new_env.append({**item, "value": value})
        else:
            new_env.append(item)
    if not found and add_if_missing:
        new_env.append({"name": key, "value": value})
    return new_env, old_value


def env_keys(env: list[dict[str, Any]]) -> list[str]:
    """Имена переменных из массива Env (с сохранением порядка)."""
    return [e["name"] for e in env if e.get("name")]


def format_env_text(env: list[dict[str, Any]]) -> str:
    """Массив Env -> текст 'NAME=value' по строкам (для textarea редактора)."""
    return "\n".join(f"{e.get('name', '')}={e.get('value', '')}" for e in env if e.get("name"))


def parse_env_text(text: str) -> list[dict[str, str]]:
    """Текст 'NAME=value' по строкам -> массив Env [{name, value}].

    Пустые строки и строки-комментарии (#) игнорируются. Разбивка по первому '='
    (значение может содержать '='). Имя и значение обрезаются по краям.
    """
    env: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _sep, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        env.append({"name": name, "value": value.strip()})
    return env


def align_env_keys(
    server_env: list[dict[str, Any]],
    main_env: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Выровнять ключи server_env под main_env, СОХРАНИВ существующие значения сервера.

    - недостающие ключи (есть в main, нет на сервере) → добавляются со значением из main;
    - лишние ключи (есть на сервере, нет в main) → удаляются;
    - общие ключи → значение сервера остаётся как есть.
    Возвращает (new_env, added_keys, removed_keys). Порядок — как в main.
    """
    main_by_key = {e["name"]: e.get("value", "") for e in main_env if e.get("name")}
    server_by_key = {e["name"]: e for e in server_env if e.get("name")}
    removed = [k for k in server_by_key if k not in main_by_key]
    added: list[str] = []
    new_env: list[dict[str, Any]] = []
    for key, main_value in main_by_key.items():
        if key in server_by_key:
            new_env.append(server_by_key[key])  # сохраняем локальное значение
        else:
            new_env.append({"name": key, "value": main_value})
            added.append(key)
    return new_env, added, removed


class PortainerClient:
    """Асинхронный клиент к одному Portainer-эндпоинту (одна VM).

    Аутентификация по логину/паролю: POST /api/auth -> JWT, далее заголовок
    Authorization: Bearer <jwt>. Логин выполняется лениво перед первым запросом.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._authed = False
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify,
            timeout=timeout,
        )

    async def __aenter__(self) -> "PortainerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def authenticate(self) -> None:
        """POST /api/auth -> JWT, кладём в заголовок Authorization для последующих запросов."""
        resp = await self._client.post(
            "/api/auth",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"Аутентификация не удалась ({resp.status_code}): {resp.text[:200]}"
            )
        jwt = resp.json().get("jwt")
        if not jwt:
            raise PortainerError("Ответ /api/auth без поля jwt")
        self._client.headers["Authorization"] = f"Bearer {jwt}"
        self._authed = True

    async def _ensure_auth(self) -> None:
        if not self._authed:
            await self.authenticate()

    async def get_endpoint_id(self, name: str | None = None) -> int:
        """Шаг 1. endpointId из API (не угадывать). При name — фильтр по Name."""
        await self._ensure_auth()
        resp = await self._client.get("/api/endpoints")
        resp.raise_for_status()
        endpoints = resp.json()
        if not endpoints:
            raise PortainerError("Нет ни одного endpoint в Portainer")
        if name is not None:
            for ep in endpoints:
                if ep.get("Name") == name:
                    return int(ep["Id"])
            raise PortainerError(f"Endpoint с Name={name!r} не найден")
        return int(endpoints[0]["Id"])

    async def list_stacks(self) -> list[dict[str, Any]]:
        """GET /api/stacks — весь массив стеков эндпоинта (для перечисления имён)."""
        await self._ensure_auth()
        resp = await self._client.get("/api/stacks")
        resp.raise_for_status()
        return resp.json()

    async def get_stack_by_name(self, name: str) -> dict[str, Any]:
        """Шаг 2. Найти стек по Name, вернуть его документ (с Id и Env)."""
        for stack in await self.list_stacks():
            if stack.get("Name") == name:
                return stack
        raise PortainerError(f"Стек с Name={name!r} не найден")

    async def get_stack_file(self, stack_id: int) -> str:
        """GET /api/stacks/{id}/file — содержимое compose-файла стека (StackFileContent)."""
        await self._ensure_auth()
        resp = await self._client.get(f"/api/stacks/{stack_id}/file")
        resp.raise_for_status()
        return resp.json().get("StackFileContent", "")

    async def create_stack_standalone(
        self,
        name: str,
        file_content: str,
        env: list[dict[str, Any]],
        endpoint_id: int,
    ) -> dict[str, Any]:
        """Создать standalone (docker-compose) стек из inline-строки.
        POST /api/stacks/create/standalone/string?endpointId=...
        (Swarm-эндпоинты не поддерживаются — личные VM это standalone Docker.)"""
        await self._ensure_auth()
        resp = await self._client.post(
            "/api/stacks/create/standalone/string",
            params={"endpointId": endpoint_id},
            json={"name": name, "stackFileContent": file_content, "env": env},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code not in (200, 201):
            raise PortainerError(
                f"Создание стека {name!r} вернуло {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def update_stack_env(
        self,
        stack_id: int,
        endpoint_id: int,
        env: list[dict[str, Any]],
        prune: bool = False,
        pull_image: bool = False,
    ) -> None:
        """Шаг 4. PUT весь массив env обратно. Безопасные дефолты prune/pullImage=false."""
        await self._ensure_auth()
        resp = await self._client.put(
            f"/api/stacks/{stack_id}",
            params={"endpointId": endpoint_id},
            json={"env": env, "prune": prune, "pullImage": pull_image},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"PUT стека {stack_id} вернул {resp.status_code}: {resp.text[:300]}"
            )

    async def update_stack_full(
        self,
        stack_id: int,
        endpoint_id: int,
        file_content: str,
        env: list[dict[str, Any]],
        prune: bool = False,
        pull_image: bool = False,
    ) -> None:
        """Полное обновление стека: compose-файл + весь массив env (с redeploy).

        Как «Update the stack» в UI Portainer. PUT /api/stacks/{id} с stackFileContent
        и env — контейнеры пересоздаются под новую конфигурацию.
        Только standalone (не swarm); git-стеки обновляются через git/redeploy.
        """
        await self._ensure_auth()
        resp = await self._client.put(
            f"/api/stacks/{stack_id}",
            params={"endpointId": endpoint_id},
            json={
                "stackFileContent": file_content,
                "env": env,
                "prune": prune,
                "pullImage": pull_image,
            },
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"PUT (full) стека {stack_id} вернул {resp.status_code}: {resp.text[:300]}"
            )

    async def stop_stack(self, stack_id: int, endpoint_id: int) -> None:
        """POST /api/stacks/{id}/stop — остановить контейнеры стека."""
        await self._ensure_auth()
        resp = await self._client.post(
            f"/api/stacks/{stack_id}/stop", params={"endpointId": endpoint_id}
        )
        if resp.status_code not in (200, 204):
            raise PortainerError(
                f"stop стека {stack_id} вернул {resp.status_code}: {resp.text[:300]}"
            )

    async def start_stack(self, stack_id: int, endpoint_id: int) -> None:
        """POST /api/stacks/{id}/start — запустить контейнеры стека."""
        await self._ensure_auth()
        resp = await self._client.post(
            f"/api/stacks/{stack_id}/start", params={"endpointId": endpoint_id}
        )
        if resp.status_code not in (200, 204):
            raise PortainerError(
                f"start стека {stack_id} вернул {resp.status_code}: {resp.text[:300]}"
            )


def is_git_stack(stack: dict[str, Any]) -> bool:
    """Стек подключён к git-репозиторию? Такие обновляются иначе (нюанс №5)."""
    return bool(stack.get("GitConfig"))
