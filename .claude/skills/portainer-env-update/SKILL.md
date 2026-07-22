---
name: portainer-env-update
description: Обновить ОДНУ env-переменную в стеке Portainer на одной или нескольких VM, не трогая остальные. Используй когда нужно поменять/добавить переменную окружения в стеке через Portainer API (read-modify-write всего массива Env).
---

# Обновление одной env-переменной в стеке Portainer

Точечного «обновить одну переменную» в API нет: PUT перезаписывает весь список env.
Поэтому протокол всегда **read-modify-write**: `GET стек → заменить одно значение в массиве
Env → PUT весь массив обратно`.

В этом проекте логика реализована в [app/portainer.py](../../../app/portainer.py)
(`PortainerClient`, `replace_env_var`) и оркеструется в
[app/services.py](../../../app/services.py) (`run_env_update`). Меняй там, а не дублируй.

## Базовое

- Base URL у каждой VM: `https://{internal_ip}:{port}` (порт по умолчанию `9443`).
- Auth: **логин/пароль** (общие на всех, из `.env`; per-server override — поля
  `username`/`password` документа Mongo). Сначала `POST /api/auth` с
  `{"username","password"}` → получаем `{"jwt": ...}`, далее во все запросы заголовок
  `Authorization: Bearer <jwt>`.
- Self-signed серты во внутренней сети → `verify=False`.
- Content-Type для PUT: `application/json`.

## Шаг 0 — аутентификация

```
POST /api/auth   body {"username": "...", "password": "..."}  ->  {"jwt": "..."}
```
JWT кладём в `Authorization: Bearer <jwt>`. В коде это делает `PortainerClient.authenticate()`
лениво перед первым запросом.

## Шаг 1 — endpointId

```
GET /api/endpoints   (Authorization: Bearer <jwt>)
```
Взять `Id` нужного endpoint. Обычно `1`, но брать из ответа, не угадывать. Если endpoint'ов
несколько — выбирать по `Name`, не вслепую `[0]`.

## Шаг 2 — найти стек по имени

```
GET /api/stacks   (Authorization: Bearer <jwt>)
```
Найти по `Name` (имя берётся из Mongo: `stacks[].stackName`), запомнить **числовой** `Id`
и массив `Env` (`[{"name": ..., "value": ...}, ...]`).
ВАЖНО: `stackID` в Mongo — это UUID основной системы, а **не** Id для Portainer API.
Числовой Id берётся только из ответа `GET /api/stacks`.

## Шаг 3 — локально заменить одну переменную

Пройти по `Env`: если `name == key` → заменить `value`, иначе оставить как есть.
Если переменной нет — добавить (`add_if_missing=True`). Результат — тот же массив с одной правкой.

## Шаг 4 — PUT весь стек обратно

```
PUT /api/stacks/{stackId}?endpointId={endpointId}   (Authorization: Bearer <jwt>, Content-Type: application/json)

Body:
{
  "env": [ ... ВЕСЬ массив с одной изменённой строкой ... ],
  "prune": false,       // не удалять сервисы, которых нет в compose
  "pullImage": false    // не перетягивать образы (true — если нужны свежие)
}
```
Успех: HTTP 200, env применяется при redeploy.

## curl для ручной проверки одной VM

```bash
IP="192.168.68.1"; PORT=9443
# 0: логин -> JWT
JWT=$(curl -sk -X POST "https://$IP:$PORT/api/auth" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme"}' | jq -r .jwt)
# 1+2: посмотреть стеки/env
curl -sk "https://$IP:$PORT/api/stacks" -H "Authorization: Bearer $JWT" | jq
# 4: PUT весь массив целиком
curl -sk -X PUT "https://$IP:$PORT/api/stacks/5?endpointId=1" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"env":[{"name":"APP_TOKEN","value":"НОВОЕ"}],"prune":false,"pullImage":false}'
```

## Нюансы (обязательно соблюдать)

1. **PUT перетирает env целиком** — никогда не отправляй только изменённую переменную.
2. **Read-modify-write, last-write-wins** — между GET и PUT стек менять не должны; для частого
   авто учитывать гонку.
3. **Имя стека** — из Mongo (`stacks[].stackName`), не хардкод. Единица работы — пара
   сервер×стек (`StackTarget`), у сервера может быть несколько стеков.
4. **endpointId** — из API.
5. **Git-стеки (Repository).** Если стек подключён к репозиторию, обычный PUT может вести себя
   иначе — смотри `POST /api/stacks/{id}/git/redeploy` и передачу env там.
6. **Dry-run.** Перед раскаткой на все серверы прогнать шаги 1–2 (только GET) по всем VM:
   убедиться что стек находится и endpoint корректный, и только потом включать PUT.
