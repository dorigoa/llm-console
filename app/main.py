"""llm-console: the API and the static page.

Run with:  uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8080
Env:       LLM_CONSOLE_CONFIG       config path (default: config.yaml in the checkout)
           LLM_CONSOLE_DRY_RUN=1    run nothing, only report the ssh commands
           LLM_CONSOLE_DRY_RUN_DELAY seconds a dry-run job takes (default 3)
           LLM_CONSOLE_METRICS=0    do not poll the engines for the live lines
           LLM_CONSOLE_VERSION      shown in the footer (default: /app/VERSION in the image, else "dev")
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config, Section, Tab, load_config
from .jobs import CheckFailed, Conflicting, JobManager, SectionBusy, stop_steps
from .metrics import TIMEOUT, MetricsPoller
from .runner import DryRunRunner, Runner, SubprocessRunner

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Written by container/build.sh into the image (/app/VERSION); absent in a checkout.
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

# POSTs must carry this header. A page on another site cannot add it without a
# CORS preflight, which this app never approves: no cross-site Run/Stop.
CSRF_HEADER = "X-LLM-Console"

log = logging.getLogger("llm_console")


class RunRequest(BaseModel):
    variant: str
    # Option id -> choice id; an option left out takes its default.
    options: dict[str, str] = Field(default_factory=dict)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def _version() -> str:
    """LLM_CONSOLE_VERSION if set, else the VERSION file of the image, else "dev"."""
    if version := os.environ.get("LLM_CONSOLE_VERSION"):
        return version
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _setup_logging() -> None:
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def _config_view(config: Config, dry_run: bool) -> dict:
    tabs = []
    for tab in config.tabs:
        host = config.hosts[tab.host]
        tabs.append({
            "id": tab.id,
            "label": tab.label,
            "host": {"id": tab.host, "address": host.address, "user": host.user},
            "sections": [
                {
                    "id": s.id,
                    "label": s.label,
                    "description": s.description,
                    "variants_legend": s.variants_legend,
                    "variants": [{"id": k, "label": v.label or k, "model": v.model or v.label or k}
                                 for k, v in s.variants.items()],
                    "default_variant": s.default_variant,
                    "options": [
                        {
                            "id": option_id,
                            "legend": o.legend,
                            "description": o.description,
                            "default": o.default,
                            "choices": [{"id": k, "label": c.label or k} for k, c in o.choices.items()],
                        }
                        for option_id, o in s.options.items()
                    ],
                    "exclusive_with": s.exclusive_with,
                    "has_metrics": s.metrics is not None,
                    "can_run": s.can_run,
                    "can_stop": s.can_stop,
                }
                for s in tab.sections
            ],
        })
    return {
        "tabs": tabs,
        "dry_run": dry_run,
        "version": _version(),
    }


def create_app(config_path: str | os.PathLike | None = None, runner: Runner | None = None,
               metrics_client: httpx.AsyncClient | None = None) -> FastAPI:
    """metrics_client: how the live lines reach the engines (tests pass a mock transport)."""
    _setup_logging()
    config = load_config(config_path)
    if runner is None:
        if _env_flag("LLM_CONSOLE_DRY_RUN"):
            runner = DryRunRunner(delay=float(os.environ.get("LLM_CONSOLE_DRY_RUN_DELAY", "3")))
        else:
            runner = SubprocessRunner()
    dry_run = isinstance(runner, DryRunRunner)
    jobs = JobManager(config, runner)
    # The live lines only read the engines' own endpoints: they run in dry-run too.
    poll_metrics = os.environ.get("LLM_CONSOLE_METRICS", "1").strip().lower() not in ("0", "false", "no")
    poller = MetricsPoller(config, metrics_client or httpx.AsyncClient(timeout=TIMEOUT))
    log.info("llm-console: %d tabs, %s, live metrics for %d sections%s", len(config.tabs),
             "DRY-RUN" if dry_run else "ssh", len(poller.readers), "" if poll_metrics else " (off)")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(poller.run(), name="metrics") if poll_metrics and poller.readers else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await poller.client.aclose()

    app = FastAPI(title="llm-console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.jobs = jobs
    app.state.metrics = poller

    @app.middleware("http")
    async def revalidate(request: Request, call_next):
        # Without it browsers reuse a cached app.js or style.css after a deploy, and
        # an old page can meet a new script. no-cache = keep it, but ask (ETag -> 304).
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def find_section(tab_id: str, section_id: str) -> tuple[Tab, Section]:
        tab = config.tab(tab_id)
        section = tab.section(section_id) if tab else None
        if tab is None or section is None:
            raise HTTPException(404, "Unknown section")
        return tab, section

    def require_csrf_header(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, f"Missing header {CSRF_HEADER}: 1")

    def busy(exc: SectionBusy) -> HTTPException:
        return HTTPException(409, {"message": "An operation is already running for this section",
                                   "job": exc.job.to_dict()})

    def conflicting(section: Section, exc: Conflicting) -> HTTPException:
        labels = " and ".join(c.section.label for c in exc.conflicts)
        return HTTPException(409, {"message": f"Stop {labels} before starting {section.label}",
                                   "conflicts": [c.to_dict() for c in exc.conflicts]})

    def check_failed(exc: CheckFailed) -> HTTPException:
        code = "timeout" if exc.exit_code is None else f"exit {exc.exit_code}"
        return HTTPException(502, {"message": f"Cannot tell whether {exc.section.label} is on ({code})",
                                   "output": "\n".join(exc.output[-20:])})

    # All handlers are `async def`: they run on the event loop, where the jobs live
    # (JobManager.start creates a task; a sync handler would run in a thread with no loop).

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/api/config")
    async def get_config() -> dict:
        return _config_view(config, dry_run)

    @app.post("/api/sections/{tab_id}/{section_id}/run", status_code=202,
              dependencies=[Depends(require_csrf_header)])
    async def run(tab_id: str, section_id: str, body: RunRequest) -> dict:
        tab, section = find_section(tab_id, section_id)
        if not section.can_run:
            raise HTTPException(400, "Run is not configured for this section")
        if body.variant not in section.variants:
            raise HTTPException(400, f"Unknown variant: {body.variant}")
        try:
            chosen = section.choose(body.options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        try:
            job = await jobs.start_run(tab, section, body.variant, chosen)
        except SectionBusy as exc:
            raise busy(exc) from None
        except Conflicting as exc:
            raise conflicting(section, exc) from None
        except CheckFailed as exc:
            raise check_failed(exc) from None
        return {"job_id": job.id, "job": job.to_dict()}

    @app.get("/api/sections/{tab_id}/{section_id}/conflicts")
    async def get_conflicts(tab_id: str, section_id: str) -> dict:
        """What would refuse a Run now: the page asks before opening the Run dialog."""
        tab, section = find_section(tab_id, section_id)
        try:
            found = await jobs.conflicts(tab, section)
        except CheckFailed as exc:
            raise check_failed(exc) from None
        return {"conflicts": [c.to_dict() for c in found]}

    @app.post("/api/sections/{tab_id}/{section_id}/stop", status_code=202,
              dependencies=[Depends(require_csrf_header)])
    async def stop(tab_id: str, section_id: str) -> dict:
        tab, section = find_section(tab_id, section_id)
        if not section.can_stop:
            raise HTTPException(400, "Stop is not configured for this section")
        try:
            job = jobs.start(tab, section, "stop", None, stop_steps(section))
        except SectionBusy as exc:
            raise busy(exc) from None
        return {"job_id": job.id, "job": job.to_dict()}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job.to_dict()

    @app.get("/api/metrics")
    async def get_metrics() -> dict:
        """The live line of every section with metrics: {"interval": s, "sections": {"tab/section": …}}."""
        return poller.view()

    @app.get("/api/sections/{tab_id}/{section_id}/job")
    async def last_job(tab_id: str, section_id: str) -> dict:
        find_section(tab_id, section_id)
        job = jobs.last(tab_id, section_id)
        return {"job": job.to_dict() if job else None}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
