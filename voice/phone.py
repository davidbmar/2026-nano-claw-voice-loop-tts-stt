"""Telnyx phone gateway: callers dial in and talk to the nano-claw agent.

Call flow (mirrors riff's proven shape, minus the flow engine):

    caller → Telnyx Call Control app → POST /api/phone/incoming (webhook)
           → answer_with_streaming() → Telnyx opens WS to /ws/phone-media
           → PCMU 8k or L16 16k frames in → UtteranceEndpointer → STT service
           → nano-claw /api/chat (knowledge persona, tools disabled)
           → TTS 48k PCM → configured phone codec → caller hears the answer

Enabled only when NANO_CLAW_PHONE=1. Required env:
    TELNYX_API_KEY                  answer/hangup Call Control commands
    NANO_CLAW_PHONE_WEBHOOK_BASE    public https base (e.g. https://nano.example.com)
    NANO_CLAW_PHONE_TOKEN           shared secret segment in webhook/media URLs;
                                    requests without it are rejected (we do not
                                    verify Telnyx Ed25519 signatures yet — the
                                    token-in-URL is the auth boundary)
Optional:
    NANO_CLAW_PHONE_GREETING        spoken on answer
    NANO_CLAW_PHONE_VOICE           TTS voice id (default af_heart; use a
                                    Piper voice on nodes where Kokoro/MPS
                                    is slow or unstable)
    NANO_CLAW_PHONE_STT_SIZE        Whisper size for phone turns (default
                                    base; "tiny" for low-powered nodes)
    NANO_CLAW_PHONE_STT_STREAM      1 = transcribe incrementally while the
                                    caller speaks; unset = one-shot STT
    NANO_CLAW_PHONE_CODEC           pcmu (default) or l16 (16 kHz wideband)
    NANO_CLAW_PHONE_RMS_MIN         minimum energy endpoint threshold
    NANO_CLAW_PHONE_RMS_RATIO       noise-floor multiplier for endpointing
    NANO_CLAW_PHONE_GAIN            off = bypass outbound peak normalization
    NANO_CLAW_PHONE_GAIN_TARGET_DB  target peak dBFS (default -3)
    NANO_CLAW_PHONE_PREBUFFER_MS    initial unpaced audio burst (default 200)
    NANO_CLAW_PHONE_PACE_FACTOR     frame interval multiplier (default 1.0)
    NANO_CLAW_PHONE_BARGE_IN        1 = caller can interrupt the agent
                                    mid-speech (buffer-flush via Telnyx
                                    "clear"); unset = half-duplex
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from aiohttp import web

from voice import call_log, metrics_db, silero_vad, voice_catalog
from voice.flow_session import (
    FLOW_MODES,
    FlowSession,
    active_scheduling_domain,
    flow_mode_greeting,
    get_flow_mode,
    get_flow_profile,
    set_flow_mode,
)
from voice.phone_audio import (
    DEFAULT_BARGE_TRIGGER_MS,
    DEFAULT_BARGE_VAD_ENTER,
    DEFAULT_ECHO_CORRELATION,
    FRAME_MS,
    BargeInDetector,
    DEFAULT_PHONE_GAIN_TARGET_DBFS,
    EchoReferenceGate,
    SentencePeakNormalizer,
    UtteranceEndpointer,
    pcm48k_to_l16_frames,
    pcm48k_to_ulaw_frames,
    transcript_looks_incomplete,
    ulaw_decode,
)
from voice.phone_tap import DEFAULT_TAP_ROOT, CallTap, tap_directory_for
from voice.processing_audio import processing_chime, thinking_tick
from voice.sentence_pipeline import SentencePipeline
from voice.speech_preparer import (
    SPEECH_COMPILER_VERSION,
    SpeechChunk,
    StreamingSpeechCompiler,
    compile_speech,
)
from voice.streaming_stt import StreamingSTTSession
from voice.text_chunker import TextChunker
from voice.tts import is_cached as tts_is_cached
from voice.tts import synthesize as tts_synthesize

log = logging.getLogger("nano-claw.phone")

NANO_CLAW_URL = os.environ.get("NANO_CLAW_URL", "http://localhost:3001")
TELNYX_API = "https://api.telnyx.com/v2"

# Back-compat alias; the live greeting now follows the active mode so the
# intro can never promise a persona the brain isn't running.
DEFAULT_GREETING = flow_mode_greeting("spacechannel")
IDLE_PROMPT_TEXT = "Hi — are you still there?"
IDLE_GOODBYE_TEXT = "It sounds like you've stepped away. Thanks for calling — goodbye!"
DEFAULT_RECORD_NOTICE = "This call may be recorded for quality and training."
MAX_BUFFERED_INBOUND_FRAMES = 30_000 // FRAME_MS
FRAME_S = FRAME_MS / 1000.0
DEFAULT_PHONE_PREBUFFER_MS = 200.0
DEFAULT_PHONE_PACE_FACTOR = 1.0
PROCESSING_CUE_SENTINEL = "\0nano-claw-processing-cue\0"
THINKING_TICK_INTERVAL_S = 0.5  # cadence of the thinking-cue clock tick
_FLOW_NOT_SUPPLIED = object()


class FramePacer:
    """Anchor real-time frame sends to monotonic absolute deadlines.

    Relative sleeps add each send's work and scheduler oversleep to every
    later frame, so jitter becomes permanent drift.  This pacer advances one
    absolute deadline by ``frame_s * pace_factor`` per frame; a late wake-up
    therefore shortens or skips following sleeps until the schedule catches
    up.

    The old phone loop used a 0.9 interval to keep Telnyx fed, but that made
    buffered surplus grow throughout a reply.  ``prebuffer_ms`` supplies the
    same safety headroom once, immediately after :meth:`reset`, while a 1.0
    factor holds the buffer steady.  Reuse one reset pacer for every sentence
    in a reply so sentence boundaries cannot trigger another prebuffer burst.
    """

    def __init__(
        self,
        frame_s: float = FRAME_S,
        *,
        prebuffer_ms: float = DEFAULT_PHONE_PREBUFFER_MS,
        pace_factor: float = DEFAULT_PHONE_PACE_FACTOR,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(frame_s) or frame_s <= 0.0:
            raise ValueError("frame_s must be finite and positive")
        if not math.isfinite(prebuffer_ms) or prebuffer_ms < 0.0:
            raise ValueError("prebuffer_ms must be finite and non-negative")
        if not math.isfinite(pace_factor) or pace_factor <= 0.0:
            raise ValueError("pace_factor must be finite and positive")
        self.frame_s = float(frame_s)
        self.prebuffer_ms = float(prebuffer_ms)
        self.pace_factor = float(pace_factor)
        self._clock = clock or time.monotonic
        self._deadline: float | None = None

    @property
    def running(self) -> bool:
        """Whether :meth:`reset` has anchored this reply's schedule."""
        return self._deadline is not None

    def reset(self) -> None:
        """Anchor a new reply and make its configured audio headroom due now."""
        prebuffer_s = self.prebuffer_ms / 1000.0
        self._deadline = self._clock() - prebuffer_s * self.pace_factor

    def now(self) -> float:
        """Read the monotonic clock used by this deadline sequence."""
        return self._clock()

    def next_deadline(self) -> float:
        """Return the next frame's absolute monotonic send deadline."""
        if self._deadline is None:
            raise RuntimeError("FramePacer.reset() must be called before pacing")
        self._deadline += self.frame_s * self.pace_factor
        return self._deadline


@dataclass(frozen=True)
class _SynthesizedSpeech:
    pcm48k: bytes
    tap: CallTap | None
    sentence_index: int | None


def idle_action(idle_s: float, prompted: bool, prompt_after_s: float) -> str:
    """Pure idle-policy decision: '', 'prompt', or 'hangup'.

    One prompt per silence stretch; a further full stretch after the prompt
    (still nothing) means the caller is gone.
    """
    if idle_s < prompt_after_s:
        return ""
    return "hangup" if prompted else "prompt"


# Runtime overrides set from the web UI (/api/phone/config). Checked before
# the environment so changes apply live — voice mid-call on the next sentence,
# model on the next turn. Persisted to NANO_CLAW_PHONE_SETTINGS_PATH on the
# data volume so console choices survive container restarts; .env stays the
# factory default underneath.
_overrides: dict[str, str] = {}

# The only keys the settings file may carry — anything else in the file is
# ignored on load and never written, so the file can't smuggle e.g. a token.
_SETTINGS_KEYS = (
    "NANO_CLAW_PHONE_VOICE",
    "NANO_CLAW_PHONE_MODEL",
    "NANO_CLAW_PHONE_SPEED",
    "NANO_CLAW_PHONE_STT_SIZE",
    "NANO_CLAW_PHONE_SPEECH_PREPARATION",
    "NANO_CLAW_PHONE_VAD",
    "NANO_CLAW_VOICE_FLOW",
)


def _cfg(name: str, default: str = "") -> str:
    if name in _overrides:
        return _overrides[name].strip()
    return os.environ.get(name, default).strip()


def _settings_path() -> Path:
    return Path(
        os.environ.get(
            "NANO_CLAW_PHONE_SETTINGS_PATH", "/app/data/phone-settings.json"
        )
    )


def _valid_setting(name: str, value: str) -> bool:
    """Shared by the config POST handler and the boot-time settings loader."""
    if name == "NANO_CLAW_PHONE_VOICE":
        return voice_catalog.lookup(value) is not None
    if name == "NANO_CLAW_PHONE_MODEL":
        return bool(value.strip())
    if name == "NANO_CLAW_PHONE_SPEED":
        try:
            return 0.5 <= float(value) <= 2.0
        except (TypeError, ValueError):
            return False
    if name == "NANO_CLAW_PHONE_STT_SIZE":
        return value in ("tiny", "base", "small", "medium")
    if name == "NANO_CLAW_PHONE_SPEECH_PREPARATION":
        return value in ("1", "raw", "batch")
    if name == "NANO_CLAW_PHONE_VAD":
        return value in VAD_MODES
    if name == "NANO_CLAW_VOICE_FLOW":
        return value in FLOW_MODES
    return False


def persist_runtime_setting(name: str, value: str) -> None:
    """Write-through for line settings owned by other modules (e.g. the
    console MODE selector in voice/server.py) so they survive restarts the
    same way the phone settings do. Silently ignores unknown or invalid
    values — persistence must never break the setter that calls it."""
    if name not in _SETTINGS_KEYS or not _valid_setting(name, str(value)):
        return
    _overrides[name] = str(value)
    _persist_overrides()


def _persist_overrides() -> None:
    """Write-through of console overrides; best-effort (dev boxes lack the
    data volume). A key absent from the file is a deliberate reset to .env."""
    path = _settings_path()
    payload = {key: _overrides[key] for key in _SETTINGS_KEYS if key in _overrides}
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("phone settings not persisted (%s unavailable)", path)


