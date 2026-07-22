# Personal Servers Manager

Веб + Python интерфейс для управления личными серверами (Proxmox VM). Список серверов — в
MongoDB. Задачи запускаются по выбранным серверам (все или конкретные). Первая задача —
обновление одной env-переменной в стеке Portainer.

## Стек

FastAPI · Jinja2 + HTMX · MongoDB (motor) · httpx · pydantic-settings.

## Запуск

Скопируй `.env.example` в `.env` и заполни `PORTAINER_USER`/`PORTAINER_PASSWORD`, `MONGO_URI`
и `MONGO_DB` (имя базы — там, где лежит коллекция `servers`). Затем:

```powershell
.\run.ps1        # PowerShell: создаст .venv, поставит зависимости, поднимет сервер
```
или `run.bat` (двойной клик / cmd). Браузер откроется сам на http://127.0.0.1:8000
(отключить: `OPEN_BROWSER=false` в `.env`; хост/порт — `APP_HOST`/`APP_PORT`).

Вручную (если venv уже есть):
```powershell
.\.venv\Scripts\python.exe run.py                       # uvicorn + авто-открытие браузера
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload   # чистый dev, без браузера
```

## Инвентарь серверов (read-only)

Список серверов берётся из коллекции `servers` основной системы — мы её **только читаем**.
Поля документа (camelCase) маппятся в модель: `serverName`, `serverInternalIP`,
`stacks[].stackName`. Единица работы — пара сервер×стек. Подробно —
`.claude/skills/server-inventory/`.

## Обновление env в Portainer

Read-modify-write всего массива Env (`GET → правка одной строки → PUT целиком`); Portainer
по адресу `https://{serverInternalIP}:9443`, авторизация общими логином/паролем из `.env`
(`POST /api/auth` → JWT). Протокол и нюансы безопасности —
`.claude/skills/portainer-env-update/`. **Сначала dry-run по всем стекам**, потом раскатка.

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest
```
