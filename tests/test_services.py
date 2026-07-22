"""Тесты оркестрации: live-листинг стеков, dry-run без PUT, реальный PUT, фильтр по токену.

Mongo не трогаем — подменяем load_servers; Portainer мокаем respx.
"""
import httpx
import pytest
import respx

from app import services
from app.config import settings
from app.models import Server

BASE = "https://10.0.0.1:9443"


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(settings, "portainer_user", "admin")
    monkeypatch.setattr(settings, "portainer_password", "pass")
    monkeypatch.setattr(settings, "portainer_verify_tls", False)


@pytest.fixture
def _one_server(monkeypatch):
    server = Server(
        _id="abc", serverName="vm-1", serverInternalIP="10.0.0.1", port=9443,
    )

    async def fake_load():
        return [server]

    monkeypatch.setattr(services, "load_servers", fake_load)


def _mock_auth():
    respx.post(f"{BASE}/api/auth").mock(
        return_value=httpx.Response(200, json={"jwt": "fake.jwt"})
    )


def _mock_full():
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}])
    )
    respx.get(f"{BASE}/api/stacks").mock(
        return_value=httpx.Response(200, json=[
            {"Id": 5, "Name": "personal api", "Env": [{"name": "APP_TOKEN", "value": "OLD"}]},
        ])
    )


# --- маппинг схемы Mongo ---

def test_server_maps_camelcase_schema():
    s = Server.model_validate({
        "_id": "id1", "serverName": "fb", "serverInternalIP": "1.2.3.4",
        "stacks": [{"stackName": "a"}], "region": "x",
    })
    assert s.name == "fb" and s.internal_ip == "1.2.3.4" and s.port == 9443


# --- live-листинг стеков ---

@respx.mock
@pytest.mark.asyncio
async def test_list_servers_grouped_ok(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/stacks").mock(
        return_value=httpx.Response(200, json=[{"Name": "personal api"}, {"Name": "db"}])
    )
    groups = await services.list_servers_grouped()
    assert len(groups) == 1
    g = groups[0]
    assert g.reachable and g.stacks == ["personal api", "db"]
    assert g.server_id == "abc"


@respx.mock
@pytest.mark.asyncio
async def test_list_servers_grouped_unreachable(_one_server):
    respx.post(f"{BASE}/api/auth").mock(side_effect=httpx.ConnectError("no route"))
    groups = await services.list_servers_grouped()
    assert len(groups) == 1
    assert groups[0].reachable is False and groups[0].stacks == []
    assert "ConnectError" in groups[0].error


@respx.mock
@pytest.mark.asyncio
async def test_list_servers_grouped_empty(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/stacks").mock(return_value=httpx.Response(200, json=[]))
    groups = await services.list_servers_grouped()
    assert len(groups) == 1 and groups[0].reachable and groups[0].stacks == []


# --- оркестрация ---

@respx.mock
@pytest.mark.asyncio
async def test_dry_run_does_not_put(_one_server):
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))

    results = await services.run_env_update(["abc::personal api"], "APP_TOKEN", "NEW", dry_run=True)

    assert len(results) == 1
    r = results[0]
    assert r.ok and r.dry_run and r.stack_name == "personal api"
    assert r.old_value == "OLD" and r.new_value == "NEW"
    assert put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_real_run_puts_full_array(_one_server):
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200, json={}))

    results = await services.run_env_update(["abc::personal api"], "APP_TOKEN", "NEW", dry_run=False)

    assert results[0].ok and not results[0].dry_run
    assert put.called
    import json
    body = json.loads(put.calls.last.request.content)
    assert body["env"] == [{"name": "APP_TOKEN", "value": "NEW"}]
    assert body["prune"] is False and body["pullImage"] is False


