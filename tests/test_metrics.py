"""The live lines: each reader against the answers of its engine (httpx.MockTransport)."""

import json

import httpx
import pytest

from app.config import load_config
from app.main import create_app
from app.metrics import (LastRequest, Live, LlamaServerReader, MetricsPoller, MlxVlmReader, StrataReader,
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


def llama_metrics(drafted=None, accepted=None, processing=0, prompt=(1000, 1.0), predicted=(500, 20.0)):
    """prompt and predicted: (tokens, seconds) since the engine started."""
    lines = ["# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.",
             "# TYPE llamacpp:prompt_tokens_total counter",
             f"llamacpp:prompt_tokens_total {prompt[0]}",
             f"llamacpp:prompt_seconds_total {prompt[1]}",
             f"llamacpp:tokens_predicted_total {predicted[0]}",
             f"llamacpp:tokens_predicted_seconds_total {predicted[1]}",
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
        # On first sight the counters hold all the requests since the engine started,
        # not the last one's: only the context of the idle slot is known.
        assert live.last == LastRequest(context=5000, max_context=262144)
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
        # The request finished: the counters moved by its prefill (4,500 tokens in
        # 4 s), its generation (300 in 10 s) and its drafts (80 of 100 accepted). The
        # slot keeps its prompt and generated tokens in the KV: the context it filled.
        engines.set(f"{PC}/metrics", llama_metrics(300, 230, prompt=(5500, 5.0), predicted=(800, 30.0)))
        engines.set(f"{PC}/slots", [slot(task=8, decoded=300, prompt=5299, processing=False),
                                    slot(task=6, prompt=900, processing=False, slot_id=1)])
        expected = LastRequest(prefill_tok_s=pytest.approx(1125.0), gen_tok_s=pytest.approx(30.0),
                               context=5299, max_context=262144, acceptance=pytest.approx(0.8))
        live = await reader.read(client, now=115.0)
        assert (live.state, live.last) == ("idle", expected)
        # It stays until another request finishes.
        live = await reader.read(client, now=118.0)
        assert live.last == expected
        # The next one, all from the cache (no prefill time) and with no drafts.
        engines.set(f"{PC}/metrics", llama_metrics(300, 230, prompt=(5500, 5.0), predicted=(840, 31.0)))
        engines.set(f"{PC}/slots", [slot(task=9, prompt=5340, processing=False)])
        live = await reader.read(client, now=121.0)
        assert live.last == LastRequest(gen_tok_s=pytest.approx(40.0), context=5340, max_context=262144)


async def test_llama_server_restart_forgets_the_last_request(engines):
    reader = LlamaServerReader(PC)
    engines.set(f"{PC}/slots", [slot(processing=False, prompt=0)])
    async with engines.client() as client:
        engines.set(f"{PC}/metrics", llama_metrics(predicted=(500, 20.0)))
        await reader.read(client, now=1.0)
        engines.set(f"{PC}/metrics", llama_metrics(predicted=(600, 22.0)))
        assert (await reader.read(client, now=4.0)).last.gen_tok_s == pytest.approx(50.0)
        # Restarted between two polls: smaller counters, and a slot that ran nothing.
        engines.set(f"{PC}/metrics", llama_metrics(predicted=(40, 1.0)))
        assert (await reader.read(client, now=7.0)).last is None


async def test_llama_server_without_mtp_has_no_acceptance(engines):
    reader = LlamaServerReader(PC)
    engines.set(f"{PC}/slots", [slot(processing=False)])
    async with engines.client() as client:
        engines.set(f"{PC}/metrics", llama_metrics())
        await reader.read(client, now=1.0)
        engines.set(f"{PC}/metrics", llama_metrics(prompt=(3000, 2.0), predicted=(600, 25.0)))
        live = await reader.read(client, now=4.0)
    assert live.last == LastRequest(prefill_tok_s=pytest.approx(2000.0), gen_tok_s=pytest.approx(20.0),
                                    context=5000, max_context=262144)


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


def strata_metrics(live, *requests):
    """requests: the finished ones, newest first (as /metrics lists them)."""
    return {"engine": {"model": "Qwen3.8-Flash-Next", "max_context": 262144}, "live": live,
            "requests": list(requests), "totals": {"requests": len(requests)},
            "hardware": {}, "history": {}, "time": 0}


def strata_request(prompt=20000, reused=12000, read=8000, prompt_ms=8000.0, output=500,
                   decode_tok_s=61.0, offered=400, accepted=300, finish="stop"):
    return {"id": "r1", "finish": finish, "prompt_tokens": prompt, "prompt_total": prompt,
            "reused": reused, "prompt_read": read, "output_tokens": output, "engine_generated": output,
            "prompt_ms": prompt_ms, "decode_ms": 8200.0, "decode_tok_s": decode_tok_s,
            "drafts_offered": offered, "drafts_accepted": accepted}


async def test_strata(engines):
    reader = StrataReader(PC)
    older = strata_request(offered=100, accepted=50)
    async with engines.client() as client:
        engines.set(f"{PC}/metrics", strata_metrics(
            {"state": "reading", "prompt_read": 8000, "prompt_total": 20000, "prefill_tok_s_mean": 950.5},
            older))
        live = await reader.read(client, now=1.0)
        assert (live.state, live.prefill_tok_s, live.prompt_done, live.prompt_total) == \
            ("prefill", 950.5, 8000, 20000)
        assert live.last.acceptance == pytest.approx(0.5)            # the request before
        engines.set(f"{PC}/metrics", strata_metrics(
            {"state": "generating", "generated": 120, "tok_s": 61.2}, older))
        live = await reader.read(client, now=4.0)
        assert (live.state, live.gen_tok_s, live.generated) == ("generating", 61.2, 120)
        # Finished: 8,000 prompt tokens read in 8 s, 20,000 + 500 tokens of context.
        engines.set(f"{PC}/metrics", strata_metrics({"state": "idle"}, strata_request(), older))
        live = await reader.read(client, now=7.0)
        assert (live.state, live.last) == ("idle", LastRequest(
            prefill_tok_s=pytest.approx(1000.0), gen_tok_s=61.0, context=20500, max_context=262144,
            acceptance=pytest.approx(0.75)))
    # Strata asks for JSON: with Accept: text/plain it would answer in Prometheus.
    assert all(r.headers["accept"] == "application/json" for r in engines.requests)


@pytest.mark.parametrize(("request_", "prefill"), [
    # The slot's own session held more than the conversation cache: 3,000 read.
    (strata_request(reused=0, read=3000, prompt_ms=2000.0), 1500.0),
    # Cancelled while reading: what it read is not "the rest was cached".
    (strata_request(reused=0, read=3000, prompt_ms=2000.0, finish="cancel"), 10000.0),
    # An older engine that does not say what it read.
    (strata_request(reused=4000, read=None, prompt_ms=4000.0), 4000.0),
    # Everything from the cache: no prefill speed.
    (strata_request(reused=20000, read=0, prompt_ms=0.0), None),
])
async def test_strata_last_prefill_counts_the_tokens_it_read(engines, request_, prefill):
    engines.set(f"{PC}/metrics", strata_metrics({"state": "idle"}, request_))
    async with engines.client() as client:
        last = (await StrataReader(PC).read(client, now=1.0)).last
    assert last.prefill_tok_s == (pytest.approx(prefill) if prefill else None)


async def test_strata_without_requests_has_no_last(engines):
    engines.set(f"{PC}/metrics", strata_metrics({"state": "idle"}))
    async with engines.client() as client:
        assert (await StrataReader(PC).read(client, now=1.0)).last is None


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


def turbo_metrics(phase=None, started=0, last=None, **gauges):
    """gauges: the running_* ones; last: the other gauges (last_*, context_max_tokens)."""
    lines = ["# TYPE turbofieldfare_requests_running gauge",
             f"turbofieldfare_requests_running {1 if phase in ('prefill', 'decode') else 0}"]
    if phase is not None:
        lines.append(f"turbofieldfare_generations_started_total {started}")
        lines += [f'turbofieldfare_running_phase{{phase="{p}"}} {1 if p == phase else 0}'
                  for p in ("idle", "prefill", "decode")]
    lines += [f"turbofieldfare_running_{name} {value}" for name, value in gauges.items()]
    lines += [f"turbofieldfare_{name} {value}" for name, value in (last or {}).items()]
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
        # Idle: the last_* gauges of the request that finished (no MTP in TurboFieldfare).
        engines.set(f"{MAC}/metrics", turbo_metrics("idle", 4, last={
            "context_max_tokens": 65536, "last_context_tokens": 23490,
            "last_prefill_tokens_per_second": 201.25, "last_decode_tokens_per_second": 33.5}))
        live = await reader.read(client, now=13.0)
        assert (live.state, live.last) == ("idle", LastRequest(
            prefill_tok_s=201.25, gen_tok_s=33.5, context=23490, max_context=65536))


async def test_turbofieldfare_before_any_request_has_no_last(engines):
    engines.set(f"{MAC}/metrics", turbo_metrics("idle", 0, last={"context_max_tokens": 262144}))
    async with engines.client() as client:
        assert (await TurboFieldfareReader(MAC).read(client, now=1.0)).last is None


@pytest.mark.parametrize(("running", "state"), [(1, "busy"), (0, "idle")])
async def test_turbofieldfare_older_build_is_busy_or_idle(engines, running, state):
    engines.set(f"{MAC}/metrics", f"turbofieldfare_requests_running {running}\n")
    async with engines.client() as client:
        assert (await TurboFieldfareReader(MAC).read(client, now=1.0)).state == state


def mlx_vlm_metrics(in_flight=0, latest=None, ready=True, preloading=False, failures=None):
    """mlx_vlm 0.8's /metrics, trimmed (as it answered on the Mac on 23/11)."""
    return {
        "latest": latest,
        "recent": [latest] if latest else [],
        "summary": {"uptime_s": 60.0, "requests_started": 2 + in_flight, "requests_completed": 2,
                    "requests_failed": 0, "in_flight": in_flight, "last_error": None},
        "server": {"loaded_model": "mlx-community/Qwen3.8-27B-4bit" if ready else None,
                   "model_ready": ready, "loaded_context_size": 262144,
                   "configured_context_limit": None, "effective_context_limit": 262144,
                   "continuous_batching_enabled": True, "request_queue_depth": 0,
                   "preload_failures": failures or {}, "preload_in_progress": preloading,
                   "apc": {"enabled": False}},
    }


# The `latest` of a real request: 2605 prompt tokens, 400 generated, MTP on.
MLX_LATEST = {"endpoint": "/chat/completions", "model": "mlx-community/Qwen3.8-27B-4bit",
              "prompt_tokens": 2605, "completion_tokens": 400, "generated_tokens": 400,
              "reasoning_tokens": 0, "total_tokens": 3005, "prompt_eval_time_s": 12.564,
              "prefill_tok_s": 207.34, "ttft_s": 12.596, "decode_elapsed_s": 14.914,
              "request_elapsed_s": 27.534, "decode_tok_s": 26.82, "finish_reason": "length"}


async def test_mlx_vlm(engines):
    reader = MlxVlmReader(MAC)
    last = LastRequest(prefill_tok_s=207.34, gen_tok_s=26.82, context=3005, max_context=262144)
    async with engines.client() as client:
        with pytest.raises(httpx.ConnectError):     # nothing on the port: the poller says "off"
            await reader.read(client, now=1.0)
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(ready=False, preloading=True))
        assert (await reader.read(client, now=2.0)) == Live("loading")
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics())
        assert (await reader.read(client, now=3.0)) == Live("idle")       # no request yet
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(in_flight=1, latest=MLX_LATEST))
        assert (await reader.read(client, now=4.0)) == Live("busy", last=last)
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(latest=MLX_LATEST))
        assert (await reader.read(client, now=5.0)) == Live("idle", last=last)


