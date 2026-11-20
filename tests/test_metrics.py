"""The live lines: each reader against the answers of its engine (httpx.MockTransport)."""

import json

import httpx
import pytest

from app.config import load_config
from app.main import create_app
from app.metrics import (LlamaServerReader, MetricsPoller, MlxLmReader, StrataReader,
                         TurboFieldfareReader, parse_prometheus)
from conftest import REPO_CONFIG

pytestmark = pytest.mark.anyio

PC = "http://192.168.1.200:8000"
MAC = "http://192.168.1.101:8080"


class Engines:
    """What each URL answers now: url -> (status, body, content type) or an exception.

    A URL not in the table is refused, as by a host with nothing on that port.
    """

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []

    def set(self, url: str, body, status: int = 200) -> None:
        if isinstance(body, (dict, list)):
            self.routes[url] = (status, json.dumps(body), "application/json")
        else:
            self.routes[url] = (status, body, "text/plain; version=0.0.4")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if isinstance(route, Exception):
            raise route
        status, body, content_type = route
        return httpx.Response(status, text=body, headers={"Content-Type": content_type})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def engines():
    return Engines()


def llama_metrics(drafted=None, accepted=None, processing=0):
    lines = ["# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.",
             "# TYPE llamacpp:prompt_tokens_total counter",
             "llamacpp:prompt_tokens_total 1000",
             f"llamacpp:requests_processing {processing}"]
    if drafted is not None:
        lines += [f"llamacpp:spec_decode_num_draft_tokens_total {drafted}",
                  f"llamacpp:spec_decode_num_accepted_tokens_total {accepted}",
                  'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 90']
    return "\n".join(lines) + "\n"


def slot(task=7, processed=0, decoded=0, prompt=5000, cache=0, processing=True, slot_id=0):
    return {"id": slot_id, "n_ctx": 262144, "speculative": True, "is_processing": processing,
            "id_task": task, "n_prompt_tokens": prompt, "n_prompt_tokens_processed": processed,
            "n_prompt_tokens_cache": cache,
            "next_token": [{"has_next_token": True, "n_remain": -1, "n_decoded": decoded}]}


def test_parse_prometheus():
    samples = parse_prometheus(llama_metrics(200, 150))
    assert samples["llamacpp:spec_decode_num_draft_tokens_total"] == 200
    assert samples['llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"}'] == 90
    assert not any(k.startswith("#") for k in samples)


async def test_llama_server_phases_and_speeds(engines):
    reader = LlamaServerReader(PC)
    engines.set(f"{PC}/metrics", llama_metrics(200, 150, processing=1))
    async with engines.client() as client:
        # Prefill: the first poll has no speed yet, the next ones the average since
        # the first (batches land at their own pace). No total: n_prompt_tokens is
        # only what the KV holds so far.
        engines.set(f"{PC}/slots", [slot(processed=1000, cache=500), slot(slot_id=1, processing=False)])
        live = await reader.read(client, now=100.0)
        assert (live.state, live.prefill_tok_s) == ("prefill", None)
        assert (live.prompt_done, live.prompt_total) == (1500, None)
        # On first sight, the acceptance since the engine started: 150 / 200.
        assert live.acceptance == pytest.approx(0.75)
        engines.set(f"{PC}/slots", [slot(processed=4000, cache=500)])
        live = await reader.read(client, now=103.0)
        assert live.prefill_tok_s == pytest.approx(1000.0)
        assert live.prompt_done == 4500
        engines.set(f"{PC}/slots", [slot(processed=4000, cache=500)])       # no batch landed
        live = await reader.read(client, now=105.0)
        assert live.prefill_tok_s == pytest.approx(600.0)
        # Generating: no speed on the poll where it switched, then tokens / s.
        engines.set(f"{PC}/slots", [slot(processed=4500, cache=500, decoded=20)])
        live = await reader.read(client, now=106.0)
        assert (live.state, live.gen_tok_s, live.generated) == ("generating", None, 20)
        engines.set(f"{PC}/slots", [slot(processed=4500, cache=500, decoded=170)])
        live = await reader.read(client, now=109.0)
        assert live.gen_tok_s == pytest.approx(50.0)
        # A new request in the same slot: no speed from the old one's counts.
        engines.set(f"{PC}/slots", [slot(task=8, processed=4500, decoded=200)])
        live = await reader.read(client, now=112.0)
        assert live.gen_tok_s is None
        # The request finished: its drafts moved the counters, 80 of 100 accepted.
        engines.set(f"{PC}/metrics", llama_metrics(300, 230))
        engines.set(f"{PC}/slots", [slot(task=8, decoded=300, processing=False)])
        live = await reader.read(client, now=115.0)
        assert live.state == "idle"
        assert live.acceptance == pytest.approx(0.8)
        # It stays until another request finishes.
        live = await reader.read(client, now=118.0)
        assert live.acceptance == pytest.approx(0.8)


