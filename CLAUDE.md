# PERSONAL-SERVERS-MANAGERS

A web + Python interface for managing personal servers. Each server is a separate Proxmox VM.
The server list is stored in MongoDB. The goal is to run tasks against selected servers (all
or specific ones). The first task was updating a single env variable in a Portainer stack.

The whole application (code, comments, docs, UI) is English-only — no Russian anywhere.

## Data (important)

The `servers` collection belongs to the **main system** — for us it's **read-only** (we never
write to it). The document schema is camelCase, mapped via alias in `models.py`:
- `serverName` -> name, `serverInternalIP` -> internal_ip
- `Username`/`password` — per-server override of Portainer creds (usually empty -> falls back to `.env`).
- `stacks[]` in Mongo is **empty** almost everywhere — NOT the source of truth for stacks.

**The stack list is pulled live from Portainer**: `load_servers()` gives the connection
inventory, `list_stacks_live()` concurrently polls each Portainer instance (`GET /api/stacks`);
unreachable servers come back as an error string (`StackListing.reachable=False`), they don't
just disappear.

**Unit of work** = a (server × stack) pair, `StackTarget` with `token = "<server_id>::<stack_name>"`.
Portainer base URL = `https://{serverInternalIP}:9443` (not `serverFQDN`). A stack in Portainer
is looked up by `Name` (the numeric Id comes from the `GET /api/stacks` response).

**Main server** (`MAIN_SERVER_NAME` in `.env`, default `Nitarti-15-f375154914`) — the reference
used for compare/normalize/propagate, marked ★ in the UI.

## Features

- **Env update** (`/`) — change one variable on selected stacks (read-modify-write).
- **Stack details** (HTMX) — env, service names (from compose), the compose file itself.
- **Manual stack editing** (HTMX, "✎ edit / restart" in the list) — edit env
  (`NAME=value` text) and compose of one stack:
  - **Save & redeploy** (`save_stack`) — a full PUT (`update_stack_full`: `stackFileContent` +
    the whole env array) with redeploy; a *pull images* checkbox. Git stacks aren't supported.
  - **Restart** (`restart_stack`) — `stop`->`start` (`POST /api/stacks/{id}/stop|start`);
    a failing `stop` (already stopped) is ignored, what matters is a successful `start`.
