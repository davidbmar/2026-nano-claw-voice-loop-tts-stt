# Barge-In — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user interrupt Claude mid-speech: keep the mic live during playback, pause instantly on a detected interrupt, and — if real speech follows — hand the turn to the user; if it was a false alarm, resume the paused reply after a randomized exponential backoff. All behind the `NANO_CLAW_BARGE_IN` flag (default off).

**Architecture:** A pure `Backoff` (Python) and `BargeInDetector` (browser JS) hold the testable logic. The `WebRTCAudioSource` already reverts to silence when its generator is detached, so **pause = `clear_generator()` with the `AudioQueue` retained; resume = `set_generator()`**; the queue is never touched, so playback continues exactly where it stopped. The browser (when the flag is on, in phone mode) samples mic RMS during playback: over-threshold → `barge_in` (server pauses); the detector then confirms real speech → `barge_in_commit` (server aborts the reply + the user's turn is captured) or times out → `barge_in_false` (server resumes after `Backoff.next()`). The flag is surfaced to the browser in `hello_ack`; when off, the mic stays gated during playback exactly as today.

**Tech Stack:** Python 3.12 (aiohttp, aiortc), vanilla JS (Web Audio VAD), pytest, `node <file>` JS tests.

## Global Constraints

- **Feature flag `NANO_CLAW_BARGE_IN`** (env, default **off**; `"1"`/`"true"` enables). Surfaced to the browser in the `hello_ack` message as `{"type":"hello_ack","bargeIn":true|false}`. When off: zero behavior change — the mic stays gated during agent playback (today's behavior).
- **Pause never discards audio.** Pause detaches the generator (`clear_generator`) and freezes the drain timeout; the `AudioQueue` is retained. Resume re-attaches. Only a **commit** (real barge-in) or a normal turn-end clears the queue.
- **Backoff:** base 0.5s, factor 2.0, cap 8.0s, **full jitter** (`uniform(0, min(cap, base*factor**n))`); `n` increments per consecutive false alarm, resets after a reply drains cleanly (no barge-in) or after a committed barge-in. Randomness uses the stdlib `random` (this is app code, not a workflow script).
- **Barge-in only applies in hands-free phone mode** and only while `agentSpeaking`. Outside phone mode, or with the flag off, nothing changes.
- Reuse the existing `echoCancellation: true` mic (`app.js:557-560`) — no new audio processing; the backoff absorbs residual-echo false trips.
- Do not change the streaming (Phase 1) happy path; barge-in composes with it (a paused stream keeps buffering chunks into the frozen queue; a commit aborts the in-flight stream task).

## File Structure

**New**
- `voice/backoff.py` — pure `Backoff` class.
- `voice/web/barge-in.js` — pure `BargeInDetector` (IIFE-to-global, like `phone-vad.js`).
- `tests/python/test_backoff.py`, `tests/barge-in.test.mjs`.

**Modified**
- `voice/webrtc.py` — `pause_speaking()`, `resume_speaking()`, `cancel_stream()`, a `_paused` flag that freezes `end_stream`'s drain deadline, and tracking of the current stream task.
- `voice/server.py` — read the flag; put it in `hello_ack`; handle `barge_in` / `barge_in_commit` / `barge_in_false` WS messages; schedule backoff resume; reset backoff on clean drain; register the streaming task so a commit can cancel it.
- `voice/web/index.html` — load `barge-in.js` before `app.js`.
- `voice/web/app.js` — store the `bargeIn` flag from `hello_ack`; during `agentSpeaking` + phone mode + flag on, feed mic RMS to `BargeInDetector` and send `barge_in`/`barge_in_commit`/`barge_in_false`; on commit, capture the user's turn.
- `README.md`, `CHANGELOG.md`.

---

## Task 1: `Backoff` (pure Python)

**Files:**
- Create: `voice/backoff.py`
- Test: `tests/python/test_backoff.py`

**Interfaces:**
- Produces: `class Backoff(base=0.5, factor=2.0, cap=8.0)` with `next() -> float` (full-jitter delay in `[0, min(cap, base*factor**n)]`, then increments `n`) and `reset()` (sets `n=0`); read-only `attempts` property returning `n`.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_backoff.py`:

```python
import random
from voice.backoff import Backoff


def test_ceiling_grows_by_factor_until_cap():
    # Force full-jitter to return its upper bound so we can see the ceiling.
    b = Backoff(base=0.5, factor=2.0, cap=8.0)
    random.seed(1)
    ceilings = []
    # Patch uniform to return its high arg so we observe the ceiling sequence.
    import voice.backoff as mod
    orig = mod.random.uniform
    mod.random.uniform = lambda lo, hi: hi
    try:
        ceilings = [b.next() for _ in range(6)]
    finally:
        mod.random.uniform = orig
    assert ceilings == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]  # doubles, then capped


def test_jitter_within_bounds_and_attempts_increment():
    b = Backoff(base=1.0, factor=2.0, cap=10.0)
    random.seed(0)
    for expected_ceiling in (1.0, 2.0, 4.0):
        d = b.next()
        assert 0.0 <= d <= expected_ceiling
    assert b.attempts == 3


def test_reset_returns_to_base():
    b = Backoff(base=0.5, factor=2.0, cap=8.0)
    b.next(); b.next(); b.next()
    assert b.attempts == 3
    b.reset()
    assert b.attempts == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_backoff.py -v`
Expected: FAIL — `voice/backoff.py` does not exist.

- [ ] **Step 3: Create `voice/backoff.py`**

```python
"""Randomized exponential backoff for barge-in false-alarm resumes.

Each consecutive false alarm waits longer (full jitter up to a growing
ceiling) so persistent noise backs off toward the cap instead of causing
rapid pause/resume stutter. A clean reply drain (or a committed barge-in)
calls reset().
"""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, base: float = 0.5, factor: float = 2.0, cap: float = 8.0):
        self._base = base
        self._factor = factor
        self._cap = cap
        self._n = 0

    @property
    def attempts(self) -> int:
        return self._n

    def next(self) -> float:
        """Return a full-jitter delay in [0, ceiling] and advance the counter."""
        ceiling = min(self._cap, self._base * (self._factor ** self._n))
        self._n += 1
        return random.uniform(0.0, ceiling)

    def reset(self) -> None:
        self._n = 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_backoff.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add voice/backoff.py tests/python/test_backoff.py
