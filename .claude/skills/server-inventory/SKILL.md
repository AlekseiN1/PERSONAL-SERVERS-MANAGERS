---
name: server-inventory
description: Справка по коллекции servers MongoDB (инвентарь VM, READ-ONLY) и маппингу её camelCase-полей в модель Server/StackTarget. Используй когда нужно понять структуру документа сервера, какие поля читаются и как строится пара сервер×стек.
---

# Инвентарь серверов (коллекция `servers`, read-only)

Коллекция принадлежит **основной системе** (рядом companies/devices/controllers/…).
Наш инструмент её **только читает** — никогда не пишет. Изменения env идут через Portainer API.

Маппинг описан моделями `Server` / `Stack` / `StackTarget` в
[app/models.py](../../../app/models.py), чтение — `services.load_targets()`.

## Схема документа (важные поля)

| Поле в Mongo (camelCase) | Модель           | Описание                                                       |
|--------------------------|------------------|----------------------------------------------------------------|
| `serverName`             | `Server.name`    | имя VM                                                          |
| `serverInternalIP`       | `Server.internal_ip` | внутренний IP → Portainer `https://{ip}:9443`              |
| `Username` / `password`  | override кредов  | обычно пусты → берём общие из `.env`                          |

Поле `stacks[]` в Mongo почти везде **пустое** и НЕ используется как источник стеков —
реальные стеки тянутся live из Portainer (`list_stacks_live`). Игнорируем (extra="ignore"):
`serverFQDN`, `serviceApiToken`/`influxToken` (зашифрованы), `stacks`, devices/region и пр.

## Единица работы — `StackTarget`

`list_stacks_live()` берёт серверы из Mongo и опрашивает каждый Portainer (`GET /api/stacks`),
возвращая строки `StackListing` (сервер + стек, либо `reachable=False` с ошибкой).
У каждой пары `token = "<server_id>::<stack_name>"` — это значение чекбоксов в UI и
элементов `targets` в запросе.

## Правила

- **Не писать** в коллекцию: ни insert, ни update. Если нужен новый сервер/стек — его заводит
  основная система, не этот инструмент.
- Имя БД — в `.env` (`MONGO_DB`). Коллекция всегда `servers`.
- Документы не нашей схемы (без `serverName`/`serverInternalIP`) `load_targets()` пропускает.
