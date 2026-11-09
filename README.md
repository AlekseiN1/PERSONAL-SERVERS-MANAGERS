# Personal Servers Manager

A web + Python interface for managing personal servers (Proxmox VMs). The server list lives
in MongoDB. Tasks run against selected servers (all or specific ones). The first task was
updating a single env variable in a Portainer stack.

## Stack

FastAPI · Jinja2 + HTMX · MongoDB (motor) · httpx · pydantic-settings.

## Running it

Copy `.env.example` to `.env` and fill in `PORTAINER_USER`/`PORTAINER_PASSWORD`, `MONGO_URI`
and `MONGO_DB` (the database name where the `servers` collection lives). Then:

```powershell
python run.py
```

The single entry point. On first run it creates `.venv` and installs dependencies itself,
then re-execs under that venv and starts the server. The browser opens automatically at
http://127.0.0.1:8000 (disable with `OPEN_BROWSER=false` in `.env`; host/port —
`APP_HOST`/`APP_PORT`).

For a plain dev mode without the browser (venv already active):
```powershell
uvicorn app.main:app --reload
```

## Server inventory (read-only)

The server list comes from the main system's `servers` collection — we **only read** it.
Document fields (camelCase) are mapped into the model: `serverName`, `serverInternalIP`,
`stacks[].stackName`. The unit of work is a server×stack pair. Details:
`.claude/skills/server-inventory/`.

## Updating env in Portainer

Read-modify-write the whole Env array (`GET -> edit one line -> PUT the whole thing`);
Portainer lives at `https://{serverInternalIP}:9443`, authenticated with a shared
login/password from `.env` (`POST /api/auth` -> JWT). Protocol and safety notes —
`.claude/skills/portainer-env-update/`. **Dry-run across all stacks first**, then roll out.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