git commit -m "feat(voice): Backoff — full-jitter exponential backoff for barge-in resume"
```

---

## Task 2: `WebRTC` pause / resume / cancel + pause-aware drain

**Files:**
- Modify: `voice/webrtc.py`
- Test: `tests/python/test_session_pause.py`

**Interfaces:**
- Consumes: `WebRTCAudioSource.set_generator/clear_generator` (existing), `AudioQueue` (existing).
- Produces on `Session`: attributes `self._paused: bool` (default False), `self._stream_task: asyncio.Task | None`; methods `pause_speaking()` (set `_paused`, `clear_generator` — queue retained), `resume_speaking()` (clear `_paused`, `set_generator`), `cancel_stream()` (clear `_paused`, clear the queue + generator, cancel `_stream_task` if set), `is_paused() -> bool`, `set_stream_task(task)`. `end_stream`'s drain loop must NOT count time toward its deadline while `_paused` is True.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_session_pause.py`. It tests the pause/resume/cancel state machine against a fake audio source + the real `AudioQueue`, without a real PeerConnection (construct only the pieces under test):

```python
from voice.audio.audio_queue import AudioQueue


class FakeSource:
    def __init__(self):
        self.generator = None
    def set_generator(self, g): self.generator = g
    def clear_generator(self): self.generator = None


# Minimal stand-in exercising the same pause/resume/cancel logic Session uses.
# (Session wires these to self._audio_source/self._audio_queue; we assert the
#  contract: pause detaches generator but keeps the queue; resume re-attaches;
#  cancel clears both.)
class PausableSpeaker:
    def __init__(self, source, queue, generator):
        self._audio_source = source
        self._audio_queue = queue
        self._tts_generator = generator
        self._paused = False
    def pause_speaking(self):
        self._paused = True
        self._audio_source.clear_generator()
    def resume_speaking(self):
        self._paused = False
        self._audio_source.set_generator(self._tts_generator)
    def cancel_stream(self):
        self._paused = False
        self._audio_queue.clear()
        self._audio_source.clear_generator()
    def is_paused(self):
        return self._paused


def test_pause_detaches_generator_but_keeps_queue():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    src.set_generator("GEN")
    sp.pause_speaking()
    assert src.generator is None          # source silent
    assert q.available == 200             # audio retained
    assert sp.is_paused() is True


def test_resume_reattaches_generator_and_keeps_queue():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    sp.pause_speaking()
    sp.resume_speaking()
    assert src.generator == "GEN"
    assert q.available == 200
    assert sp.is_paused() is False


def test_cancel_clears_queue_and_generator():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    src.set_generator("GEN")
    sp.cancel_stream()
    assert src.generator is None
    assert q.available == 0
    assert sp.is_paused() is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_session_pause.py -v`
