"""Jobs: one Run or Stop of a section, run in the background over ssh.

Kept in memory only (lost when the container restarts). One job at a time per
section: a second Run/Stop while one is running is refused (SectionBusy -> 409).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .config import Config, Host, Ready, Section, Tab
from .runner import Runner, build_ssh_argv

log = logging.getLogger("llm_console")

OUTPUT_LINES = 200


class JobState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Step:
    command: str
    timeout: float
    # False: the exit code is reported but does not fail the job (the stop before a run).
    check: bool = True
    # Shown in the output instead of `$ command` (for the generated ready checks).
    label: str | None = None


def port_free_command(ready: Ready) -> str:
    """Exit 1 if something already listens on the port of ready.url (another engine)."""
    sport = shlex.quote(f"sport = :{ready.port}")
    return (
        f"busy=$(ss -ltnpH {sport}); "
        f'if [ -n "$busy" ]; then echo "La porta {ready.port} è già in uso:" >&2; '
        f'echo "$busy" >&2; exit 1; fi; '
        f'echo "Porta {ready.port} libera."'
    )


def wait_ready_command(ready: Ready) -> str:
    """Loops until ready.process listens on the port and ready.url answers; exit 1 if it dies."""
    proc = shlex.quote(ready.process)
    owner = shlex.quote(f'"{ready.process}"')  # how ss -p names the process: users:(("name",…
    sport = shlex.quote(f"sport = :{ready.port}")
    url = shlex.quote(ready.url)
    return (
        "while :; do "
        f"if ! pgrep -x {proc} >/dev/null; then "
        f'echo "{ready.process} non è più in esecuzione: vedi il suo log." >&2; exit 1; fi; '
        f"if ss -ltnpH {sport} | grep -qF {owner} "
        f"&& curl -fs -o /dev/null --max-time 5 {url}; then "
        f"echo Pronto: {url}; exit 0; fi; "
        "sleep 2; done"
    )


def run_steps(section: Section, variant: str) -> list[Step]:
    steps = []
    if section.stop_before_run and section.stop:
        steps.append(Step(section.stop, section.stop_timeout, check=False))
    ready = section.ready
    if ready:
        steps.append(Step(port_free_command(ready), 30,
                          label=f"controllo che la porta {ready.port} sia libera"))
    steps.append(Step(section.variants[variant].command, section.run_timeout))
    if ready:
        steps.append(Step(wait_ready_command(ready), ready.timeout,
                          label=f"attendo che {ready.process} risponda su {ready.url}"))
    return steps


def stop_steps(section: Section) -> list[Step]:
    assert section.stop
    return [Step(section.stop, section.stop_timeout)]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    tab: str
    section: str
    action: str
    variant: str | None
    host: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.RUNNING
    exit_code: int | None = None
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=OUTPUT_LINES))

    def finish(self, state: JobState, exit_code: int | None) -> None:
        self.state = state
        self.exit_code = exit_code
        self.ended_at = _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tab": self.tab,
            "section": self.section,
            "action": self.action,
            "variant": self.variant,
            "host": self.host,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "output_tail": "\n".join(self.output),
        }


class SectionBusy(Exception):
    def __init__(self, job: Job) -> None:
        super().__init__(f"job {job.id} is running")
        self.job = job


class JobManager:
    def __init__(self, config: Config, runner: Runner, keep: int = 100) -> None:
        self.config = config
        self.runner = runner
        self.keep = keep
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._last: dict[tuple[str, str], Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def last(self, tab_id: str, section_id: str) -> Job | None:
        return self._last.get((tab_id, section_id))

    def start(self, tab: Tab, section: Section, action: str, variant: str | None, steps: list[Step]) -> Job:
        # No await between the check and the registration: two requests cannot both pass.
        key = (tab.id, section.id)
        current = self._last.get(key)
        if current and current.state is JobState.RUNNING:
            raise SectionBusy(current)

        host = self.config.hosts[tab.host]
        job = Job(tab=tab.id, section=section.id, action=action, variant=variant,
                  host=f"{host.user}@{host.address}")
        self._jobs[job.id] = job
        self._last[key] = job
        self._trim()

        task = asyncio.create_task(self._execute(job, host, steps), name=f"job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("job %s: %s/%s %s %s started", job.id, tab.id, section.id, action, variant or "")
        return job

    async def _execute(self, job: Job, host: Host, steps: list[Step]) -> None:
        exit_code: int | None = None
        try:
            for step in steps:
                argv = build_ssh_argv(self.config.ssh, host, step.command)
                job.output.append(f"[{step.label}]" if step.label else f"$ {step.command}")
                log.info("job %s: ssh %s@%s %s", job.id, host.user, host.address, step.command)
                exit_code = await self.runner.run(argv, step.timeout, job.output.append)
                if exit_code is None:
                    job.output.append(f"[timeout dopo {step.timeout:g} s: comando interrotto]")
                    job.finish(JobState.TIMEOUT, None)
                    break
                if exit_code != 0:
                    if step.check:
                        job.finish(JobState.FAILED, exit_code)
                        break
                    job.output.append(f"[exit {exit_code}, ignorato]")
            else:
                job.finish(JobState.SUCCEEDED, exit_code)
        except Exception as exc:  # e.g. the ssh binary is missing
            log.exception("job %s failed", job.id)
            job.output.append(f"[errore: {exc}]")
            job.finish(JobState.FAILED, None)
        log.info("job %s: %s (exit %s)", job.id, job.state.value, job.exit_code)

    def _trim(self) -> None:
        latest = {j.id for j in self._last.values()}
        for job_id in list(self._jobs):
            if len(self._jobs) <= self.keep:
                break
            if job_id not in latest and self._jobs[job_id].state is not JobState.RUNNING:
                del self._jobs[job_id]
