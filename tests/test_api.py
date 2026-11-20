import asyncio

import httpx
import pytest

from app.main import CSRF_HEADER, create_app
from conftest import REPO_CONFIG, wait_done

pytestmark = pytest.mark.anyio

STRATA = "/api/sections/linux/strata"
LLAMA = "/api/sections/linux/llama-server"
SCRIPTS = {
    "iq4": "~/Strata/run-unsloth-ud-iq4_xs-v31.sh",
    "q4": "~/Strata/run-unsloth-ud-q4_k_xl.sh",
    "q5": "~/Strata/run-unsloth-ud-q5_k_xl.sh",
    "q6": "~/Strata/run-unsloth-ud-q6_k_xl.sh",
    "q8": "~/Strata/run-unsloth-ud-q8_k_xl.sh",
}


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


async def test_index_is_served(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "llm-console" in r.text


@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css", "/api/config"])
async def test_browsers_revalidate(client, path):
    r = await client.get(path)
    assert r.headers["cache-control"] == "no-cache"


async def test_config_view(client):
    r = await client.get("/api/config")
    data = r.json()
    assert data["dry_run"] is False
    assert [t["id"] for t in data["tabs"]] == ["linux", "mac"]
    linux, mac = data["tabs"]
    assert linux["label"] == "Linux"
    assert linux["host"]["address"] == "192.168.1.200"
    llama, strata = linux["sections"]
    assert [v["id"] for v in llama["variants"]] == ["qwen-q8", "qwen-q4", "gemma-q8", "gemma-q4"]
    assert llama["variants"][0] == {"id": "qwen-q8", "label": "Qwen3.8-27B · 8 bit (UD-Q8_K_XL)",
                                    "model": "Qwen3.8-27B UD-Q8_K_XL"}
    assert llama["default_variant"] == "qwen-q8"
    assert llama["description"] == "Models: Qwen3.8-27B, Gemma-4-31B-it"
    assert llama["variants_legend"] == "Model"
    assert llama["can_run"] and llama["can_stop"]
    assert [v["id"] for v in strata["variants"]] == ["iq4", "q4", "q5", "q6", "q8"]
    assert strata["variants"][1] == {"id": "q4", "label": "q4 · UD-Q4_K_XL",
                                     "model": "Qwen3.8-Flash-Next UD-Q4_K_XL"}
    assert strata["description"] == "Model: Qwen3.8-Flash-Next"
    assert strata["variants_legend"] == "Quantization"
    assert strata["default_variant"] is None
    assert strata["can_run"] and strata["can_stop"]
    assert mac["label"] == "Mac"
    assert mac["sections"] == []
    # Commands never leave the server.
    assert ".sh" not in r.text and "~/" not in r.text and "killall" not in r.text
    assert "pgrep" not in r.text and "jq" not in r.text
    assert llama["exclusive_with"] == ["strata"] and strata["exclusive_with"] == ["llama-server"]


@pytest.mark.parametrize("variant", list(SCRIPTS))
async def test_run_stops_then_runs_the_variant_script(client, fake_runner, variant):
    r = await client.post(f"{STRATA}/run", json={"variant": variant})
    assert r.status_code == 202
    job = await wait_done(client, r.json()["job_id"])

    assert job["state"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["action"] == "run" and job["variant"] == variant
    assert job["host"] == "alvise@192.168.1.200"
    commands = fake_runner.remote_commands
    assert len(commands) == 3
    assert commands[0].endswith("; ~/Strata/stop.sh")
    assert commands[1].endswith(f"; {SCRIPTS[variant]}")
    assert "curl -fs -o /dev/null --max-time 5 http://192.168.1.200:8000/v1/models" in commands[2]
    assert "pgrep" not in commands[2]  # no process to watch: strata launch waits by itself
    assert all(argv[-2] == "alvise@192.168.1.200" for argv, _ in fake_runner.calls)
    assert [timeout for _, timeout in fake_runner.calls] == [120, 900, 120]
    assert f"$ {SCRIPTS[variant]}" in job["output_tail"]
    assert "[waiting for http://192.168.1.200:8000/v1/models to answer]" in job["output_tail"]


async def test_stop_runs_stop_script(client, fake_runner):
    r = await client.post(f"{STRATA}/stop")
    assert r.status_code == 202
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "succeeded"
    assert job["action"] == "stop" and job["variant"] is None
    assert len(fake_runner.calls) == 1
    assert fake_runner.remote_commands[0].endswith("; ~/Strata/stop.sh")


async def test_second_request_while_running_is_409(client, fake_runner):
    fake_runner.gate = asyncio.Event()
    first = await client.post(f"{STRATA}/run", json={"variant": "q4"})
    assert first.status_code == 202
    job_id = first.json()["job_id"]

    again = await client.post(f"{STRATA}/run", json={"variant": "q8"})
    assert again.status_code == 409
    assert again.json()["detail"]["job"]["id"] == job_id
    assert (await client.post(f"{STRATA}/stop")).status_code == 409

    fake_runner.gate.set()
    assert (await wait_done(client, job_id))["state"] == "succeeded"
    assert len(fake_runner.calls) == 3  # stop, q4, ready: the refused requests ran nothing
    assert (await client.post(f"{STRATA}/stop")).status_code == 202


async def test_unknown_variant_is_400(client, fake_runner):
    r = await client.post(f"{STRATA}/run", json={"variant": "q3"})
    assert r.status_code == 400
    assert fake_runner.calls == []


async def test_command_in_body_is_not_accepted(client, fake_runner):
    r = await client.post(f"{STRATA}/run", json={"variant": "~/Strata/stop.sh; rm -rf ~"})
    assert r.status_code == 400
    assert fake_runner.calls == []


@pytest.mark.parametrize("path", ["/api/sections/linux/nope/run", "/api/sections/nope/strata/run",
                                  "/api/sections/mac/strata/run"])
async def test_unknown_section_is_404(client, fake_runner, path):
    r = await client.post(path, json={"variant": "q4"})
    assert r.status_code == 404
    assert fake_runner.calls == []


async def test_unconfigured_section_is_400(tmp_path, fake_runner):
    path = tmp_path / "c.yaml"
    path.write_text("hosts: {pc: {address: 10.0.0.1, user: u}}\n"
                    "tabs: [{id: linux, label: L, host: pc, sections: [{id: s, label: S}]}]\n")
    app = create_app(path, runner=fake_runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={CSRF_HEADER: "1"}) as client:
        assert (await client.post("/api/sections/linux/s/run", json={"variant": "q4"})).status_code == 400
        assert (await client.post("/api/sections/linux/s/stop")).status_code == 400
    assert fake_runner.calls == []


async def test_post_without_csrf_header_is_403(client, fake_runner):
    headers = {CSRF_HEADER: ""}
    assert (await client.post(f"{STRATA}/run", json={"variant": "q4"}, headers=headers)).status_code == 403
    assert (await client.post(f"{STRATA}/stop", headers=headers)).status_code == 403
    assert fake_runner.calls == []


async def test_failed_run(client, fake_runner):
    fake_runner.exit_codes["q8_k_xl"] = 1
    r = await client.post(f"{STRATA}/run", json={"variant": "q8"})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed"
    assert job["exit_code"] == 1


async def test_stop_failure_before_run_is_ignored(client, fake_runner):
    fake_runner.exit_codes["stop.sh"] = 1
    r = await client.post(f"{STRATA}/run", json={"variant": "q4"})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "succeeded"
    assert len(fake_runner.calls) == 3
    assert "[exit 1, ignored]" in job["output_tail"]


async def test_stop_failure_fails_stop_job(client, fake_runner):
    fake_runner.exit_codes["stop.sh"] = 2
    r = await client.post(f"{STRATA}/stop")
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed"
    assert job["exit_code"] == 2


async def test_timeout(client, fake_runner):
    fake_runner.exit_codes["q5_k_xl"] = None
    r = await client.post(f"{STRATA}/run", json={"variant": "q5"})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "timeout"
    assert job["exit_code"] is None
    assert "timed out after 900 s" in job["output_tail"]


async def test_runner_error_fails_job(client, fake_runner):
    fake_runner.raises["stop.sh"] = FileNotFoundError("ssh")
    r = await client.post(f"{STRATA}/stop")
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed"
    assert "[error:" in job["output_tail"]
    # The section is free again.
    assert (await client.post(f"{STRATA}/stop")).status_code == 202


async def test_last_job_of_section(client):
    assert (await client.get(f"{STRATA}/job")).json() == {"job": None}
    r = await client.post(f"{STRATA}/run", json={"variant": "iq4"})
    job_id = r.json()["job_id"]
    await wait_done(client, job_id)
    last = (await client.get(f"{STRATA}/job")).json()["job"]
    assert last["id"] == job_id
    assert (await client.get("/api/sections/linux/nope/job")).status_code == 404


async def test_unknown_job_is_404(client):
    assert (await client.get("/api/jobs/doesnotexist")).status_code == 404


@pytest.mark.parametrize(("variant", "script"), [
    ("qwen-q8", "./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200"),
    ("qwen-q4", "./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200 -4bit"),
    ("gemma-q8", "./run-Gemma-4-31b.sh -port 8000 -host 192.168.1.200"),
    ("gemma-q4", "./run-Gemma-4-31b.sh -port 8000 -host 192.168.1.200 -qat4bit"),
])
async def test_llama_run_checks_port_runs_then_waits_ready(client, fake_runner, variant, script):
    r = await client.post(f"{LLAMA}/run", json={"variant": variant})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "succeeded"
    stop, port_free, run, ready = fake_runner.remote_commands
    assert stop.endswith("; killall -q -w llama-server || true")
    assert "ss -ltnpH 'sport = :8000'" in port_free
    assert run.endswith(f"; cd ~/Llama && {script}")
    assert "pgrep -x llama-server" in ready and "http://192.168.1.200:8000/v1/models" in ready
    assert [timeout for _, timeout in fake_runner.calls] == [60, 30, 60, 600]
    out = job["output_tail"]
    assert "[checking that port 8000 is free]" in out
    assert "[waiting for http://192.168.1.200:8000/v1/models to answer]" in out
    assert "$ while" not in out and "$ busy=" not in out  # generated scripts: shown by label


async def test_llama_busy_port_stops_the_run(client, fake_runner):
    fake_runner.exit_codes["ss -ltnpH 'sport = :8000'); if"] = 1
    r = await client.post(f"{LLAMA}/run", json={"variant": "qwen-q8"})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed"
    assert len(fake_runner.calls) == 2  # stop + port check: run-qwen3.8-27b.sh never ran


async def test_llama_dying_server_fails_the_run(client, fake_runner):
    fake_runner.exit_codes["pgrep -x llama-server"] = 1
    r = await client.post(f"{LLAMA}/run", json={"variant": "qwen-q4"})
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed" and job["exit_code"] == 1
    assert len(fake_runner.calls) == 4


STRATA_ON = "strata launch --status"  # the `running` command of Strata
LLAMA_ON = "pgrep -x llama-server"


@pytest.mark.parametrize(("path", "variant", "on", "other"), [
    (LLAMA, "qwen-q8", STRATA_ON, {"id": "strata", "label": "Strata", "reason": "on"}),
    (STRATA, "q4", LLAMA_ON, {"id": "llama-server", "label": "llama-server", "reason": "on"}),
])
async def test_run_refused_while_the_other_engine_is_on(client, fake_runner, path, variant, on, other):
    fake_runner.probe_codes[on] = 0
    r = await client.post(f"{path}/run", json={"variant": variant})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["conflicts"] == [other]
    assert detail["message"].startswith(f"Stop {other['label']} before starting ")
    assert fake_runner.calls == []  # nothing ran: not even the stop before the run
    assert len(fake_runner.probes) == 1 and on in fake_runner.probes[0]
    assert (await client.get(f"{path}/job")).json() == {"job": None}

    conflicts = (await client.get(f"{path}/conflicts")).json()
    assert conflicts == {"conflicts": [other]}


async def test_run_checks_the_other_engine_first(client, fake_runner):
    r = await client.post(f"{LLAMA}/run", json={"variant": "qwen-q8"})
    assert r.status_code == 202
    assert len(fake_runner.probes) == 1
    assert fake_runner.probes[0].endswith("; out=$(~/.local/bin/strata launch --status 2>/dev/null) || exit 2; "
                                          "printf '%s\\n' \"$out\" | grep -qE ': (starting|ready) at '")
    assert (await client.get(f"{LLAMA}/conflicts")).json() == {"conflicts": []}


async def test_run_refused_while_the_other_section_has_a_job(client, fake_runner):
    fake_runner.gate = asyncio.Event()
    first = await client.post(f"{STRATA}/run", json={"variant": "q8"})
    assert first.status_code == 202
    probes = len(fake_runner.probes)

    r = await client.post(f"{LLAMA}/run", json={"variant": "qwen-q8"})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"] == [{"id": "strata", "label": "Strata", "reason": "busy"}]
    assert len(fake_runner.probes) == probes  # a running job is enough: no ssh check
    # Stop is never refused.
    assert (await client.post(f"{LLAMA}/stop")).status_code == 202
    fake_runner.gate.set()
    await wait_done(client, first.json()["job_id"])


async def test_concurrent_runs_of_exclusive_sections(client, fake_runner):
    fake_runner.gate = asyncio.Event()
    a, b = await asyncio.gather(client.post(f"{LLAMA}/run", json={"variant": "qwen-q8"}),
                                client.post(f"{STRATA}/run", json={"variant": "q8"}))
    assert sorted([a.status_code, b.status_code]) == [202, 409]
    fake_runner.gate.set()


@pytest.mark.parametrize("code", [255, 5, None])
async def test_failed_check_refuses_the_run(client, fake_runner, code):
    fake_runner.probe_codes[STRATA_ON] = code
    r = await client.post(f"{LLAMA}/run", json={"variant": "qwen-q8"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["message"].startswith("Cannot tell whether Strata is on")
    assert ("timeout" if code is None else f"exit {code}") in detail["message"]
    assert fake_runner.calls == []
    assert (await client.get(f"{LLAMA}/conflicts")).status_code == 502


async def test_version(tmp_path, monkeypatch, fake_runner):
    import app.main as main

    async def version():
        app = create_app(REPO_CONFIG, runner=fake_runner)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return (await c.get("/api/config")).json()["version"]

    monkeypatch.delenv("LLM_CONSOLE_VERSION", raising=False)
    monkeypatch.setattr(main, "VERSION_FILE", tmp_path / "VERSION")
    assert await version() == "dev"
    (tmp_path / "VERSION").write_text("ce7e711\n")
    assert await version() == "ce7e711"
    monkeypatch.setenv("LLM_CONSOLE_VERSION", "from-env")
    assert await version() == "from-env"
