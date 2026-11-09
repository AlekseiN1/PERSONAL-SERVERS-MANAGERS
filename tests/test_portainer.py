"""Unit tests for the pure replace_env_var logic and the HTTP client (via respx)."""
import httpx
import pytest
import respx

from app.portainer import (
    PortainerClient,
    PortainerError,
    compose_project_name,
    demux_docker_log,
    format_env_text,
    is_git_stack,
    parse_env_text,
    replace_env_var,
)

BASE = "https://10.0.0.1:9443"


def _mock_auth():
    respx.post(f"{BASE}/api/auth").mock(
        return_value=httpx.Response(200, json={"jwt": "fake.jwt.token"})
    )


def _client():
    return PortainerClient(BASE, "admin", "pass")


def test_replace_existing_keeps_others():
    env = [
        {"name": "APP_PORT", "value": "3000"},
        {"name": "APP_ENV", "value": "production"},
        {"name": "APP_TOKEN", "value": "OLD"},
    ]
    new_env, old = replace_env_var(env, "APP_TOKEN", "NEW")
    assert old == "OLD"
    assert new_env == [
        {"name": "APP_PORT", "value": "3000"},
        {"name": "APP_ENV", "value": "production"},
        {"name": "APP_TOKEN", "value": "NEW"},
    ]
    # the original array isn't mutated
    assert env[2]["value"] == "OLD"


def test_replace_adds_when_missing():
    new_env, old = replace_env_var([{"name": "A", "value": "1"}], "B", "2")
    assert old is None
    assert {"name": "B", "value": "2"} in new_env
    assert len(new_env) == 2


def test_replace_no_add_when_disabled():
    new_env, old = replace_env_var([], "X", "1", add_if_missing=False)
    assert new_env == [] and old is None


def test_align_env_keys_adds_removes_keeps_values():
    from app.portainer import align_env_keys
    main = [{"name": "A", "value": "ma"}, {"name": "B", "value": "mb"}]
    server = [{"name": "A", "value": "local-a"}, {"name": "C", "value": "c"}]
    new_env, added, removed = align_env_keys(server, main)
    assert added == ["B"] and removed == ["C"]
    # A keeps its local value, B is added with main's value, C is removed
    assert new_env == [{"name": "A", "value": "local-a"}, {"name": "B", "value": "mb"}]


def test_align_env_keys_already_aligned():
    from app.portainer import align_env_keys
    main = [{"name": "A", "value": "x"}]
    new_env, added, removed = align_env_keys([{"name": "A", "value": "y"}], main)
    assert added == [] and removed == [] and new_env == [{"name": "A", "value": "y"}]


def test_is_git_stack():
    assert is_git_stack({"GitConfig": {"URL": "..."}}) is True
    assert is_git_stack({"GitConfig": None}) is False
    assert is_git_stack({}) is False


def test_format_and_parse_env_text_roundtrip():
    env = [{"name": "A", "value": "1"}, {"name": "B", "value": "x=y"}]
    text = format_env_text(env)
    assert text == "A=1\nB=x=y"
    assert parse_env_text(text) == env  # a value containing '=' is preserved


def test_parse_env_text_skips_blank_and_comments():
    text = "\n# comment\nA=1\n  \nB = 2 \nBAD_NO_NAME\n=orphan\n"
    assert parse_env_text(text) == [
        {"name": "A", "value": "1"},
        {"name": "B", "value": "2"},          # name/value get trimmed
        {"name": "BAD_NO_NAME", "value": ""},  # no '=' -> empty value
    ]  # the '=orphan' line (empty name) is skipped


@respx.mock
@pytest.mark.asyncio
async def test_authenticate_sets_bearer():
    _mock_auth()
    endpoints = respx.get(f"{BASE}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}])
    )
    async with _client() as c:
        await c.get_endpoint_id()
    assert endpoints.calls.last.request.headers["Authorization"] == "Bearer fake.jwt.token"


@respx.mock
@pytest.mark.asyncio
async def test_authenticate_raises_on_bad_creds():
    respx.post(f"{BASE}/api/auth").mock(return_value=httpx.Response(401, text="invalid"))
    async with _client() as c:
        with pytest.raises(PortainerError):
            await c.get_endpoint_id()


@respx.mock
@pytest.mark.asyncio
async def test_get_endpoint_id_by_name():
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[
            {"Id": 1, "Name": "local"}, {"Id": 7, "Name": "edge"},
        ])
    )
    async with _client() as c:
        assert await c.get_endpoint_id() == 1
        assert await c.get_endpoint_id("edge") == 7
        with pytest.raises(PortainerError):
            await c.get_endpoint_id("nope")


