"""Live metrics: what each engine is doing now, read from its own HTTP endpoints.

Every POLL_SECONDS the app asks every section with `metrics` in config.yaml, all
at once, and keeps the latest answer; the page shows it as the card's live line.
An engine counts as on when its endpoint answers the way that kind of engine
does: llama-server and Strata share port 8000, TurboFieldfare and mlx_vlm.server
share 8080, and each reader recognizes its own engine's answer (anything else is
"off").

What each kind gives (see PLAN.md §15 and §16):
- llama-server: /slots for the phase and the token counts so far (its /metrics
  counters move only when a prefill or a request ends); /metrics' counters for
  the last request (they move when it ends: the differences are its prefill and
  generation time and tokens, and its MTP drafts); the idle slot that ran it for
  the context it filled.
- strata: /metrics in JSON (what its Monitor shows): state, prefill progress and
  speed, generation speed, and the last requests with their timings and drafts.
- turbofieldfare: /metrics, with the running_* gauges of the fork (phase, prompt
  progress, tokens so far, speeds) and its last_* gauges; an older build gives
  busy/idle only.
- mlx-vlm: /metrics in JSON: the model loading, requests in flight (busy, with no
  phase or tokens so far: it records a request when it ends), and the last
  request with its speeds and tokens. No MTP acceptance.

Speeds measured by the app are differences between two polls of the same request
in the same phase; in between, the line shows the phase without a speed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("llm_console")

POLL_SECONDS = 3.0
# Per request: a host that is down must not hold up the next round.
TIMEOUT = 2.5


@dataclass
class LastRequest:
    """The last request the engine finished, over its whole run."""

    # Prompt tokens computed (not taken from a cache) per second of prefill.
    prefill_tok_s: float | None = None
    # Seconds of prefill, when the speed cannot be told (mlx_vlm with a prefix
    # cache does not say how many prompt tokens it took from it).
    prefill_s: float | None = None
    # Tokens generated per second of generation.
    gen_tok_s: float | None = None
    # Tokens the request filled in the context (prompt + generated), of the
    # engine's context length.
    context: int | None = None
    max_context: int | None = None
    # MTP: drafted tokens accepted / drafted.
    acceptance: float | None = None

    def known(self) -> LastRequest | None:
        """None when the engine said nothing about it (e.g. no request yet)."""
        return self if any(v is not None for k, v in asdict(self).items() if k != "max_context") else None


@dataclass
class Live:
    """One engine at one poll."""

    # off | loading | unloaded | idle | busy | prefill | generating | unknown
    state: str
    prefill_tok_s: float | None = None
    gen_tok_s: float | None = None
    # Prompt tokens in the KV so far (cache hits included) of the prompt's length.
    prompt_done: int | None = None
    prompt_total: int | None = None
    # Tokens the running request has generated so far.
    generated: int | None = None
    # The last finished request (while the engine runs the next one too).
    last: LastRequest | None = None
    # Why off or unknown (e.g. "connection refused").
    note: str | None = None
    # time.time() of the poll.
    at: float = 0.0

    def to_dict(self, now: float) -> dict:
        data = asdict(self)
        data["age_s"] = round(max(0.0, now - data.pop("at")), 1)
        return data


def parse_prometheus(text: str) -> dict[str, float]:
    """Sample lines of the Prometheus text format: 'name{labels}' (as written) -> value."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        try:
            samples[key] = float(value)
        except ValueError:
            continue
    return samples


def _rate(count: float, prev_count: float, dt: float) -> float | None:
    return (count - prev_count) / dt if dt > 0 and count >= prev_count else None


def _ratio(part: float | None, whole: float | None) -> float | None:
    return part / whole if part is not None and whole else None


