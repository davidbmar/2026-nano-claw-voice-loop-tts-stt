import pytest

from voice import kokoro_client


class _FakeResponse:
    def __init__(self, content, headers, status=200):
        self.content = content
        self.headers = headers
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._response


def test_synthesize_parses_pcm_and_rate(monkeypatch):
    pcm = b"\x01\x00\x02\x00"
    resp = _FakeResponse(pcm, {"X-Sample-Rate": "24000"})
    monkeypatch.setattr(kokoro_client.httpx, "Client", lambda *a, **k: _FakeClient(response=resp))
    out, rate = kokoro_client.synthesize("hi", "af_heart", 1.0)
    assert out == pcm
    assert rate == 24000


def test_synthesize_raises_kokoro_unavailable_on_error(monkeypatch):
    monkeypatch.setattr(
        kokoro_client.httpx, "Client",
        lambda *a, **k: _FakeClient(exc=Exception("connection refused")),
    )
    with pytest.raises(kokoro_client.KokoroUnavailable):
        kokoro_client.synthesize("hi", "af_heart", 1.0)
