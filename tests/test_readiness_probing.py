"""A readiness probe must not decide how often the database is woken.

A 15-second blackbox scrape opened 5,760 SQL connections a day, and serverless
Azure SQL bills vCore-seconds for being online rather than for doing work. The
free grant is roughly 28 vCore-hours a month, so probing alone consumed it.
"""

import asyncio

import pytest

from app.api import health


@pytest.fixture(autouse=True)
def reset_probe_state():
    health._LAST_SQL_PROBE = (0.0, False)
    yield
    health._LAST_SQL_PROBE = (0.0, False)


class _Settings:
    def __init__(self, *, verify: bool = True, interval: int = 300) -> None:
        self.readiness_verify_database = verify
        self.readiness_database_interval_seconds = interval


@pytest.fixture
def counted(monkeypatch):
    calls = {"count": 0, "fail": False}

    async def fake_probe() -> None:
        calls["count"] += 1
        if calls["fail"]:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(health, "_verify_database", fake_probe)
    return calls


def test_repeated_probes_open_one_connection(counted):
    async def run():
        for _ in range(20):
            await health._verify_database_readiness(_Settings())

    asyncio.run(run())
    assert counted["count"] == 1


def test_a_failure_keeps_being_reported_without_reconnecting(counted):
    counted["fail"] = True

    async def run():
        with pytest.raises(Exception):
            await health._verify_database_readiness(_Settings())
        # Still failing, but the window has not elapsed: report it, do not
        # silently pass, and do not open another connection either.
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await health._verify_database_readiness(_Settings())

    asyncio.run(run())
    assert counted["count"] == 1


def test_disabling_verification_opens_no_connection_at_all(counted):
    async def run():
        for _ in range(10):
            await health._verify_database_readiness(_Settings(verify=False))

    asyncio.run(run())
    assert counted["count"] == 0


def test_a_zero_interval_probes_every_time(counted):
    # The escape hatch for a paid database where freshness matters more than cost.
    async def run():
        for _ in range(3):
            await health._verify_database_readiness(_Settings(interval=0))

    asyncio.run(run())
    assert counted["count"] == 3
