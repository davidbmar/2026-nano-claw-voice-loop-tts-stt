import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")
pytest.importorskip("scipy")

_spec = importlib.util.spec_from_file_location(
    "stt_server", Path(__file__).resolve().parents[2] / "stt-service" / "server.py"
)
# Import without starting uvicorn (module guards run under __main__).
stt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt)


def test_sizes_list():
    assert stt.SIZES == ["tiny", "base", "small", "medium"]


def test_valid_size():
    assert stt._valid_size("small") is True
    assert stt._valid_size("huge") is False
    assert stt._valid_size("") is False