class Reader:
    """Reads one engine; keeps what the next poll needs to compute speeds."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reset()

    def reset(self) -> None:
        """Forget the previous poll: the engine is off, or another one answers."""

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        raise NotImplementedError

    def off(self, note: str) -> Live:
        self.reset()
        return Live("off", note=note)


class LlamaServerReader(Reader):
    """/slots, per processing slot: n_prompt_tokens_processed grows per batch during
    the prefill, next_token[0].n_decoded per token during the generation. Its
    n_prompt_tokens is the part of the prompt already in the KV, not the prompt's
    length, so there is no total to show. A batch (e.g. 2048 tokens) can take
    about as long as a poll, so the prefill speed is the average since the poll
    that first saw the phase, not the last interval (which would read 0, 1x or 2x).

    The last request: /metrics' prompt counters move when its prefill ends, the
    generation and MTP ones when it ends; so when tokens_predicted_total moves,
    the differences since it last moved are that request's (the engine's own
    timings). The console first seeing an engine that already served requests
    knows only their sum, so it waits for the next one. The context it filled is
    n_prompt_tokens of the idle slot that ran it (the KV it left: prompt and
    generated tokens), of that slot's n_ctx."""

    COUNTERS = ("prompt_tokens_total", "prompt_seconds_total", "tokens_predicted_total",
                "tokens_predicted_seconds_total", "spec_decode_num_draft_tokens_total",
                "spec_decode_num_accepted_tokens_total")

    def reset(self) -> None:
        super().reset()
        # slot id -> (id_task, phase, processed, decoded, when the phase was first seen,
        #             processed then)
        self.slots: dict[Any, tuple] = {}
        self.slots_at = 0.0
        # The counters when tokens_predicted_total last moved, and what that move said.
        self.counters: dict[str, float] | None = None
        self.finished = LastRequest()

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        if r.status_code == 503:                       # loading the model
            return Live("loading")
        if r.status_code != 200 or not _has_line(r.text, "llamacpp:"):
            return self.off(f"no llama-server metrics (HTTP {r.status_code})")
        m = parse_prometheus(r.text)
        self._track_requests(m)
        slots = await client.get(f"{self.url}/slots")
        if slots.status_code != 200 or not isinstance(slots.json(), list):
            busy = m.get("llamacpp:requests_processing", 0) > 0     # started with --no-slots
            return Live("busy" if busy else "idle", last=self.finished.known())
        live = self._from_slots(slots.json(), now)
        live.last = self._last(slots.json())
        return live

    def _track_requests(self, m: dict[str, float]) -> None:
        counters = {k: m.get(f"llamacpp:{k}", 0.0) for k in self.COUNTERS}
        prev = self.counters
        if prev is None or counters["tokens_predicted_total"] < prev["tokens_predicted_total"]:
            self.counters, self.finished = counters, LastRequest()    # first sight, or a restart
        elif counters["tokens_predicted_total"] > prev["tokens_predicted_total"]:
            d = {k: counters[k] - prev[k] for k in self.COUNTERS}
            self.finished = LastRequest(
                prefill_tok_s=_ratio(d["prompt_tokens_total"], d["prompt_seconds_total"]),
                gen_tok_s=_ratio(d["tokens_predicted_total"], d["tokens_predicted_seconds_total"]),
                acceptance=_ratio(d["spec_decode_num_accepted_tokens_total"],
                                  d["spec_decode_num_draft_tokens_total"]))
            self.counters = counters

    def _last(self, slots: list) -> LastRequest | None:
        """What the counters said, with the context from the idle slot of the newest task."""
        idle = [s for s in slots if isinstance(s, dict) and not s.get("is_processing")
                and isinstance(s.get("id_task"), int) and s.get("n_prompt_tokens")]
        newest = max(idle, key=lambda s: s["id_task"], default=None)
        last = LastRequest(**asdict(self.finished))
        if newest is not None:
            last.context, last.max_context = int(newest["n_prompt_tokens"]), _int(newest.get("n_ctx"))
        return last.known()

    def _from_slots(self, slots: list, now: float) -> Live:
        dt = now - self.slots_at
        current, phases = {}, set()
        prefill = gen = None
        done = generated = None
        for s in slots:
            if not isinstance(s, dict) or not s.get("is_processing"):
                continue
            task = s.get("id_task")
            processed = int(s.get("n_prompt_tokens_processed") or 0)
            next_token = s.get("next_token") or [{}]
            decoded = int((next_token[0] if isinstance(next_token, list) else next_token).get("n_decoded") or 0)
            phase = "generating" if decoded > 0 else "prefill"
            phases.add(phase)
            prev = self.slots.get(s.get("id"))
            same = prev is not None and prev[0] == task and prev[1] == phase
            since, processed_then = (prev[4], prev[5]) if same else (now, processed)
            current[s.get("id")] = (task, phase, processed, decoded, since, processed_then)
            if phase == "prefill":
                if same and (r := _rate(processed, processed_then, now - since)) is not None:
                    prefill = (prefill or 0.0) + r
                done = (done or 0) + processed + int(s.get("n_prompt_tokens_cache") or 0)
            else:
                if same and (r := _rate(decoded, prev[3], dt)) is not None:
                    gen = (gen or 0.0) + r
                generated = (generated or 0) + decoded
        self.slots, self.slots_at = current, now
        if "generating" in phases:
            return Live("generating", gen_tok_s=gen, generated=generated)
        if "prefill" in phases:
            return Live("prefill", prefill_tok_s=prefill, prompt_done=done)
        return Live("idle")


