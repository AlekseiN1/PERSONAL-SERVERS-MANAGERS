# PERSONAL-SERVERS-MANAGERS

Веб + Python интерфейс для управления личными серверами. Каждый сервер — отдельная VM
в Proxmox. Список серверов хранится в MongoDB. Цель — запускать задачи по выбранным
серверам (все или конкретные). Первая задача — обновление одной env-переменной в стеке
Portainer.

## Данные (важно)

Коллекция `servers` принадлежит **основной системе** — для нас она **read-only** (никогда
не пишем в неё). Схема документа в camelCase, маппим через alias в `models.py`:
- `serverName` → name, `serverInternalIP` → internal_ip
- `Username`/`password` — per-server override Portainer-кредов (обычно пусты → берём из `.env`).
- `stacks[]` в Mongo почти везде **пустое** — НЕ источник правды по стекам.

**Список стеков тянется live из Portainer**: `load_servers()` даёт инвентарь подключений,
`list_stacks_live()` конкурентно опрашивает каждый Portainer (`GET /api/stacks`); недоступные
серверы возвращаются строкой с ошибкой (`StackListing.reachable=False`), не пропадают.

**Единица работы** = пара (сервер × стек), `StackTarget` с `token = "<server_id>::<stack_name>"`.
Portainer base URL = `https://{serverInternalIP}:9443` (не `serverFQDN`). Стек в Portainer
ищется по `Name` (числовой Id берём из ответа `GET /api/stacks`).

**Главный сервер** (`MAIN_SERVER_NAME` в `.env`, по умолч. `Nitarti-15-f375154914`) — эталон
для сравнения/нормализации/распространения, в UI помечен ★.

## Возможности

- **Env-update** (`/`) — поменять одну переменную на выбранных стеках (read-modify-write).
- **Детали стека** (HTMX) — env, имена сервисов (из compose), сам compose-файл.
- **Ручное редактирование стека** (HTMX, «✎ edit / restart» в списке) — правка env
  (текст `NAME=value`) и compose одного стека:
  - **Save & redeploy** (`save_stack`) — полный PUT (`update_stack_full`: `stackFileContent` +
    весь массив env) с redeploy; чекбокс *pull images*. Git-стеки не поддержаны.
  - **Restart** (`restart_stack`) — `stop`→`start` (`POST /api/stacks/{id}/stop|start`);
    падение `stop` (уже остановлен) игнорируется, важен успешный `start`.
- **Compare & sync** (`/compare`) — сравнить одноимённый стек по серверам с эталоном (diff
  ключей env), затем:
  - **Normalize keys** (`normalize_keys`) — выровнять ключи env под эталон, **значения на
    серверах сохраняются** (`align_env_keys`); per-server токены не перетираются.
  - **Propagate** (`propagate_stack`) — создать стек на серверах, где его нет, из compose
    (редактируемый, с эталона) + env эталона. `POST /api/stacks/create/standalone/string`
    (только standalone Docker; swarm не поддержан).
- Все мутации поддерживают **dry-run** (по умолчанию включён).

## Стек

- **Backend**: FastAPI (async)
- **Frontend**: server-side Jinja2 + HTMX (без отдельного JS-фреймворка)
- **YAML**: `pyyaml` — парсинг compose-файлов стеков (имена сервисов)
- **DB**: MongoDB через `motor` (async)
- **HTTP-клиент**: `httpx.AsyncClient` (для Portainer API; auth по логину/паролю → JWT)
- **Конфиг**: `pydantic-settings`, секреты из `.env`

## Структура

```
app/
├── config.py        # настройки из .env (PORTAINER_USER/PASSWORD, MONGO_URI/DB, дефолтный порт)
├── db.py            # подключение к Mongo, коллекция servers (read-only)
├── models.py        # Server/Stack (маппинг camelCase), StackTarget, EnvUpdateRequest, TaskResult
├── portainer.py     # PortainerClient (auth→JWT, list/file/create/update) + replace_env_var/align_env_keys
├── services.py      # load_servers, list_servers_grouped, run_env_update, compare_stacks, normalize_keys, propagate_stack
├── main.py          # FastAPI app, монтаж роутеров/шаблонов/static
├── routers/         # servers.py, tasks.py
├── templates/       # base.html, index.html, partials/
└── static/
tests/               # test_portainer.py, test_services.py
```

## Команды

```bash
pip install -r requirements.txt
python run.py                        # запуск + авто-открытие браузера (OPEN_BROWSER/APP_HOST/APP_PORT в .env)
uvicorn app.main:app --reload        # чистый dev (без браузера), http://127.0.0.1:8000
pytest                               # тесты
```

## Конвенции

- Async везде (роуты, db, http).
- Pydantic-модели на входе/выходе, не голые dict.
- Секреты **только** в `.env` (он в `.gitignore`); креды/IP никогда не хардкодить —
  IP и имя стека берутся из Mongo, логин/пароль Portainer из `.env` (общие на всех;
  per-server override в Mongo — опционально).
- Mongo — **read-only**. Изменения env идут только через Portainer API, не в документ.
- TLS `verify=False` для Portainer — осознанно: self-signed серты во внутренней сети.

## Безопасность Portainer (критично)

1. **Read-modify-write всего массива Env.** PUT перетирает env целиком — всегда отправляй
   весь массив из GET с одной правкой, иначе остальные переменные исчезнут.
2. `prune: false`, `pullImage: false` — безопасные дефолты в body PUT.
3. `endpointId` брать из `GET /api/endpoints`, не угадывать (не вслепую `[0]`).
4. Имя стека — из Mongo, не хардкод.
5. Git-стеки (Repository) обновляются иначе → `POST /api/stacks/{id}/git/redeploy`.
6. **Dry-run** (только GET endpoints+stacks) перед раскаткой на все серверы.

Подробный протокол задачи — в `.claude/skills/portainer-env-update/`.
