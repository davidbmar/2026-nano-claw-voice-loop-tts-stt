"""Failure-isolated audio and timing capture for real-time voice calls.

``CallTap`` records four reusable observation points in a voice pipeline:
encoded inbound frames after transport receipt, source PCM immediately after
48 kHz TTS, encoded outbound frames after transport send, and named monotonic
timing events supplied by the caller.  The phone gateway uses these points for
endpoint, STT, agent, synthesis, pacing, and barge-in measurements; a future
web voice path can use the same API without phone-specific behavior here.

The tap has a strict never-crash contract.  Creation and every public method
catch all tap failures internally.  The first failure logs one warning,
closes any usable files, and permanently disables that tap so capture can
never interrupt a live call.
"""

from __future__ import annotations

import json
import logging
import os
import time
import wave
from pathlib import Path
from typing import Any, TextIO

from voice.phone_audio import ulaw_decode

log = logging.getLogger("nano-claw.phone_tap")

DEFAULT_TAP_ROOT = "/tmp/nano-claw-phone-taps"


class CallTap:
    """Per-call WAV and JSONL capture with failure isolation."""

    def __init__(
        self,
        call_id: str,
        codec: str,
        inbound_rate: int,
        outbound_rate: int,
    ) -> None:
        self.call_id = str(call_id)
        self.codec = str(codec).lower()
        self.inbound_rate = int(inbound_rate)
        self.outbound_rate = int(outbound_rate)
        self.directory: Path | None = None
        self._inbound: wave.Wave_write | None = None
        self._tts: wave.Wave_write | None = None
        self._outbound: wave.Wave_write | None = None
        self._timings: TextIO | None = None
        self._disabled = False
        self._warning_logged = False

    @classmethod
    def create(
        cls,
        call_id: str,
        codec: str,
        inbound_rate: int,
        outbound_rate: int,
    ) -> CallTap | None:
        """Create capture files for one call, or return ``None`` when off.

        ``call_id`` names the output subdirectory. ``codec`` is ``pcmu`` for
        G.711 mu-law bytes or a linear PCM16 codec name. Rates are integer Hz
        for the inbound and outbound WAV files. Only the exact environment
        value ``NANO_CLAW_PHONE_TAP=1`` enables capture.
        """
        tap: CallTap | None = None
        try:
            if os.environ.get("NANO_CLAW_PHONE_TAP") != "1":
                return None
            tap = cls(call_id, codec, inbound_rate, outbound_rate)
            tap._open()
            return tap
        except BaseException as exc:
            if tap is not None:
                tap._disable(exc)
            else:
                cls._warn_safely(call_id, exc)
            return None

    def inbound_frame(self, raw: bytes) -> None:
        """Append one received frame in its transport codec.

        ``raw`` is encoded PCMU when this tap's codec is ``pcmu``; otherwise
        it is little-endian mono PCM16. The resulting WAV uses
        ``inbound_rate`` Hz.
        """
        if self._disabled:
            return
        try:
            pcm = ulaw_decode(raw).tobytes() if self.codec == "pcmu" else raw
            if self._inbound is None:
                raise RuntimeError("inbound WAV is not open")
            self._inbound.writeframesraw(pcm)
        except BaseException as exc:
            self._disable(exc)

    def tts_pcm48k(self, pcm: bytes) -> None:
        """Append mono PCM16 TTS source bytes sampled at 48,000 Hz."""
        if self._disabled:
            return
        try:
            if self._tts is None:
                raise RuntimeError("TTS WAV is not open")
            self._tts.writeframesraw(pcm)
        except BaseException as exc:
            self._disable(exc)

    def outbound_frame(self, raw: bytes) -> None:
        """Append one successfully sent frame in its transport codec.

        ``raw`` is encoded PCMU when this tap's codec is ``pcmu``; otherwise
        it is little-endian mono PCM16. The resulting WAV uses
        ``outbound_rate`` Hz.
        """
        if self._disabled:
            return
        try:
            pcm = ulaw_decode(raw).tobytes() if self.codec == "pcmu" else raw
            if self._outbound is None:
                raise RuntimeError("outbound WAV is not open")
            self._outbound.writeframesraw(pcm)
        except BaseException as exc:
            self._disable(exc)

    def event(self, name: str, **fields: Any) -> None:
        """Append one timing event with an automatic monotonic timestamp.

        ``name`` is the event label. ``fields`` must be JSON-serializable and
        should state units in their keys (for example ``ms`` or ``samples``).
        The automatically supplied ``t`` value is monotonic seconds.
        """
        if self._disabled:
            return
        try:
            if self._timings is None:
                raise RuntimeError("timings JSONL is not open")
            record = dict(fields)
            record["event"] = str(name)
            record["t"] = time.monotonic()
            self._timings.write(json.dumps(record, sort_keys=True) + "\n")
            self._timings.flush()
        except BaseException as exc:
            self._disable(exc)

    def close(self) -> None:
        """Finalize all WAV headers and close this tap without raising."""
        if self._disabled:
            return
        try:
            self._close_handles()
            self._disabled = True
        except BaseException as exc:
            self._disable(exc)

    def _open(self) -> None:
        if self.inbound_rate <= 0 or self.outbound_rate <= 0:
            raise ValueError("tap sample rates must be positive")
        root = Path(os.environ.get("NANO_CLAW_PHONE_TAP_DIR", DEFAULT_TAP_ROOT))
        self.directory = root / self.call_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._inbound = self._open_wav(self.directory / "inbound.wav", self.inbound_rate)
        self._tts = self._open_wav(self.directory / "tts_48k.wav", 48_000)
        self._outbound = self._open_wav(self.directory / "outbound.wav", self.outbound_rate)
        self._timings = (self.directory / "timings.jsonl").open("w", encoding="utf-8")

    @staticmethod
    def _open_wav(path: Path, rate: int) -> wave.Wave_write:
        output = wave.open(str(path), "wb")
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        return output

    def _disable(self, exc: BaseException) -> None:
        if not self._warning_logged:
            self._warn_safely(self.call_id, exc)
            self._warning_logged = True
        self._disabled = True
        try:
            self._close_handles()
        except BaseException:
            pass

    def _close_handles(self) -> None:
        first_error: BaseException | None = None
        for attribute in ("_inbound", "_tts", "_outbound", "_timings"):
            handle = getattr(self, attribute)
            setattr(self, attribute, None)
            if handle is None:
                continue
            try:
                handle.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _warn_safely(call_id: str, exc: BaseException) -> None:
        try:
            log.warning("[phone %s] call tap disabled: %s", str(call_id)[:8], exc)
        except BaseException:
            pass