class StrataReader(Reader):
    def reset(self) -> None:
        super().reset()
        self.prompt_read: tuple[float, float] | None = None    # (when, prompt tokens read)

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics", headers={"Accept": "application/json"})
        data = _json(r) if r.status_code == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("live"), dict) or "engine" not in data:
            return self.off(f"no Strata metrics (HTTP {r.status_code})")
        last = _strata_last(data)
        live = data["live"]
        state = live.get("state")
        if state == "reading":
            read, total = live.get("prompt_read"), live.get("prompt_total") or live.get("prompt_tokens")
            rate = live.get("prefill_tok_s_mean")
            if rate is None and read is not None and self.prompt_read is not None:
                rate = _rate(read, self.prompt_read[1], now - self.prompt_read[0])
            self.prompt_read = (now, read) if read is not None else None
            return Live("prefill", prefill_tok_s=rate, prompt_done=read, prompt_total=total, last=last)
        self.prompt_read = None
        if state == "generating":
            return Live("generating", gen_tok_s=live.get("tok_s"), generated=live.get("generated"), last=last)
        if state in ("idle", "unloaded"):
            return Live(state, last=last)
        return Live("unknown", note=f"Strata state {state!r}", last=last)


def _strata_last(data: dict) -> LastRequest | None:
    """The newest of /metrics' requests, read as Strata's own request_timings() does
    (serve/server_parts/core.py): the prompt tokens it computed are the prompt less
    what the conversation cache, or the slot's own session, already held."""
    requests = data.get("requests")
    r = requests[0] if isinstance(requests, list) and requests and isinstance(requests[0], dict) else None
    if r is None:
        return None
    seen = r.get("prompt_tokens") or 0
    cached = int(r.get("reused") or 0)
    if r.get("prompt_read") is not None and r.get("finish") != "cancel":
        cached = max(cached, seen - int(r["prompt_read"]))
    computed, prompt_ms = max(0, seen - cached), r.get("prompt_ms")
    whole = r.get("prompt_total") if r.get("prompt_total") is not None else seen
    return LastRequest(
        prefill_tok_s=computed / (prompt_ms / 1000) if computed and prompt_ms else None,
        gen_tok_s=r.get("decode_tok_s"),
        context=whole + (r.get("output_tokens") or 0) if whole else None,
        max_context=_int((data.get("engine") or {}).get("max_context")),
        acceptance=_ratio(r.get("drafts_accepted"), r.get("drafts_offered"))).known()


class TurboFieldfareReader(Reader):
    def reset(self) -> None:
        super().reset()
        self.decode: tuple | None = None     # (generations started, when, tokens generated)

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        if r.status_code != 200 or not _has_line(r.text, "turbofieldfare_"):
            return self.off(f"no TurboFieldfare metrics (HTTP {r.status_code})")
        m = parse_prometheus(r.text)
        # The last_* gauges: the context and speeds of the last completed request
        # (a speed stays from an earlier one when the last did no work in that phase).
        last = LastRequest(prefill_tok_s=m.get("turbofieldfare_last_prefill_tokens_per_second"),
                           gen_tok_s=m.get("turbofieldfare_last_decode_tokens_per_second"),
                           context=_int(m.get("turbofieldfare_last_context_tokens")),
                           max_context=_int(m.get("turbofieldfare_context_max_tokens"))).known()
        phase = next((p for p in ("prefill", "decode", "idle")
                      if m.get(f'turbofieldfare_running_phase{{phase="{p}"}}') == 1), None)
        if phase is None:        # a build without the running_* gauges: busy or idle only
            self.decode = None
            return Live("busy" if m.get("turbofieldfare_requests_running", 0) > 0 else "idle", last=last)
        started = m.get("turbofieldfare_generations_started_total")
        if phase == "prefill":
            self.decode = None
            return Live("prefill", prefill_tok_s=m.get("turbofieldfare_running_prefill_tokens_per_second"),
                        prompt_done=_int(m.get("turbofieldfare_running_prompt_done_tokens")),
                        prompt_total=_int(m.get("turbofieldfare_running_prompt_tokens")), last=last)
        if phase == "decode":
            generated = m.get("turbofieldfare_running_generated_tokens", 0)
            rate = None
            if self.decode is not None and self.decode[0] == started:
                rate = _rate(generated, self.decode[2], now - self.decode[1])
            if rate is None:     # first sight of this request's decode: its average so far
                rate = m.get("turbofieldfare_running_decode_tokens_per_second")
            self.decode = (started, now, generated)
            return Live("generating", gen_tok_s=rate, generated=_int(generated), last=last)
        self.decode = None
        return Live("idle", last=last)


