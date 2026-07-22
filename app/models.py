"""Pydantic-модели I/O.

Коллекция `servers` принадлежит основной системе (read-only для нас). Поля документа в
camelCase (`serverName`, `serverInternalIP`, `stacks[].stackName`) маппятся через alias.
"""
from typing import Literal

from pydantic import BaseModel, Field


class Stack(BaseModel):
    """Стек внутри документа сервера. stackID в Mongo — UUID, НЕ числовой Id Portainer
    (в Portainer стек ищется по stackName)."""

    stack_name: str = Field(alias="stackName")
    stack_id: str | None = Field(default=None, alias="stackID")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class Server(BaseModel):
    """Документ коллекции servers (read-only). Маппинг camelCase-полей основной системы."""

    id: str | None = Field(default=None, alias="_id")
    name: str = Field(alias="serverName")
    internal_ip: str = Field(alias="serverInternalIP")
    port: int = 9443  # в документе нет — дефолт Portainer HTTPS
    username: str | None = Field(default=None, alias="Username")  # обычно пусто -> .env
    password: str | None = Field(default=None, alias="password")
    stacks: list[Stack] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "ignore"}


class StackTarget(BaseModel):
    """Цель задачи: конкретный стек на конкретном сервере (развёрнутая пара)."""

    server_id: str
    server_name: str
    internal_ip: str
    port: int = 9443
    stack_name: str
    username: str | None = None
    password: str | None = None

    @property
    def token(self) -> str:
        """Стабильный id пары для чекбоксов UI: '<server_id>::<stack_name>'."""
        return f"{self.server_id}::{self.stack_name}"


class ServerGroup(BaseModel):
    """Сервер со списком его живых стеков из Portainer (или ошибка доступа). Для UI."""

    server_id: str
    server_name: str
    internal_ip: str
    port: int = 9443
    is_main: bool = False
    reachable: bool = True
    error: str | None = None
    stacks: list[str] = Field(default_factory=list)  # имена стеков


class StackDetail(BaseModel):
    """Детали одного стека: env, имена сервисов, compose-файл."""

    server_id: str
    server_name: str
    stack_name: str
    stack_id: int | None = None
    env: list[dict] = Field(default_factory=list)  # [{name, value}, ...]
    services: list[str] = Field(default_factory=list)
    compose: str = ""
    reachable: bool = True
    error: str | None = None


class StackOnServer(BaseModel):
    """Состояние стека с данным именем на одном сервере (для сравнения с эталоном)."""

    server_id: str
    server_name: str
    is_main: bool = False
    reachable: bool = True
    error: str | None = None
    present: bool = False
    env_keys: list[str] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)  # есть в main, нет тут
    extra_keys: list[str] = Field(default_factory=list)     # есть тут, нет в main


class StackComparison(BaseModel):
    """Сравнение одноимённого стека по всем серверам относительно главного."""

    stack_name: str
    main_present: bool
    main_keys: list[str] = Field(default_factory=list)
    main_compose: str = ""
    servers: list[StackOnServer] = Field(default_factory=list)


class EnvUpdateRequest(BaseModel):
    """Запрос на обновление одной env-переменной по выбранным стекам."""

    targets: list[str] | Literal["all"] = "all"  # токены StackTarget.token
    key: str
    value: str
    dry_run: bool = True


class TaskResult(BaseModel):
    """Результат задачи по одному стеку."""

    server_name: str
    stack_name: str
    ok: bool
    message: str
    old_value: str | None = None
    new_value: str | None = None
    dry_run: bool = False