async def test_mlx_vlm_unloaded_or_failed(engines):
    reader = MlxVlmReader(MAC)
    async with engines.client() as client:
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(ready=False))       # after POST /unload
        assert (await reader.read(client, now=1.0)).state == "unloaded"
        # A request loads it again on demand: busy meanwhile.
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(ready=False, in_flight=1))
        assert (await reader.read(client, now=2.0)).state == "busy"
        failures = {"text_generation": {"model": "m", "label": "language model",
                                        "error": "IncompleteSnapshotError"}}
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(ready=False, failures=failures))
        live = await reader.read(client, now=3.0)
        assert (live.state, live.note) == ("unknown", "loading the model failed: IncompleteSnapshotError")


async def test_mlx_vlm_and_turbofieldfare_share_the_port(engines):
    async with engines.client() as client:
        engines.set(f"{MAC}/metrics", turbo_metrics("idle", 1))           # TurboFieldfare is on
        live = await MlxVlmReader(MAC).read(client, now=1.0)
        assert (live.state, live.note) == ("off", "no mlx_vlm metrics (HTTP 200)")
        engines.set(f"{MAC}/metrics", mlx_vlm_metrics(latest=MLX_LATEST))  # mlx_vlm is on
        live = await TurboFieldfareReader(MAC).read(client, now=2.0)
        assert (live.state, live.note) == ("off", "no TurboFieldfare metrics (HTTP 200)")