def _load_persisted_overrides() -> None:
    """Restore console settings at boot. Unknown keys and invalid values are
    dropped with a warning; any failure leaves the override map untouched so
    a corrupt file can never brick call handling."""
    path = _settings_path()
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        log.warning("phone settings file %s unreadable — ignored", path)
        return
    if not isinstance(data, dict):
        log.warning("phone settings file %s is not a JSON object — ignored", path)
        return
    for name, value in data.items():
        if name not in _SETTINGS_KEYS:
            log.warning("phone settings: unknown key %s ignored", name)
            continue
        value = str(value)
        if not _valid_setting(name, value):
            log.warning("phone settings: invalid %s=%r dropped", name, value)
            continue
        _overrides[name] = value
    # The console MODE selector persists here too; re-apply it so a restart
    # boots into the mode the user last chose, not the .env factory default
    # (07-27 bug: Space Channel intro over Document Intelligence answers).
    persisted_mode = _overrides.get("NANO_CLAW_VOICE_FLOW")
    if persisted_mode:
        set_flow_mode(persisted_mode)
    if _overrides:
        log.info(
            "phone settings restored from %s: %s",
            path,
            ", ".join(sorted(k for k in _overrides if k in _SETTINGS_KEYS)),
        )


def _compose_greeting(base: str) -> str:
    """Append the recording disclosure to the greeting.

    Calls are recorded for the review panel, so the caller must hear a
    disclosure up front. ``NANO_CLAW_PHONE_RECORD_NOTICE`` overrides the
    spoken line; ``off``/``0`` restores the plain greeting (for deployments
    whose owner has not approved the line — recording stays on regardless).
    """
    notice = _cfg("NANO_CLAW_PHONE_RECORD_NOTICE", DEFAULT_RECORD_NOTICE)
    if notice.lower() in ("off", "0", ""):
        return base
    return f"{base} {notice}"


_config_fallback_warned: set[str] = set()


def _warn_config_fallback(name: str, raw: object, default: object) -> None:
    """Warn ONCE per setting when unparseable config falls back to a default.

    Silently ignored operator config is a support trap (a typo'd .env value
    "applies" in the operator's head but never in the process). Once per
    name keeps hot paths (per-sentence synthesis) from spamming."""
    if name in _config_fallback_warned:
        return
    _config_fallback_warned.add(name)
    log.warning("Ignoring unparseable %s=%r; using default %r", name, raw, default)