@respx.mock
@pytest.mark.asyncio
async def test_get_stack_by_name():
    _mock_auth()
    respx.get(f"{BASE}/api/stacks").mock(
        return_value=httpx.Response(200, json=[
            {"Id": 5, "Name": "myapp", "Env": [{"name": "A", "value": "1"}]},
        ])
    )
    async with _client() as c:
        stack = await c.get_stack_by_name("myapp")
        assert stack["Id"] == 5
        with pytest.raises(PortainerError):
            await c.get_stack_by_name("ghost")


@respx.mock
@pytest.mark.asyncio
async def test_update_stack_env_sends_full_array():
    _mock_auth()
    route = respx.put(f"{BASE}/api/stacks/5").mock(
        return_value=httpx.Response(200, json={})
    )
    env = [{"name": "A", "value": "2"}]
    async with _client() as c:
        await c.update_stack_env(5, 1, env)
    req = route.calls.last.request
    assert req.url.params["endpointId"] == "1"
    import json
    body = json.loads(req.content)
    assert body == {"env": env, "prune": False, "pullImage": False}


@respx.mock
@pytest.mark.asyncio
async def test_update_stack_env_raises_on_non_200():
    _mock_auth()
    respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(500, text="boom"))
    async with _client() as c:
        with pytest.raises(PortainerError):
            await c.update_stack_env(5, 1, [])


@respx.mock
@pytest.mark.asyncio
async def test_get_stack_file():
    _mock_auth()
    respx.get(f"{BASE}/api/stacks/5/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": "services:\n  web: {}\n"})
    )
    async with _client() as c:
        assert "services:" in await c.get_stack_file(5)


@respx.mock
@pytest.mark.asyncio
async def test_update_stack_full_sends_compose_and_env():
    _mock_auth()
    route = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200, json={}))
    env = [{"name": "A", "value": "2"}]
    async with _client() as c:
        await c.update_stack_full(5, 1, "services: {}", env, pull_image=True)
    req = route.calls.last.request
    assert req.url.params["endpointId"] == "1"
    import json
    body = json.loads(req.content)
    assert body == {
        "stackFileContent": "services: {}", "env": env,
        "prune": False, "pullImage": True,
    }


@respx.mock
@pytest.mark.asyncio
async def test_stop_and_start_stack():
    _mock_auth()
    stop = respx.post(f"{BASE}/api/stacks/5/stop").mock(return_value=httpx.Response(200, json={}))
    start = respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(204))
    async with _client() as c:
        await c.stop_stack(5, 1)
        await c.start_stack(5, 1)
    assert stop.calls.last.request.url.params["endpointId"] == "1"
    assert start.calls.last.request.url.params["endpointId"] == "1"


@respx.mock
@pytest.mark.asyncio
async def test_start_stack_raises_on_error():
    _mock_auth()
    respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(500, text="boom"))
    async with _client() as c:
        with pytest.raises(PortainerError):
            await c.start_stack(5, 1)


def _frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def test_demux_docker_log_splits_multiplexed_frames():
    raw = _frame(1, b"stdout line\n") + _frame(2, b"stderr line\n")
    assert demux_docker_log(raw) == "stdout line\nstderr line\n"


def test_demux_docker_log_falls_back_to_plain_text():
    raw = b"just plain text, not multiplexed at all\n"
    assert demux_docker_log(raw) == raw.decode()


def test_demux_docker_log_empty():
    assert demux_docker_log(b"") == ""


def test_compose_project_name_normalizes():
    assert compose_project_name("personal api") == "personal-api"
    assert compose_project_name("My_Stack.1") == "my_stack-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_container_logs_raises_on_error():
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/c1/logs").mock(
        return_value=httpx.Response(500, text="boom"))
    async with _client() as c:
        with pytest.raises(PortainerError):
            await c.get_container_logs(1, "c1")


@respx.mock
@pytest.mark.asyncio
async def test_create_stack_standalone():
    _mock_auth()
    route = respx.post(f"{BASE}/api/stacks/create/standalone/string").mock(
        return_value=httpx.Response(200, json={"Id": 9})
    )
    async with _client() as c:
        res = await c.create_stack_standalone("myapp", "services: {}", [{"name": "A", "value": "1"}], 1)
    assert res["Id"] == 9
    req = route.calls.last.request
    assert req.url.params["endpointId"] == "1"
    import json
    body = json.loads(req.content)
    assert body["name"] == "myapp" and body["stackFileContent"] == "services: {}"