@respx.mock
@pytest.mark.asyncio
async def test_all_enumerates_live(_one_server):
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))
    # "all" -> live-листинг найдёт стек 'personal api', dry-run
    results = await services.run_env_update("all", "APP_TOKEN", "V", dry_run=True)
    assert len(results) == 1 and results[0].stack_name == "personal api"
    assert put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_unknown_token_skipped(_one_server):
    _mock_full()
    assert await services.run_env_update(["nope::x"], "K", "V") == []


@respx.mock
@pytest.mark.asyncio
async def test_git_stack_blocked(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}])
    )
    respx.get(f"{BASE}/api/stacks").mock(
        return_value=httpx.Response(200, json=[
            {"Id": 5, "Name": "personal api", "Env": [], "GitConfig": {"URL": "git@..."}},
        ])
    )
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))

    results = await services.run_env_update(["abc::personal api"], "APP_TOKEN", "NEW", dry_run=False)
    assert not results[0].ok and "git" in results[0].message.lower()
    assert put.called is False


@pytest.mark.asyncio
async def test_no_servers_returns_empty(monkeypatch):
    async def fake_load():
        return []
    monkeypatch.setattr(services, "load_servers", fake_load)
    assert await services.run_env_update("all", "K", "V") == []


# --- ручное редактирование / перезапуск одного стека ---

@respx.mock
@pytest.mark.asyncio
async def test_save_stack_dry_run_no_put(_one_server):
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))
    r = await services.save_stack("abc", "personal api", "services: {}",
                                  [{"name": "A", "value": "1"}], dry_run=True)
    assert r.ok and r.dry_run and put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_save_stack_real_puts_compose_and_env(_one_server):
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200, json={}))
    r = await services.save_stack("abc", "personal api", "services:\n  web: {}",
                                  [{"name": "A", "value": "1"}], pull_image=True, dry_run=False)
    assert r.ok and not r.dry_run and put.called
    import json
    body = json.loads(put.calls.last.request.content)
    assert body["stackFileContent"] == "services:\n  web: {}"
    assert body["env"] == [{"name": "A", "value": "1"}] and body["pullImage"] is True


@respx.mock
@pytest.mark.asyncio
async def test_save_stack_git_blocked(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(
        return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    respx.get(f"{BASE}/api/stacks").mock(return_value=httpx.Response(200, json=[
        {"Id": 5, "Name": "personal api", "Env": [], "GitConfig": {"URL": "g"}},
    ]))
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))
    r = await services.save_stack("abc", "personal api", "x", [], dry_run=False)
    assert not r.ok and "git" in r.message.lower() and put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_restart_stack_dry_run(_one_server):
    _mock_full()
    stop = respx.post(f"{BASE}/api/stacks/5/stop").mock(return_value=httpx.Response(200))
    start = respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(200))
    r = await services.restart_stack("abc", "personal api", dry_run=True)
    assert r.ok and r.dry_run and stop.called is False and start.called is False


@respx.mock
@pytest.mark.asyncio
async def test_restart_stack_stop_then_start(_one_server):
    _mock_full()
    stop = respx.post(f"{BASE}/api/stacks/5/stop").mock(return_value=httpx.Response(200))
    start = respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(200))
    r = await services.restart_stack("abc", "personal api", dry_run=False)
    assert r.ok and not r.dry_run and stop.called and start.called


@respx.mock
@pytest.mark.asyncio
async def test_restart_stack_tolerates_stop_failure(_one_server):
    _mock_full()
    # стек уже остановлен -> stop падает, start всё равно выполняется
    respx.post(f"{BASE}/api/stacks/5/stop").mock(return_value=httpx.Response(409, text="not running"))
    start = respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(200))
    r = await services.restart_stack("abc", "personal api", dry_run=False)
    assert r.ok and start.called


# --- сервисы из compose ---

def test_parse_service_names():
    compose = "version: '3'\nservices:\n  web:\n    image: nginx\n  db:\n    image: pg\n"
    assert services.parse_service_names(compose) == ["web", "db"]
    assert services.parse_service_names("not: [valid") == []
    assert services.parse_service_names("foo: bar") == []


# --- compare / normalize / propagate ---