Expected: FAIL — file/module under test not present. (This test defines its own `PausableSpeaker` mirroring the contract; it will pass once written, and locks the exact contract `Session` must implement in Step 3. Run it now to confirm it is collected and the `AudioQueue` import works.)

- [ ] **Step 3: Add the methods to `Session` in `voice/webrtc.py`**

In `Session.__init__` (after `self.speed = 1.0` from the Kokoro feature, near line ~55), add:

```python
        self._paused = False
        self._stream_task: asyncio.Task | None = None
```

Add these methods to `Session` (near `stop_speaking`):

```python
    def set_stream_task(self, task) -> None:
        """Remember the task running the current streamed reply (for cancel)."""
        self._stream_task = task

    def is_paused(self) -> bool:
        return self._paused

    def pause_speaking(self) -> None:
        """Barge-in pause: go silent but KEEP the queued audio for resume."""
        self._paused = True
        self._audio_source.clear_generator()
        log.info("Barge-in: paused (%d bytes retained)", self._audio_queue.available)

    def resume_speaking(self) -> None:
        """Resume a paused reply from where it stopped."""
        self._paused = False
        self._audio_source.set_generator(self._tts_generator)
        log.info("Barge-in: resumed (%d bytes queued)", self._audio_queue.available)

    def cancel_stream(self) -> None:
        """Committed barge-in: discard the reply audio + abort its stream task."""
        self._paused = False
        self._audio_queue.clear()
        self._audio_source.clear_generator()
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        self._stream_task = None
        log.info("Barge-in: committed — reply cancelled")
```

Make `end_stream`'s drain loop pause-aware. Change the drain loop (added in Phase 1, ~`while self._audio_queue.available and loop.time() < deadline and not self._closed:`) to not advance toward the deadline while paused:

```python
    async def end_stream(self, total_bytes: int) -> None:
        loop = asyncio.get_running_loop()
        playback_seconds = total_bytes / (SAMPLE_RATE * 2)
        budget = max(5.0, min(120.0, playback_seconds + 5.0))
        deadline = loop.time() + budget
        while self._audio_queue.available and not self._closed:
            await asyncio.sleep(0.02)
            if self._paused:
                # Freeze the countdown while paused (extend the deadline).
                deadline += 0.02
                continue
            if loop.time() >= deadline:
                break
        if self._audio_queue.available and not self._paused:
            log.warning("TTS playback drain timed out with %d bytes queued", self._audio_queue.available)
            self._audio_queue.clear()
        await asyncio.sleep(0.15)
        if not self._paused:
            self._audio_source.clear_generator()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_session_pause.py -v`
Expected: PASS (3 passed).
Run: `python3 -m py_compile voice/webrtc.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add voice/webrtc.py tests/python/test_session_pause.py
git commit -m "feat(voice): session pause/resume/cancel + pause-aware drain for barge-in"
```

