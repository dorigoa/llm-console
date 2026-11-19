import asyncio
from pathlib import Path

import httpx
import pytest

from app.main import CSRF_HEADER, create_app

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class FakeRunner:
    """Records the ssh argv lists instead of running them.

    exit_codes: substring of the remote command -> exit code to return (None = timeout).
    raises:     substring of the remote command -> exception to raise.
    gate:       when set, every run waits for it (to keep a job running).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.exit_codes: dict[str, int | None] = {}
        self.raises: dict[str, Exception] = {}
        self.gate: asyncio.Event | None = None

    @property
    def remote_commands(self) -> list[str]:
        return [argv[-1] for argv, _ in self.calls]

    async def run(self, argv, timeout, on_output):
        self.calls.append((argv, timeout))
        on_output(f"fake output for {argv[-1]}")
        if self.gate is not None:
            await self.gate.wait()
        for needle, exc in self.raises.items():
            if needle in argv[-1]:
                raise exc
        for needle, code in self.exit_codes.items():
            if needle in argv[-1]:
                return code
        return 0


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_runner():
    return FakeRunner()


@pytest.fixture
async def client(fake_runner):
    app = create_app(REPO_CONFIG, runner=fake_runner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={CSRF_HEADER: "1"}) as c:
        yield c


async def wait_done(client, job_id: str) -> dict:
    for _ in range(500):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["state"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} still running")