async def test_llama_server_without_mtp_has_no_acceptance(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    engines.set(f"{PC}/slots", [slot(processing=False)])
    async with engines.client() as client:
        live = await LlamaServerReader(PC).read(client, now=1.0)
    assert (live.state, live.acceptance) == ("idle", None)


async def test_llama_server_without_slots_says_busy(engines):
    engines.set(f"{PC}/metrics", llama_metrics(processing=1))
    engines.set(f"{PC}/slots", {"error": {"message": "This server does not support slots endpoint."}},
                status=501)
    async with engines.client() as client:
        live = await LlamaServerReader(PC).read(client, now=1.0)
    assert live.state == "busy"


@pytest.mark.parametrize(("status", "body", "state"), [
    (503, '{"error": {"message": "Loading model"}}', "loading"),
    (200, {"engine": {}, "live": {"state": "idle"}}, "off"),       # Strata on the same port
    (404, "Not Found", "off"),
])
async def test_llama_server_loading_or_another_engine(engines, status, body, state):
    engines.set(f"{PC}/metrics", body, status=status)
    async with engines.client() as client:
        live = await LlamaServerReader(PC).read(client, now=1.0)
    assert live.state == state


def strata_metrics(live, offered=0, accepted=0):
    return {"engine": {"model": "Qwen3.8-Flash-Next", "max_context": 262144}, "live": live,
            "requests": [], "totals": {"requests": 3, "drafts_offered": offered,
                                       "drafts_accepted": accepted},
            "hardware": {}, "history": {}, "time": 0}


async def test_strata(engines):
    reader = StrataReader(PC)
    async with engines.client() as client:
        engines.set(f"{PC}/metrics", strata_metrics(
            {"state": "reading", "prompt_read": 8000, "prompt_total": 20000, "prefill_tok_s_mean": 950.5},
            offered=400, accepted=300))
        live = await reader.read(client, now=1.0)
        assert (live.state, live.prefill_tok_s, live.prompt_done, live.prompt_total) == \
            ("prefill", 950.5, 8000, 20000)
        assert live.acceptance == pytest.approx(0.75)
        engines.set(f"{PC}/metrics", strata_metrics(
            {"state": "generating", "generated": 120, "tok_s": 61.2}, offered=400, accepted=300))
        live = await reader.read(client, now=4.0)
        assert (live.state, live.gen_tok_s, live.generated) == ("generating", 61.2, 120)
        engines.set(f"{PC}/metrics", strata_metrics({"state": "idle"}, offered=500, accepted=390))
        live = await reader.read(client, now=7.0)
        assert (live.state, live.acceptance) == ("idle", pytest.approx(0.9))
    # Strata asks for JSON: with Accept: text/plain it would answer in Prometheus.
    assert all(r.headers["accept"] == "application/json" for r in engines.requests)


async def test_strata_prefill_speed_from_progress(engines):
    reader = StrataReader(PC)
    async with engines.client() as client:
        for now, read in ((1.0, 1000), (4.0, 4000)):
            engines.set(f"{PC}/metrics", strata_metrics(
                {"state": "reading", "prompt_read": read, "prompt_total": 9000}))
            live = await reader.read(client, now=now)
    assert live.prefill_tok_s == pytest.approx(1000.0)


async def test_strata_reader_ignores_llama_server(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    async with engines.client() as client:
        assert (await StrataReader(PC).read(client, now=1.0)).state == "off"


def turbo_metrics(phase=None, started=0, **gauges):
    lines = ["# TYPE turbofieldfare_requests_running gauge",
             f"turbofieldfare_requests_running {1 if phase in ('prefill', 'decode') else 0}"]
    if phase is not None:
        lines.append(f"turbofieldfare_generations_started_total {started}")
        lines += [f'turbofieldfare_running_phase{{phase="{p}"}} {1 if p == phase else 0}'
                  for p in ("idle", "prefill", "decode")]
    lines += [f"turbofieldfare_running_{name} {value}" for name, value in gauges.items()]
    return "\n".join(lines) + "\n"


async def test_turbofieldfare(engines):
    reader = TurboFieldfareReader(MAC)
    async with engines.client() as client:
        engines.set(f"{MAC}/metrics", turbo_metrics("prefill", 3, prompt_tokens=23344,
                                                    prompt_done_tokens=1280,
                                                    prefill_tokens_per_second=208.5))
        live = await reader.read(client, now=1.0)
        assert (live.state, live.prefill_tok_s, live.prompt_done, live.prompt_total) == \
            ("prefill", 208.5, 1280, 23344)
        # First sight of the decode: the server's average so far; then the app's window.
        engines.set(f"{MAC}/metrics", turbo_metrics("decode", 3, generated_tokens=41,
                                                    decode_tokens_per_second=32.8))
        live = await reader.read(client, now=4.0)
        assert (live.state, live.gen_tok_s, live.generated) == ("generating", 32.8, 41)
        engines.set(f"{MAC}/metrics", turbo_metrics("decode", 3, generated_tokens=146,
                                                    decode_tokens_per_second=34.0))
        live = await reader.read(client, now=7.0)
        assert live.gen_tok_s == pytest.approx(35.0)
        # Another request (started 4): its own average, not a difference with the old one.
        engines.set(f"{MAC}/metrics", turbo_metrics("decode", 4, generated_tokens=10,
                                                    decode_tokens_per_second=30.0))
        live = await reader.read(client, now=10.0)
        assert live.gen_tok_s == 30.0
        engines.set(f"{MAC}/metrics", turbo_metrics("idle", 4))
        assert (await reader.read(client, now=13.0)).state == "idle"
        assert (await reader.read(client, now=13.0)).acceptance is None


@pytest.mark.parametrize(("running", "state"), [(1, "busy"), (0, "idle")])
async def test_turbofieldfare_older_build_is_busy_or_idle(engines, running, state):
    engines.set(f"{MAC}/metrics", f"turbofieldfare_requests_running {running}\n")
    async with engines.client() as client:
        assert (await TurboFieldfareReader(MAC).read(client, now=1.0)).state == state


async def test_mlx_lm_serving_or_not(engines):
    reader = MlxLmReader(MAC)
    async with engines.client() as client:
        with pytest.raises(httpx.ConnectError):     # nothing on the port: the poller says "off"
            await reader.read(client, now=1.0)
        engines.set(f"{MAC}/metrics", "Not Found", status=404)
        engines.set(f"{MAC}/health", {"status": "ok"})
        assert (await reader.read(client, now=2.0)).state == "serving"
        engines.set(f"{MAC}/metrics", turbo_metrics("idle", 1))           # TurboFieldfare is on
        assert (await reader.read(client, now=3.0)).state == "off"
        # And the TurboFieldfare reader does not take mlx_lm.server's 404 for itself.
        engines.set(f"{MAC}/metrics", "Not Found", status=404)
        assert (await TurboFieldfareReader(MAC).read(client, now=4.0)).state == "off"


async def test_poller_reads_every_section_of_the_config(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    engines.set(f"{PC}/slots", [slot(processing=False)])
    engines.set(f"{MAC}/metrics", "Not Found", status=404)
    engines.set(f"{MAC}/health", {"status": "ok"})
    clock = iter([10.0, 13.0])
    poller = MetricsPoller(load_config(REPO_CONFIG), engines.client(), clock=lambda: next(clock),
                           wallclock=lambda: 1000.0)
    assert sorted(poller.readers) == ["linux/llama-server", "linux/strata", "mac/mlx-lm",
                                      "mac/turbofieldfare"]
    await poller.poll_once()
    sections = poller.view()["sections"]
    assert sections["linux/llama-server"]["state"] == "idle"
    assert sections["linux/strata"]["state"] == "off"            # llama-server holds port 8000
    assert sections["mac/mlx-lm"]["state"] == "serving"
    assert sections["mac/turbofieldfare"]["state"] == "off"
    assert sections["linux/llama-server"]["age_s"] == 0.0
    assert poller.view()["interval"] == 3.0
    await poller.client.aclose()


async def test_poller_survives_bad_answers_and_timeouts(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    engines.set(f"{PC}/slots", "not json")
    engines.routes[f"{MAC}/metrics"] = httpx.ReadTimeout("timed out")
    poller = MetricsPoller(load_config(REPO_CONFIG), engines.client())
    await poller.poll_once()
    sections = poller.view()["sections"]
    assert sections["linux/llama-server"]["state"] == "unknown"
    turbo = sections["mac/turbofieldfare"]
    assert (turbo["state"], turbo["note"]) == ("off", "no answer")
    assert sections["linux/strata"]["note"] == "no Strata metrics (HTTP 200)"


async def test_poller_says_off_when_nothing_listens(engines):
    poller = MetricsPoller(load_config(REPO_CONFIG), engines.client())
    await poller.poll_once()
    for live in poller.view()["sections"].values():
        assert (live["state"], live["note"]) == ("off", "connection refused")
    await poller.client.aclose()


async def test_poller_after_a_bad_answer(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    engines.set(f"{PC}/slots", "not json")
    poller = MetricsPoller(load_config(REPO_CONFIG), engines.client())
    await poller.poll_once()
    assert poller.view()["sections"]["linux/llama-server"]["state"] == "unknown"
    engines.set(f"{PC}/slots", [slot(processing=False)])
    await poller.poll_once()
    assert poller.view()["sections"]["linux/llama-server"]["state"] == "idle"
    await poller.client.aclose()


async def test_api_metrics(engines, fake_runner):
    engines.set(f"{PC}/metrics", llama_metrics(10, 7, processing=1))
    engines.set(f"{PC}/slots", [slot(processed=100)])
    app = create_app(REPO_CONFIG, runner=fake_runner, metrics_client=engines.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/metrics")).json() == {"interval": 3.0, "sections": {}}
        await app.state.metrics.poll_once()
        data = (await c.get("/api/metrics")).json()
        config = (await c.get("/api/config")).json()
    llama = data["sections"]["linux/llama-server"]
    assert (llama["state"], llama["prompt_done"], llama["prompt_total"]) == ("prefill", 100, None)
    assert llama["acceptance"] == pytest.approx(0.7)
    assert data["sections"]["mac/mlx-lm"]["state"] == "off"
    assert all(s["has_metrics"] for t in config["tabs"] for s in t["sections"])
    # The engines' addresses stay on the server, as the commands do.
    assert "192.168.1.200:8000" not in json.dumps(config)
