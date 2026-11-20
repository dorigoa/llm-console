"""config.yaml: the hosts, the tabs, their sections and the commands each one runs.

It is the only place commands come from: the API only receives tab, section and
variant ids and looks them up here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

Ident = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# {option-id} in a variant command: replaced by the value of the chosen option.
# Not ${...} (a shell variable), and no spaces or commas ({ a; b; }, {a,b} are shell).
PLACEHOLDER = re.compile(r"(?<!\$)\{([a-z0-9][a-z0-9_-]*)\}")


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
    # The model this variant serves, for the status line ("Strata is launching
    # <model>…", "<model> is ready to serve requests"); None = the label.
    model: str | None = None


class Choice(_Model):
    # Replaces {<option id>} in the variant commands (trusted: it comes from here).
    value: str
    # Text of the radio button; None = the choice id.
    label: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _number_is_text(cls, value):
        # value: 262144 in YAML is an int.
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class Option(_Model):
    """One more group of radio buttons in the Run dialog, e.g. the context size.

    The browser sends only the id of the chosen choice; its value replaces
    {<option id>} in the command of the variant being run.
    """

    legend: str
    # One line under the legend.
    description: str | None = None
    # Choice id -> value (or {value, label}), in the order of the radio buttons.
    choices: dict[Ident, Choice]
    # Selected when the dialog opens, and used when a Run does not name a choice;
    # None = none (a Run must then name one).
    default: Ident | None = None

    @field_validator("choices", mode="before")
    @classmethod
    def _plain_values(cls, value):
        if isinstance(value, dict):
            return {k: v if isinstance(v, dict) else {"value": v} for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _check_choices(self) -> Option:
        if not self.choices:
            raise ValueError(f"option {self.legend!r} has no choices")
        if self.default is not None and self.default not in self.choices:
            raise ValueError(f"option {self.legend!r}: default {self.default!r} is not a choice")
        return self


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {value!r}")
    return value


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class Metrics(_Model):
    """The live line of the card: the app polls the engine every few seconds.

    `kind` says what the engine at `url` answers and how to read it (see
    app/metrics.py): llama-server (/slots and /metrics), strata (/metrics in
    JSON), turbofieldfare (/metrics), mlx-lm (no metrics: /health only). Engines
    that share a port tell each other apart by those answers.
    """

    kind: Literal["llama-server", "strata", "turbofieldfare", "mlx-lm"]
    # The engine's root, e.g. http://192.168.1.200:8000 (no path).
    url: str

    @field_validator("url")
    @classmethod
    def _root_url(cls, value: str) -> str:
        return _http_url(value).rstrip("/")


class Ready(_Model):
    """When a Run is done: after the command, wait until `url` answers 2xx.

    With `process`, for a command that starts the server in the background and
    returns at once: before the command the port of `url` must be free (after the
    stop, if any); after it `process` must also be running and listening on that
    port, and the Run fails as soon as it is no longer running.
    """

    url: str
    # Process name, as pgrep -x and ss -p show it (at most 15 characters).
    process: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,15}$")] | None = None
    timeout: float = Field(default=600, gt=0)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        return _http_url(value)

    @property
    def port(self) -> int:
        return _host_port(self.url)[1]


class Section(_Model):
    id: Ident
    label: str
    # One line under the card title and in the Run dialog (e.g. the models it serves).
    description: str | None = None
    # Legend over the radio buttons of the Run dialog.
    variants_legend: str = "Quantization"
    # Variant id -> remote command (or {command, label}), in the order of the
    # dialog's radio buttons.
    variants: dict[Ident, Variant] = Field(default_factory=dict)
    # Radio button selected when the dialog opens; None = none.
    default_variant: Ident | None = None
    # More radio groups in the Run dialog (option id -> Option), after the
    # variants; every variant command must contain {<option id>}.
    options: dict[Ident, Option] = Field(default_factory=dict)
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
    # The card's live line (idle, prefill, generating, tok/s, MTP acceptance);
    # None = no live line.
    metrics: Metrics | None = None

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

    @model_validator(mode="after")
    def _placeholders_match_options(self) -> Section:
        # A choice that changes nothing, or a {typo} sent to the shell, is a config error.
        for variant_id, variant in self.variants.items():
            used = set(PLACEHOLDER.findall(variant.command))
            if unknown := used - set(self.options):
                raise ValueError(f"section {self.id!r}, variant {variant_id!r}: "
                                 f"no option {', '.join(sorted(unknown))}")
            if missing := set(self.options) - used:
                raise ValueError(f"section {self.id!r}, variant {variant_id!r}: the command does "
                                 f"not use {', '.join('{' + m + '}' for m in sorted(missing))}")
        return self

    def choose(self, requested: dict[str, str]) -> dict[str, str]:
        """Option id -> choice id for a Run: the requested ones, else the defaults.

        ValueError for an unknown option or choice, or an option with no choice
        and no default.
        """
        if unknown := set(requested) - set(self.options):
            raise ValueError(f"Unknown option: {', '.join(sorted(unknown))}")
        chosen = {}
        for option_id, option in self.options.items():
            choice = requested.get(option_id, option.default)
            if choice is None:
                raise ValueError(f"Choose a value for {option.legend}")
            if choice not in option.choices:
                raise ValueError(f"Unknown {option.legend} choice: {choice}")
            chosen[option_id] = choice
        return chosen

    def command(self, variant_id: str, chosen: dict[str, str]) -> str:
        """The variant's command with {<option id>} replaced by the chosen values."""
        values = {o: self.options[o].choices[c].value for o, c in chosen.items()}
        return PLACEHOLDER.sub(lambda m: values[m.group(1)], self.variants[variant_id].command)

    @property
    def can_run(self) -> bool:
        return bool(self.variants)

    @property
    def can_stop(self) -> bool:
        return bool(self.stop)

    @property
    def endpoint(self) -> str | None:
        """host:port the engine serves on: the metrics URL's, else the ready URL's."""
        url = self.metrics.url if self.metrics else self.ready.url if self.ready else None
        if url is None:
            return None
        host, port = _host_port(url)
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


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