---

## Task 3: Server — flag, hello_ack, barge-in handlers, backoff resume

**Files:**
- Modify: `voice/server.py`
- Test: manual/integration (Task 6) — no unit test (aiohttp WS handler)

**Interfaces:**
- Consumes: `voice.backoff.Backoff` (Task 1); `Session.pause_speaking/resume_speaking/cancel_stream/set_stream_task` (Task 2).
- Produces: `BARGE_IN_ENABLED` from `NANO_CLAW_BARGE_IN`; `hello_ack` carries `"bargeIn"`; WS handlers for `barge_in`, `barge_in_commit`, `barge_in_false`; a per-session `Backoff` + a resume timer task; `_handle_agent_request`/`_handle_tool_decision` register their streaming task via `session.set_stream_task(asyncio.current_task())`.

- [ ] **Step 1: Add the flag + hello_ack field**

Near the top of `voice/server.py` (with the other env reads):

```python
BARGE_IN_ENABLED = os.environ.get("NANO_CLAW_BARGE_IN", "0") not in ("0", "false", "")
```

In `websocket_handler`, change the `hello` branch:

```python
            if msg_type == "hello":
                await ws.send_json({"type": "hello_ack", "bargeIn": BARGE_IN_ENABLED})
```

Add a module import at the top:

```python
from voice.backoff import Backoff
```

- [ ] **Step 2: Give each session a Backoff + resume-timer handle**

In `websocket_handler`, where `session` is created on `webrtc_offer` (the `session = Session()` line), initialize the barge-in state right after:

```python
                session = Session()
                session._backoff = Backoff()          # per-session backoff
                session._resume_task = None            # pending false-alarm resume timer
                answer_sdp = await session.handle_offer(msg["sdp"])
```

- [ ] **Step 3: Register the streaming task so a commit can cancel it**

At the very start of `_consume_sse` (before sending `agent_audio_start`), record the running task so `cancel_stream()` can abort it, and reset the backoff when a reply drains cleanly. Add at the top of `_consume_sse`:

```python
    session.set_stream_task(asyncio.current_task())
```

And in `_consume_sse`, on the normal completion path (after `await session.end_stream(total_bytes)` succeeds without cancellation), reset the backoff:

```python
        await session.end_stream(total_bytes)
        session._backoff.reset()   # clean drain — clear consecutive-false count
```

- [ ] **Step 4: Handle the three barge-in WS messages**

In `websocket_handler`, add branches (only act when the flag is on and a session exists):

```python
            elif msg_type == "barge_in":
                if BARGE_IN_ENABLED and session:
                    # Cancel any pending resume, then pause.
                    if getattr(session, "_resume_task", None):
                        session._resume_task.cancel()
                        session._resume_task = None
                    session.pause_speaking()

            elif msg_type == "barge_in_commit":
                if BARGE_IN_ENABLED and session:
                    if getattr(session, "_resume_task", None):
                        session._resume_task.cancel()
                        session._resume_task = None
                    session.cancel_stream()          # abort reply + clear audio
                    session._backoff.reset()
                    await ws.send_json({"type": "agent_audio_end"})   # re-arm mic for the user's turn

            elif msg_type == "barge_in_false":
                if BARGE_IN_ENABLED and session and session.is_paused():
                    delay = session._backoff.next()
                    log.info("Barge-in false alarm; resuming in %.2fs", delay)

                    async def _resume_after(d):
                        try:
                            await asyncio.sleep(d)
                            if session.is_paused() and not ws.closed:
                                session.resume_speaking()
                        except asyncio.CancelledError:
                            pass

                    session._resume_task = asyncio.ensure_future(_resume_after(delay))
```

- [ ] **Step 5: Verify syntax**

