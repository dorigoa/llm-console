"""config.yaml: the hosts, the tabs, their sections and the commands each one runs.

It is the only place commands come from: the API only receives tab, section and
variant ids and looks them up here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

Ident = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SSHSettings(_Model):
    binary: str = "ssh"
    # Private key and known_hosts as seen by the app (in the container: what
    # entrypoint.sh and the Quadlet unit put there). None = ssh's own defaults.
    identity: str | None = None
    known_hosts: str | None = None
    connect_timeout: int = Field(default=10, ge=1)
    # Prepended to PATH in the remote shell: a non-interactive ssh session does
    # not read ~/.profile, so ~/.local/bin is missing there.
    remote_path_prefix: str | None = "$HOME/.local/bin"

    @field_validator("identity", "known_hosts")
    @classmethod
    def _expand_user(cls, value: str | None) -> str | None:
        return os.path.expanduser(value) if value else value


class Host(_Model):
    address: str
    user: str


class Section(_Model):
    id: Ident
    label: str
    # Variant id -> remote command, in the order of the dialog's radio buttons.
    variants: dict[Ident, str] = Field(default_factory=dict)
    stop: str | None = None
    # Run first runs `stop` (its exit code is ignored), then the variant: switching
    # model is one click, with no --restart in the scripts.
    stop_before_run: bool = False
    run_timeout: float = Field(default=900, gt=0)
    stop_timeout: float = Field(default=120, gt=0)

    @field_validator("variants", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return {} if value is None else value

    @model_validator(mode="after")
    def _stop_before_run_needs_stop(self) -> Section:
        if self.stop_before_run and not self.stop:
            raise ValueError(f"section {self.id!r}: stop_before_run needs a stop command")
        return self

    @property
    def can_run(self) -> bool:
        return bool(self.variants)

    @property
    def can_stop(self) -> bool:
        return bool(self.stop)


class Tab(_Model):
    id: Ident
    label: str
    host: str
    sections: list[Section] = Field(default_factory=list)

    @field_validator("sections", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return [] if value is None else value

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)


class Config(_Model):
    ssh: SSHSettings = Field(default_factory=SSHSettings)
    hosts: dict[Ident, Host]
    tabs: list[Tab]

    @model_validator(mode="after")
    def _check_references(self) -> Config:
        tab_ids = [t.id for t in self.tabs]
        if len(tab_ids) != len(set(tab_ids)):
            raise ValueError(f"duplicate tab ids: {tab_ids}")
        for tab in self.tabs:
            if tab.host not in self.hosts:
                raise ValueError(f"tab {tab.id!r}: unknown host {tab.host!r}")
            section_ids = [s.id for s in tab.sections]
            if len(section_ids) != len(set(section_ids)):
                raise ValueError(f"tab {tab.id!r}: duplicate section ids: {section_ids}")
        return self

    def tab(self, tab_id: str) -> Tab | None:
        return next((t for t in self.tabs if t.id == tab_id), None)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Reads and validates the config: LLM_CONSOLE_CONFIG, else config.yaml in the checkout."""
    path = Path(path or os.environ.get("LLM_CONSOLE_CONFIG") or DEFAULT_CONFIG_PATH)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)
