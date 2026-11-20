"""config.yaml: the hosts, the tabs, their sections and the commands each one runs.

It is the only place commands come from: the API only receives tab, section and
variant ids and looks them up here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

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


class Variant(_Model):
    command: str
    # Text of the radio button; None = the variant id.
    label: str | None = None


class Ready(_Model):
    """For a Run command that starts the server in the background and returns at once.

    Before the command: the port of `url` must be free (after the stop, if any).
    After it: wait until `process` is running, listens on that port and `url`
    answers 2xx; the Run fails as soon as `process` is no longer running.
    """

    url: str
    # Process name, as pgrep -x and ss -p show it (at most 15 characters).
    process: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,15}$")]
    timeout: float = Field(default=600, gt=0)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {value!r}")
        return value

    @property
    def port(self) -> int:
        parts = urlsplit(self.url)
        return parts.port or (443 if parts.scheme == "https" else 80)


class Section(_Model):
    id: Ident
    label: str
    # Variant id -> remote command (or {command, label}), in the order of the
    # dialog's radio buttons.
    variants: dict[Ident, Variant] = Field(default_factory=dict)
    # Radio button selected when the dialog opens; None = none.
    default_variant: Ident | None = None
    stop: str | None = None
    # Run first runs `stop` (its exit code is ignored), then the variant: switching
    # model is one click, with no --restart in the scripts.
    stop_before_run: bool = False
    run_timeout: float = Field(default=900, gt=0)
    stop_timeout: float = Field(default=120, gt=0)
    ready: Ready | None = None
    # Remote command telling whether this engine is on: exit 0 = on, 1 = off,
    # anything else = could not tell. Needed when another section lists this one
    # in exclusive_with.
    running: str | None = None
    # Sections of the same tab that cannot be on together with this one (same GPUs,
    # same port): Run is refused, and the page says to stop them first, while one
    # of them is on or has an operation in progress.
    exclusive_with: list[Ident] = Field(default_factory=list)

    @field_validator("variants", mode="before")
    @classmethod
    def _plain_commands(cls, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return {k: {"command": v} if isinstance(v, str) else v for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _default_variant_exists(self) -> Section:
        if self.default_variant is not None and self.default_variant not in self.variants:
            raise ValueError(f"section {self.id!r}: default_variant {self.default_variant!r} is not a variant")
        return self

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
            for section in tab.sections:
                for other_id in section.exclusive_with:
                    other = tab.section(other_id)
                    if other is None or other is section:
                        raise ValueError(f"section {section.id!r}: exclusive_with {other_id!r} "
                                         f"is not another section of tab {tab.id!r}")
                    if not other.running:
                        raise ValueError(f"section {section.id!r}: exclusive_with {other_id!r} "
                                         f"needs a running command in {other_id!r}")
        return self

    def tab(self, tab_id: str) -> Tab | None:
        return next((t for t in self.tabs if t.id == tab_id), None)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Reads and validates the config: LLM_CONSOLE_CONFIG, else config.yaml in the checkout."""
    path = Path(path or os.environ.get("LLM_CONSOLE_CONFIG") or DEFAULT_CONFIG_PATH)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)