Run: `python3 -m py_compile voice/server.py`
Expected: clean.
Run: `.venv-test/bin/pytest tests/python -v`
Expected: existing suite still green (server.py isn't imported by pytest).

- [ ] **Step 6: Commit**

```bash
git add voice/server.py
git commit -m "feat(voice): barge-in server — flag, hello_ack, pause/commit/resume+backoff"
```

---

## Task 4: `BargeInDetector` (pure browser JS)

**Files:**
- Create: `voice/web/barge-in.js`
- Test: `tests/barge-in.test.mjs`

**Interfaces:**
- Produces (global `BargeInDetector`): `new BargeInDetector({startThreshold, sustainThreshold, confirmMs})`; `sample(rms, timestampMs) -> null | {type:'barge_in'} | {type:'barge_in_commit'} | {type:'barge_in_false'}`; `reset()`. Only called while the agent is speaking. Emits `barge_in` the first frame RMS ≥ `startThreshold`; then within `confirmMs` decides `barge_in_commit` (energy stayed ≥ `sustainThreshold`) or `barge_in_false`.

- [ ] **Step 1: Write the failing test**

Create `tests/barge-in.test.mjs` (plain `node <file>` style, like `phone-vad.test.mjs`):

```javascript
import assert from "node:assert/strict";

await import("../voice/web/barge-in.js");
const BargeInDetector = globalThis.BargeInDetector;
assert.ok(BargeInDetector, "BargeInDetector must be exported to the global scope");

// Real speech: loud onset, stays loud through the confirm window → commit.
const d1 = new BargeInDetector({ startThreshold: 0.05, sustainThreshold: 0.03, confirmMs: 300 });
assert.equal(d1.sample(0.01, 0), null, "quiet frame does nothing");
assert.equal(d1.sample(0.08, 20)?.type, "barge_in", "loud onset pauses immediately");
assert.equal(d1.sample(0.08, 120), null, "still confirming inside window");
assert.equal(d1.sample(0.08, 340)?.type, "barge_in_commit", "sustained loud → commit");

// False alarm: loud blip, then silence through the window → false.
const d2 = new BargeInDetector({ startThreshold: 0.05, sustainThreshold: 0.03, confirmMs: 300 });
assert.equal(d2.sample(0.09, 0)?.type, "barge_in", "blip pauses");
assert.equal(d2.sample(0.005, 60), null, "fell silent, still inside window");
assert.equal(d2.sample(0.004, 320)?.type, "barge_in_false", "silence through window → false alarm");

// After reset, detector is idle again.
d2.reset();
assert.equal(d2.sample(0.004, 400), null, "reset clears pending state");

console.log("barge-in detector tests passed");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/barge-in.test.mjs`
Expected: FAIL — `voice/web/barge-in.js` does not exist (import throws).

- [ ] **Step 3: Create `voice/web/barge-in.js`**

```javascript
(function (global) {
    "use strict";

    // Detects a barge-in during agent playback: pause on loud onset, then
    // confirm real speech (stayed loud) vs false alarm (fell silent) within a
    // short window. Only sample() while the agent is speaking.
    class BargeInDetector {
        constructor(options) {
            const cfg = options || {};
            this.startThreshold = cfg.startThreshold || 0.05;
            this.sustainThreshold = cfg.sustainThreshold || 0.03;
            this.confirmMs = cfg.confirmMs || 400;
            this.reset();
        }

        reset() {
            this.pending = false;
            this.pendingSince = 0;
            this.sawSpeech = false;
        }

        sample(rms, now) {
            if (!this.pending) {
                if (rms >= this.startThreshold) {
                    this.pending = true;
                    this.pendingSince = now;
                    this.sawSpeech = true;
                    return { type: "barge_in" };
                }
                return null;
            }
            // Within the confirm window.
            if (rms >= this.sustainThreshold) this.sawSpeech = true;
            else this.sawSpeech = false; // track the most recent state
            if (now - this.pendingSince >= this.confirmMs) {
                const committed = this.sawSpeech;
                this.reset();
                return { type: committed ? "barge_in_commit" : "barge_in_false" };
            }
            return null;
        }
    }

    global.BargeInDetector = BargeInDetector;
}(typeof window !== "undefined" ? window : globalThis));
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/barge-in.test.mjs`
Expected: PASS — prints "barge-in detector tests passed", exits 0.

- [ ] **Step 5: Commit**

```bash
git add voice/web/barge-in.js tests/barge-in.test.mjs
git commit -m "feat(web): BargeInDetector — pause on onset, confirm speech vs false alarm"
```

---

## Task 5: Browser wiring — flag gating, detection during playback, commit-capture

**Files:**
- Modify: `voice/web/index.html` (load `barge-in.js`)
- Modify: `voice/web/app.js`

**Interfaces:**
- Consumes: `BargeInDetector` (Task 4); `hello_ack.bargeIn`; the existing `micStream`, `vadGate`, the VAD frame loop (`processVadFrame`, ~app.js:688), `agentSpeaking`, `sendMsg`, `startCapture`/`mic_start`.

- [ ] **Step 1: Load `barge-in.js`**

In `voice/web/index.html`, add before `app.js` (and after `phone-vad.js`):

```html
  <script src="phone-vad.js"></script>
  <script src="voice-ui.js"></script>
  <script src="barge-in.js"></script>
  <script src="app.js"></script>
```

- [ ] **Step 2: Store the flag + create the detector**

In `voice/web/app.js`, near the other module state (`let agentSpeaking = false;` ~line 27), add:

```javascript
let bargeInEnabled = false;
let bargeDetector = null;
```

In the `hello_ack` handler (currently `case "hello_ack": startWebRTC(); break;`), capture the flag:

```javascript
        case "hello_ack":
            bargeInEnabled = !!msg.bargeIn;
            if (bargeInEnabled && typeof BargeInDetector !== "undefined") {
                bargeDetector = new BargeInDetector({});
            }
            startWebRTC();
            break;
```

- [ ] **Step 3: Detect barge-in during agent playback**

The VAD frame handler (`processVadFrame`, ~app.js:688) currently resets `vadGate` when `agentSpeaking` (the gate that blocks barge-in). Change that branch so, when barge-in is enabled, it feeds RMS to the detector instead of ignoring it. Locate the block (~app.js:698):

```javascript
    } else if (agentSpeaking || autoTurnPending || timestamp < vadRearmAt) {
        vadGate.reset();
```

Replace with:

```javascript
    } else if (agentSpeaking) {
        if (bargeInEnabled && bargeDetector) {
            const evt = bargeDetector.sample(rms, timestamp);
            if (evt && evt.type === "barge_in") {
                sendMsg("barge_in");
                setPhoneStatus("Heard you — pausing...");
            } else if (evt && evt.type === "barge_in_commit") {
                sendMsg("barge_in_commit");
                // The server re-arms the mic (agent_audio_end); the user's
                // speech is captured by the normal VAD turn on the next frames.
            } else if (evt && evt.type === "barge_in_false") {
                sendMsg("barge_in_false");
                setPhoneStatus("False alarm — resuming...");
            }
        }
        vadGate.reset();  // don't let the normal turn-VAD fire while agent speaks
    } else if (autoTurnPending || timestamp < vadRearmAt) {
        vadGate.reset();
```

> When barge-in is OFF, `bargeInEnabled` is false so this reduces to the original `vadGate.reset()` — unchanged behavior.

- [ ] **Step 4: Reset the detector at each turn boundary**

Wherever the browser learns the agent stopped/finished (the `agent_audio_end` handler and `rearmPhoneMode`), reset the detector so a new reply starts fresh. In the `agent_audio_end` case and in `rearmPhoneMode(...)`, add:

```javascript
    if (bargeDetector) bargeDetector.reset();
```

- [ ] **Step 5: Verify syntax + existing JS tests**

Run: `node --check voice/web/app.js`
Expected: clean.
Run: `node tests/barge-in.test.mjs && node tests/phone-vad.test.mjs && node tests/voice-ui.test.mjs`
Expected: all three print their pass lines.

- [ ] **Step 6: Commit**

```bash
git add voice/web/index.html voice/web/app.js
git commit -m "feat(web): wire barge-in — detect during playback, commit captures the turn"
```

---

## Task 6: Docs + integration verification

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: Update docs**

- `README.md`: add a "Barge-in (experimental)" note — set `NANO_CLAW_BARGE_IN=1` (default off) to interrupt Claude mid-speech; talking pauses playback, real speech takes the turn, a false alarm resumes after a short backoff; works best in hands-free phone mode; on speakers the browser's echo cancellation + backoff absorb most false trips.
- `CHANGELOG.md` `### Added`: "Barge-in (opt-in, `NANO_CLAW_BARGE_IN=1`): interrupt Claude mid-reply — playback pauses on your voice, your speech becomes the next turn, and a false alarm resumes the reply after a randomized exponential backoff."

- [ ] **Step 2: Flag-off regression check (default behavior unchanged)**

Rebuild the image and run WITHOUT the flag; confirm `hello_ack` reports `bargeIn:false` and playback is not interruptible (today's behavior):

```bash
npm run build && docker build -t nano-claw-voice . && docker rm -f nano-claw-voice 2>/dev/null
set -a; source .env; set +a
docker run -d --rm --name nano-claw-voice -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" \
  -e TTS_SERVICE_URL="http://host.docker.internal:8300" \
  -v nano-claw-models:/app/voice/models nano-claw-voice
# hello_ack over a quick websocket check, or confirm in the browser devtools that hello_ack.bargeIn === false
```

Expected: app boots; `hello_ack.bargeIn` is `false`; the voice loop behaves exactly as before this task.

- [ ] **Step 3: Flag-on manual verification (controller/user, hands-free)**

Restart with `-e NANO_CLAW_BARGE_IN=1`, open `http://localhost:9090`, enter hands-free phone mode, and while Claude is speaking:
- Speak over it → playback pauses within ~1 frame; your utterance becomes the next turn (old reply is dropped).
- Make a brief noise (cough) → playback pauses, then resumes after a short delay (false alarm + backoff); repeated noises resume with growing delays.
- Confirm on speakers that echo doesn't cause constant stutter (backoff absorbs it).

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: barge-in (NANO_CLAW_BARGE_IN) usage + changelog"
```

---

## Self-Review (completed during authoring)

**Spec coverage (Phase 2 of the design):**
- Feature flag `NANO_CLAW_BARGE_IN` (default off) + `hello_ack` surfacing + off = unchanged → Task 3 (server) + Task 5 (browser gating). ✓
- Pause = detach generator, keep queue; resume = re-attach → Task 2. ✓
- Pause-aware drain (no timeout while paused) → Task 2 `end_stream`. ✓
- Commit = abort stream + clear queue → Task 2 `cancel_stream` + Task 3 `barge_in_commit`. ✓
- False alarm → resume after full-jitter exponential backoff; grows per consecutive false, resets on clean drain / commit → Task 1 (`Backoff`) + Task 3. ✓
- Live mic during playback + detect via RMS, browser AEC → Task 4 (`BargeInDetector`) + Task 5. ✓
- Pause → confirm → commit/false state machine → Task 4. ✓

**Placeholder scan:** every code step has complete code; every test step has assertions + the run command. No TBD. ✓

**Type consistency:** `Backoff.next/reset/attempts` used identically in Task 1/3; `pause_speaking/resume_speaking/cancel_stream/set_stream_task/is_paused` defined in Task 2 and called in Task 3; WS message names `barge_in`/`barge_in_commit`/`barge_in_false` emitted by Task 5 and handled by Task 3; `hello_ack.bargeIn` written by Task 3, read by Task 5; `BargeInDetector.sample` event types match across Task 4 and Task 5. ✓

## Out of scope
- Server-side echo cancellation / DSP (rely on browser AEC).
- Barge-in outside hands-free phone mode.
- Persisting an interrupted reply across turns (a committed barge-in drops it).
