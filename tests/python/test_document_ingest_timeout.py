"""Large documents, and the timeout that made the first one fail.

The IRS 1040 instructions are 630k characters. Ingesting them took just over a
minute, against a flat 60-second client timeout — so the upload reported a
failure while the platform quietly finished the work, leaving a document that
was indexed and searchable but which the registry had no id for and could
therefore never delete.

Both halves are tested here: the timeout has to scale with the text, and a
timeout that happens anyway has to be retried rather than recorded as a loss.
"""

from __future__ import annotations

import httpx
import pytest

from voice.documents import DocumentError, PlatformClient


def test_the_timeout_scales_with_the_document():
    small = PlatformClient.ingest_timeout(2_000)
    irs = PlatformClient.ingest_timeout(630_221)
    assert small == 120.0, "a one-page form still gets a sane floor"
    assert irs > 120.0, "630k characters needs longer than the floor"
    # The real ingest took ~65s; the allowance must clear that with room to
    # spare, because the platform shares its database with live retrieval.
    assert irs >= 126.0
    assert PlatformClient.ingest_timeout(50_000_000) == 900.0, "and it is capped"


class _Recorder:
    """Times out the first attempt, succeeds on the second."""

    def __init__(self, *, always_timeout: bool = False) -> None:
        self.attempts: list[float] = []
        self.always_timeout = always_timeout

    async def __call__(self, payload, timeout):
        self.attempts.append(timeout)
        if self.always_timeout or len(self.attempts) == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(
            200, json={"document_id": "doc::healed", "created": False}
        )


async def test_a_timeout_is_retried_because_the_platform_may_have_finished():
    # document_key is deterministic, so re-sending identical content returns
    # the same document_id rather than making a second copy. The retry heals
    # the orphan instead of compounding it.
    client = PlatformClient(base_url="http://platform.test")
    recorder = _Recorder()
    client._post_ingest = recorder

    document_id = await client.ingest_text(
        document_key="space:doc", title="t", text="x" * 630_000,
        collection_id="space", source_ref="nano-claw://documents/doc",
    )

    assert document_id == "doc::healed"
    assert len(recorder.attempts) == 2
    assert recorder.attempts[1] > recorder.attempts[0], "the retry waits longer"


async def test_a_second_timeout_says_it_may_still_land():
    # Telling the customer it simply failed would be wrong: the platform is
    # very likely still working, and re-uploading is safe but not required.
    client = PlatformClient(base_url="http://platform.test")
    client._post_ingest = _Recorder(always_timeout=True)

    with pytest.raises(DocumentError) as excinfo:
        await client.ingest_text(
            document_key="space:doc", title="t", text="x" * 630_000,
            collection_id="space", source_ref="ref",
        )

    assert excinfo.value.status == 504
    assert "background" in excinfo.value.message


async def test_a_normal_upload_still_makes_exactly_one_request():
    client = PlatformClient(base_url="http://platform.test")
    recorder = _Recorder()
    recorder.always_timeout = False
    recorder.attempts.append(0.0)  # pretend the first attempt already happened

    calls = []

    async def ok(payload, timeout):
        calls.append(timeout)
        return httpx.Response(200, json={"document_id": "doc::a"})

    client._post_ingest = ok
    assert await client.ingest_text(
        document_key="k", title="t", text="a short document",
        collection_id="c", source_ref="r",
    ) == "doc::a"
    assert len(calls) == 1
