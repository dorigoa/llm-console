"""llm-console: the API and the static page.

Run with:  uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8080
Env:       LLM_CONSOLE_CONFIG       config path (default: config.yaml in the checkout)
           LLM_CONSOLE_DRY_RUN=1    run nothing, only report the ssh commands
           LLM_CONSOLE_DRY_RUN_DELAY seconds a dry-run job takes (default 3)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config, Section, Tab, load_config
from .jobs import CheckFailed, Conflicting, JobManager, SectionBusy, stop_steps
from .runner import DryRunRunner, Runner, SubprocessRunner

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# POSTs must carry this header. A page on another site cannot add it without a
# CORS preflight, which this app never approves: no cross-site Run/Stop.
CSRF_HEADER = "X-LLM-Console"

log = logging.getLogger("llm_console")


class RunRequest(BaseModel):
    variant: str


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


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
                    "variants": [{"id": k, "label": v.label or k} for k, v in s.variants.items()],
                    "default_variant": s.default_variant,
                    "exclusive_with": s.exclusive_with,
                    "can_run": s.can_run,
                    "can_stop": s.can_stop,
                }
                for s in tab.sections
            ],
        })
    return {
        "tabs": tabs,
        "dry_run": dry_run,
        "version": os.environ.get("LLM_CONSOLE_VERSION", "dev"),
    }


def create_app(config_path: str | os.PathLike | None = None, runner: Runner | None = None) -> FastAPI:
    _setup_logging()
    config = load_config(config_path)
    if runner is None:
        if _env_flag("LLM_CONSOLE_DRY_RUN"):
            runner = DryRunRunner(delay=float(os.environ.get("LLM_CONSOLE_DRY_RUN_DELAY", "3")))
        else:
            runner = SubprocessRunner()
    dry_run = isinstance(runner, DryRunRunner)
    jobs = JobManager(config, runner)
    log.info("llm-console: %d tabs, %s", len(config.tabs), "DRY-RUN" if dry_run else "ssh")

    app = FastAPI(title="llm-console", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.jobs = jobs

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
            raise HTTPException(404, "Sezione sconosciuta")
        return tab, section

    def require_csrf_header(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, f"Header {CSRF_HEADER}: 1 mancante")

    def busy(exc: SectionBusy) -> HTTPException:
        return HTTPException(409, {"message": "Un'operazione è già in corso per questa sezione",
                                   "job": exc.job.to_dict()})

    def conflicting(section: Section, exc: Conflicting) -> HTTPException:
        labels = " e ".join(c.section.label for c in exc.conflicts)
        return HTTPException(409, {"message": f"Prima di avviare {section.label} spegni {labels}",
                                   "conflicts": [c.to_dict() for c in exc.conflicts]})

    def check_failed(exc: CheckFailed) -> HTTPException:
        code = "timeout" if exc.exit_code is None else f"exit {exc.exit_code}"
        return HTTPException(502, {"message": f"Non riesco a verificare se {exc.section.label} "
                                              f"è acceso ({code})",
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
            raise HTTPException(400, "Run non configurato per questa sezione")
        if body.variant not in section.variants:
            raise HTTPException(400, f"Variante sconosciuta: {body.variant}")
        try:
            job = await jobs.start_run(tab, section, body.variant)
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
            raise HTTPException(400, "Stop non configurato per questa sezione")
        try:
            job = jobs.start(tab, section, "stop", None, stop_steps(section))
        except SectionBusy as exc:
            raise busy(exc) from None
        return {"job_id": job.id, "job": job.to_dict()}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job sconosciuto")
        return job.to_dict()

    @app.get("/api/sections/{tab_id}/{section_id}/job")
    async def last_job(tab_id: str, section_id: str) -> dict:
        find_section(tab_id, section_id)
        job = jobs.last(tab_id, section_id)
        return {"job": job.to_dict() if job else None}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
