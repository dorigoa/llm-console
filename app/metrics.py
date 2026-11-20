"""Live metrics: what each engine is doing now, read from its own HTTP endpoints.

Every POLL_SECONDS the app asks every section with `metrics` in config.yaml, all
at once, and keeps the latest answer; the page shows it as the card's live line.
An engine counts as on when its endpoint answers the way that kind of engine
does: llama-server and Strata share port 8000, TurboFieldfare and mlx_lm.server
share 8080, and each reader recognizes its own engine's answer (anything else is
"off").

What each kind gives (see PLAN.md §15):
- llama-server: /slots for the phase and the token counts so far (its /metrics
  counters move only when a prefill or a request ends), /metrics for the MTP
  drafts (spec_decode_*), counted when a request ends.
- strata: /metrics in JSON (what its Monitor shows): state, prefill progress and
  speed, generation speed, and the drafts of the finished requests.
- turbofieldfare: /metrics, with the running_* gauges of the fork (phase, prompt
  progress, tokens so far, speeds); an older build gives busy/idle only.
- mlx-lm: no metrics at all; /health answering (and /metrics not) = serving.

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
class Live:
    """One engine at one poll."""

    # off | loading | unloaded | idle | busy | prefill | generating | serving | unknown
    state: str
    prefill_tok_s: float | None = None
    gen_tok_s: float | None = None
    # Prompt tokens in the KV so far (cache hits included) of the prompt's length.
    prompt_done: int | None = None
    prompt_total: int | None = None
    # Tokens the running request has generated so far.
    generated: int | None = None
    # MTP: drafted tokens accepted / drafted, in the requests that finished
    # between the last two changes of the counters (on first sight: since start).
    acceptance: float | None = None
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


class Reader:
    """Reads one engine; keeps what the next poll needs to compute speeds."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reset()

    def reset(self) -> None:
        """Forget the previous poll: the engine is off, or another one answers."""
        self.drafted: float | None = None
        self.accepted: float | None = None
        self.acceptance: float | None = None

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        raise NotImplementedError

    def off(self, note: str) -> Live:
        self.reset()
        return Live("off", note=note)

    def track_acceptance(self, drafted: float | None, accepted: float | None) -> float | None:
        """MTP acceptance of the requests that finished since the counters last moved."""
        if drafted is None or accepted is None:
            return self.acceptance
        if self.drafted is None or drafted < self.drafted:     # first sight, or the engine restarted
            self.acceptance = accepted / drafted if drafted > 0 else None
        elif drafted > self.drafted:
            self.acceptance = (accepted - self.accepted) / (drafted - self.drafted)
        self.drafted, self.accepted = drafted, accepted
        return self.acceptance


class LlamaServerReader(Reader):
    """/slots, per processing slot: n_prompt_tokens_processed grows per batch during
    the prefill, next_token[0].n_decoded per token during the generation. Its
    n_prompt_tokens is the part of the prompt already in the KV, not the prompt's
    length, so there is no total to show. A batch (e.g. 2048 tokens) can take
    about as long as a poll, so the prefill speed is the average since the poll
    that first saw the phase, not the last interval (which would read 0, 1x or 2x)."""

    def reset(self) -> None:
        super().reset()
        # slot id -> (id_task, phase, processed, decoded, when the phase was first seen,
        #             processed then)
        self.slots: dict[Any, tuple] = {}
        self.slots_at = 0.0

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        if r.status_code == 503:                       # loading the model
            return Live("loading")
        if r.status_code != 200 or not _has_line(r.text, "llamacpp:"):
            return self.off(f"no llama-server metrics (HTTP {r.status_code})")
        m = parse_prometheus(r.text)
        acceptance = self.track_acceptance(m.get("llamacpp:spec_decode_num_draft_tokens_total"),
                                           m.get("llamacpp:spec_decode_num_accepted_tokens_total"))
        slots = await client.get(f"{self.url}/slots")
        if slots.status_code != 200 or not isinstance(slots.json(), list):
            busy = m.get("llamacpp:requests_processing", 0) > 0     # started with --no-slots
            return Live("busy" if busy else "idle", acceptance=acceptance)
        live = self._from_slots(slots.json(), now)
        live.acceptance = acceptance
        return live

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
        totals = data.get("totals") or {}
        acceptance = self.track_acceptance(totals.get("drafts_offered"), totals.get("drafts_accepted"))
        live = data["live"]
        state = live.get("state")
        if state == "reading":
            read, total = live.get("prompt_read"), live.get("prompt_total") or live.get("prompt_tokens")
            rate = live.get("prefill_tok_s_mean")
            if rate is None and read is not None and self.prompt_read is not None:
                rate = _rate(read, self.prompt_read[1], now - self.prompt_read[0])
            self.prompt_read = (now, read) if read is not None else None
            return Live("prefill", prefill_tok_s=rate, prompt_done=read, prompt_total=total,
                        acceptance=acceptance)
        self.prompt_read = None
        if state == "generating":
            return Live("generating", gen_tok_s=live.get("tok_s"), generated=live.get("generated"),
                        acceptance=acceptance)
        if state in ("idle", "unloaded"):
            return Live(state, acceptance=acceptance)
        return Live("unknown", note=f"Strata state {state!r}", acceptance=acceptance)


class TurboFieldfareReader(Reader):
    def reset(self) -> None:
        super().reset()
        self.decode: tuple | None = None     # (generations started, when, tokens generated)

    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        if r.status_code != 200 or not _has_line(r.text, "turbofieldfare_"):
            return self.off(f"no TurboFieldfare metrics (HTTP {r.status_code})")
        m = parse_prometheus(r.text)
        phase = next((p for p in ("prefill", "decode", "idle")
                      if m.get(f'turbofieldfare_running_phase{{phase="{p}"}}') == 1), None)
        if phase is None:        # a build without the running_* gauges: busy or idle only
            self.decode = None
            return Live("busy" if m.get("turbofieldfare_requests_running", 0) > 0 else "idle")
        started = m.get("turbofieldfare_generations_started_total")
        if phase == "prefill":
            self.decode = None
            return Live("prefill", prefill_tok_s=m.get("turbofieldfare_running_prefill_tokens_per_second"),
                        prompt_done=_int(m.get("turbofieldfare_running_prompt_done_tokens")),
                        prompt_total=_int(m.get("turbofieldfare_running_prompt_tokens")))
        if phase == "decode":
            generated = m.get("turbofieldfare_running_generated_tokens", 0)
            rate = None
            if self.decode is not None and self.decode[0] == started:
                rate = _rate(generated, self.decode[2], now - self.decode[1])
            if rate is None:     # first sight of this request's decode: its average so far
                rate = m.get("turbofieldfare_running_decode_tokens_per_second")
            self.decode = (started, now, generated)
            return Live("generating", gen_tok_s=rate, generated=_int(generated))
        self.decode = None
        return Live("idle")


class MlxLmReader(Reader):
    async def read(self, client: httpx.AsyncClient, now: float) -> Live:
        r = await client.get(f"{self.url}/metrics")
        if r.status_code != 404:             # TurboFieldfare answers /metrics on the same port
            return self.off(f"another engine answers (HTTP {r.status_code} on /metrics)")
        health = await client.get(f"{self.url}/health")
        if health.status_code != 200:
            return self.off(f"/health: HTTP {health.status_code}")
        return Live("serving")


READERS: dict[str, type[Reader]] = {
    "llama-server": LlamaServerReader,
    "strata": StrataReader,
    "turbofieldfare": TurboFieldfareReader,
    "mlx-lm": MlxLmReader,
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
