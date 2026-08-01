import pytest


@pytest.fixture(autouse=True)
def _tap_off_by_default(monkeypatch, tmp_path):
    # Call recording defaults ON in production; tests must opt in explicitly
    # so unrelated suites never write tap files to the real filesystem.
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP", "0")
    monkeypatch.setenv("NANO_CLAW_PHONE_TAP_DIR", str(tmp_path / "phone-taps"))
