"""The generated ready checks, run by a real bash with stub ss, pgrep, curl and sleep."""

import subprocess

import pytest

from app.config import Ready
from app.jobs import port_free_command, wait_ready_command

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
    assert "Porta 8000 libera." in result.stdout


def test_port_busy(run):
    result = run(port_free_command(READY), ss=[OTHER])
    assert result.returncode == 1
    assert "La porta 8000 è già in uso:" in result.stderr
    assert '"strata"' in result.stderr


def test_ready_after_loading(run):
    # Loading: not listening yet, then listening but /health not 200, then ready.
    result = run(wait_ready_command(READY), ss=["", LISTENER], curl=[22, 0])
    assert result.returncode == 0, result.stderr
    assert "Pronto: http://192.168.1.200:8000/health" in result.stdout


def test_another_engine_on_the_port_is_not_ready(run):
    # Strata answers /health on 8000, then llama-server dies (port busy / out of memory).
    result = run(wait_ready_command(READY), ss=[OTHER], curl=[0], pgrep=[0, 0, 0, 1])
    assert result.returncode == 1
    assert "llama-server non è più in esecuzione" in result.stderr
    assert "Pronto" not in result.stdout


def test_server_died(run):
    result = run(wait_ready_command(READY), pgrep=[1])
    assert result.returncode == 1
    assert "llama-server non è più in esecuzione" in result.stderr
