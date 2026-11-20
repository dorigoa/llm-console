"""Shell checks run by a real bash with stub commands: the generated ready checks
(stub ss, pgrep, curl, sleep) and the `running` command of Strata in config.yaml."""

import shlex
import subprocess

import pytest

from app.config import Ready, load_config
from app.jobs import port_free_command, wait_ready_command
from conftest import REPO_CONFIG

READY = Ready(url="http://192.168.1.200:8000/health", process="llama-server")
LISTENER = 'LISTEN 0 512 192.168.1.200:8000 0.0.0.0:* users:(("llama-server",pid=42,fd=5))'
OTHER = 'LISTEN 0 512 192.168.1.200:8000 0.0.0.0:* users:(("strata",pid=7,fd=9))'


@pytest.fixture
def run(tmp_path):
    """run(command, ss=..., pgrep=[exit codes], curl=[exit codes]) -> CompletedProcess.

    Each stub returns the next value of its list (the last one repeats);
    `ss` prints the next line of its list. sleep returns at once.
    """

    def stub(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def next_value(name, values):
        (tmp_path / f"{name}.values").write_text("\n".join(values) + "\n")
        # Pop the first line; keep the last one forever.
        return (f'f={tmp_path}/{name}.values; v=$(head -n1 "$f"); '
                f'[ "$(wc -l < "$f")" -gt 1 ] && sed -i.bak 1d "$f"; ')

    def _run(command, ss=("",), pgrep=(0,), curl=(0,)):
        stub("ss", next_value("ss", list(ss)) + 'echo "$v" | grep -v "^$"; exit 0\n')
        stub("pgrep", next_value("pgrep", [str(c) for c in pgrep]) + 'exit "$v"\n')
        stub("curl", next_value("curl", [str(c) for c in curl]) + 'exit "$v"\n')
        stub("sleep", "exit 0\n")
        env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True,
                              timeout=10)

    return _run


def test_port_free(run):
    result = run(port_free_command(READY), ss=[""])
    assert result.returncode == 0
    assert "Port 8000 is free." in result.stdout


def test_port_busy(run):
    result = run(port_free_command(READY), ss=[OTHER])
    assert result.returncode == 1
    assert "Port 8000 is already in use:" in result.stderr
    assert '"strata"' in result.stderr


def test_ready_after_loading(run):
    # Loading: not listening yet, then listening but /health not 200, then ready.
    result = run(wait_ready_command(READY), ss=["", LISTENER], curl=[22, 0])
    assert result.returncode == 0, result.stderr
    assert "Ready: http://192.168.1.200:8000/health" in result.stdout


def test_another_engine_on_the_port_is_not_ready(run):
    # Strata answers /health on 8000, then llama-server dies (port busy / out of memory).
    result = run(wait_ready_command(READY), ss=[OTHER], curl=[0], pgrep=[0, 0, 0, 1])
    assert result.returncode == 1
    assert "llama-server is no longer running" in result.stderr
    assert "Ready" not in result.stdout


def test_server_died(run):
    result = run(wait_ready_command(READY), pgrep=[1])
    assert result.returncode == 1
    assert "llama-server is no longer running" in result.stderr


@pytest.mark.parametrize(("stdout", "exit_code", "expected"), [
    ("ud-q4_k_xl-256k: ready at http://192.168.1.200:8000", 0, 0),
    ("ud-q4_k_xl-256k: starting at http://192.168.1.200:8000", 0, 0),
    ("Nothing from `strata launch` is running.", 0, 1),
    ("", 2, 2),  # strata itself failed: cannot tell
])
def test_strata_running_command(tmp_path, stdout, exit_code, expected):
    """config.yaml's `running` of Strata against a stub ~/.local/bin/strata."""
    strata = tmp_path / ".local" / "bin" / "strata"
    strata.parent.mkdir(parents=True)
    strata.write_text('#!/bin/bash\n[ "$*" = "launch --status" ] || exit 9\n'
                      f"printf '%s\\n' {shlex.quote(stdout)}\nexit {exit_code}\n")
    strata.chmod(0o755)
    command = load_config(REPO_CONFIG).tab("linux").section("strata").running
    result = subprocess.run(["bash", "-c", command], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(("section", "argv0"), [
    ("turbofieldfare", "/Users/x/.local/lib/turbofieldfare/TurboFieldfareServer"),
    ("mlx-lm", "/Users/x/.local/bin/mlx_lm.server"),
])
def test_mac_running_commands(section, argv0):
    """config.yaml's `running` of the Mac engines, with a real pgrep -f: on while a
    process with that path runs, off otherwise, although the pattern is in the
    command line of the shell that runs it."""
    command = load_config(REPO_CONFIG).tab("mac").section(section).running

    def check():
        return subprocess.run(["bash", "-c", command], timeout=10).returncode

    if check() == 0:
        pytest.skip(f"a real {section} is running on this machine")
    # A process whose command line has that path, as the real servers do.
    fake = subprocess.Popen(["bash", "-c", "sleep 30; :", argv0, "--model", "m"])
    try:
        assert check() == 0
    finally:
        fake.kill()
        fake.wait()
    assert check() == 1


def test_ready_without_process_only_waits_for_the_url(run):
    ready = Ready(url="http://192.168.1.200:8000/v1/models")
    command = wait_ready_command(ready)
    assert "pgrep" not in command and "ss " not in command
    result = run(command, curl=[7, 22, 0], pgrep=[1])  # pgrep would say "dead": not used
    assert result.returncode == 0, result.stderr
    assert "Ready: http://192.168.1.200:8000/v1/models" in result.stdout
