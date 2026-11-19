"""Runs the remote commands: builds the ssh command line and executes it.

`SubprocessRunner` really runs it; `DryRunRunner` (LLM_CONSOLE_DRY_RUN=1) only
reports what it would run. Both take an argv list: no local shell is involved,
and the remote command always comes from config.yaml.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Callable
from typing import Protocol

from .config import Host, SSHSettings

OutputFn = Callable[[str], None]

# Colour/cursor escapes (CSI) and terminal titles (OSC): the output goes to a web page.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def remote_command(ssh: SSHSettings, command: str) -> str:
    """The command line the remote shell runs: `command`, with remote_path_prefix in PATH."""
    if ssh.remote_path_prefix:
        return f'export PATH="{ssh.remote_path_prefix}:$PATH"; {command}'
    return command


def build_ssh_argv(ssh: SSHSettings, host: Host, command: str) -> list[str]:
    argv = [
        ssh.binary,
        "-T",
        "-n",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={ssh.connect_timeout}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        # Never reuse a multiplexed connection: it would skip the host key check.
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
    ]
    if ssh.known_hosts:
        argv += ["-o", f"UserKnownHostsFile={ssh.known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    if ssh.identity:
        argv += ["-i", ssh.identity, "-o", "IdentitiesOnly=yes"]
    argv += [f"{host.user}@{host.address}", remote_command(ssh, command)]
    return argv


def clean_line(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").rstrip("\r\n")
    # Progress bars redraw the line with \r: keep what it shows last.
    text = text.rsplit("\r", 1)[-1]
    return _ANSI.sub("", text)


class Runner(Protocol):
    async def run(self, argv: list[str], timeout: float, on_output: OutputFn) -> int | None:
        """Runs argv; returns its exit code, or None if it ran past `timeout` seconds and was killed."""
        ...


class SubprocessRunner:
    async def run(self, argv: list[str], timeout: float, on_output: OutputFn) -> int | None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1 << 20,
        )

        async def pump() -> int:
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                on_output(clean_line(line))
            return await proc.wait()

        try:
            return await asyncio.wait_for(pump(), timeout)
        except TimeoutError:
            return None
        finally:
            # Timeout, or the job was cancelled (app shutting down): do not leave ssh behind.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


class DryRunRunner:
    """Runs nothing: reports the command and waits `delay` seconds, as a real job would."""

    def __init__(self, delay: float = 3.0) -> None:
        self.delay = delay

    async def run(self, argv: list[str], timeout: float, on_output: OutputFn) -> int | None:
        on_output(f"[dry-run] {shlex.join(argv)}")
        await asyncio.sleep(min(self.delay, timeout))
        on_output("[dry-run] nessun comando eseguito")
        return 0