- **Compare & sync** (`/compare`) — compare a same-named stack across servers against the
  reference (env key diff), then:
  - **Normalize keys** (`normalize_keys`) — align env keys to the reference, **server values
    are kept** (`align_env_keys`); per-server tokens aren't overwritten.
  - **Propagate** (`propagate_stack`) — create the stack on servers that don't have it, from
    the reference's compose (editable) + reference's env. `POST /api/stacks/create/standalone/string`
    (standalone Docker only; swarm isn't supported).
  - **Backup browsing** (`list_backups`) — the `/backups` page also lists what's already on
    disk under `settings.backup_dir`, grouped by server/stack directory (sanitized names via
    `safe_path_part`) with newest-first timestamps; **View** reads a backup's compose+env
    straight from disk (`read_backup_content`), **Restore** (`restore_backup`) re-resolves the
    sanitized dir names against the live inventory (`_resolve_backup_target`, since sanitizing
    is lossy) and redeploys via the same `save_stack` path (dry-run + git-stack guard included,
    no pull-images option — restore always redeploys as-is). **Delete** (`delete_backup`)
    removes a backup's compose+env files from disk; also dry-run gated (irreversible otherwise).
  - **Inline backup indicators** — in the servers/stacks table itself (not just the flat list
    below it): each server row shows a total backup count + most-recent timestamp
    (`backup_server_summary`), each stack row shows its own count/last-timestamp and a
    "🗄" button that opens a native `<dialog>` modal listing that stack's backups with the
    same View/Restore actions. The row-to-backup join is by sanitized name
    (`backup_lookup_map`, keyed `"<safe_server>::<safe_stack>"`) since backups on disk aren't
    tied to a `server_id`. `safe_path_part`/`fmt_ts` (`format_backup_ts`) are registered as
    Jinja globals in `main.py` so templates can do this lookup themselves.
- **Images** (`/images`) — docker images per server (its own tab): tags, size, status
  (`in use` / `unused` / `dangling`). "unused" = not referenced by any container (cross-checking
  `docker/images/json` against `docker/containers/json?all=true` by `ImageID`), "dangling" =
  untagged. Deletion is either targeted (`delete_image`) or all unused at once on a server
  (`delete_unused_images`), via `DELETE /api/endpoints/{id}/docker/images/{imageId}` (Portainer
  proxies the Docker Engine API). Also supports dry-run.
- **Logs** (`/logs`) — container logs, **only for explicitly selected stacks** (there's no
  "all" mode — nothing loads on its own). A stack's containers are found by the
  `com.docker.compose.project` label (the stack name is normalized with `compose_project_name`,
  the same way Docker Compose does it). Logs are fetched via `GET .../docker/containers/{id}/logs`
  and demultiplexed (`demux_docker_log`) — Docker writes an 8-byte header per stdout/stderr
  frame when the container has no tty. The tab supports a wide/fullscreen layout, an
  auto-refresh timer, and a client-side keyword filter over the rendered lines.
- All mutations support **dry-run** (on by default).

## Stack

- **Backend**: FastAPI (async)
- **Frontend**: server-side Jinja2 + HTMX (no separate JS framework)
- **YAML**: `pyyaml` — parsing stack compose files (service names)
- **DB**: MongoDB via `motor` (async)
- **HTTP client**: `httpx.AsyncClient` (for the Portainer API; login/password auth -> JWT)
- **Config**: `pydantic-settings`, secrets from `.env`

## Structure

```
app/
├── config.py        # settings from .env (PORTAINER_USER/PASSWORD, MONGO_URI/DB, default port)
├── db.py            # Mongo connection, servers collection (read-only)
├── models.py        # Server/Stack (camelCase mapping), StackTarget, EnvUpdateRequest, TaskResult
├── portainer.py     # PortainerClient (auth->JWT, list/file/create/update, docker images/containers/logs) + replace_env_var/align_env_keys/demux_docker_log
├── services.py      # load_servers, list_servers_grouped, run_env_update, compare_stacks, normalize_keys, propagate_stack, list_images_grouped, delete_image(s), get_stack_logs, list_backups, read_backup_content, restore_backup, delete_backup
├── main.py          # FastAPI app, mounts routers/templates/static
├── routers/         # servers.py, tasks.py
├── templates/       # base.html, index.html, partials/
└── static/
tests/               # test_portainer.py, test_services.py
```

## Commands

```bash
pip install -r requirements.txt
python run.py                        # run + auto-open browser (OPEN_BROWSER/APP_HOST/APP_PORT in .env)
uvicorn app.main:app --reload        # plain dev (no browser), http://127.0.0.1:8000
pytest                               # tests
```

`run.py` is the single entry point: on first run it creates `.venv`, installs dependencies,
then re-execs itself under that venv and starts the server. There's no run.bat/run.ps1 anymore.

## Conventions

- Async everywhere (routes, db, http).
- Pydantic models for input/output, never bare dicts.
- Secrets **only** in `.env` (it's in `.gitignore`); creds/IPs are never hardcoded —
  IP and stack name come from Mongo, Portainer login/password from `.env` (shared across all
  servers; per-server override in Mongo is optional).
- Mongo is **read-only**. Env changes go only through the Portainer API, never into the document.
- TLS `verify=False` for Portainer is intentional: self-signed certs on an internal network.
- English only — code, comments, docstrings, docs, UI text. No Russian anywhere.

## Portainer safety (critical)

1. **Read-modify-write the whole Env array.** PUT overwrites env entirely — always send back
   the full array from GET with one edit applied, or the other variables disappear.
2. `prune: false`, `pullImage: false` — safe defaults in the PUT body.
3. Get `endpointId` from `GET /api/endpoints`, never guess it (never blindly `[0]`).
4. The stack name comes from Mongo, never hardcoded.
5. Git stacks (Repository) update differently -> `POST /api/stacks/{id}/git/redeploy`.
6. **Dry-run** (GET endpoints+stacks only) before rolling out to every server.

The detailed task protocol lives in `.claude/skills/portainer-env-update/`.