async def test_poller_reads_every_section_of_the_config(engines):
    engines.set(f"{PC}/metrics", llama_metrics())
    engines.set(f"{PC}/slots", [slot(processing=False)])
    engines.set(f"{MAC}/metrics", mlx_vlm_metrics())
    clock = iter([10.0, 13.0])
    poller = MetricsPoller(load_config(REPO_CONFIG), engines.client(), clock=lambda: next(clock),
                           wallclock=lambda: 1000.0)
    assert sorted(poller.readers) == ["linux/llama-server", "linux/strata", "mac/mlx-vlm",
                                      "mac/turbofieldfare"]
    await poller.poll_once()
    sections = poller.view()["sections"]
    assert sections["linux/llama-server"]["state"] == "idle"
    assert sections["linux/strata"]["state"] == "off"            # llama-server holds port 8000
    assert sections["mac/mlx-vlm"]["state"] == "idle"
    assert sections["mac/turbofieldfare"]["state"] == "off"      # mlx_vlm holds port 8080
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
    assert llama["last"] is None             # the console has not seen a request end yet
    assert data["sections"]["mac/mlx-vlm"]["state"] == "off"
    assert all(s["has_metrics"] for t in config["tabs"] for s in t["sections"])
    # The page shows where each engine listens; the URLs the app polls stay on the server.
    endpoints = {f"{t['id']}/{s['id']}": s["endpoint"] for t in config["tabs"] for s in t["sections"]}
    assert endpoints == {"linux/llama-server": "192.168.1.200:8000", "linux/strata": "192.168.1.200:8000",
                         "mac/turbofieldfare": "192.168.1.101:8080", "mac/mlx-vlm": "192.168.1.101:8080"}
    assert "http://" not in json.dumps(config)


async def test_api_metrics_last_request(engines, fake_runner):
    engines.set(f"{PC}/metrics", llama_metrics(10, 7))
    engines.set(f"{PC}/slots", [slot(processing=False, prompt=1234)])
    app = create_app(REPO_CONFIG, runner=fake_runner, metrics_client=engines.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await app.state.metrics.poll_once()
        engines.set(f"{PC}/metrics", llama_metrics(20, 15, prompt=(2000, 2.0), predicted=(600, 24.0)))
        await app.state.metrics.poll_once()
        llama = (await c.get("/api/metrics")).json()["sections"]["linux/llama-server"]
    assert llama["last"] == {"prefill_tok_s": 1000.0, "gen_tok_s": 25.0, "context": 1234,
                             "max_context": 262144, "acceptance": 0.8}
