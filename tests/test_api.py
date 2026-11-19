import asyncio

import pytest

from app.main import CSRF_HEADER
from conftest import wait_done

pytestmark = pytest.mark.anyio

STRATA = "/api/sections/linux/strata"
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


async def test_config_view(client):
    r = await client.get("/api/config")
    data = r.json()
    assert data["dry_run"] is False
    assert [t["id"] for t in data["tabs"]] == ["linux", "mac"]
    linux, mac = data["tabs"]
    assert linux["label"] == "Linux"
    assert linux["host"]["address"] == "192.168.1.200"
    llama, strata = linux["sections"]
    assert llama == {"id": "llama-server", "label": "llama-server", "variants": [],
                     "can_run": False, "can_stop": False}
    assert strata["variants"] == ["iq4", "q4", "q5", "q6", "q8"]
    assert strata["can_run"] and strata["can_stop"]
    assert mac["label"] == "Mac"
    assert mac["sections"] == []
    # Commands never leave the server.
    assert ".sh" not in r.text and "~/" not in r.text


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
    assert len(commands) == 2
    assert commands[0].endswith("; ~/Strata/stop.sh")
    assert commands[1].endswith(f"; {SCRIPTS[variant]}")
    assert all(argv[-2] == "alvise@192.168.1.200" for argv, _ in fake_runner.calls)
    assert [timeout for _, timeout in fake_runner.calls] == [120, 900]
    assert f"$ {SCRIPTS[variant]}" in job["output_tail"]


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
    assert len(fake_runner.calls) == 2  # stop + q4 only: the refused requests ran nothing
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


async def test_unconfigured_section_is_400(client, fake_runner):
    assert (await client.post("/api/sections/linux/llama-server/run", json={"variant": "q4"})).status_code == 400
    assert (await client.post("/api/sections/linux/llama-server/stop")).status_code == 400
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
    assert len(fake_runner.calls) == 2
    assert "[exit 1, ignorato]" in job["output_tail"]


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
    assert "timeout dopo 900 s" in job["output_tail"]


async def test_runner_error_fails_job(client, fake_runner):
    fake_runner.raises["stop.sh"] = FileNotFoundError("ssh")
    r = await client.post(f"{STRATA}/stop")
    job = await wait_done(client, r.json()["job_id"])
    assert job["state"] == "failed"
    assert "[errore:" in job["output_tail"]
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
