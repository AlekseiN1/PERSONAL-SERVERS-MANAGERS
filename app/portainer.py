"""Portainer API client + pure env-editing logic.

Core of the feature: read-modify-write the whole Env array.
PUT overwrites the entire env list — there's no point update in the API.
Protocol and gotchas: .claude/skills/portainer-env-update/.
"""
from __future__ import annotations

import re
from typing import Any

import httpx


class PortainerError(RuntimeError):
    """Error talking to the Portainer API."""


def replace_env_var(
    env: list[dict[str, Any]],
    key: str,
    value: str,
    add_if_missing: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """Pure read-modify-write edit of a SINGLE variable in the Env array.

    Returns (new_array, old_value). Other entries are left untouched.
    If the variable is missing and add_if_missing=True, it's appended at the end.
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
    """Variable names from the Env array (order preserved)."""
    return [e["name"] for e in env if e.get("name")]


def format_env_text(env: list[dict[str, Any]]) -> str:
    """Env array -> 'NAME=value' text, one per line (for the editor textarea)."""
    return "\n".join(f"{e.get('name', '')}={e.get('value', '')}" for e in env if e.get("name"))


def parse_env_text(text: str) -> list[dict[str, str]]:
    """'NAME=value' text, one per line -> Env array [{name, value}].

    Blank lines and comment lines (#) are ignored. Split on the first '='
    (the value may itself contain '='). Name and value are trimmed.
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
    """Align server_env's KEYS to main_env, KEEPING the server's existing values.

    - missing keys (in main, not on the server) -> added with main's value;
    - extra keys (on the server, not in main) -> removed;
    - shared keys -> the server's value is left as-is.
    Returns (new_env, added_keys, removed_keys). Order follows main.
    """
    main_by_key = {e["name"]: e.get("value", "") for e in main_env if e.get("name")}
    server_by_key = {e["name"]: e for e in server_env if e.get("name")}
    removed = [k for k in server_by_key if k not in main_by_key]
    added: list[str] = []
    new_env: list[dict[str, Any]] = []
    for key, main_value in main_by_key.items():
        if key in server_by_key:
            new_env.append(server_by_key[key])  # keep the server's local value
        else:
            new_env.append({"name": key, "value": main_value})
            added.append(key)
    return new_env, added, removed


class PortainerClient:
    """Async client for one Portainer endpoint (one VM).

    Auth is login/password: POST /api/auth -> JWT, then header
    Authorization: Bearer <jwt>. Login happens lazily before the first request.
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
        """POST /api/auth -> JWT, stored in the Authorization header for later requests."""
        resp = await self._client.post(
            "/api/auth",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"Authentication failed ({resp.status_code}): {resp.text[:200]}"
            )
        jwt = resp.json().get("jwt")
        if not jwt:
            raise PortainerError("/api/auth response has no jwt field")
        self._client.headers["Authorization"] = f"Bearer {jwt}"
        self._authed = True

    async def _ensure_auth(self) -> None:
        if not self._authed:
            await self.authenticate()

    async def get_endpoint_id(self, name: str | None = None) -> int:
        """Step 1. endpointId from the API (never guessed). With name, filters by Name."""
        await self._ensure_auth()
        resp = await self._client.get("/api/endpoints")
        resp.raise_for_status()
        endpoints = resp.json()
        if not endpoints:
            raise PortainerError("No endpoints in Portainer")
        if name is not None:
            for ep in endpoints:
                if ep.get("Name") == name:
                    return int(ep["Id"])
            raise PortainerError(f"Endpoint with Name={name!r} not found")
        return int(endpoints[0]["Id"])

    async def list_stacks(self) -> list[dict[str, Any]]:
        """GET /api/stacks — all stacks on the endpoint (for enumerating names)."""
        await self._ensure_auth()
        resp = await self._client.get("/api/stacks")
        resp.raise_for_status()
        return resp.json()

    async def get_stack_by_name(self, name: str) -> dict[str, Any]:
        """Step 2. Find a stack by Name, return its document (with Id and Env)."""
        for stack in await self.list_stacks():
            if stack.get("Name") == name:
                return stack
        raise PortainerError(f"Stack with Name={name!r} not found")

    async def get_stack_file(self, stack_id: int) -> str:
        """GET /api/stacks/{id}/file — the stack's compose file content (StackFileContent)."""
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
        """Create a standalone (docker-compose) stack from an inline string.
        POST /api/stacks/create/standalone/string?endpointId=...
        (Swarm endpoints aren't supported — personal VMs run standalone Docker.)"""
        await self._ensure_auth()
        resp = await self._client.post(
            "/api/stacks/create/standalone/string",
            params={"endpointId": endpoint_id},
            json={"name": name, "stackFileContent": file_content, "env": env},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code not in (200, 201):
            raise PortainerError(
                f"Creating stack {name!r} returned {resp.status_code}: {resp.text[:300]}"
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
        """Step 4. PUT the whole env array back. Safe defaults: prune/pullImage=false."""
        await self._ensure_auth()
        resp = await self._client.put(
            f"/api/stacks/{stack_id}",
            params={"endpointId": endpoint_id},
            json={"env": env, "prune": prune, "pullImage": pull_image},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"PUT stack {stack_id} returned {resp.status_code}: {resp.text[:300]}"
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
        """Full stack update: compose file + the whole env array (with redeploy).

        Same as "Update the stack" in the Portainer UI. PUT /api/stacks/{id} with
        stackFileContent and env — containers get recreated with the new config.
        Standalone only (not swarm); git stacks are updated via git/redeploy instead.
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
                f"PUT (full) stack {stack_id} returned {resp.status_code}: {resp.text[:300]}"
            )

    async def list_images(self, endpoint_id: int) -> list[dict[str, Any]]:
        """GET /api/endpoints/{id}/docker/images/json — images on the endpoint (Docker API proxy)."""
        await self._ensure_auth()
        resp = await self._client.get(
            f"/api/endpoints/{endpoint_id}/docker/images/json", params={"all": "false"}
        )
        resp.raise_for_status()
        return resp.json()

    async def list_containers(self, endpoint_id: int) -> list[dict[str, Any]]:
        """GET /api/endpoints/{id}/docker/containers/json?all=true — all containers
        (running + stopped), used to figure out which images are in use."""
        await self._ensure_auth()
        resp = await self._client.get(
            f"/api/endpoints/{endpoint_id}/docker/containers/json", params={"all": "true"}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_container_logs(self, endpoint_id: int, container_id: str, tail: int = 200) -> bytes:
        """GET .../docker/containers/{id}/logs — raw logs (Docker multiplexes stdout/stderr
        with frame headers when the container has no tty — see demux_docker_log)."""
        await self._ensure_auth()
        resp = await self._client.get(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail), "timestamps": "false"},
        )
        if resp.status_code != 200:
            raise PortainerError(
                f"GET logs {container_id} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.content

    async def delete_image(self, endpoint_id: int, image_id: str, force: bool = False) -> None:
        """DELETE /api/endpoints/{id}/docker/images/{imageId} — delete an image."""
        await self._ensure_auth()
        resp = await self._client.delete(
            f"/api/endpoints/{endpoint_id}/docker/images/{image_id}",
            params={"force": "true" if force else "false"},
        )
        if resp.status_code not in (200, 204):
            raise PortainerError(
                f"DELETE image {image_id} returned {resp.status_code}: {resp.text[:300]}"
            )

    async def stop_stack(self, stack_id: int, endpoint_id: int) -> None:
        """POST /api/stacks/{id}/stop — stop the stack's containers."""
        await self._ensure_auth()
        resp = await self._client.post(
            f"/api/stacks/{stack_id}/stop", params={"endpointId": endpoint_id}
        )
        if resp.status_code not in (200, 204):
            raise PortainerError(
                f"stop stack {stack_id} returned {resp.status_code}: {resp.text[:300]}"
            )

    async def start_stack(self, stack_id: int, endpoint_id: int) -> None:
        """POST /api/stacks/{id}/start — start the stack's containers."""
        await self._ensure_auth()
        resp = await self._client.post(
            f"/api/stacks/{stack_id}/start", params={"endpointId": endpoint_id}
        )
        if resp.status_code not in (200, 204):
            raise PortainerError(
                f"start stack {stack_id} returned {resp.status_code}: {resp.text[:300]}"
            )


def is_git_stack(stack: dict[str, Any]) -> bool:
    """Is the stack backed by a git repository? Those update differently (gotcha #5)."""
    return bool(stack.get("GitConfig"))


def demux_docker_log(raw: bytes) -> str:
    """Docker multiplexes stdout/stderr with an 8-byte header per frame (when the
    container has no tty): [type(1) | 000(3) | size(4, big-endian)] + payload.
    With a tty, logs are plain text.

    We try a strict parse as a multiplexed stream; if any frame doesn't line up
    (meaning it was plain text after all), we just decode the whole thing as-is.
    """
    out = bytearray()
    i, n = 0, len(raw)
    while i + 8 <= n:
        stream_type = raw[i]
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        if stream_type not in (0, 1, 2) or i + 8 + size > n:
            return raw.decode("utf-8", errors="replace")
        out += raw[i + 8:i + 8 + size]
        i += 8 + size
    if i != n:
        return raw.decode("utf-8", errors="replace")
    return out.decode("utf-8", errors="replace")


def compose_project_name(stack_name: str) -> str:
    """Docker Compose normalizes the project name: lowercase, only [a-z0-9_-] (else -> '-').
    Portainer deploys standalone stacks via compose with project = the (normalized) stack
    name, so that's how we find its containers via the 'com.docker.compose.project' label."""
    return re.sub(r"[^a-z0-9_-]+", "-", stack_name.lower()).strip("-")
