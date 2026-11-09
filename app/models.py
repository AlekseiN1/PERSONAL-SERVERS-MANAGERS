"""Pydantic I/O models.

The `servers` collection belongs to the main system (read-only for us). Document fields
are camelCase (`serverName`, `serverInternalIP`, `stacks[].stackName`), mapped via alias.
"""
from typing import Literal

from pydantic import BaseModel, Field


class Stack(BaseModel):
    """A stack inside a server document. stackID in Mongo is a UUID, NOT Portainer's
    numeric Id (in Portainer, a stack is looked up by stackName)."""

    stack_name: str = Field(alias="stackName")
    stack_id: str | None = Field(default=None, alias="stackID")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class Server(BaseModel):
    """A document from the `servers` collection (read-only). Maps the main system's
    camelCase fields."""

    id: str | None = Field(default=None, alias="_id")
    name: str = Field(alias="serverName")
    internal_ip: str = Field(alias="serverInternalIP")
    port: int = 9443  # not in the document — Portainer HTTPS default
    username: str | None = Field(default=None, alias="Username")  # usually empty -> .env
    password: str | None = Field(default=None, alias="password")
    stacks: list[Stack] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "ignore"}


class StackTarget(BaseModel):
    """A task target: one specific stack on one specific server (an expanded pair)."""

    server_id: str
    server_name: str
    internal_ip: str
    port: int = 9443
    stack_name: str
    username: str | None = None
    password: str | None = None

    @property
    def token(self) -> str:
        """Stable id of the pair for UI checkboxes: '<server_id>::<stack_name>'."""
        return f"{self.server_id}::{self.stack_name}"


class ServerGroup(BaseModel):
    """A server with its live stacks from Portainer (or an access error). For the UI."""

    server_id: str
    server_name: str
    internal_ip: str
    port: int = 9443
    is_main: bool = False
    reachable: bool = True
    error: str | None = None
    stacks: list[str] = Field(default_factory=list)  # stack names


class StackDetail(BaseModel):
    """Details of one stack: env, service names, compose file."""

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
    """State of a stack with a given name on one server (for comparing against the reference)."""

    server_id: str
    server_name: str
    is_main: bool = False
    reachable: bool = True
    error: str | None = None
    present: bool = False
    env_keys: list[str] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)  # present in main, not here
    extra_keys: list[str] = Field(default_factory=list)     # present here, not in main


class StackComparison(BaseModel):
    """Comparison of a same-named stack across all servers against the main one."""

    stack_name: str
    main_present: bool
    main_keys: list[str] = Field(default_factory=list)
    main_compose: str = ""
    servers: list[StackOnServer] = Field(default_factory=list)


class EnvUpdateRequest(BaseModel):
    """Request to update one env variable across selected stacks."""

    targets: list[str] | Literal["all"] = "all"  # StackTarget.token tokens
    key: str
    value: str
    dry_run: bool = True


class TaskResult(BaseModel):
    """Result of a task on one stack."""

    server_name: str
    stack_name: str
    ok: bool
    message: str
    old_value: str | None = None
    new_value: str | None = None
    dry_run: bool = False


class BackupRequest(BaseModel):
    """Request to back up compose+env of selected stacks."""

    targets: list[str] | Literal["all"] = "all"  # StackTarget.token tokens
    dry_run: bool = True


class BackupResult(BaseModel):
    """Result of backing up (compose+env) one stack."""

    server_name: str
    stack_name: str
    ok: bool
    message: str
    path: str | None = None  # where it was saved (backup files directory)
    dry_run: bool = False


class BackupStackEntry(BaseModel):
    """Saved backup timestamps for one stack directory on disk."""

    stack_dir: str
    timestamps: list[str] = Field(default_factory=list)  # newest first


class BackupServerEntry(BaseModel):
    """Saved backups for one server directory on disk, grouped by stack."""

    server_dir: str
    stacks: list[BackupStackEntry] = Field(default_factory=list)


class BackupContent(BaseModel):
    """Compose+env content of one saved backup, read straight off disk."""

    server_name: str
    stack_name: str
    timestamp: str
    compose: str = ""
    env_text: str = ""
    error: str | None = None


class ImageInfo(BaseModel):
    """One docker image on a server (from GET .../docker/images/json)."""

    id: str  # "sha256:..."
    tags: list[str] = Field(default_factory=list)  # empty/<none> -> dangling
    size_mb: float = 0.0
    created: int = 0  # unix timestamp
    in_use: bool = False  # referenced by at least one container (running or stopped)
    dangling: bool = False  # untagged (RepoTags empty or <none>:<none>)


class ServerImages(BaseModel):
    """Images on one server (or an access error). For the Images tab."""

    server_id: str
    server_name: str
    is_main: bool = False
    reachable: bool = True
    error: str | None = None
    images: list[ImageInfo] = Field(default_factory=list)


class ImageDeleteResult(BaseModel):
    """Result of deleting one image."""

    server_name: str
    image_id: str
    ok: bool
    message: str
    dry_run: bool = False


class ContainerLogs(BaseModel):
    """Logs of one container of a stack."""

    container_name: str
    container_id: str
    text: str = ""
    error: str | None = None


class StackLogs(BaseModel):
    """Logs of all containers of one stack on one server."""

    server_name: str
    stack_name: str
    ok: bool
    message: str = ""
    containers: list[ContainerLogs] = Field(default_factory=list)
