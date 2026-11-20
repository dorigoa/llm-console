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
# Seconds for a `running` check (strata launch --status takes ~0.4 s over ssh).
CHECK_TIMEOUT = 20


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
        f'if [ -n "$busy" ]; then echo "Port {ready.port} is already in use:" >&2; '
        f'echo "$busy" >&2; exit 1; fi; '
        f'echo "Port {ready.port} is free."'
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
        f'echo "{ready.process} is no longer running: see its log." >&2; exit 1; fi; '
        f"if ss -ltnpH {sport} | grep -qF {owner} "
        f"&& curl -fs -o /dev/null --max-time 5 {url}; then "
        f"echo Ready: {url}; exit 0; fi; "
        "sleep 2; done"
    )


def run_steps(section: Section, variant: str) -> list[Step]:
    steps = []
    if section.stop_before_run and section.stop:
        steps.append(Step(section.stop, section.stop_timeout, check=False))
    ready = section.ready
    if ready:
        steps.append(Step(port_free_command(ready), 30,
                          label=f"checking that port {ready.port} is free"))
    steps.append(Step(section.variants[variant].command, section.run_timeout))
    if ready:
        steps.append(Step(wait_ready_command(ready), ready.timeout,
                          label=f"waiting for {ready.process} to answer on {ready.url}"))
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


@dataclass(frozen=True)
class Conflict:
    """A section listed in exclusive_with that is on ("on") or has a job running ("busy")."""

    section: Section
    reason: str

    def to_dict(self) -> dict:
        return {"id": self.section.id, "label": self.section.label, "reason": self.reason}


class Conflicting(Exception):
    def __init__(self, conflicts: list[Conflict]) -> None:
        super().__init__(", ".join(c.section.id for c in conflicts))
        self.conflicts = conflicts


class CheckFailed(Exception):
    """A `running` check answered neither 0 (on) nor 1 (off): Run cannot go ahead."""

    def __init__(self, section: Section, exit_code: int | None, output: list[str]) -> None:
        super().__init__(f"{section.id}: exit {exit_code}")
        self.section = section
        self.exit_code = exit_code
        self.output = output


class JobManager:
    def __init__(self, config: Config, runner: Runner, keep: int = 100) -> None:
        self.config = config
        self.runner = runner
        self.keep = keep
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._last: dict[tuple[str, str], Job] = {}
        self._tasks: set[asyncio.Task] = set()
        # Check + start of a Run of an exclusive section, one at a time: two Runs of
        # sections that exclude each other cannot both pass the check.
        self._exclusive = asyncio.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def last(self, tab_id: str, section_id: str) -> Job | None:
        return self._last.get((tab_id, section_id))

    def running_job(self, tab_id: str, section_id: str) -> Job | None:
        job = self._last.get((tab_id, section_id))
        return job if job and job.state is JobState.RUNNING else None

    async def conflicts(self, tab: Tab, section: Section) -> list[Conflict]:
        """The sections in section.exclusive_with that are on now, or have a job running."""
        found = []
        host = self.config.hosts[tab.host]
        for other_id in section.exclusive_with:
            other = tab.section(other_id)
            assert other is not None and other.running  # checked by Config
            if self.running_job(tab.id, other.id):
                found.append(Conflict(other, "busy"))
                continue
            argv = build_ssh_argv(self.config.ssh, host, other.running)
            exit_code, output = await self.runner.probe(argv, CHECK_TIMEOUT)
            log.info("check %s/%s running: exit %s", tab.id, other.id, exit_code)
            if exit_code == 0:
                found.append(Conflict(other, "on"))
            elif exit_code != 1:
                raise CheckFailed(other, exit_code, output)
        return found

    async def start_run(self, tab: Tab, section: Section, variant: str) -> Job:
        """start() for a Run, refused (Conflicting) while an exclusive section is on."""
        current = self.running_job(tab.id, section.id)
        if current:
            raise SectionBusy(current)
        steps = run_steps(section, variant)
        if not section.exclusive_with:
            return self.start(tab, section, "run", variant, steps)
        async with self._exclusive:
            conflicts = await self.conflicts(tab, section)
            if conflicts:
                raise Conflicting(conflicts)
            return self.start(tab, section, "run", variant, steps)

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
                    job.output.append(f"[timed out after {step.timeout:g} s: command killed]")
                    job.finish(JobState.TIMEOUT, None)
                    break
                if exit_code != 0:
                    if step.check:
                        job.finish(JobState.FAILED, exit_code)
                        break
                    job.output.append(f"[exit {exit_code}, ignored]")
            else:
                job.finish(JobState.SUCCEEDED, exit_code)
        except Exception as exc:  # e.g. the ssh binary is missing
            log.exception("job %s failed", job.id)
            job.output.append(f"[error: {exc}]")
            job.finish(JobState.FAILED, None)
        log.info("job %s: %s (exit %s)", job.id, job.state.value, job.exit_code)

    def _trim(self) -> None:
        latest = {j.id for j in self._last.values()}
        for job_id in list(self._jobs):
            if len(self._jobs) <= self.keep:
                break
            if job_id not in latest and self._jobs[job_id].state is not JobState.RUNNING:
                del self._jobs[job_id]
