# llm-console

A small web app that starts and stops LLM inference engines on remote hosts over ssh.

The page has one tab per host and one card per engine. Each card has a green **Run** and a red
**Stop** button:
- **Run** opens a dialog to pick a model or quantization, runs the matching script on the host, and
  reports when the model's API answers.
- **Stop** runs the engine's stop command.

The Linux tab drives llama-server and Strata on a PC; the Mac tab drives TurboFieldfareServer and
mlx_lm.server on a Mac Studio.

It runs in a rootless podman container on a small Linux box (the author's is a Raspberry Pi) and listens
on `127.0.0.1:80`. Two optional front doors make it reachable:
- **Tailscale:** `tailscale serve` publishes it on the tailnet over HTTPS.
- **Home LAN:** a socat forwarder publishes it on the box's LAN address, for devices that are not on the
  tailnet.

> **No authentication.** Anyone who can open the page can start and stop the engines. Expose it only on
> networks you trust (loopback, tailnet, home LAN).

Design notes and decisions (in Italian): [PLAN.md](PLAN.md).

## How it works

- [`config.yaml`](config.yaml) is the only source of commands. The browser sends tab, section, variant and
  option ids only, never a command or a value that goes into one.
- Every Run or Stop is a job: one or more `ssh user@host '<command>'` calls made from the container, with
  the key mounted from the host. The page polls the job and shows its output.
- A Run can stop the engine first, so switching model is one click. It can also refuse to start while an
  engine that uses the same GPUs is on. It succeeds only once the model's API (`/v1/models`) answers.

## Install on Linux from scratch

These steps were tested on Debian 13 (trixie) with podman 5.4. Any distribution with systemd and
podman 4.4 or later (for Quadlet units) should work, with its own package names. Run every command as
the user that will run the service, not as root, except where `sudo` is shown.

### 1. Packages

```bash
sudo apt install podman passt uidmap socat git curl iproute2
```

- `passt` provides `pasta`, the network that rootless podman 5 uses.
- `socat` runs the LAN forwarder.

### 2. Let a non-root user bind port 80

Rootless podman runs as your user, and Linux reserves the ports below 1024 to root. The container
publishes `127.0.0.1:80`, and the LAN forwarder listens on port 80 of the LAN address. Lower the limit to
80, now and at every boot:

```bash
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/50-llm-console.conf
```
```bash
sudo sysctl --system
```

Check it: `cat /proc/sys/net/ipv4/ip_unprivileged_port_start` must print `80`. `install.sh` refuses to
go on otherwise.

This lets every local user bind ports 80 to 1023. If you would rather keep the default, use ports above
1023 instead:
- `PublishPort=` in [`container/llm-console.container`](container/llm-console.container), for example
  `127.0.0.1:8081:8080`;
- `APP_ADDRESS` and `LAN_PORT` in [`container/lan.env`](container/lan.env);
- the target of `tailscale serve` (step 9).

### 3. Start the services at boot

They are systemd *user* services. Without lingering they would start only when you log in:

```bash
sudo loginctl enable-linger "$USER"
```

### 4. ssh key and host keys

The container runs every command as `ssh <user>@<host>`. It uses `~/.ssh/id_ecdsa`, which the unit
mounts read-only, and checks host keys against `~/.ssh/known_hosts` with `StrictHostKeyChecking=yes`.

Create the key, unless you already have `~/.ssh/id_ecdsa`:

```bash
ssh-keygen -t ecdsa -f ~/.ssh/id_ecdsa -N ''
```

Then do the following for every host in `config.yaml` (user and address as written there):

1. Authorize the key on the host:
   ```bash
   ssh-copy-id -i ~/.ssh/id_ecdsa.pub user@192.168.1.200
   ```
2. Record the host's key in `~/.ssh/known_hosts`, after checking the fingerprint it prints:
   ```bash
   ssh -o ControlPath=none -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ecdsa user@192.168.1.200 true
   ```

Keep `-o ControlPath=none`. If your `~/.ssh/config` multiplexes connections (`ControlMaster`), ssh reuses
an open connection, skips the host-key exchange and never adds the key.

To use another key file, edit the two `Volume=…id_ecdsa…` lines in `container/llm-console.container` and
`ssh.identity` in `config.yaml`.

### 5. Get the code

```bash
git clone https://github.com/dorigoa/llm-console.git ~/llm-console
```

It must be `~/llm-console`: the units read `config.yaml` and `container/lan.env` from there.

### 6. Configure

- **[`config.yaml`](config.yaml):** hosts (address and user), tabs, sections, and the commands that Run
  and Stop execute; see [Configuration](#configuration). The file in the repository is the author's
  setup: use it as an example.
- **[`container/lan.env`](container/lan.env):** `LAN_ADDRESS` is this machine's address on your LAN,
  as shown by `ip -4 addr`; `LAN_PORT` is the port the LAN uses. `install.sh` refuses an address the
  machine does not have.

Both files are tracked by git. Your edits stay local, and updates keep them; see
[Update](#update-when-the-code-on-github-changes).

### 7. What the remote hosts need

- The public key in `~/.ssh/authorized_keys` of the configured user (step 4).
- The scripts and commands named in `config.yaml`.
- For the llama-server checks in the example config: `curl`, `ss` (iproute2), `pgrep` (procps) and
  `killall` (psmisc).
- For Strata: `~/.local/bin/strata`.
- On the Mac of the example config:
  - `~/TurboFF/run-turboff.sh`, `~/TurboFF/stop.sh`, `~/MLX/run-mlx.sh` and `~/MLX/stop.sh`, from
    [dorigoa/TurboFF](https://github.com/dorigoa/TurboFF) and [dorigoa/MLX](https://github.com/dorigoa/MLX),
    cloned to `~/TurboFF` and `~/MLX`. A run script starts the server in the background, checks
    the port first with `lsof`, and returns once the server answers or as soon as it exits. Each stop
    script waits until its server has exited.
  - `TurboFieldfareServer` in `~/.local/bin`.
  - `mlx_lm.server` from `uv tool install mlx-lm`, also in `~/.local/bin`.

### 8. Install and start

```bash
~/llm-console/container/install.sh
```

It does the following:
- it checks the prerequisites: podman and socat, the port limit, free ports, the LAN address, the
  checkout path, the ssh key and `known_hosts`;
- it warns if lingering is off;
- it builds the image, which takes a few minutes the first time;
- it installs the Quadlet unit and the LAN forwarder unit;
- it starts both and waits for them to answer:

```
llm-console 145bc0f is up: http://127.0.0.1:80
llm-console is up on the LAN: http://192.168.1.40:80
```

From now on both start at boot. The LAN forwarder (`llm-console-lan.service`) starts and stops with the
web app. Open `http://<LAN_ADDRESS>/` from the LAN; the footer shows the installed version (`git describe`).

### 9. Optional: Tailscale

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:80
```

The page is then at `https://<machine>.<tailnet>.ts.net`, for tailnet devices only.

## Update when the code on GitHub changes

```bash
cd ~/llm-console
```
```bash
git pull --autostash
```
```bash
container/install.sh
```

`install.sh` does the following:
- it rebuilds the image, though only the layers that changed;
- it reinstalls the units;
- it restarts the web app and the LAN forwarder.

The page is down for a few seconds, and a Run or Stop in progress is cut off, because jobs live in
memory: check that no card shows one before updating. Afterwards the footer shows the new version.

- **Local edits:** `--autostash` puts your local changes to `config.yaml` or `lan.env` back after the
  pull. If the same lines changed upstream, git says "Applying autostash resulted in conflicts" and
  your version stays in the stash. To finish:
  1. edit the file to keep the right lines, removing the `<<<<<<<`, `=======` and `>>>>>>>` markers;
  2. run `git reset -q`, which leaves your edit as a local change;
  3. run `git stash drop`.
- **Only your `config.yaml` changed:** no rebuild is needed. Run `systemctl --user restart llm-console`,
  since the file is mounted into the container.
- **A copy without `.git`** (copied by `deploy.sh`, see [Development](#development)): turn it into a
  clone once, keeping the files:

  ```bash
  cd ~/llm-console
  ```
  ```bash
  git init -b main && git remote add origin https://github.com/dorigoa/llm-console.git
  ```
  ```bash
  git fetch origin && git reset origin/main && git branch --set-upstream-to=origin/main main
  ```
  ```bash
  git status
  ```

  `git status` lists the files that differ from GitHub, such as your `config.yaml`. From then on, update
  as above.

## Configuration

`config.yaml` holds the hosts, the tabs and their sections. A section with no `variants` has no Run, and
one with no `stop` has no Stop: the page shows them with the buttons disabled. A section can have these
fields:

| Field | Meaning |
|---|---|
| `variants` | Variant id → remote command, or `{command, label, model}`, in the order of the radio buttons. `label` is the radio text; `model` names the model in the status line ("Strata is launching *model*…", then "*model* is ready to serve requests"). |
| `stop` | Remote command run by Stop. |
| `description` | One line under the card title and in the Run dialog, e.g. the models served. |
| `variants_legend` | Legend over the radio buttons (default "Quantization"). |
| `default_variant` | Radio button selected when the dialog opens. |
| `options` | More radio groups in the Run dialog, after the variants: option id → `{legend, description, default, choices}`, where `choices` maps a choice id to its value, or to `{value, label}`. The chosen value replaces `{option id}` in the variant's command, and every variant must use every option. Example: the context size of TurboFieldfare, `-ctx {context}`. |
| `stop_before_run` | Run first runs `stop`; its exit code is ignored. |
| `run_timeout`, `stop_timeout` | Seconds before a step is killed. |
| `ready: {url, process, timeout}` | A Run succeeds only once `url` answers 2xx (e.g. `/v1/models`). See below for `process`. |
| `running` | Remote command telling whether the engine is on: exit 0 = on, 1 = off, anything else = cannot tell. |
| `exclusive_with: [id, …]` | Sections of the same tab that cannot be on together with this one; each needs `running`. While one of them is on, or has a job running, Run is refused and the page says to stop it first. |

`process` in `ready` is optional. It is for a script that starts the server in the background and returns
at once, such as `run-qwen3.8-27b.sh`. With `process`:
- before the Run, the port of `url` must be free;
- after the Run, `process` must also be listening on that port, and the Run fails as soon as it dies.

These checks use `ss` and `pgrep -x`, so `process` needs a Linux host. macOS has no `ss`, and
`mlx_lm.server` runs as `python`. On the Mac, leave `process` out and let the run script do the checks
and the waiting, as `~/TurboFF/run-turboff.sh` does.

Hosts, tabs and the ssh settings are at the top of the file; the comments there explain them.

## Development

On any machine with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```
```bash
uv run pytest
```
```bash
LLM_CONSOLE_DRY_RUN=1 uv run uvicorn --factory app.main:create_app --port 8765 --reload
```

In dry-run (`LLM_CONSOLE_DRY_RUN=1`) nothing is executed. Each job shows the ssh command it would run and
lasts `LLM_CONSOLE_DRY_RUN_DELAY` seconds (default 3), and every engine counts as off.

After changing the dependencies in `pyproject.toml`, regenerate the image's requirements:

```bash
uv export --no-dev --no-hashes --no-header --no-emit-project -o requirements.txt
```

To try a change on the server before committing it, `./deploy.sh [host]` (default `raspy`) copies the
working tree to `host:~/llm-console` with rsync and runs `container/install.sh` there. The version then
ends in `-dirty` when there are uncommitted changes. `container/build.sh` alone builds the image with
`container` on macOS, or with podman or docker.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/config` | Tabs and sections, without commands. |
| POST | `/api/sections/{tab}/{section}/run` | Body `{"variant": "q4"}`, or with options `{"variant": "gemma-q8", "options": {"context": "128k"}}` (an option left out takes its default) → 202 `{job_id}`. 400 for an unknown variant, option or choice. 409 if a job is running, or if a section in `exclusive_with` is on (`conflicts`); 502 if that cannot be checked. |
| GET | `/api/sections/{tab}/{section}/conflicts` | `{conflicts: [{id, label, reason: on/busy}]}`: what would refuse a Run now. |
| POST | `/api/sections/{tab}/{section}/stop` | → 202 `{job_id}`; 409 if a job is running. |
| GET | `/api/jobs/{id}` | State, exit code, last 200 lines of output. |
| GET | `/api/sections/{tab}/{section}/job` | The section's last job. |
| GET | `/healthz` | `ok` |

POSTs need the header `X-LLM-Console: 1` (CSRF protection), for example:

```bash
curl -X POST -H 'X-LLM-Console: 1' -H 'Content-Type: application/json' -d '{"variant":"q4"}' http://127.0.0.1/api/sections/linux/strata/run
```

## Logs and troubleshooting

```bash
journalctl --user-unit llm-console -f
```
```bash
journalctl --user-unit llm-console-lan -f
```
```bash
systemctl --user status llm-console llm-console-lan
```
```bash
podman exec -it llm-console sh
```

The first line shows the web app log: every job, the commands it runs and their exit codes. The second
shows the LAN forwarder log, the third the state of both services, and the last opens a shell in the
container.

## Uninstall

```bash
systemctl --user stop llm-console
```
```bash
rm ~/.config/containers/systemd/llm-console.container ~/.config/systemd/user/llm-console-lan.service
```
```bash
systemctl --user daemon-reload && podman rmi localhost/llm-console:latest
```

If nothing else needs the lower port limit, also remove `/etc/sysctl.d/50-llm-console.conf`; the
default (1024) comes back at the next boot.