def _phone_pacing_value(name: str, default: float, *, allow_zero: bool) -> float:
    """Read one finite pacing value, falling back on unsafe input."""
    raw = _cfg(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        _warn_config_fallback(name, raw, default)
        return default
    minimum_ok = value >= 0.0 if allow_zero else value > 0.0
    if not (math.isfinite(value) and minimum_ok):
        _warn_config_fallback(name, raw, default)
        return default
    return value


def _phone_frame_pacer(*, clock: Callable[[], float] | None = None) -> FramePacer:
    """Build a reply pacer from the live phone environment/override config."""
    return FramePacer(
        prebuffer_ms=_phone_pacing_value(
            "NANO_CLAW_PHONE_PREBUFFER_MS",
            DEFAULT_PHONE_PREBUFFER_MS,
            allow_zero=True,
        ),
        pace_factor=_phone_pacing_value(
            "NANO_CLAW_PHONE_PACE_FACTOR",
            DEFAULT_PHONE_PACE_FACTOR,
            allow_zero=False,
        ),
        clock=clock,
    )


def phone_codec() -> str:
    """'pcmu' (default, 8 kHz μ-law) or 'l16' (16 kHz wideband PCM)."""
    codec = _cfg("NANO_CLAW_PHONE_CODEC", "pcmu").lower()
    return "l16" if codec == "l16" else "pcmu"


def phone_rate() -> int:
    return 16000 if phone_codec() == "l16" else 8000


def phone_enabled() -> bool:
    return _cfg("NANO_CLAW_PHONE") in ("1", "true", "yes")


def barge_in_enabled() -> bool:
    """Caller can interrupt the agent mid-speech (NANO_CLAW_PHONE_BARGE_IN=1).
    Off by default: the phone leg is half-duplex unless opted in."""
    return _cfg("NANO_CLAW_PHONE_BARGE_IN") in ("1", "true", "yes")


def _clamped_phone_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read one finite phone setting and clamp it to an operator-safe range."""
    raw = _cfg(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        _warn_config_fallback(name, raw, default)
        return default
    if not math.isfinite(value):
        _warn_config_fallback(name, raw, default)
        return default
    return float(np.clip(value, minimum, maximum))


def _clamped_phone_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Read one integer phone setting and clamp it to its supported range."""
    raw = _cfg(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        _warn_config_fallback(name, raw, default)
        return default
    return min(max(value, minimum), maximum)


def phone_barge_vad_enter() -> float:
    """Raw Silero probability required for a barge vote."""
    return _clamped_phone_float(
        "NANO_CLAW_PHONE_BARGE_VAD_ENTER",
        DEFAULT_BARGE_VAD_ENTER,
        0.5,
        0.95,
    )


def phone_barge_trigger_ms() -> int:
    """Sustained barge-vote duration required to interrupt playback."""
    return _clamped_phone_int(
        "NANO_CLAW_PHONE_BARGE_TRIGGER_MS",
        DEFAULT_BARGE_TRIGGER_MS,
        120,
        1_500,
    )


def phone_echo_correlation() -> float:
    """Normalized outbound-reference correlation that identifies echo."""
    return _clamped_phone_float(
        "NANO_CLAW_PHONE_ECHO_CORR",
        DEFAULT_ECHO_CORRELATION,
        0.0,
        1.0,
    )


def phone_echo_gate_enabled() -> bool:
    """Echo defense defaults on whenever phone barge-in is enabled."""
    default = "1" if barge_in_enabled() else "0"
    return _cfg("NANO_CLAW_PHONE_ECHO_GATE", default).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def phone_speech_mode() -> str:
    """"prepared" (sentence-streaming compilation, the default), "batch"
    (compile only after the full reply — the pre-streaming behavior, kept
    as an escape hatch), or "raw" (no speech compiler at all)."""

    value = _cfg(
        "NANO_CLAW_PHONE_SPEECH_PREPARATION",
        _cfg("NANO_CLAW_SPEECH_PREPARATION", "1"),
    ).lower()
    if value in ("0", "false", "off", "no", "raw"):
        return "raw"
    if value == "batch":
        return "batch"
    return "prepared"


def _speech_compile_params() -> tuple[int, int]:
    """(max_words, max_chunk_duration_ms) for the speech compiler."""
    try:
        max_words = int(_cfg("NANO_CLAW_SPEECH_MAX_WORDS", "18"))
    except ValueError:
        _warn_config_fallback("NANO_CLAW_SPEECH_MAX_WORDS", _cfg("NANO_CLAW_SPEECH_MAX_WORDS", "18"), 18)
        max_words = 18
    try:
        max_duration = int(_cfg("NANO_CLAW_SPEECH_MAX_CHUNK_MS", "2500"))
    except ValueError:
        _warn_config_fallback("NANO_CLAW_SPEECH_MAX_CHUNK_MS", _cfg("NANO_CLAW_SPEECH_MAX_CHUNK_MS", "2500"), 2500)
        max_duration = 2500
    return max_words, max_duration


def _phone_gain_normalizer() -> SentencePeakNormalizer:
    """Build one call-owned normalizer from the phone gain environment."""
    raw_target = _cfg(
        "NANO_CLAW_PHONE_GAIN_TARGET_DB",
        str(DEFAULT_PHONE_GAIN_TARGET_DBFS),
    )
    try:
        target_dbfs = float(raw_target)
    except ValueError:
        _warn_config_fallback("NANO_CLAW_PHONE_GAIN_TARGET_DB", raw_target, DEFAULT_PHONE_GAIN_TARGET_DBFS)
        target_dbfs = DEFAULT_PHONE_GAIN_TARGET_DBFS
    if not np.isfinite(target_dbfs):
        _warn_config_fallback("NANO_CLAW_PHONE_GAIN_TARGET_DB", raw_target, DEFAULT_PHONE_GAIN_TARGET_DBFS)
        target_dbfs = DEFAULT_PHONE_GAIN_TARGET_DBFS
    return SentencePeakNormalizer(
        target_dbfs=target_dbfs,
        enabled=_cfg("NANO_CLAW_PHONE_GAIN", "on").lower() != "off",
    )


VAD_MODES = ("energy", "silero")
_vad_mode: str | None = None  # resolved lazily; runtime-switchable via /api/phone/vad


def get_vad_mode() -> str:
    """Active VAD for NEW calls: runtime selection > env > energy default.
    Falls back to energy loudly if silero is selected but unavailable."""
    global _vad_mode
    if _vad_mode is None:
        want = _cfg("NANO_CLAW_PHONE_VAD", "energy").lower()
        _vad_mode = want if want in VAD_MODES else "energy"
    if _vad_mode == "silero" and not silero_vad.available():
        log.error("[phone] silero VAD selected but unavailable — using energy")
        return "energy"
    return _vad_mode


def set_vad_mode(mode: str) -> bool:
    global _vad_mode
    if mode not in VAD_MODES:
        return False
    _vad_mode = mode
    log.info("[phone] VAD switched to %s (applies to new calls)", mode)
    return True


def dynamic_endpoint_enabled() -> bool:
    """Two-stage endpointing (NANO_CLAW_PHONE_DYNAMIC_ENDPOINT=1): endpoint
    on a short pause, but if the transcript ends mid-thought ('...tell me
    about'), keep listening and merge the continuation instead of answering
    the fragment. Emulates the semantic half of LiveKit-style turn detection
    with a deterministic tail check."""
    return _cfg("NANO_CLAW_PHONE_DYNAMIC_ENDPOINT") in ("1", "true", "yes")


def phone_stt_stream_enabled() -> bool:
    """Incremental STT is dark unless explicitly enabled."""
    return _cfg("NANO_CLAW_PHONE_STT_STREAM").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _telnyx_cmd(client: httpx.AsyncClient, cid: str, command: str, payload: dict) -> bool:
    """POST a Call Control command; never raises (a webhook must always 200)."""
    try:
        resp = await client.post(
            f"{TELNYX_API}/calls/{cid}/actions/{command}",
            headers={"Authorization": f"Bearer {_cfg('TELNYX_API_KEY')}"},
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
        log.info("[telnyx] %s OK cid=%s", command, cid[:16])
        return True
    except Exception as exc:
        log.error("[telnyx] %s failed cid=%s: %s", command, cid[:16], exc)
        return False


# Live call ids — lets /api/phone/config report whether a change lands
# mid-call or on the next call.
_active_calls: set[str] = set()


def _contained_call_id(call_id: str) -> str:
    """Return the sanitized id whose tap path is one contained child."""

    root = os.environ.get("NANO_CLAW_PHONE_TAP_DIR", DEFAULT_TAP_ROOT)
    return tap_directory_for(root, call_id).name


class PhoneCall:
    """One live call: endpointing → STT → agent → TTS, half-duplex."""

    def __init__(
        self,
        ws: web.WebSocketResponse,
        call_id: str,
        *,
        _flow=_FLOW_NOT_SUPPLIED,
        _flow_domain_id=_FLOW_NOT_SUPPLIED,
        _telnyx_call_id: str | None = None,
    ) -> None:
        telnyx_call_id = (
            str(call_id) if _telnyx_call_id is None else str(_telnyx_call_id)
        )
        safe_call_id = _contained_call_id(telnyx_call_id)
        self.ws = ws
        # Persistence, tap paths, and logs use the contained id. The raw id is
        # retained only for Telnyx Call Control commands, where it is the
        # provider's remote resource identifier.
        self.call_id = safe_call_id
        self.telnyx_call_id = telnyx_call_id
        _active_calls.add(safe_call_id)
        # The agent API validates session ids against ^[A-Za-z0-9_-]{1,64}$.
        # Telnyx call ids now carry a "v3:" prefix, and the colon (plus any
        # other punctuation) makes the id fail validation, so /api/chat returns
        # 400 and the caller hears only the fallback line. The tap-directory
        # invariant strips to the safe alphabet before this id is sliced.
        self.session_id = f"phone-{safe_call_id[:24]}"
        codec = phone_codec()
        rate = 16000 if codec == "l16" else 8000
        self.tap = CallTap.create(safe_call_id, codec, rate, rate)
        self._tap_sentence_index = 0
        self._active_tap_sentence_index: int | None = None
        if self.tap:
            self.tap.event(
                "call_start",
                codec=codec,
                voice=_cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
            )
        # Dynamic mode endpoints fast (450 ms) because the semantic tail
        # check can rescue fragments; fixed mode keeps the safer 700 ms.
        self.dynamic = dynamic_endpoint_enabled()
        self.endpointer = UtteranceEndpointer(
            end_silence_ms=450 if self.dynamic else 700,
            rate_hz=phone_rate(),
            codec=codec,
        )
        self._tail_extensions = 0
        self._primed_len = 0
        self._primed_text = ""
        self._stt_stream: StreamingSTTSession | None = None
        self._stt_stream_failed = False
        self._stt_stream_error_logged = False
        self._stt_utterance_size: str | None = None
        echo_correlation = phone_echo_correlation()
        self.barge = BargeInDetector(
            rate_hz=rate,
            trigger_ms=phone_barge_trigger_ms(),
            vad_enter=phone_barge_vad_enter(),
            echo_corr_threshold=echo_correlation,
        )
        self.echo_gate = (
            EchoReferenceGate(
                rate_hz=rate,
                correlation_threshold=echo_correlation,
            )
            if phone_echo_gate_enabled()
            else None
        )
        # Neural VAD (one streaming instance per call; None = energy mode)
        self.vad_mode = get_vad_mode()
        if self.vad_mode == "silero":
            self.vad = (
                silero_vad.SileroVAD(sample_rate=16000)
                if phone_codec() == "l16"
                else silero_vad.SileroVAD()
            )
        else:
            self.vad = None
        self._vad_frames = 0
        log.info("[phone %s] VAD: %s", safe_call_id[:8], self.vad_mode)
        self.speaking = False
        self.interrupted = False
        self.closed = False
        self._playback_flush_sent = False
        self._turn_task: asyncio.Task | None = None
        self._sentence_pipelines: set[SentencePipeline] = set()
        self._gain_normalizer = _phone_gain_normalizer()
        self._frame_pacer: FramePacer | None = None
        self._inbound_buffer: deque[tuple[np.ndarray, bool | None]] = deque()
        self._inbound_buffer_drops = 0
        self._http = httpx.AsyncClient(timeout=120.0)
        flow_domain_id = (
            active_scheduling_domain(get_flow_mode())
            if _flow_domain_id is _FLOW_NOT_SUPPLIED
            else _flow_domain_id
        )
        if _flow is _FLOW_NOT_SUPPLIED:
            self.flow = (
                FlowSession.create(domain_id=flow_domain_id)
                if flow_domain_id is not None
                else None
            )
        else:
            self.flow = _flow
        self._flow_domain_id = flow_domain_id
        self._flow_create_failed = False
        self.default_greeting = (
            self.flow.greeting if self.flow else flow_mode_greeting()
        )
        self._call_end_emitted = False
        self._thinking_cue_task: asyncio.Task | None = None
        self._thinking_cue_stop: asyncio.Event | None = None
        start_voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        call_log.emit(
            _metrics_conn,
            safe_call_id,
            "call_start",
            {
                "codec": codec,
                "vad": self.vad_mode,
                "voice": start_voice,
                "engine": (voice_catalog.lookup(start_voice) or {}).get(
                    "engine", "unknown"
                ),
                "sttSize": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
                "speed": _cfg("NANO_CLAW_PHONE_SPEED", "1.0"),
                "model": _cfg("NANO_CLAW_PHONE_MODEL") or None,
                "mode": "scheduler" if self.flow else "persona",
                "flowDomain": flow_domain_id,
                "sessionId": self.session_id,
            },
        )
        # Idle policy: clock runs from the last time the caller spoke or the
        # agent finished speaking; one "are you still there?" per stretch.
        self.last_activity = time.monotonic()
        self.idle_prompted = False
        self._idle_task = asyncio.create_task(self._idle_watchdog())

    @classmethod
    async def create_async(
        cls,
        ws: web.WebSocketResponse,
        call_id: str,
    ) -> PhoneCall:
        """Build live-calendar flows off-loop before the phone greeting."""

        telnyx_call_id = str(call_id)
        safe_call_id = _contained_call_id(telnyx_call_id)
        domain_id = active_scheduling_domain(get_flow_mode())
        flow = (
            await FlowSession.create_async(domain_id=domain_id)
            if domain_id is not None
            else None
        )
        return cls(
            ws,
            safe_call_id,
            _flow=flow,
            _flow_domain_id=domain_id,
            _telnyx_call_id=telnyx_call_id,
        )

    async def close(self) -> None:
        was_speaking = self.speaking
        self.speaking = False
        self.closed = True
        self._stop_thinking_cue()
        for pipeline in tuple(self._sentence_pipelines):
            await pipeline.aclose()
        if was_speaking or self.interrupted:
            await self._flush_playback()
        _active_calls.discard(self.call_id)
        self._inbound_buffer.clear()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        stt_stream = getattr(self, "_stt_stream", None)
        self._stt_stream = None
        if stt_stream is not None:
            await stt_stream.close()
        try:
            await self._http.aclose()
        finally:
            tap, self.tap = self.tap, None
            if tap:
                tap.event("call_end")
                tap.close()
            if not self._call_end_emitted:
                self._call_end_emitted = True
                call_log.emit(_metrics_conn, self.call_id, "call_end")
            if tap is not None:
                _schedule_seam_capture(self.call_id, getattr(tap, "directory", None))

    def _sync_flow_mode(self) -> None:
        """Re-evaluate the Flow dropdown at each turn boundary so a change in
        the web UI applies to the caller's next utterance, mid-call.

        Off → a scheduling domain joins the flow cold (no flow greeting; it
        engages with whatever the caller says next). Leaving or changing the
        scheduling domain abandons the prior negotiation. A failed plumber
        FlowSession create falls back to persona chat and is not retried for
        the rest of the call. Runtime turns use the async sibling below."""
        domain_id = active_scheduling_domain(get_flow_mode())
        if domain_id != self._flow_domain_id:
            if self.flow is not None:
                log.info(
                    "[phone %s] flow changed mid-call (%s → %s)",
                    self.call_id[:8],
                    self._flow_domain_id or "persona",
                    domain_id or "persona",
                )
            self.flow = None
            self._flow_create_failed = False
            self._flow_domain_id = domain_id
        if (
            domain_id is not None
            and self.flow is None
            and not self._flow_create_failed
        ):
            self.flow = FlowSession.create(domain_id=domain_id)
            if self.flow is None:
                self._flow_create_failed = True
                log.warning("[phone %s] flow switch requested but FlowSession "
                            "unavailable — staying in persona chat", self.call_id[:8])
            else:
                log.info(
                    "[phone %s] flow joined mid-call (%s)",
                    self.call_id[:8],
                    domain_id,
                )
        elif domain_id is None and self.flow is not None:
            log.info("[phone %s] flow left mid-call (scheduler → persona)", self.call_id[:8])
            self.flow = None

    async def _sync_flow_mode_async(self) -> None:
        """Async runtime variant so live availability never blocks the loop."""

        domain_id = active_scheduling_domain(get_flow_mode())
        if domain_id != self._flow_domain_id:
            if self.flow is not None:
                log.info(
                    "[phone %s] flow changed mid-call (%s → %s)",
                    self.call_id[:8],
                    self._flow_domain_id or "persona",
                    domain_id or "persona",
                )
            self.flow = None
            self._flow_create_failed = False
            self._flow_domain_id = domain_id
        if (
            domain_id is not None
            and self.flow is None
            and not self._flow_create_failed
        ):
            self.flow = await FlowSession.create_async(domain_id=domain_id)
            if self.flow is None:
                self._flow_create_failed = True
                log.warning(
                    "[phone %s] flow switch requested but FlowSession "
                    "unavailable — staying in persona chat",
                    self.call_id[:8],
                )
            else:
                log.info(
                    "[phone %s] flow joined mid-call (%s)",
                    self.call_id[:8],
                    domain_id,
                )
        elif domain_id is None and self.flow is not None:
            log.info("[phone %s] flow left mid-call (scheduler → persona)", self.call_id[:8])
            self.flow = None

    def _mark_activity(self) -> None:
        self.last_activity = time.monotonic()
        self.idle_prompted = False

    async def _idle_watchdog(self) -> None:
        """Prompt after NANO_CLAW_PHONE_IDLE_S of silence; hang up after a
        second full stretch with still no reply (default 30s → 60s total)."""
        prompt_after = float(_cfg("NANO_CLAW_PHONE_IDLE_S", "30") or 30)
        while not self.closed:
            await asyncio.sleep(2.5)
            if self.closed:
                return
            if self.speaking or (self._turn_task and not self._turn_task.done()):
                continue
            action = idle_action(
                time.monotonic() - self.last_activity, self.idle_prompted, prompt_after
            )
            if action == "prompt":
                log.info("[phone %s] idle %.0fs — prompting caller", self.call_id[:8], prompt_after)
                self.idle_prompted = True
                call_log.emit(
                    _metrics_conn,
                    self.call_id,
                    "assistant_turn",
                    {"text": IDLE_PROMPT_TEXT, "mode": "idle"},
                )
                await self.speak(IDLE_PROMPT_TEXT)
            elif action == "hangup":
                log.info("[phone %s] idle after prompt — hanging up", self.call_id[:8])
                call_log.emit(
                    _metrics_conn,
                    self.call_id,
                    "assistant_turn",
                    {"text": IDLE_GOODBYE_TEXT, "mode": "idle"},
                )
                await self.speak(IDLE_GOODBYE_TEXT)
                await _telnyx_cmd(
                    self._http,
                    getattr(self, "telnyx_call_id", self.call_id),
                    "hangup",
                    {},
                )
                self.closed = True
                return

    # ── Inbound audio ────────────────────────────────────────────

    def feed_media(self, payload_b64: str) -> None:
        if self.closed:
            return
        payload = base64.b64decode(payload_b64)
        if self.tap:
            self.tap.inbound_frame(payload)
        pcm = (
            np.frombuffer(payload, dtype=np.int16)
            if phone_codec() == "l16"
            else ulaw_decode(payload)
        )
        # Feed the neural VAD continuously (its recurrent state needs every
        # frame); both detectors then share one speech decision per frame.
        is_speech = self.vad.feed_speech(pcm) if self.vad else None
        if self.vad:
            self._vad_frames += 1
            if self._vad_frames % 250 == 0:  # every ~5s of call audio
                vmax, vmean = self.vad.take_stats()
                log.info(
                    "[phone %s] silero last5s: max=%.2f mean=%.2f in_speech=%s",
                    self.call_id[:8], vmax, vmean, is_speech,
                )

        if self.speaking:
            # Barge-in (NANO_CLAW_PHONE_BARGE_IN=1): listen for the caller
            # talking over us; otherwise stay half-duplex.
            if barge_in_enabled():
                echo_correlation = (
                    self.echo_gate.feed_inbound(pcm) if self.echo_gate else None
                )
                committed = self.barge.feed(
                    pcm,
                    is_speech=is_speech,
                    speech_prob=self.vad.prob if self.vad else None,
                    echo_correlation=echo_correlation,
                )
                if committed:
                    self._interrupt()
                elif self.tap and self.barge.last_candidate:
                    candidate = self.barge.last_candidate
                    self.tap.event(
                        "barge_candidate",
                        reason=candidate.reason,
                        prob=candidate.prob,
                        corr=candidate.corr,
                    )
            return

        # A completed task's callback normally replays first, but finish it
        # here too so a newly arrived frame can never overtake older audio.
        if self._turn_task and self._turn_task.done():
            self._turn_finished(self._turn_task)

        # While a turn is still thinking (STT/LLM), hold audio for ordered
        # replay — unless we just interrupted, in which case the caller's
        # speech is already feeding the barge-in-primed endpointer.
        if self._turn_task and not self._turn_task.done() and not self.interrupted:
            self._buffer_inbound(pcm, is_speech)
            return

        utterance = self._feed_endpointer(pcm, is_speech)
        if utterance:
            self._mark_activity()
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()  # interrupted turn still unwinding
                self._inbound_buffer.clear()
            self.interrupted = False
            self._start_turn(utterance)

    def _feed_endpointer(
        self, pcm: np.ndarray, is_speech: bool | None
    ) -> bytes | None:
        """Feed one decoded frame and capture endpoint state transitions."""
        tap = self.tap
        was_in_utterance = self.endpointer.in_utterance
        utterance = self.endpointer.feed(pcm, is_speech=is_speech)
        is_in_utterance = self.endpointer.in_utterance
        rms = self.endpointer.current_rms
        floor = self.endpointer.noise_floor
        if not was_in_utterance and is_in_utterance:
            if tap:
                tap.event("utterance_start", rms=rms, floor=floor)
            # The endpointer has just promoted its short preroll into
            # ``_frames``.  Tee that exact accumulated prefix into STT so the
            # incremental and eventual one-shot paths see the same syllables.
            frames = getattr(self.endpointer, "_frames", ())
            initial_pcm = (
                np.concatenate(frames).astype(np.int16).tobytes()
                if frames
                else pcm.astype(np.int16).tobytes()
            )
            self._begin_stt_stream(initial_pcm)
        elif was_in_utterance:
            self._feed_stt_stream(pcm.astype(np.int16).tobytes())
        if was_in_utterance and not is_in_utterance:
            if tap:
                tap.event(
                    "utterance_end",
                    rms=rms,
                    floor=floor,
                    accepted=utterance is not None,
                )
            if utterance is None:
                self._abandon_stt_stream_nowait()
        return utterance

    def _record_stt_pass(self, item: dict) -> None:
        tap = self.tap
        if tap is None:
            return
        tap.event(
            "stt_pass",
            pass_count=int(item.get("pass_count") or 0),
            window_ms=float(item.get("window_ms") or 0.0),
            ms=float(item.get("ms") or 0.0),
        )

    def _begin_stt_stream(self, initial_pcm: bytes) -> None:
        """Start one utterance session and queue its endpointer preroll."""
        if (
            not phone_stt_stream_enabled()
            or getattr(self, "_stt_stream", None) is not None
        ):
            return
        self._stt_stream_failed = False
        self._stt_stream_error_logged = False
        self._stt_utterance_size = _cfg("NANO_CLAW_PHONE_STT_SIZE", "base")
        stt_url = os.environ.get(
            "STT_SERVICE_URL", "http://host.docker.internal:8200"
        )
        try:
            self._stt_stream = StreamingSTTSession(
                self._http,
                stt_url,
                sample_rate=phone_rate(),
                model_size=self._stt_utterance_size,
                on_pass=self._record_stt_pass,
            )
            self._stt_stream.feed_nowait(initial_pcm)
        except Exception as exc:
            self._mark_stt_stream_failed(exc)

    def _feed_stt_stream(self, pcm: bytes) -> None:
        stream = getattr(self, "_stt_stream", None)
        if stream is not None and not getattr(self, "_stt_stream_failed", False):
            stream.feed_nowait(pcm)

    def _mark_stt_stream_failed(self, exc: BaseException) -> None:
        self._stt_stream_failed = True
        if not getattr(self, "_stt_stream_error_logged", False):
            self._stt_stream_error_logged = True
            log.warning(
                "[phone %s] streaming STT failed; falling back to one-shot "
                "for this utterance: %s",
                self.call_id[:8],
                exc,
            )

    def _abandon_stt_stream_nowait(self) -> None:
        """Drop a rejected/cancelled utterance without blocking frame ingest."""
        stream = getattr(self, "_stt_stream", None)
        self._stt_stream = None
        self._stt_stream_failed = False
        self._stt_utterance_size = None
        if stream is not None:
            asyncio.create_task(stream.close())

    async def _complete_stt_utterance(self) -> None:
        """Release a kept dynamic session after the semantic tail is final."""
        stream = getattr(self, "_stt_stream", None)
        self._stt_stream = None
        if stream is not None:
            await stream.close()
        self._stt_stream_failed = False
        self._stt_utterance_size = None

    def _buffer_inbound(self, pcm: np.ndarray, is_speech: bool | None) -> None:
        if len(self._inbound_buffer) >= MAX_BUFFERED_INBOUND_FRAMES:
            self._inbound_buffer.popleft()
            self._inbound_buffer_drops += 1
            if self._inbound_buffer_drops == 1 or self._inbound_buffer_drops % 250 == 0:
                log.warning(
                    "[phone %s] inbound buffer capped at %d frames — dropped %d oldest",
                    self.call_id[:8], MAX_BUFFERED_INBOUND_FRAMES, self._inbound_buffer_drops,
                )
        self._inbound_buffer.append((pcm, is_speech))

    def _start_turn(self, utterance: bytes) -> None:
        task = asyncio.create_task(self._run_turn(utterance))
        self._turn_task = task
        task.add_done_callback(self._turn_finished)

    def _turn_finished(self, task: asyncio.Task) -> None:
        if task is not self._turn_task:
            return
        self._stop_thinking_cue()
        self._turn_task = None
        if self.closed or task.cancelled() or self.interrupted:
            # Barge-in has already primed the endpointer; stale thinking audio
            # must neither precede that interruption nor reset it via replay.
            self._inbound_buffer.clear()
            return
        self._replay_inbound()

    def _replay_inbound(self) -> None:
        while self._inbound_buffer and not self.closed:
            pcm, is_speech = self._inbound_buffer.popleft()
            utterance = self._feed_endpointer(pcm, is_speech)
            if utterance:
                self._mark_activity()
                self._start_turn(utterance)
                return

    def _interrupt(self) -> None:
        """Caller talked over the agent: stop speaking and turn the
        interruption itself into the next utterance."""
        log.info("[phone %s] barge-in — caller interrupted", self.call_id[:8])
        self._stop_thinking_cue()
        if self.tap:
            self.tap.event(
                "barge_in", sentence_index=self._active_tap_sentence_index
            )
        call_log.emit(_metrics_conn, self.call_id, "barge_in")
        self._mark_activity()
        self.interrupted = True
        self.speaking = False  # speak() loop sees this and aborts
        frames = self.barge.take_frames()
        self.endpointer.prime(frames)
        if frames:
            self._begin_stt_stream(
                np.concatenate(frames).astype(np.int16).tobytes()
            )
        asyncio.create_task(self._flush_playback())

    def _reset_barge_in(self) -> None:
        """Start a reply with empty sustain and inbound comparison windows."""
        self.barge.reset()
        echo_gate = getattr(self, "echo_gate", None)
        if echo_gate:
            echo_gate.reset_inbound()

    def _feed_echo_reference(self, frame: bytes, codec: str) -> None:
        """Tee one successfully sent transport frame into the echo gate."""
        echo_gate = getattr(self, "echo_gate", None)
        if echo_gate is None:
            return
        pcm = (
            np.frombuffer(frame, dtype=np.int16)
            if codec == "l16"
            else ulaw_decode(frame)
        )
        echo_gate.feed_outbound(pcm)

    async def _flush_playback(self) -> None:
        """Clear the bounded prebuffer surplus queued by frame pacing.

        Playback starts with a small one-time burst so Telnyx does not starve.
        Telnyx can still hold that audio after a local interruption; one clear
        drops the buffered tail.
        """
        if self._playback_flush_sent or getattr(self.ws, "closed", False):
            return
        self._playback_flush_sent = True
        try:
            await self.ws.send_json({"event": "clear"})
        except Exception:
            log.exception("[phone %s] clear failed", self.call_id[:8])
        else:
            if self.tap:
                self.tap.event("clear_sent")

    # ── Thinking cue ─────────────────────────────────────────────

    def _start_thinking_cue(self) -> None:
        """Acknowledge the accepted utterance, then tick while the turn
        thinks (STT + LLM) so the wait never sounds like a dead line."""
        if _cfg("NANO_CLAW_PHONE_THINKING_CUE", "on").lower() == "off":
            return
        self._stop_thinking_cue()
        stop = asyncio.Event()
        self._thinking_cue_stop = stop
        self._thinking_cue_task = asyncio.create_task(self._thinking_cue_loop(stop))

    def _stop_thinking_cue(self) -> None:
        # getattr: several tests build PhoneCall skeletons via __new__.
        stop = getattr(self, "_thinking_cue_stop", None)
        task = getattr(self, "_thinking_cue_task", None)
        self._thinking_cue_stop = None
        self._thinking_cue_task = None
        if stop is not None:
            stop.set()
        if task is not None and not task.done():
            task.cancel()

    async def _thinking_cue_loop(self, stop: asyncio.Event) -> None:
        ticks = 0
        if self.tap:
            self.tap.event("thinking_cue_start")
        try:
            await self._play_cue(processing_chime(), stop)  # "I heard you"
            while not stop.is_set() and not self.closed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=THINKING_TICK_INTERVAL_S)
                    break
                except asyncio.TimeoutError:
                    pass
                if stop.is_set() or self.closed:
                    break
                await self._play_cue(thinking_tick(), stop)
                ticks += 1
        except asyncio.CancelledError:
            pass  # health-ok: _stop_thinking_cue cancels this task on purpose
        except Exception:
            log.exception("[phone %s] thinking cue failed", self.call_id[:8])
        finally:
            if self.tap:
                self.tap.event("thinking_cue_stop", ticks=ticks)

    async def _play_cue(self, pcm48k: bytes, stop: asyncio.Event) -> None:
        """Pace one pre-tuned earcon to the transport, outside the sentence
        pipeline (which does not exist while STT/LLM think). Uses its own
        pacer and never touches ``speaking``, so reply pacing, inbound
        buffering, and barge-in semantics stay untouched. The stop event is
        checked before every frame so cue audio can never interleave with a
        reply that is starting."""
        codec = phone_codec()
        frames = (
            pcm48k_to_l16_frames(pcm48k)
            if codec == "l16"
            else pcm48k_to_ulaw_frames(pcm48k)
        )
        pacer = _phone_frame_pacer()
        pacer.reset()
        for frame in frames:
            if self.closed or stop.is_set():
                return
            deadline = pacer.next_deadline()
            await asyncio.sleep(max(0.0, deadline - pacer.now()))
            if self.closed or stop.is_set():
                return
            await self.ws.send_json(
                {"event": "media", "media": {"payload": base64.b64encode(frame).decode()}}
            )
            self._feed_echo_reference(frame, codec)
            if self.tap:
                self.tap.outbound_frame(frame)

    # ── One conversational turn ──────────────────────────────────

    async def _run_turn(self, pcm: bytes) -> None:
        self._start_thinking_cue()
        try:
            # If we extended the window and the caller stayed quiet (<600 ms
            # of new audio), don't re-transcribe near-identical audio — they
            # trailed off; answer what we already heard.
            new_audio_bytes = len(pcm) - self._primed_len
            if self._tail_extensions and new_audio_bytes < int(phone_rate() * 2 * 0.6):
                text = self._primed_text
                self._tail_extensions = 2  # no further extensions
            else:
                text = await self._transcribe(pcm)
            if not text.strip():
                self._tail_extensions = 0
                await self._complete_stt_utterance()
                return
            # Semantic tail check: a transcript ending mid-thought means the
            # short pause was a breath, not a turn end. Re-prime the
            # endpointer with the same audio and keep listening; the next
            # endpoint re-transcribes the MERGED utterance. Bounded to 2
            # extensions so a trailing-off caller still gets an answer.
            if (
                self.dynamic
                and self._tail_extensions < 2
                and transcript_looks_incomplete(text)
            ):
                self._tail_extensions += 1
                self._primed_len = len(pcm)
                self._primed_text = text
                log.info(
                    "[phone %s] tail-incomplete (%r…) — extending listen window (%d)",
                    self.call_id[:8], text[-30:], self._tail_extensions,
                )
                pcm_samples = np.frombuffer(pcm, dtype=np.int16)
                frame = phone_rate() * FRAME_MS // 1000
                self.endpointer.prime(
                    [
                        pcm_samples[i : i + frame]
                        for i in range(0, len(pcm_samples), frame)
                    ]
                )
                return
            self._tail_extensions = 0
            await self._complete_stt_utterance()
            log.info("[phone %s] caller: %s", self.call_id[:8], text)
            metrics_db.bump_call_turns(_metrics_conn, self.call_id)
            call_log.emit(_metrics_conn, self.call_id, "user_turn", {"text": text})
            await self._sync_flow_mode_async()
            if self.flow:
                agent_started = time.monotonic() if self.tap else None
                reply = await self.flow.reply(text)
                if self.tap and agent_started is not None:
                    self.tap.event(
                        "agent_done", ms=(time.monotonic() - agent_started) * 1000.0
                    )
                log.info(
                    "[phone %s] flow outcome=%s slots=%s",
                    self.call_id[:8],
                    reply.outcome or "continue",
                    reply.slots,
                )
                outcome = getattr(reply, "outcome", None)
                call_log.emit(
                    _metrics_conn,
                    self.call_id,
                    "assistant_turn",
                    {
                        "text": reply.text,
                        "mode": "scheduler",
                        "outcome": getattr(outcome, "value", outcome),
                        "slots": getattr(reply, "slots", {}),
                        "rejected": getattr(reply, "rejected", []),
                        "supervisorMs": getattr(reply, "supervisor_ms", None),
                        "turnsUsed": getattr(reply, "turns_used", None),
                        "maxTurns": getattr(reply, "max_turns", None),
                        "eventId": getattr(reply, "event_id", None),
                        "done": reply.done,
                        "model": str(
                            getattr(getattr(self.flow, "_runner", None), "_model", "")
                            or ""
                        )
                        or None,
                    },
                )
                await self.speak(reply.text)
                if reply.done:
                    await _telnyx_cmd(
                        self._http,
                        getattr(self, "telnyx_call_id", self.call_id),
                        "hangup",
                        {},
                    )
                    self.closed = True
                return
            await self._stream_reply(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[phone %s] turn failed", self.call_id[:8])
            error_line = "Sorry, something went wrong on my end. Try asking again."
            call_log.emit(
                _metrics_conn,
                self.call_id,
                "assistant_turn",
                {"text": error_line, "mode": "error"},
            )
            await self.speak(error_line)

    async def _stream_reply(self, text: str) -> None:
        """Stream the agent's reply (SSE) and speak each sentence as it
        completes — the caller hears the first sentence while the model is
        still writing the rest. Falls back to the non-stream JSON shape when
        the API has streaming disabled (NANO_CLAW_STREAM=0)."""
        t0 = time.monotonic()
        tap = self.tap
        agent_done_recorded = False

        def record_agent_done() -> None:
            nonlocal agent_done_recorded
            if agent_done_recorded:
                return
            agent_done_recorded = True
            if tap:
                tap.event("agent_done", ms=(time.monotonic() - t0) * 1000.0)

        self._playback_flush_sent = False
        self.speaking = True
        self._reset_barge_in()
        chunker = TextChunker()
        first_spoken_at: float | None = None
        reply_complete = False
        spoken_parts: list[str] = []
        # Which model actually wrote the turn (fallback-aware, from the API's
        # debug payload); requested is present only when a fallback answered.
        turn_model: dict[str, str | None] = {"served": None, "requested": None}
        # Streaming prepared speech: set when the final response diverged from
        # the streamed deltas (guard rewrite) — surfaced on the turn event.
        stream_flags = {"mismatch": False}

        def record_turn_model(obj: dict) -> None:
            debug = obj.get("debug")
            if isinstance(debug, dict):
                turn_model["served"] = debug.get("model") or turn_model["served"]
                turn_model["requested"] = (
                    debug.get("requestedModel") or turn_model["requested"]
                )

        async def record_spoken(source):
            # Accumulate exactly what is handed to synthesis so the call
            # review timeline shows what the caller actually heard —
            # including barge-in truncation.
            async for unit in source:
                if unit is not PROCESSING_CUE_SENTINEL:
                    unit_text = unit if isinstance(unit, str) else getattr(unit, "text", "")
                    if unit_text and unit_text.strip():
                        spoken_parts.append(unit_text.strip())
                yield unit

        try:
            payload: dict = {
                "message": text,
                "sessionId": self.session_id,
                "responseMode": "voice",
                # The console MODE selector sets the shared flow mode; the phone
                # must pass its profile per turn so a switch to riff/nano-claw/
                # intelligence takes effect on the phone too. Without this the
                # agent falls back to the default persona (Space Channel).
                "profile": get_flow_profile(),
            }
            model = _cfg("NANO_CLAW_PHONE_MODEL")
            if model:
                payload["model"] = model  # else: server's configured default
            async with self._http.stream(
                "POST",
                f"{NANO_CLAW_URL}/api/chat",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if "text/event-stream" not in resp.headers.get("content-type", ""):
                    body = json.loads(await resp.aread())
                    reply = body.get("response", "") or "I didn't catch that — could you say it again?"
                    record_turn_model(body)
                    record_agent_done()
                    reply_complete = True
                    spoken_parts.append(reply)
                    log.info("[phone %s] agent non-stream (%.1fs)", self.call_id[:8], time.monotonic() - t0)
                    await self._speak_sentences(self._speech_units(reply))
                    return

                async def stream_sentences():
                    nonlocal first_spoken_at, reply_complete
                    event = ""
                    data_lines: list[str] = []
                    last_processing_cue = 0.0
                    mode = phone_speech_mode()
                    prepared_parts: list[str] = []  # batch mode + held turns
                    speech_compiler: StreamingSpeechCompiler | None = None
                    yielded_prepared = False
                    # A held turn buffers everything and compiles at final —
                    # used when the server may rewrite the reply after
                    # streaming (deep turns, held-response synthetic deltas).
                    held_turn = False
                    saw_delta = False

                    def mark_first_spoken() -> None:
                        nonlocal first_spoken_at
                        if first_spoken_at is None:
                            first_spoken_at = time.monotonic()
                            log.info(
                                "[phone %s] first sentence at %.1fs",
                                self.call_id[:8], first_spoken_at - t0,
                            )

                    async for raw in resp.aiter_lines():
                        if self.closed or not self.speaking:
                            return  # hangup or barge-in: stop consuming the stream
                        if raw == "":
                            event_payload = "\n".join(data_lines)
                            data_lines = []
                            ev, event = event, ""
                            if not event_payload:
                                continue
                            obj = json.loads(event_payload)
                            if ev == "delta":
                                delta = obj.get("text", "")
                                if not isinstance(delta, str) or not delta:
                                    continue
                                if mode == "batch" or held_turn:
                                    prepared_parts.append(delta)
                                    continue
                                if mode == "prepared":
                                    # The server marks its held-response
                                    # synthetic delta with held:true (the text
                                    # may have been guard-rewritten) — compile
                                    # at final instead of streaming it. The
                                    # size heuristic remains as a fallback for
                                    # servers predating the flag.
                                    if obj.get("held") or (
                                        not saw_delta and len(delta) > 350
                                    ):
                                        saw_delta = True
                                        held_turn = True
                                        prepared_parts.append(delta)
                                        continue
                                    saw_delta = True
                                    if speech_compiler is None:
                                        words, duration = _speech_compile_params()
                                        speech_compiler = StreamingSpeechCompiler(
                                            max_words_per_chunk=words,
                                            max_chunk_duration_ms=duration,
                                        )
                                    for unit in speech_compiler.feed(delta):
                                        mark_first_spoken()
                                        yielded_prepared = True
                                        yield unit
                                    continue
                                for chunk in chunker.push(delta):
                                    mark_first_spoken()
                                    yield chunk
                            elif ev == "deep_started":
                                held_turn = True
                                acknowledgement = obj.get(
                                    "acknowledgement",
                                    "Let me think deeply about this.",
                                )
                                if (
                                    isinstance(acknowledgement, str)
                                    and acknowledgement.strip()
                                ):
                                    if first_spoken_at is None:
                                        first_spoken_at = time.monotonic()
                                    yield acknowledgement.strip()
                                last_processing_cue = time.monotonic()
                            elif ev == "deep_progress":
                                now = time.monotonic()
                                if (
                                    obj.get("phase")
                                    not in {"completed", "failed", "cancelled"}
                                    and now - last_processing_cue >= 2.6
                                ):
                                    last_processing_cue = now
                                    yield PROCESSING_CUE_SENTINEL
                            elif ev == "final":
                                record_turn_model(obj)
                                record_agent_done()
                                reply_complete = True
                                response_text = obj.get("response", "")
                                response_text = (
                                    response_text.strip()
                                    if isinstance(response_text, str)
                                    else ""
                                )
                                if mode == "raw":
                                    tail = chunker.flush()
                                    if tail:
                                        yield tail
                                elif (
                                    mode == "batch"
                                    or held_turn
                                    or speech_compiler is None
                                ):
                                    source_text = (
                                        response_text
                                        or "".join(prepared_parts).strip()
                                    )
                                    for unit in self._speech_units(source_text):
                                        mark_first_spoken()
                                        yield unit
                                else:
                                    fed = speech_compiler.fed_text.strip()
                                    if (
                                        yielded_prepared
                                        and response_text
                                        and response_text != fed
                                        and not response_text.startswith(fed)
                                    ):
                                        # The server rewrote the reply after
                                        # we already spoke streamed sentences.
                                        # Never double-speak: the streamed
                                        # prefix is what the caller heard.
                                        stream_flags["mismatch"] = True
                                        if tap:
                                            tap.event(
                                                "prepared_stream_mismatch",
                                                fed_len=len(fed),
                                                final_len=len(response_text),
                                            )
                                        log.warning(
                                            "[phone %s] final response diverged"
                                            " from streamed deltas — tail not"
                                            " spoken",
                                            self.call_id[:8],
                                        )
                                    else:
                                        for unit in speech_compiler.finish(
                                            response_text or None
                                        ):
                                            mark_first_spoken()
                                            yield unit
                            elif ev == "tool_pending":
                                record_turn_model(obj)
                                yield (
                                    "I can't take actions over the phone, but I'm happy "
                                    "to answer questions."
                                )
                            elif ev == "error":
                                record_agent_done()
                                yield "Sorry, something went wrong. Try asking again."
                        elif raw.startswith("event:"):
                            event = raw[6:].strip()
                        elif raw.startswith("data:"):
                            data_lines.append(raw[5:].strip())

                await self._speak_sentences(record_spoken(stream_sentences()))
                if reply_complete:
                    log.info(
                        "[phone %s] reply complete (%.1fs total)",
                        self.call_id[:8], time.monotonic() - t0,
                    )
                if self.closed or not self.speaking:
                    return
                record_agent_done()
        finally:
            self.speaking = False
            if spoken_parts:
                call_log.emit(
                    _metrics_conn,
                    self.call_id,
                    "assistant_turn",
                    {
                        "text": " ".join(spoken_parts),
                        "mode": "persona",
                        "complete": reply_complete,
                        "interrupted": self.interrupted,
                        "model": turn_model["served"],
                        "modelRequested": turn_model["requested"],
                        "modelFallback": bool(turn_model["requested"]),
                        **(
                            {"preparedStreamMismatch": True}
                            if stream_flags["mismatch"]
                            else {}
                        ),
                    },
                )
            if not self.interrupted:
                self.endpointer.reset()
            self.last_activity = time.monotonic()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        chunker = TextChunker()
        out = chunker.push(text)
        tail = chunker.flush()
        if tail:
            out.append(tail)
        return out

    @staticmethod
    def _speech_units(text: str) -> list[str | SpeechChunk]:
        """Compile one complete phone response, retaining the raw rollback."""

        if phone_speech_mode() == "raw":
            return PhoneCall._sentences(text)
        max_words, max_duration = _speech_compile_params()
        try:
            plan = compile_speech(
                text,
                max_words_per_chunk=max_words,
                max_chunk_duration_ms=max_duration,
            )
        except Exception:
            log.exception("[phone] speech preparation failed; using raw text")
            return PhoneCall._sentences(text)
        metadata = plan.public_metadata()
        log.info(
            "[phone] speech plan compiled: version=%s chunks=%d normalizations=%d",
            metadata["compilerVersion"],
            metadata["chunkCount"],
            metadata["normalizationCount"],
        )
        return list(plan.chunks)

    async def _transcribe(self, pcm: bytes) -> str:
        stream = getattr(self, "_stt_stream", None)
        if (
            phone_stt_stream_enabled()
            and stream is not None
            and not getattr(self, "_stt_stream_failed", False)
        ):
            started = time.monotonic() if self.tap else None
            try:
                result = await stream.finish(
                    keep_session=bool(getattr(self, "dynamic", False))
                )
            except Exception as exc:
                self._mark_stt_stream_failed(exc)
                await stream.close()
                if getattr(self, "_stt_stream", None) is stream:
                    self._stt_stream = None
            else:
                if not getattr(self, "dynamic", False):
                    self._stt_stream = None
                if self.tap and started is not None:
                    self.tap.event(
                        "stt_done",
                        ms=(time.monotonic() - started) * 1000.0,
                        text_len=len(result.text),
                        streamed=True,
                        committed_chars=result.committed_chars,
                        finish_ms=result.finish_ms,
                    )
                return result.text

        return await self._transcribe_one_shot(pcm)

    async def _transcribe_one_shot(self, pcm: bytes) -> str:
        stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
        started = time.monotonic() if self.tap else None
        resp = await self._http.post(
            f"{stt_url}/transcribe",
            content=pcm,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": str(phone_rate()),
                # Lower-powered nodes (M1 failover) run "tiny" for speed.
                "X-Model-Size": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
            },
        )
        text = resp.json().get("text", "")
        if self.tap and started is not None:
            self.tap.event(
                "stt_done",
                ms=(time.monotonic() - started) * 1000.0,
                text_len=len(text),
            )
        return text

    # ── Outbound audio ───────────────────────────────────────────

    async def _synthesize_sentence(
        self, sentence: str | SpeechChunk
    ) -> _SynthesizedSpeech:
        """Synthesize one sentence and retain its tap correlation fields."""
        if sentence == PROCESSING_CUE_SENTINEL:
            return _SynthesizedSpeech(processing_chime(), self.tap, None)
        spoken_text = sentence.text if isinstance(sentence, SpeechChunk) else sentence
        pause_after_ms = (
            sentence.pause_after_ms if isinstance(sentence, SpeechChunk) else None
        )
        tap = self.tap
        sentence_index: int | None = None
        if tap:
            self._tap_sentence_index += 1
            sentence_index = self._tap_sentence_index
            tap.event("synth_start", sentence_index=sentence_index)
        synth_started = time.monotonic() if tap else None
        voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        try:
            speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
        except ValueError:
            _warn_config_fallback("NANO_CLAW_PHONE_SPEED", _cfg("NANO_CLAW_PHONE_SPEED", "1.0"), 1.0)
            speed = 1.0
        loop = asyncio.get_running_loop()
        synth_args = (spoken_text, voice, speed)
        if pause_after_ms is not None:
            synth_args = (*synth_args, pause_after_ms)
        pcm48k = await loop.run_in_executor(None, tts_synthesize, *synth_args)
        if tap and synth_started is not None:
            tap.tts_pcm48k(pcm48k)
            tap.event(
                "synth_done",
                sentence_index=sentence_index,
                ms=(time.monotonic() - synth_started) * 1000.0,
                samples=len(pcm48k) // 2,
            )
        return _SynthesizedSpeech(pcm48k, tap, sentence_index)

    def _synthesis_failed(
        self, sentence: str | SpeechChunk, error: Exception
    ) -> None:
        log.error(
            "[phone %s] sentence synthesis failed",
            self.call_id[:8],
            exc_info=(type(error), error, error.__traceback__),
        )

    def _record_synth_ahead(self, ready: bool, wait_s: float) -> None:
        tap = self.tap
        if tap:
            tap.event(
                "synth_ahead_hit" if ready else "synth_ahead_miss",
                sentence_index=self._tap_sentence_index,
                wait_ms=wait_s * 1000.0,
            )

    async def _play_synthesized(
        self,
        speech: _SynthesizedSpeech,
        pacer: FramePacer | None = None,
    ) -> None:
        """Pace one already-synthesized sentence to the phone transport."""
        pacer = pacer or getattr(self, "_frame_pacer", None) or _phone_frame_pacer()
        tap = speech.tap
        sentence_index = speech.sentence_index
        send_started: float | None = None
        send_times: list[float] | None = [] if tap else None
        audio_s_sent = 0.0
        last_frame_audio_ms = 0.0
        if tap:
            self._active_tap_sentence_index = sentence_index
        try:
            codec = phone_codec()
            gain = self._gain_normalizer.normalize(speech.pcm48k)
            if tap:
                tap.event(
                    "gain_applied",
                    sentence_index=sentence_index,
                    measured_peak_dbfs=gain.measured_peak_dbfs,
                    applied_gain_db=gain.applied_gain_db,
                )
            frames = (
                pcm48k_to_l16_frames(gain.pcm16)
                if codec == "l16"
                else pcm48k_to_ulaw_frames(gain.pcm16)
            )
            if frames and not pacer.running:
                # The first sentence's synthesis must not consume prebuffer
                # time. Anchor only once its transport frames are ready.
                pacer.reset()
            if tap:
                outbound_rate = 16000 if codec == "l16" else 8000
                sample_width = 2 if codec == "l16" else 1
                send_started = pacer.now()
            for frame in frames:
                if self.closed or not self.speaking:
                    break  # hung up or barged in
                deadline = pacer.next_deadline()
                await asyncio.sleep(max(0.0, deadline - pacer.now()))
                if self.closed or not self.speaking:
                    break  # interruption may have landed during the sleep
                await self.ws.send_json(
                    {"event": "media", "media": {"payload": base64.b64encode(frame).decode()}}
                )
                self._feed_echo_reference(frame, codec)
                if tap and send_times is not None:
                    sent_at = pacer.now()
                    tap.outbound_frame(frame)
                    send_times.append(sent_at)
                    frame_samples = len(frame) // sample_width
                    audio_s_sent += frame_samples / outbound_rate
                    last_frame_audio_ms = frame_samples * 1000.0 / outbound_rate
        except Exception:
            log.exception("[phone %s] speak failed", self.call_id[:8])
        finally:
            if tap and send_times is not None:
                elapsed_s = (
                    pacer.now() - send_started if send_started is not None else 0.0
                )
                intervals_ms = np.diff(send_times) * 1000.0
                if len(intervals_ms):
                    interval_p50_ms, interval_p95_ms = np.percentile(
                        intervals_ms, [50, 95]
                    )
                    interval_max_ms = float(np.max(intervals_ms))
                else:
                    interval_p50_ms = interval_p95_ms = interval_max_ms = 0.0
                fields = {
                    "sentence_index": sentence_index,
                    "count": len(send_times),
                    "interval_p50_ms": float(interval_p50_ms),
                    "interval_p95_ms": float(interval_p95_ms),
                    "interval_max_ms": interval_max_ms,
                    "audio_s": audio_s_sent,
                    "elapsed_s": elapsed_s,
                    "surplus_s": audio_s_sent - elapsed_s,
                }
                if send_times:
                    fields.update(
                        first_frame_t=send_times[0],
                        last_frame_t=send_times[-1],
                        last_frame_audio_ms=last_frame_audio_ms,
                    )
                tap.event("frames_sent", **fields)
                if self._active_tap_sentence_index == sentence_index:
                    self._active_tap_sentence_index = None

    async def _speak_sentences(self, sentences) -> None:
        """Synthesize and play a sync or async sentence source in order."""
        self._stop_thinking_cue()  # real speech replaces the thinking cue
        if self.closed or not self.speaking:
            return
        self._gain_normalizer.reset()
        previous_pacer = getattr(self, "_frame_pacer", None)
        self._frame_pacer = _phone_frame_pacer()
        pipeline = SentencePipeline(
            sentences,
            self._synthesize_sentence,
            on_error=self._synthesis_failed,
            on_ahead=self._record_synth_ahead,
        )
        self._sentence_pipelines.add(pipeline)
        try:
            async with pipeline:
                async for synthesized in pipeline:
                    if self.closed or not self.speaking:
                        return
                    await self._play_synthesized(synthesized.audio)
                    if self.closed or not self.speaking:
                        return
        finally:
            self._frame_pacer = previous_pacer
            self._sentence_pipelines.discard(pipeline)

    async def _speak_chunk(self, sentence: str) -> None:
        """TTS one sentence → paced phone frames. Caller manages `speaking`."""
        if self.closed or not self.speaking or not sentence:
            return
        await self._speak_sentences((sentence,))

    async def speak(self, text: str) -> None:
        """Speak a complete text (greeting, idle prompts, error lines)."""
        if self.closed or not text:
            return
        self._playback_flush_sent = False
        self.speaking = True
        self._reset_barge_in()
        try:
            await self._speak_sentences(self._speech_units(text))
        finally:
            self.speaking = False
            if not self.interrupted:
                self.endpointer.reset()  # drop anything "heard" while talking
            # else: the endpointer was primed with the interruption — keep it
            # Idle clock restarts when we stop talking — but only the clock;
            # clearing idle_prompted here would make the idle prompt reset
            # itself and re-prompt forever instead of hanging up.
            self.last_activity = time.monotonic()


# ── HTTP handlers ────────────────────────────────────────────────

_answered: dict[str, float] = {}  # call_control_id → answer time (webhook retries dedup)
_metrics_conn = None  # set in register_phone_routes; every write is best-effort


def _node() -> str:
    return _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE").replace("https://", "").rstrip("/")


def _token_ok(request: web.Request) -> bool:
    """Authenticate only the Telnyx webhook and media-stream surfaces."""

    expected = _cfg("NANO_CLAW_PHONE_TOKEN")
    if not expected:
        return False
    # Telnyx webhook/media URLs must carry ?token= because Telnyx cannot set
    # our custom header. Operator-data endpoints must use
    # require_operator_read() instead.
    supplied = request.headers.get("X-NC-Phone-Token") or request.query.get("token")
    return secrets.compare_digest(supplied or "", expected)


OPERATOR_READ_HEADER = "X-NC-Operator-Read"
OPERATOR_READ_TOKEN_ENV = "NANO_CLAW_OPERATOR_READ_TOKEN"


def require_operator_read(request: web.Request) -> bool:
    """Enforce header-only, fail-closed authentication for operator data.

    ``NANO_CLAW_PHONE_TOKEN`` is accepted through the operator-read header
    only as a one-release migration fallback. Query parameters are
    deliberately never consulted here.
    """

    supplied = request.headers.get(OPERATOR_READ_HEADER, "")
    expected = _cfg(OPERATOR_READ_TOKEN_ENV)
    if expected:
        return secrets.compare_digest(supplied, expected)

    legacy = _cfg("NANO_CLAW_PHONE_TOKEN")
    if legacy:
        authorized = secrets.compare_digest(supplied, legacy)
        if authorized:
            log.warning(
                "operator read for %s used deprecated NANO_CLAW_PHONE_TOKEN "
                "fallback — set NANO_CLAW_OPERATOR_READ_TOKEN",
                request.path,
            )
        return authorized

    log.error(
        "operator read refused for %s: NANO_CLAW_OPERATOR_READ_TOKEN is unset "
        "— set it to enable operator-data reads",
        request.path,
    )
    return False


async def incoming_handler(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return web.Response(status=403, text="bad token")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")

    data = body.get("data", {})
    event = data.get("event_type", "")
    payload = data.get("payload", {})
    cid = str(payload.get("call_control_id", "") or "")

    if event == "call.initiated" and cid:
        safe_cid = _contained_call_id(cid)
        now = time.monotonic()
        for k, t in list(_answered.items()):  # keep the dedup map bounded
            if now - t > 3600:
                _answered.pop(k, None)
        if cid in _answered:
            return web.json_response({"ok": True, "dedup": True})
        _answered[cid] = now

        base = _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE").rstrip("/")
        ws_url = (
            base.replace("https://", "wss://", 1)
            + f"/ws/phone-media?token={_cfg('NANO_CLAW_PHONE_TOKEN')}"
        )
        caller = payload.get("from", "?")
        log.info("[phone] incoming call from %s → answering", caller)
        metrics_db.record_call_start(
            _metrics_conn, safe_cid, caller, payload.get("to", "?"), _node()
        )
        codec = phone_codec()
        async with httpx.AsyncClient() as client:
            await _telnyx_cmd(client, cid, "answer", {
                "command_id": f"answer-{cid}",
                "stream_url": ws_url,
                "stream_track": "inbound_track",
                "stream_codec": "L16" if codec == "l16" else "PCMU",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "L16" if codec == "l16" else "PCMU",
                "stream_bidirectional_sampling_rate": phone_rate(),
            })
    elif event == "call.hangup":
        safe_cid = _contained_call_id(cid)
        log.info("[phone] hangup cid=%s", safe_cid[:16])
        _answered.pop(cid, None)
        metrics_db.record_call_end(_metrics_conn, safe_cid)

    return web.json_response({"ok": True})


async def media_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    if not _token_ok(request):
        raise web.HTTPForbidden(text="bad token")
    await ws.prepare(request)

    call: PhoneCall | None = None
    try:
        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                log.debug("Skipping non-JSON telephony WS frame: %.80s", raw.data)
                continue
            event = msg.get("event", "")

            if event == "start":
                meta = msg.get("start") or {}
                cid = str(
                    meta.get("call_control_id")
                    or msg.get("stream_id")
                    or "unknown"
                )
                safe_cid = _contained_call_id(cid)
                call = await PhoneCall.create_async(ws, cid)
                log.info("[phone %s] media stream started", safe_cid[:8])
                # Calls that never hit the webhook (loopback tests, direct
                # media connections) still get a reviewable phone_calls row;
                # INSERT OR IGNORE keeps the webhook row when both fire.
                metrics_db.record_call_start(
                    _metrics_conn, safe_cid, "?", "?", _node()
                )
                greeting = _compose_greeting(
                    _cfg("NANO_CLAW_PHONE_GREETING") or call.default_greeting
                )
                # Pre-answer health gate (07-26 incident: the node answered
                # while the pipeline was dying and the caller talked into a
                # line that couldn't hear). TTS-down is survivable — the
                # greeting is cached and Piper runs in-process — but STT-down
                # is fatal, so a dead STT gets a canned apology + hangup
                # instead of a conversation that can never work.
                if await _stt_reachable(getattr(call, "_http", None)):
                    call_log.emit(
                        _metrics_conn,
                        safe_cid,
                        "assistant_turn",
                        {"text": greeting, "mode": "greeting"},
                    )
                    asyncio.create_task(call.speak(greeting))
                else:
                    log.error(
                        "[phone %s] STT unreachable at answer — apologizing"
                        " and hanging up",
                        safe_cid[:8],
                    )
                    call_log.emit(
                        _metrics_conn,
                        safe_cid,
                        "degraded_answer",
                        {"reason": "stt_unreachable"},
                    )
                    call_log.emit(
                        _metrics_conn,
                        safe_cid,
                        "assistant_turn",
                        {"text": DEGRADED_ANSWER_LINE, "mode": "error"},
                    )
                    asyncio.create_task(
                        _apologize_and_hangup(call, cid, safe_cid)
                    )
            elif event == "media" and call:
                call.feed_media((msg.get("media") or {}).get("payload", ""))
            elif event == "stop":
                log.info("[phone] media stream stopped")
                break
    finally:
        if call:
            await call.close()
            # Loopback/dropped-socket calls never get a hangup webhook; give
            # them a duration (a webhook-recorded end is never overwritten).
            # `safe_cid` is always bound when `call` is — both are assigned
            # only in the start branch.
            metrics_db.record_call_end_if_open(_metrics_conn, safe_cid)
    return ws


async def calls_handler(request: web.Request) -> web.Response:
    """Recent call log — operator-gated: caller numbers are not public data."""
    if not require_operator_read(request):
        return web.Response(status=403, text="bad token")
    try:
        conn = metrics_db.connect()
    except Exception:
        log.warning("Call-log request served degraded: metrics DB unavailable", exc_info=True)
        return web.json_response(
            {"node": _node(), "vad": get_vad_mode(), "calls": [], "error": "db unavailable"}
        )
    try:
        calls = metrics_db.recent_calls(conn)
        _attach_seam_summaries(conn, calls)
        return web.json_response(
            {"node": _node(), "vad": get_vad_mode(), "calls": calls}
        )
    finally:
        conn.close()


def _attach_seam_summaries(conn, calls: list) -> None:
    """Merge each call's stored seam aggregate into the listing, in one query.

    Purely additive and fully defensive: the call list is the operator's
    primary view, so a missing or malformed seam row must degrade to "no seam
    data for that call" rather than break the panel.
    """

    if not calls:
        return
    ids = [c.get("call_id") for c in calls if isinstance(c, dict) and c.get("call_id")]
    if not ids:
        return
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT call_id, payload FROM call_events "
            f"WHERE kind = 'audio_seams' AND call_id IN ({placeholders}) "
            "ORDER BY seq ASC",
            ids,
        ).fetchall()
    except Exception:
        log.warning("seam summary lookup failed; listing served without it", exc_info=True)
        return
    latest: dict[str, dict] = {}
    for call_id, payload in rows:
        try:
            parsed = json.loads(payload) if payload else None
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            latest[call_id] = parsed
    for call in calls:
        summary = latest.get(call.get("call_id")) if isinstance(call, dict) else None
        if not summary:
            continue
        call["seams"] = {
            "seamCount": summary.get("seamCount"),
            "harshCount": summary.get("harshCount"),
            "harshRate": summary.get("harshRate"),
            "voice": summary.get("voice"),
        }


async def vad_get_handler(request: web.Request) -> web.Response:
    """Pipeline-settings surface: which VAD is active, what's selectable."""
    return web.json_response({
        "active": get_vad_mode(),
        "options": list(VAD_MODES),
        "silero_available": silero_vad.available(),
    })


async def vad_set_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")
    mode = str(body.get("mode", "")).lower()
    if not set_vad_mode(mode):
        return web.Response(status=400, text=f"unknown mode: {mode}")
    _overrides["NANO_CLAW_PHONE_VAD"] = mode
    _persist_overrides()
    return web.json_response({"active": get_vad_mode()})


def _seam_capture_enabled() -> bool:
    return _cfg("NANO_CLAW_PHONE_SEAM_METRICS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _capture_seam_metrics(call_id: str, tap_dir) -> None:
    """Persist a compact seam summary for one finished call. Never raises.

    The seam analysis already exists (audio_inspect) but only ran on demand
    when someone opened one call in the review panel, so nothing accumulated:
    there was no way to ask whether clicks are getting worse, or which voice
    produces them. This writes the aggregate — not the per-seam list, which
    stays available on demand from the tap — as a `audio_seams` call_event,
    alongside the pipeline settings that were in force, so the numbers are
    correlatable rather than just present.

    Runs in an executor off the call path. A failure here must never affect a
    call, so everything is swallowed after logging.
    """

    try:
        from voice import audio_inspect

        analysis = audio_inspect.analyze_outbound(Path(tap_dir))
        if not analysis.get("available"):
            return
        seams = analysis.get("seams") or []
        harsh = int(analysis.get("harshCount", 0))
        try:
            speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
        except ValueError:
            speed = 1.0
        call_log.emit(
            _metrics_conn,
            call_id,
            "audio_seams",
            {
                "durationS": analysis.get("durationS"),
                "peak": analysis.get("peak"),
                "seamCount": len(seams),
                "harshCount": harsh,
                # Rate matters more than count: a long call has more seams.
                "harshRate": round(harsh / len(seams), 4) if seams else 0.0,
                "edgeSummary": analysis.get("edgeSummary"),
                # Pipeline settings in force, so a regression can be attributed.
                "voice": _cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
                "model": _cfg("NANO_CLAW_PHONE_MODEL", ""),
                "speed": speed,
                "sttSize": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
                "speechMode": phone_speech_mode(),
                "speechVersion": SPEECH_COMPILER_VERSION,
                "sampleRate": analysis.get("sampleRate"),
            },
        )
    except Exception:
        log.exception("[phone %s] seam metric capture failed", str(call_id)[:8])


def _schedule_seam_capture(call_id: str, tap_dir) -> None:
    """Fire-and-forget the seam analysis so call teardown never waits on numpy."""

    if not tap_dir or not _seam_capture_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.run_in_executor(None, _capture_seam_metrics, call_id, tap_dir)


async def config_get_handler(request: web.Request) -> web.Response:
    """Pipeline-settings surface: the phone line's live-tunable config."""
    try:
        speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
    except ValueError:
        _warn_config_fallback("NANO_CLAW_PHONE_SPEED", _cfg("NANO_CLAW_PHONE_SPEED", "1.0"), 1.0)
        speed = 1.0
    return web.json_response({
        "voice": _cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
        "model": _cfg("NANO_CLAW_PHONE_MODEL", ""),  # "" → server default
        "speed": speed,
        "stt_size": _cfg("NANO_CLAW_PHONE_STT_SIZE", "base"),
        "active_calls": len(_active_calls),
        "speech_mode": phone_speech_mode(),
        "speech_version": SPEECH_COMPILER_VERSION,
        "display_number": _cfg("NANO_CLAW_PHONE_DISPLAY_NUMBER", ""),
    })


async def config_set_handler(request: web.Request) -> web.Response:
    """Set runtime overrides from the web UI. Voice applies to the next
    spoken sentence (even mid-call); model applies to the next agent turn.
    Overrides persist to the data volume and reload at boot; .env remains
    the factory default underneath."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")

    if "voice" in body:
        voice = str(body["voice"])
        if voice_catalog.lookup(voice) is None:
            return web.Response(status=400, text=f"unknown voice: {voice}")
        _overrides["NANO_CLAW_PHONE_VOICE"] = voice
    if "model" in body:
        model = str(body["model"]).strip()
        if model:
            _overrides["NANO_CLAW_PHONE_MODEL"] = model
        else:
            _overrides.pop("NANO_CLAW_PHONE_MODEL", None)  # back to server default
    if "speed" in body:
        try:
            speed = float(body["speed"])
        except (TypeError, ValueError):
            return web.Response(status=400, text="bad speed")
        if not 0.5 <= speed <= 2.0:
            return web.Response(status=400, text="speed out of range (0.5-2.0)")
        _overrides["NANO_CLAW_PHONE_SPEED"] = str(speed)
    if "stt_size" in body:
        size = str(body["stt_size"])
        if size not in ("tiny", "base", "small", "medium"):
            return web.Response(status=400, text=f"unknown stt size: {size}")
        # Read per transcription request, so this applies to the caller's
        # next utterance even mid-call.
        _overrides["NANO_CLAW_PHONE_STT_SIZE"] = size
    if "speech_mode" in body:
        mode = str(body["speech_mode"]).strip().lower()
        if mode not in ("prepared", "batch", "raw"):
            return web.Response(status=400, text=f"unknown speech mode: {mode}")
        # "prepared" streams the speech compiler sentence-by-sentence;
        # "batch" compiles only after the full reply (the pre-streaming
        # behavior, kept as an escape hatch); "raw" skips the compiler.
        # Read per response, so it applies to the next agent turn even
        # mid-call. Lets the console A/B the modes live.
        _overrides["NANO_CLAW_PHONE_SPEECH_PREPARATION"] = (
            "1" if mode == "prepared" else mode
        )

    _persist_overrides()
    log.info("phone config updated: voice=%s model=%s speed=%s (%d active call(s))",
             _cfg("NANO_CLAW_PHONE_VOICE", "af_heart"),
             _cfg("NANO_CLAW_PHONE_MODEL") or "(default)",
             _cfg("NANO_CLAW_PHONE_SPEED", "1.0"), len(_active_calls))
    return await config_get_handler(request)


def _retention_days() -> float:
    """Call-content retention window in days; 0/empty disables the sweep."""
    raw = _cfg("NANO_CLAW_CALL_RETENTION_DAYS", "30")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        _warn_config_fallback("NANO_CLAW_CALL_RETENTION_DAYS", raw, 30)
        return 30.0


async def _retention_sweep_loop() -> None:
    try:
        while True:
            days = _retention_days()
            if days > 0:
                tap_root = os.environ.get("NANO_CLAW_PHONE_TAP_DIR", DEFAULT_TAP_ROOT)
                call_log.sweep(_metrics_conn, tap_root, days)
            await asyncio.sleep(24 * 3600)
    except asyncio.CancelledError:
        return  # health-ok: sweep loop cancellation is orderly shutdown


async def _retention_sweep_context(app: web.Application):
    task = asyncio.create_task(_retention_sweep_loop())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


DEGRADED_ANSWER_LINE = (
    "I'm sorry — this line is having technical trouble right now. "
    "Please call back in a few minutes."
)


async def _stt_reachable(http) -> bool:
    """Fail-open pre-answer probe: only a definitive connection failure marks
    the STT service down (any HTTP answer, even an error status, means the
    service is there). ``http`` is the call's client; absent → assume healthy
    so stubbed/test calls never trip the gate."""
    if http is None:
        return True
    stt_url = os.environ.get("STT_SERVICE_URL", "http://host.docker.internal:8200")
    try:
        await http.get(f"{stt_url}/health", timeout=1.5)
        return True
    except Exception:
        return False


async def _apologize_and_hangup(
    call,
    telnyx_call_id: str,
    safe_call_id: str,
) -> None:
    """Degraded answer: canned apology (cache/piper-backed — no service
    dependency) then hang up. Best-effort: media-only calls have no Telnyx
    leg to hang up."""
    try:
        await call.speak(DEGRADED_ANSWER_LINE)
    except Exception:
        log.exception("[phone %s] degraded apology failed", safe_call_id[:8])
    try:
        await _telnyx_cmd(call._http, telnyx_call_id, "hangup", {})
    except Exception:
        log.exception("[phone %s] degraded hangup failed", safe_call_id[:8])
    call.closed = True


async def _warm_greeting_cache(app: web.Application) -> None:
    """Pre-synthesize the greeting at boot so pickup is instant.

    The 2026-07-26 incident opened with 32s of dead air: a cold TTS
    synthesizing the same fixed greeting the line speaks on every call.
    Retries while the native TTS services finish starting; fallback renders
    are never cached (tts.synthesize guarantees that), so retrying until
    ``is_cached`` proves the real voice landed.
    """

    async def warm() -> None:
        loop = asyncio.get_running_loop()
        voice = _cfg("NANO_CLAW_PHONE_VOICE", "af_heart")
        try:
            speed = float(_cfg("NANO_CLAW_PHONE_SPEED", "1.0") or 1.0)
        except ValueError:
            speed = 1.0
        greeting = _compose_greeting(
            _cfg("NANO_CLAW_PHONE_GREETING") or flow_mode_greeting()
        )
        texts = [
            getattr(unit, "text", unit) for unit in PhoneCall._speech_units(greeting)
        ]
        texts = [text for text in texts if isinstance(text, str) and text.strip()]
        for attempt in range(1, 7):
            try:
                for text in texts:
                    if not tts_is_cached(text, voice, speed):
                        await loop.run_in_executor(
                            None, tts_synthesize, text, voice, speed
                        )
                if all(tts_is_cached(text, voice, speed) for text in texts):
                    log.info(
                        "[phone] greeting cache warm: %d units (attempt %d)",
                        len(texts),
                        attempt,
                    )
                    return
            except Exception:
                log.exception("[phone] greeting warm-up attempt %d failed", attempt)
            await asyncio.sleep(20)
        log.warning(
            "[phone] greeting cache never warmed — first pickup synthesizes live"
        )

    app["phone_greeting_warm_task"] = asyncio.create_task(warm())


def register_phone_routes(app: web.Application) -> None:
    """Attach gateway routes when NANO_CLAW_PHONE=1 (no-op otherwise)."""
    global _metrics_conn
    if not phone_enabled():
        return
    _load_persisted_overrides()
    missing = [
        name
        for name in ("TELNYX_API_KEY", "NANO_CLAW_PHONE_WEBHOOK_BASE", "NANO_CLAW_PHONE_TOKEN")
        if not _cfg(name)
    ]
    if missing:
        log.error("[phone] NANO_CLAW_PHONE=1 but missing env: %s — gateway NOT registered", missing)
        return
    _metrics_conn = metrics_db.init_db()
    call_log.ensure_schema(_metrics_conn)
    # Imported here, not at module top: call_review imports this module for
    # the token check and metrics connection.
    from voice import call_review

    app.on_startup.append(_warm_greeting_cache)
    app.router.add_post("/api/phone/incoming", incoming_handler)
    app.router.add_get("/ws/phone-media", media_ws_handler)
    app.router.add_get("/api/calls", calls_handler)
    call_review.register_call_review_routes(app)
    app.router.add_get("/api/phone/vad", vad_get_handler)
    app.router.add_post("/api/phone/vad", vad_set_handler)
    app.router.add_get("/api/phone/config", config_get_handler)
    app.router.add_post("/api/phone/config", config_set_handler)
    app.cleanup_ctx.append(_retention_sweep_context)
    log.info("[phone] Telnyx gateway registered (webhook base: %s, VAD: %s)",
             _cfg("NANO_CLAW_PHONE_WEBHOOK_BASE"), get_vad_mode())
