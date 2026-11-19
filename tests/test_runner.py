import subprocess
import sys
import time

import pytest

from app.config import Host, SSHSettings
from app.runner import DryRunRunner, SubprocessRunner, build_ssh_argv, clean_line, remote_command

HOST = Host(address="192.168.1.200", user="alvise")


def option_values(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]


def test_ssh_argv_with_key_and_known_hosts():
    ssh = SSHSettings(identity="/keys/id_ecdsa", known_hosts="/kh", connect_timeout=7)
    argv = build_ssh_argv(ssh, HOST, "~/Strata/stop.sh")
    options = option_values(argv)
    assert argv[0] == "ssh"
    assert "-n" in argv and "-T" in argv
    assert {"BatchMode=yes", "ConnectTimeout=7", "ControlPath=none", "ControlMaster=no",
            "UserKnownHostsFile=/kh", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"} <= set(options)
    assert argv[argv.index("-i") + 1] == "/keys/id_ecdsa"
    assert argv[-2] == "alvise@192.168.1.200"
    assert argv[-1] == 'export PATH="$HOME/.local/bin:$PATH"; ~/Strata/stop.sh'


def test_ssh_argv_defaults_leave_keys_to_ssh():
    argv = build_ssh_argv(SSHSettings(), HOST, "true")
    options = option_values(argv)
    assert "-i" not in argv
    assert not any(o.startswith(("UserKnownHostsFile", "StrictHostKeyChecking")) for o in options)


def test_remote_command_without_prefix():
    assert remote_command(SSHSettings(remote_path_prefix=None), "x y") == "x y"


def test_remote_command_is_valid_shell():
    cmd = remote_command(SSHSettings(remote_path_prefix="/opt/fake/bin"), 'echo "$PATH"')
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True).stdout
    assert out.startswith("/opt/fake/bin:")


def test_clean_line():
    assert clean_line(b"\x1b[32mready\x1b[0m at http://x\r\n") == "ready at http://x"
    assert clean_line(b"10%\r50%\r100%\n") == "100%"
    assert clean_line(b"\xff ok\n") == "� ok"


@pytest.mark.anyio
async def test_subprocess_runner_exit_code_and_output():
    out = []
    code = await SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print('one'); print('two', file=sys.stderr); sys.exit(3)"],
        10, out.append)
    assert code == 3
    assert sorted(out) == ["one", "two"]


@pytest.mark.anyio
async def test_subprocess_runner_timeout_kills():
    start = time.monotonic()
    code = await SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(30)"], 0.3, [].append)
    assert code is None
    assert time.monotonic() - start < 5


@pytest.mark.anyio
async def test_dry_run_runs_nothing():
    out = []
    code = await DryRunRunner(delay=0).run(["ssh", "alvise@h", "rm -rf ~"], 10, out.append)
    assert code == 0
    assert out[0] == "[dry-run] ssh alvise@h 'rm -rf ~'"
