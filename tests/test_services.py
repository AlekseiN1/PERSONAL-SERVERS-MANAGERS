"""Orchestration tests: live stack listing, dry-run without PUT, real PUT, filtering by token.

We never touch Mongo — load_servers is patched; Portainer is mocked with respx.
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


# --- Mongo schema mapping ---

def test_server_maps_camelcase_schema():
    s = Server.model_validate({
        "_id": "id1", "serverName": "fb", "serverInternalIP": "1.2.3.4",
        "stacks": [{"stackName": "a"}], "region": "x",
    })
    assert s.name == "fb" and s.internal_ip == "1.2.3.4" and s.port == 9443


# --- live stack listing ---

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


# --- orchestration ---

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
    # "all" -> the live listing will find stack 'personal api', dry-run
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


# --- backup (compose + env) ---

@respx.mock
@pytest.mark.asyncio
async def test_backup_dry_run_no_write(_one_server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    _mock_full()
    respx.get(f"{BASE}/api/stacks/5/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": "services: {}"}))

    results = await services.backup_stacks(["abc::personal api"], dry_run=True)

    assert len(results) == 1
    r = results[0]
    assert r.ok and r.dry_run and r.stack_name == "personal api"
    assert not list(tmp_path.rglob("*.compose.yml"))


@respx.mock
@pytest.mark.asyncio
async def test_backup_real_writes_compose_and_env(_one_server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    _mock_full()
    respx.get(f"{BASE}/api/stacks/5/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": "services:\n  web: {}"}))

    results = await services.backup_stacks(["abc::personal api"], dry_run=False)

    assert len(results) == 1
    r = results[0]
    assert r.ok and not r.dry_run
    compose_files = list(tmp_path.rglob("*.compose.yml"))
    env_files = list(tmp_path.rglob("*.env"))
    assert len(compose_files) == 1 and len(env_files) == 1
    assert compose_files[0].read_text() == "services:\n  web: {}"
    assert env_files[0].read_text() == "APP_TOKEN=OLD"


@pytest.mark.asyncio
async def test_backup_unknown_token_skipped(_one_server):
    assert await services.backup_stacks(["nope::x"]) == []


# --- browsing and restoring existing backups ---

@pytest.mark.asyncio
async def test_list_backups_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "does-not-exist"))
    assert services.list_backups() == []


@respx.mock
@pytest.mark.asyncio
async def test_list_backups_groups_by_server_and_stack(_one_server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    _mock_full()
    respx.get(f"{BASE}/api/stacks/5/file").mock(
        return_value=httpx.Response(200, json={"StackFileContent": "services: {}"}))
    await services.backup_stacks(["abc::personal api"], dry_run=False)

    groups = services.list_backups()
    assert len(groups) == 1
    assert groups[0].server_dir == "vm-1"
    assert len(groups[0].stacks) == 1
    assert groups[0].stacks[0].stack_dir == "personal_api"
    assert len(groups[0].stacks[0].timestamps) == 1


def test_format_backup_ts_parses_and_falls_back():
    assert services.format_backup_ts("20260809T142911") == "2026-08-09 14:29:11"
    assert services.format_backup_ts("garbage") == "garbage"


def test_backup_lookup_map_keys_by_sanitized_names(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    (stack_dir / "20260101T000000.compose.yml").write_text("x", encoding="utf-8")
    (stack_dir / "20260101T000000.env").write_text("", encoding="utf-8")

    m = services.backup_lookup_map()
    assert set(m) == {"vm-1::personal_api"}
    assert m["vm-1::personal_api"].timestamps == ["20260101T000000"]


def test_backup_server_summary_aggregates_across_stacks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    for stack, ts in [("api", "20260101T000000"), ("api", "20260102T000000"), ("db", "20260103T000000")]:
        stack_dir = tmp_path / "vm-1" / stack
        stack_dir.mkdir(parents=True, exist_ok=True)
        (stack_dir / f"{ts}.compose.yml").write_text("x", encoding="utf-8")
        (stack_dir / f"{ts}.env").write_text("", encoding="utf-8")

    summary = services.backup_server_summary()
    count, latest = summary["vm-1"]
    assert count == 3
    assert latest == "20260103T000000"


def test_backup_server_summary_empty_when_no_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    assert services.backup_server_summary() == {}


def test_read_backup_content_reads_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    (stack_dir / "20260101T000000.compose.yml").write_text("services:\n  web: {}", encoding="utf-8")
    (stack_dir / "20260101T000000.env").write_text("A=1", encoding="utf-8")

    c = services.read_backup_content("vm-1", "personal_api", "20260101T000000")
    assert c.error is None
    assert c.compose == "services:\n  web: {}" and c.env_text == "A=1"


def test_read_backup_content_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    c = services.read_backup_content("vm-1", "personal_api", "20260101T000000")
    assert c.error is not None


@respx.mock
@pytest.mark.asyncio
async def test_restore_backup_dry_run_no_put(_one_server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    (stack_dir / "20260101T000000.compose.yml").write_text("services:\n  web: {}", encoding="utf-8")
    (stack_dir / "20260101T000000.env").write_text("APP_TOKEN=OLD", encoding="utf-8")
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200))

    r = await services.restore_backup("vm-1", "personal_api", "20260101T000000", dry_run=True)
    assert r.ok and r.dry_run and put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_restore_backup_real_redeploys(_one_server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    (stack_dir / "20260101T000000.compose.yml").write_text("services:\n  web: {}", encoding="utf-8")
    (stack_dir / "20260101T000000.env").write_text("APP_TOKEN=OLD", encoding="utf-8")
    _mock_full()
    put = respx.put(f"{BASE}/api/stacks/5").mock(return_value=httpx.Response(200, json={}))

    r = await services.restore_backup("vm-1", "personal_api", "20260101T000000", dry_run=False)
    assert r.ok and not r.dry_run and put.called
    import json
    body = json.loads(put.calls.last.request.content)
    assert body["stackFileContent"] == "services:\n  web: {}"
    assert body["env"] == [{"name": "APP_TOKEN", "value": "OLD"}]


@pytest.mark.asyncio
async def test_restore_backup_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    r = await services.restore_backup("vm-1", "personal_api", "nope", dry_run=True)
    assert not r.ok and "not found on disk" in r.message


@respx.mock
@pytest.mark.asyncio
async def test_restore_backup_no_matching_live_server(tmp_path, monkeypatch):
    async def fake_load():
        return []
    monkeypatch.setattr(services, "load_servers", fake_load)
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    (stack_dir / "20260101T000000.compose.yml").write_text("services: {}", encoding="utf-8")
    (stack_dir / "20260101T000000.env").write_text("", encoding="utf-8")

    r = await services.restore_backup("vm-1", "personal_api", "20260101T000000", dry_run=True)
    assert not r.ok and "not found live" in r.message


def test_delete_backup_dry_run_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    compose = stack_dir / "20260101T000000.compose.yml"
    env = stack_dir / "20260101T000000.env"
    compose.write_text("x", encoding="utf-8")
    env.write_text("A=1", encoding="utf-8")

    r = services.delete_backup("vm-1", "personal_api", "20260101T000000", dry_run=True)
    assert r.ok and r.dry_run
    assert compose.exists() and env.exists()


def test_delete_backup_real_removes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    stack_dir = tmp_path / "vm-1" / "personal_api"
    stack_dir.mkdir(parents=True)
    compose = stack_dir / "20260101T000000.compose.yml"
    env = stack_dir / "20260101T000000.env"
    compose.write_text("x", encoding="utf-8")
    env.write_text("A=1", encoding="utf-8")

    r = services.delete_backup("vm-1", "personal_api", "20260101T000000", dry_run=False)
    assert r.ok and not r.dry_run
    assert not compose.exists() and not env.exists()


def test_delete_backup_missing_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    r = services.delete_backup("vm-1", "personal_api", "nope", dry_run=True)
    assert not r.ok and "not found on disk" in r.message


# --- manual edit / restart of one stack ---

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
    # the stack is already stopped -> stop fails, start still runs
    respx.post(f"{BASE}/api/stacks/5/stop").mock(return_value=httpx.Response(409, text="not running"))
    start = respx.post(f"{BASE}/api/stacks/5/start").mock(return_value=httpx.Response(200))
    r = await services.restart_stack("abc", "personal api", dry_run=False)
    assert r.ok and start.called


# --- services from compose ---

def test_parse_service_names():
    compose = "version: '3'\nservices:\n  web:\n    image: nginx\n  db:\n    image: pg\n"
    assert services.parse_service_names(compose) == ["web", "db"]
    assert services.parse_service_names("not: [valid") == []
    assert services.parse_service_names("foo: bar") == []


# --- compare / normalize / propagate ---


BASE2 = "https://10.0.0.2:9443"


@pytest.fixture
def _main_and_other(monkeypatch):
    """Main server (10.0.0.1, has stack 'app') + a second server (10.0.0.2)."""
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
    # main: keys A,B ; other: keys A,C  -> other missing=[B], extra=[C]
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
    assert cmp.servers[0].is_main  # main server first
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
    # the main server (m) is excluded, only o remains
    assert len(results) == 1
    assert results[0].ok and results[0].dry_run
    assert "B" in results[0].message and put.called is False


@respx.mock
@pytest.mark.asyncio
async def test_propagate_skips_existing_and_creates(_main_and_other):
    _auth(BASE); _auth(BASE2)
    # other does NOT have stack 'app' -> create it
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


# --- docker images: listing + deletion ---

def _images_json():
    return [
        {"Id": "sha256:used", "RepoTags": ["nginx:latest"], "Size": 100 * 1024 * 1024, "Created": 1},
        {"Id": "sha256:unused", "RepoTags": ["old-app:1.0"], "Size": 50 * 1024 * 1024, "Created": 2},
        {"Id": "sha256:dangling", "RepoTags": None, "Size": 10 * 1024 * 1024, "Created": 3},
    ]


def _containers_json():
    return [{"ImageID": "sha256:used"}]


@respx.mock
@pytest.mark.asyncio
async def test_list_images_grouped_marks_in_use_and_dangling(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    respx.get(f"{BASE}/api/endpoints/1/docker/images/json").mock(
        return_value=httpx.Response(200, json=_images_json()))
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/json").mock(
        return_value=httpx.Response(200, json=_containers_json()))

    groups = await services.list_images_grouped()
    assert len(groups) == 1 and groups[0].reachable
    by_id = {i.id: i for i in groups[0].images}
    assert by_id["sha256:used"].in_use and not by_id["sha256:used"].dangling
    assert not by_id["sha256:unused"].in_use and not by_id["sha256:unused"].dangling
    assert not by_id["sha256:dangling"].in_use and by_id["sha256:dangling"].dangling
    assert by_id["sha256:used"].size_mb == 100.0


@respx.mock
@pytest.mark.asyncio
async def test_list_images_grouped_unreachable(_one_server):
    respx.post(f"{BASE}/api/auth").mock(side_effect=httpx.ConnectError("no route"))
    groups = await services.list_images_grouped()
    assert len(groups) == 1 and groups[0].reachable is False and groups[0].images == []


@respx.mock
@pytest.mark.asyncio
async def test_delete_image_dry_run_no_delete(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    delete = respx.delete(f"{BASE}/api/endpoints/1/docker/images/sha256:unused").mock(
        return_value=httpx.Response(200))

    r = await services.delete_image("abc", "sha256:unused", dry_run=True)
    assert r.ok and r.dry_run and delete.called is False


@respx.mock
@pytest.mark.asyncio
async def test_delete_image_real_deletes(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    delete = respx.delete(f"{BASE}/api/endpoints/1/docker/images/sha256:unused").mock(
        return_value=httpx.Response(200))

    r = await services.delete_image("abc", "sha256:unused", dry_run=False)
    assert r.ok and not r.dry_run and delete.called


@respx.mock
@pytest.mark.asyncio
async def test_delete_unused_images_skips_in_use(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    respx.get(f"{BASE}/api/endpoints/1/docker/images/json").mock(
        return_value=httpx.Response(200, json=_images_json()))
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/json").mock(
        return_value=httpx.Response(200, json=_containers_json()))
    delete = respx.delete(url__regex=r".*/docker/images/sha256:.*").mock(return_value=httpx.Response(200))

    results = await services.delete_unused_images("abc", dry_run=True)
    deleted_ids = {r.image_id for r in results}
    assert deleted_ids == {"sha256:unused", "sha256:dangling"}
    assert delete.called is False


# --- stack container logs ---

@pytest.mark.asyncio
async def test_get_stack_logs_no_targets_no_network(_one_server):
    # with nothing explicitly selected, there should be zero network calls (no 'all')
    assert await services.get_stack_logs([]) == []


@respx.mock
@pytest.mark.asyncio
async def test_get_stack_logs_matches_by_compose_project(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/json").mock(return_value=httpx.Response(200, json=[
        {"Id": "c1", "Names": ["/personal-api-web-1"],
         "Labels": {"com.docker.compose.project": "personal-api"}},
        {"Id": "c2", "Names": ["/other-stack-web-1"],
         "Labels": {"com.docker.compose.project": "other-stack"}},
    ]))
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/c1/logs").mock(
        return_value=httpx.Response(200, content=b"hello from web\n"))

    results = await services.get_stack_logs(["abc::personal api"], tail=50)
    assert len(results) == 1 and results[0].ok
    assert [c.container_name for c in results[0].containers] == ["personal-api-web-1"]
    assert results[0].containers[0].text == "hello from web\n"


@respx.mock
@pytest.mark.asyncio
async def test_get_stack_logs_no_matching_containers(_one_server):
    _mock_auth()
    respx.get(f"{BASE}/api/endpoints").mock(return_value=httpx.Response(200, json=[{"Id": 1, "Name": "local"}]))
    respx.get(f"{BASE}/api/endpoints/1/docker/containers/json").mock(return_value=httpx.Response(200, json=[]))

    results = await services.get_stack_logs(["abc::personal api"])
    assert len(results) == 1 and not results[0].ok and "no containers found" in results[0].message