class MlxVlmReader(Reader):
    """/metrics in JSON (mlx_vlm 0.8): `server` is the state of the model (loading,
    ready, failed), `summary.in_flight` the requests running, `latest` the last
    finished one. mlx_vlm records a request only when it ends, so while one runs
    there is no phase and no token count: busy. The MTP drafts are only in each
    answer's `timings`, not here."""

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        data = _json(r) if r.status_code == 200 else None
        if (not isinstance(data, dict) or not isinstance(data.get("server"), dict)
                or not isinstance(data.get("summary"), dict)):
            return self.off(f"no mlx_vlm metrics (HTTP {r.status_code})")
        server = data["server"]
        last = _mlx_vlm_last(data.get("latest"), server)
        if server.get("preload_in_progress"):            # the model given with --model
            return Live("loading", last=last)
        if (data["summary"].get("in_flight") or 0) > 0:  # loading on demand counts too
            return Live("busy", last=last)
        if server.get("model_ready"):
            return Live("idle", last=last)
        failed = (server.get("preload_failures") or {}).get("text_generation")
        if isinstance(failed, dict):
            return Live("unknown", note=f"loading the model failed: {failed.get('error')}", last=last)
        return Live("unloaded", last=last)


def _mlx_vlm_last(latest: Any, server: dict) -> LastRequest | None:
    """The engine's own speeds of the last request: prompt tokens per second of
    prefill, and generated tokens (thinking included) per second of decode.

    With the prefix cache (APC) on, mlx_vlm's prefill_tok_s divides the whole
    prompt by the time it took to compute only the part not in the cache (e.g.
    37,290 tokens over 0.4 s: 92,453 tok/s), and nothing in /metrics says how
    many came from the cache: the line gives the prefill time instead."""
    if not isinstance(latest, dict):
        return None
    prompt, generated = latest.get("prompt_tokens"), latest.get("generated_tokens")
    apc = (server.get("apc") or {}).get("enabled")
    return LastRequest(
        prefill_tok_s=None if apc else latest.get("prefill_tok_s"),
        prefill_s=latest.get("prompt_eval_time_s") if apc else None,
        gen_tok_s=latest.get("decode_tok_s"),
        context=int(prompt) + int(generated or 0) if prompt else None,
        max_context=_int(server.get("effective_context_limit"))).known()


READERS: dict[str, type[Reader]] = {
    "llama-server": LlamaServerReader,
    "strata": StrataReader,
    "turbofieldfare": TurboFieldfareReader,
    "mlx-vlm": MlxVlmReader,
}


def _has_line(text: str, prefix: str) -> bool:
    return any(line.startswith(prefix) for line in text.splitlines())


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _int(value: float | None) -> int | None:
    return None if value is None else int(value)


class MetricsPoller:
    """Polls every engine with `metrics` in the config; `latest` has the last answers."""

    def __init__(self, config: Config, client: httpx.AsyncClient,
                 interval: float = POLL_SECONDS, clock=time.monotonic, wallclock=time.time) -> None:
        self.client = client
        self.interval = interval
        self.clock = clock
        self.wallclock = wallclock
        self.readers: dict[str, Reader] = {
            f"{tab.id}/{s.id}": READERS[s.metrics.kind](s.metrics.url)
            for tab in config.tabs for s in tab.sections if s.metrics
        }
        self.latest: dict[str, Live] = {}

    async def poll_once(self) -> None:
        now = self.clock()
        await asyncio.gather(*(self._read(key, reader, now) for key, reader in self.readers.items()))

    async def _read(self, key: str, reader: Reader, now: float) -> None:
        try:
            live = await reader.read(self.client, now)
        except httpx.TimeoutException:
            live = reader.off("no answer")
        except httpx.TransportError as exc:
            live = reader.off(_transport_note(exc))
        except Exception as exc:                   # an answer this reader does not understand
            log.warning("metrics %s: %s: %s", key, type(exc).__name__, exc)
            reader.reset()
            live = Live("unknown", note=f"{type(exc).__name__}: {exc}")
        live.at = self.wallclock()
        self.latest[key] = live

    async def run(self) -> None:
        while True:
            started = self.clock()
            try:
                await self.poll_once()
            except Exception:                      # never let the loop die
                log.exception("metrics poll failed")
            await asyncio.sleep(max(0.0, self.interval - (self.clock() - started)))

    def view(self) -> dict:
        now = self.wallclock()
        return {"interval": self.interval,
                "sections": {key: live.to_dict(now) for key, live in self.latest.items()}}


def _transport_note(exc: httpx.TransportError) -> str:
    text = str(exc).lower()
    if "refused" in text:
        return "connection refused"
    if isinstance(exc, httpx.ConnectError):
        return "cannot connect"
    return type(exc).__name__
