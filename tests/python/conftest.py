import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests without depending on pytest-asyncio.

    Without this an async test is collected, never awaited, and reported as a
    pass — the worst possible outcome. Sync tests are untouched.
    """

    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments = {
        name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(pyfuncitem.obj(**arguments))
    return True


@pytest.fixture(autouse=True)
def _tap_off_by_default(monkeypatch, tmp_path):
    # Call recording defaults ON in production; tests must opt in explicitly
    # so unrelated suites never write tap files to the real filesystem.
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path / "phone-taps"))