BASE2 = "https://10.0.0.2:9443"


@pytest.fixture
def _main_and_other(monkeypatch):
    """Главный (10.0.0.1, есть стек 'app') + второй сервер (10.0.0.2)."""
    monkeypatch.setattr(settings, "main_server_name", "vm-1")
    main = Server(_id="m", serverName="vm-1", serverInternalIP="10.0.0.1", port=9443)
    other = Server(_id="o", serverName="vm-2", serverInternalIP="10.0.0.2", port=9443)

    async def fake_load():
        return [main, other]

    monkeypatch.setattr(services, "load_servers", fake_load)


def _auth(base):
    respx.post(f"{base}/api/auth").mock(return_value=httpx.Response(200, json={"jwt": "j"}))


@respx.mock
@pytest.mark.asyncio
async def test_compare_stacks_diff(_main_and_other):
    _auth(BASE); _auth(BASE2)
    # main: ключи A,B ; other: ключи A,C  -> other missing=[B], extra=[C]
    respx.get(f"{BASE}/api/stacks").mock(return_value=httpx.Response(200, json=[
        {"Id": 5, "Name": "app", "Env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]},
    ]))
    respx.get(f"{BASE}/api/stacks/5/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": "services: {}"}))
    respx.get(f"{BASE2}/api/stacks").mock(return_value=httpx.Response(200, json=[
        {"Id": 7, "Name": "app", "Env": [{"name": "A", "value": "x"}, {"name": "C", "value": "z"}]},
    ]))

    cmp = await services.compare_stacks("app")
    assert cmp.main_present and cmp.main_keys == ["A", "B"]
    by_name = {s.server_name: s for s in cmp.servers}
    assert cmp.servers[0].is_main  # главный первым
    assert by_name["vm-2"].missing_keys == ["B"] and by_name["vm-2"].extra_keys == ["C"]


@respx.mock
@pytest.mark.asyncio
async def test_normalize_keys_dry_run(_main_and_other):
    _auth(BASE); _auth(BASE2)
    respx.get(f"{BASE}/api/stacks").mock(return_value=httpx.Response(200, json=[
        {"Id": 5, "Name": "app", "Env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]},
    ]))
    respx.get(f"{BASE2}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "l"}]))
    respx.get(f"{BASE2}/api/stacks").mock(return_value=httpx.Response(200, json=[
        {"Id": 7, "Name": "app", "Env": [{"name": "A", "value": "x"}, {"name": "C", "value": "z"}]},
    ]))
    put = respx.put(f"{BASE2}/api/stacks/7").mock(return_value=httpx.Response(200))

    results = await services.normalize_keys("app", ["m", "o"], dry_run=True)
    # главный (m) исключён, остаётся только o
    assert len(results) == 1
    assert results[0].ok and results[0].dry_run
    assert "B" in results[0].message and put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_propagate_skips_existing_and_creates(_main_and_other):
    _auth(BASE); _auth(BASE2)
    # other НЕ имеет стека 'app' -> создаём
    respx.get(f"{BASE2}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "l"}]))
    respx.get(f"{BASE2}/api/stacks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{BASE2}/api/stacks/create/standalone/string").mock(
        return_value=httpx.Response(200, json={"Id": 11}))

    results = await services.propagate_stack("app", "services: {}", [{"name": "A", "value": "1"}],
                                             ["o"], dry_run=False)
    assert len(results) == 1 and results[0].ok and create.called


@respx.mock
@pytest.mark.asyncio
async def test_propagate_dry_run_no_create(_main_and_other):
    _auth(BASE2)
    respx.get(f"{BASE2}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "l"}]))
    respx.get(f"{BASE2}/api/stacks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{BASE2}/api/stacks/create/standalone/string").mock(
        return_value=httpx.Response(200, json={}))

    results = await services.propagate_stack("app", "c", [], ["o"], dry_run=True)
    assert results[0].ok and results[0].dry_run and create.called is False
