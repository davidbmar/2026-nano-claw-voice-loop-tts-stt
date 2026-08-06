"""Live-session adapter for bounded goal-region voice flows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from voice.calendar_client import (
    CalendarClient,
    CalendarError,
    availability_snapshot,
    load_calendar_settings,
)
from voice.goal_region import (
    BUSINESS_TIMEZONE,
    FreeWindow,
    GoalRegionRunner,
    RegionConfig,
)
from voice.scheduling_domains import (
    DOMAINS,
    SCHEDULER_GREETING,
    SchedulingDomain,
    region_config_for,
)
from voice.turn_delegate import DelegateUrlRefused, validate_delegate_url

if TYPE_CHECKING:
    from voice.booking import BookingFlow

log = logging.getLogger("nano-claw.flow")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AVAILABILITY_PATH = REPO_ROOT / "scripts/scheduling_eval/availability.json"
FlowOutcome = Literal["booked", "escape", "budget", "not_booked"]


class FlowModeConfig(TypedDict):
    """One labeled assistant mode exposed by the voice configuration API."""

    label: str
    profile: str
    scheduler: bool
    domain: NotRequired[str]
    abstract: str


FLOW_MODES: dict[str, FlowModeConfig] = {
    # First entry = top of the dropdown. The base layer is the persona-free
    # voice-assistant identity every other profile composes on top of;
    # selectable here so it can be tested directly.
    "base": {
        "label": "Base",
        "profile": "base",
        "scheduler": False,
        "abstract": (
            "The self-aware voice-assistant identity layer under every "
            "persona — select to test it directly."
        ),
    },
    "none": {
        "label": "None",
        "profile": "none",
        "scheduler": False,
        "abstract": "Plain assistant with no persona and no document knowledge.",
    },
    "spacechannel": {
        "label": "Spacechannel",
        "profile": "spacechannel",
        "scheduler": False,
        "abstract": "Space Channel persona for general conversation and demos.",
    },
    "intelligence": {
        "label": "Document Intelligence",
        "profile": "intelligence",
        "scheduler": False,
        "abstract": (
            "Answers from ingested documents with citations; deep strategy "
            "analysis on request. Answers come from whichever documents are "
            "ticked in the Documents panel."
        ),
    },
    "riff": {
        "label": "Code: Riff",
        "profile": "riff",
        "scheduler": False,
        "abstract": (
            "Answers questions about the Riff codebase (~/src/riff) from its "
            "indexed source files."
        ),
    },
    "nanoclaw": {
        "label": "Code: nano-claw",
        "profile": "nanoclaw",
        "scheduler": False,
        "abstract": (
            "Answers questions about the nano-claw voice agent codebase from "
            "its indexed source files."
        ),
    },
    "intelligence-platform": {
        "label": "Code: intelligence-platform",
        "profile": "intelligence-platform",
        "scheduler": False,
        "abstract": (
            "Answers questions about the intelligence-platform codebase from "
            "its indexed source files."
        ),
    },
    "replicantpm": {
        "label": "Replicant PM",
        "profile": "replicantpm",
        "scheduler": False,
        "abstract": "Replicant product-manager persona.",
    },
    "scheduler": {
        "label": "Plumber Scheduler",
        # If the scheduler is unavailable or ends, retain the Space Channel
        # fallback behavior for subsequent normal agent turns.
        "profile": "spacechannel",
        "scheduler": True,
        "domain": "plumber",
        "abstract": "Goal-driven plumber scheduling flow with live availability.",
    },
    "lawyer": {
        "label": "Lawyer Scheduler",
        # Non-scheduling turns (flow completed, escaped, or unavailable) fall
        # back to the neutral base assistant — a law office must never answer
        # as the Space Channel persona. Not "none": that keeps the configured
        # defaults.systemPrompt, which is the Space Channel persona in the
        # container config. Plumber above keeps its Space Channel fallback on
        # purpose: that demo runs on the Space Channel line.
        "profile": "base",
        "scheduler": True,
        "domain": "lawyer",
        "abstract": (
            "Goal-driven law-office scheduling with live calendar booking."
        ),
    },
    # The only mode whose turns never reach nano-claw's own model. Everything
    # acoustic stays here; the words come from another app over the turn-delegate
    # contract (riff-builder `docs/turn-delegate-contract.md`).
    #
    # `profile` is "none" because no persona of ours may colour a reply we did
    # not author — the delegate's text is spoken as written.
    #
    # The URL is deliberately NOT a key here. It is per-connection state on the
    # Session, defaulting from NANO_CLAW_DELEGATE_URL, because the contract says
    # "one delegate URL == one conversation". Putting it on this process-global
    # entry would give one fact two homes, and two homes is where every drift bug
    # in this system has started.
    "delegate": {
        "label": "Turn Delegate",
        "profile": "none",
        "scheduler": False,
        "abstract": (
            "Routes every turn to another app over HTTP and speaks its reply; "
            "nano-claw supplies only the voice."
        ),
    },
    # A listening mode, not an assistant mode. Every utterance is transcribed,
    # printed, and handed to gemma4 on the M5 — and nothing is ever spoken back.
    # Built for j-space probing, where the point is to feed the local model real
    # speech and inspect its internals on that host.
    #
    # It is the mirror of "delegate" above: delegate supplies only the voice and
    # borrows the words, this supplies only the ears and returns no voice at all.
    #
    # `profile` is "none" for a sharper reason than delegate's. A probe measuring
    # how a model represents speech must not have a persona shaping the prompt,
    # or the persona becomes part of what is measured.
    "transcribe": {
        "label": "Transcribe Mode",
        "profile": "none",
        "scheduler": False,
        "abstract": (
            "Transcribes everything heard and forwards it to gemma4 on the M5. "
            "Never speaks and never answers — a listening probe."
        ),
    },
}
DEFAULT_FLOW_MODE = "spacechannel"

# Spoken phone greeting per mode, so the intro always matches the brain that
# answers (07-27 bug: every restart re-read NANO_CLAW_VOICE_FLOW and the line
# played the Space Channel intro over Document Intelligence answers).
# Scheduler modes carry their own flow greeting and never consult this table;
# the recording notice is appended separately by the phone layer.
_MODE_GREETINGS: dict[str, str] = {
    "spacechannel": (
        "You've reached Space Channel. Ask me about rocket launches, "
        "U F O cases, space news, podcasts, or live shows."
    ),
    "intelligence": (
        "You've reached the document intelligence assistant. Ask me anything "
        "about the documents currently loaded."
    ),
    "riff": (
        "You've reached the Riff codebase assistant. Ask me about the Riff "
        "source."
    ),
    "nanoclaw": (
        "You've reached the nano-claw codebase assistant. Ask me how this "
        "voice system works."
    ),
    "intelligence-platform": (
        "You've reached the intelligence-platform codebase assistant. Ask me "
        "about that codebase."
    ),
    "replicantpm": (
        "You've reached the Replicant product assistant. How can I help?"
    ),
    # Deliberately names nobody. The delegate contract (v0) has no greeting
    # exchange, so the gateway does not know whose line this is — and a greeting
    # that guessed would be the assistant claiming an identity it was never
    # given. The delegate introduces itself on the first reply.
    "delegate": "Hello — how can I help you today?",
    # The one line this mode ever speaks, and it is a disclosure rather than an
    # answer: the phone layer plays a greeting before any turn runs, so a
    # transcribe-mode call would otherwise answer to total silence and read as a
    # dead line. Saying plainly that nothing will reply is also the honest thing
    # to do to someone whose speech is being captured for research.
    #
    # The "never speaks" contract belongs to the turn handler, which is where it
    # is enforced. This greeting is outside that path by design.
    "transcribe": (
        "You've reached a transcription-only research line. Everything you say "
        "is transcribed and recorded, and nothing will reply."
    ),
}
_GENERIC_GREETING = (
    "Hello! You've reached the nano-claw voice assistant. How can I help?"
)


def flow_mode_greeting(mode: str | None = None) -> str:
    """Phone greeting matching the active (or given) assistant mode."""

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    if active is None:
        active = DEFAULT_FLOW_MODE
    return _MODE_GREETINGS.get(active, _GENERIC_GREETING)
_flow_mode: str | None = None
# Keep the provider-verified dropdown registry centralized here. These exact
# IDs were checked against the live provider /v1/models endpoints on 2026-07-17.
REGION_MODELS = {
    "claude-haiku-4-5": "Claude Haiku 4.5 — proven",
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash — cheapest",
    "xai/grok-4.20-0309-non-reasoning": "Grok 4.20 fast",
    "xai/grok-4.3": "Grok 4.3",
    "gemini/gemini-2.5-flash-lite": "Gemini Flash-Lite — fastest TTFT",
    "groq/openai/gpt-oss-20b": "GPT-OSS 20B (Groq) — fastest",
    "openrouter/meta-llama/llama-4-scout": "Llama 4 Scout — 11/11",
    "openrouter/openai/gpt-oss-20b:nitro": "GPT-OSS 20B (fastest route)",
    "local/qwen3:14b": "Local Ollama qwen3:14b",
}
DEFAULT_REGION_MODEL = "claude-haiku-4-5"
_region_model: str | None = None
# None = "nobody has chosen", so fall through to the environment. "" = an
# operator explicitly cleared it, which must NOT fall back to the env value —
# clearing has to actually clear, or the console would appear to disarm a
# delegate that is still armed.
_delegate_url: str | None = None
_AVAILABILITY_ERRORS = (
    OSError,
    json.JSONDecodeError,
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    IndexError,
)


@dataclass
class FlowReply:
    """One flow response, including any deterministic terminal outcome."""

    text: str
    done: bool
    outcome: FlowOutcome | None
    slots: dict
    rejected: list[str] = field(default_factory=list)
    turns_used: int | None = None
    max_turns: int | None = None
    supervisor_ms: float | None = None
    event_id: str | None = None


def _normalize_flow_mode(mode: str) -> str | None:
    """Return a registered mode, including the legacy ``off`` alias."""

    if mode == "off":
        return DEFAULT_FLOW_MODE
    return mode if mode in FLOW_MODES else None


def get_flow_mode() -> str:
    """Return the runtime flow selection, initialized from the environment."""

    global _flow_mode
    if _flow_mode is None:
        configured = os.environ.get("NANO_CLAW_VOICE_FLOW", "").strip()
        _flow_mode = _normalize_flow_mode(configured) or DEFAULT_FLOW_MODE
    return _flow_mode


def set_flow_mode(mode: str) -> bool:
    """Select a flow for new calls and browser sessions."""

    global _flow_mode
    normalized = _normalize_flow_mode(mode) if isinstance(mode, str) else None
    if normalized is None:
        return False
    _flow_mode = normalized
    log.info("Voice flow switched to %s (applies to new sessions/calls)", normalized)
    return True


def phone_mode_pins() -> dict[str, str]:
    """DID -> mode pins: numbers that always answer in one declared mode.

    The console MODE selector is one shared dial, which is right for a demo
    box and wrong for a phone number with an identity: switching the browser
    to a codebase assistant must not change what a business line says to the
    next caller (2026-08-02: the CA number briefly answered as the nano-claw
    codebase assistant because an operator was exploring the dropdown).

    Read per call, like delegate_starts, so a pin can be added or removed
    without a restart. Unknown modes are dropped with a warning rather than
    honoured blindly — a typo must not silence a line. Scheduler modes cannot
    be pinned yet (their flow sessions are built from the global mode before
    the DID is known); a scheduler pin is ignored with a warning.
    """

    raw = os.environ.get("NANO_CLAW_PHONE_MODE_PINS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("NANO_CLAW_PHONE_MODE_PINS is not valid JSON — ignored")
        return {}
    if not isinstance(data, dict):
        log.warning("NANO_CLAW_PHONE_MODE_PINS must be a JSON object — ignored")
        return {}
    pins: dict[str, str] = {}
    for did, mode in data.items():
        normalized = _normalize_flow_mode(str(mode))
        if normalized is None:
            log.warning("phone mode pin %s -> %r names no known mode — dropped", did, mode)
            continue
        if FLOW_MODES[normalized].get("scheduler"):
            log.warning("phone mode pin %s -> %s is a scheduler mode — not yet "
                        "supported for pinning, dropped", did, normalized)
            continue
        pins[str(did)] = normalized
    return pins


def pinned_mode_for(did: str | None) -> str | None:
    """The pinned mode for a called number, or None to follow the global mode."""

    if not did:
        return None
    return phone_mode_pins().get(str(did))


def get_flow_profile(mode: str | None = None) -> str:
    """Return the agent profile paired with a runtime assistant mode."""

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    if active is None:
        active = DEFAULT_FLOW_MODE
    return FLOW_MODES[active]["profile"]


def is_delegate_mode(mode: str | None = None) -> bool:
    """True when turns must be routed to another app rather than to our model."""

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    return active == "delegate"


def is_transcribe_mode(mode: str | None = None) -> bool:
    """True when turns are transcribed and forwarded, and nothing is spoken.

    The one mode that produces no audio. Callers must treat this as a hard
    property rather than a preference: the turn is consumed before any
    synthesis path is reachable, not muted on the way out.
    """

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    return active == "transcribe"


def delegate_allowed_hosts() -> frozenset[str]:
    """Non-loopback hosts a delegate URL may point at.

    Loopback needs no entry. Anything else must be named here, because this URL
    receives everything the human says, on every turn.
    """

    raw = os.environ.get("NANO_CLAW_DELEGATE_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def default_delegate_url() -> str:
    """The delegate URL a new session starts with, or "" for none.

    A *default*, not the truth: the session owns its URL, because the contract
    pairs one URL with one conversation. This only decides where a connection
    points before anyone says otherwise.

    Precedence is the one this module already uses twice — for the flow mode and
    for the region model: an operator's runtime choice beats the environment.
    Per-session state then beats both, which is `resolve_delegate_url`'s job.

    An unset or refused value yields "", which leaves delegate mode inert rather
    than dialling something unvetted — the same fail-closed choice as
    `validate_delegate_url` itself.
    """

    if _delegate_url is not None:
        return _delegate_url
    configured = os.environ.get("NANO_CLAW_DELEGATE_URL", "").strip()
    if not configured:
        return ""
    try:
        return validate_delegate_url(configured, allowed_hosts=delegate_allowed_hosts())
    except DelegateUrlRefused as exc:
        log.warning("NANO_CLAW_DELEGATE_URL refused, delegate mode inert: %s", exc)
        return ""


def set_default_delegate_url(url: str) -> bool:
    """Point new conversations at `url`. "" clears it. False if refused.

    Validated here rather than at dial time so a bad value is rejected while an
    operator is looking at the answer. Returning False instead of raising keeps
    the refusal a 400 to the operator, not a 500.
    """

    global _delegate_url
    if not isinstance(url, str):
        return False
    candidate = url.strip()
    if candidate:
        try:
            candidate = validate_delegate_url(
                candidate, allowed_hosts=delegate_allowed_hosts())
        except DelegateUrlRefused as exc:
            log.warning("delegate URL refused: %s", exc)
            return False
    _delegate_url = candidate
    log.info("Delegate URL set to %s (applies to new conversations)",
             candidate or "(none)")
    return True


def active_scheduling_domain(mode: str | None = None) -> str | None:
    """Return the selected scheduling domain, if the mode enables one."""

    active = get_flow_mode() if mode is None else _normalize_flow_mode(mode)
    if active is None:
        return None
    config = FLOW_MODES[active]
    domain_id = config.get("domain")
    if not config["scheduler"] or domain_id not in DOMAINS:
        return None
    return domain_id


def get_region_model() -> str:
    """Return the runtime scheduler model or its environment/default fallback."""

    if _region_model is not None:
        return _region_model
    configured = os.environ.get("SCHED_EVAL_MODEL", "").strip()
    return configured or DEFAULT_REGION_MODEL


def set_region_model(name: str) -> bool:
    """Select the supervisor model used when the next scheduler turn starts."""

    global _region_model
    if not isinstance(name, str) or name not in REGION_MODELS:
        return False
    _region_model = name
    log.info("Scheduler model switched to %s (applies to new turns)", name)
    return True


def scheduler_flow_enabled() -> bool:
    """Compatibility helper for callers that only need a boolean check."""

    return active_scheduling_domain() is not None


def scheduler_region_config(
    digest: str, *, domain: SchedulingDomain | None = None
) -> RegionConfig:
    """Return the scheduler configuration shared by live voice and evals."""

    selected_domain = DOMAINS["plumber"] if domain is None else domain
    return region_config_for(selected_domain, digest)


def load_free_windows(availability: dict) -> list[FreeWindow]:
    """Convert the persisted availability digest to validator windows."""

    return [
        FreeWindow(
            start=datetime.fromisoformat(window["start"]),
            end=datetime.fromisoformat(window["end"]),
        )
        for windows in availability["days"].values()
        for window in windows
    ]


def anchor_availability(availability: dict, *, now: datetime | None = None) -> dict:
    """Shift a static availability fixture onto upcoming days.

    A frozen fixture goes stale the moment its week passes: on 2026-07-27 the
    plumber scheduler was still offering July 17-23, so every slot a caller
    could accept was in the past. The calendar component therefore anchors to
    the current day at launch and prefers upcoming days.

    The shift is a whole number of WEEKS, so each day keeps its weekday and the
    fixture's weekend/business-day shape (which the scheduling evals encode)
    survives. Availability that already starts in the future — every live
    calendar snapshot, which begins at tomorrow — is returned untouched.
    Best-effort: malformed input is returned as-is rather than raising, because
    scheduling must degrade rather than break a live call.
    """

    try:
        days = availability.get("days") or {}
        if not days:
            return availability
        current = (now or datetime.now(ZoneInfo(availability["timezone"]))).date()
        first_day = min(date.fromisoformat(day) for day in days)
        earliest_allowed = current + timedelta(days=1)
        if first_day >= earliest_allowed:
            return availability
        weeks = -(-(earliest_allowed - first_day).days // 7)  # ceil division
        shift = timedelta(weeks=weeks)
    except Exception:
        log.exception("availability anchoring failed; using it unshifted")
        return availability

    def _shift(value: str) -> str:
        return (datetime.fromisoformat(value) + shift).isoformat()

    shifted_days: dict[str, list[dict[str, str]]] = {}
    for day, windows in days.items():
        shifted_day = (date.fromisoformat(day) + shift).isoformat()
        shifted_days[shifted_day] = [
            {"start": _shift(window["start"]), "end": _shift(window["end"])}
            for window in windows
        ]
    anchored = dict(availability)
    anchored["days"] = shifted_days
    log.info(
        "availability anchored forward %d week(s): now starts %s",
        weeks,
        min(shifted_days),
    )
    return anchored


def _render_availability_window(window: dict) -> str:
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    capacity_minutes = int((end - start).total_seconds() // 60)
    return f"{start:%H:%M}–{end:%H:%M} (fits ≤{capacity_minutes}m)"


def availability_digest(availability: dict, *, now: datetime | None = None) -> str:
    """Render availability for the goal-region supervisor prompt.

    The digest opens with today's date so the supervisor can resolve relative
    days ("Monday", "tomorrow") against a known anchor instead of guessing,
    and can notice if it is ever handed days that contradict it.
    """

    try:
        current = now or datetime.now(ZoneInfo(availability["timezone"]))
    except Exception:
        current = now or datetime.now()
    lines = [
        f"Today is {current:%A %B} {current.day}, {current:%Y} ({current.date().isoformat()}); "
        f"the current time is {current:%H:%M}. Only ever offer or book times after now.",
        f"All times are {availability['timezone']}; business hours are 08:00–18:00.",
        "A visit must fit inside one listed half-open free window:",
    ]
    for day, windows in availability["days"].items():
        parsed_day = datetime.fromisoformat(day)
        label = f"{parsed_day:%A %B} {parsed_day.day}"
        rendered = ", ".join(
            _render_availability_window(window)
            for window in windows
        )
        lines.append(f"- {label} ({day}): {rendered or 'no availability'}")
    return "\n".join(lines)


def digest_from_windows(windows: list[FreeWindow], timezone: str) -> str:
    """Render runner free windows without mutating them."""

    days: dict[str, list[dict[str, str]]] = {}
    for window in sorted(windows, key=lambda item: (item.start, item.end)):
        day = window.start.date().isoformat()
        days.setdefault(day, []).append(
            {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
            }
        )
    return availability_digest({"timezone": timezone, "days": days})


class RefusalSession:
    """Terminal scheduler surface used when a live calendar cannot be trusted."""

    greeting = SCHEDULER_GREETING

    def __init__(self, domain: SchedulingDomain) -> None:
        self._domain = domain
        self.greeting = domain.greeting
        self._turns_used = 0

    @property
    def goal(self) -> str:
        return self._domain.goal

    @property
    def slots(self) -> dict:
        return {}

    @property
    def turns_used(self) -> int:
        return self._turns_used

    @property
    def max_turns(self) -> int:
        return self._domain.max_turns

    async def reply(self, caller_text: str) -> FlowReply:
        """Speak one terminal apology without attempting stale negotiation."""

        del caller_text
        self._turns_used = 1
        return FlowReply(
            text=self._domain.apology_unavailable,
            done=True,
            outcome="not_booked",
            slots={},
            turns_used=self.turns_used,
            max_turns=self.max_turns,
        )


class FlowSession:
    """Async voice-facing wrapper around the synchronous goal-region runner."""

    greeting = SCHEDULER_GREETING

    def __init__(
        self,
        booking: BookingFlow,
        greeting: str = SCHEDULER_GREETING,
    ) -> None:
        self._booking = booking
        self.greeting = greeting
        self._turn_lock = asyncio.Lock()
        self._inflight_turn: asyncio.Future | None = None

    @property
    def goal(self) -> str:
        runner = getattr(self._booking, "_runner", None)
        return str(getattr(getattr(runner, "config", None), "goal", ""))

    @property
    def slots(self) -> dict:
        runner = getattr(self._booking, "_runner", None)
        return dict(getattr(runner, "slots", {}) or {})

    @property
    def turns_used(self) -> int:
        runner = getattr(self._booking, "_runner", None)
        return int(getattr(runner, "turns_used", 0))

    @property
    def max_turns(self) -> int:
        runner = getattr(self._booking, "_runner", None)
        return int(getattr(runner, "max_turns", 0))

    @classmethod
    def availability_ok(cls, mode: str | None = None) -> bool:
        """Check mode prerequisites without probing a live calendar."""

        domain_id = active_scheduling_domain(mode)
        if domain_id is None:
            return True
        domain = DOMAINS[domain_id]
        if domain.uses_live_calendar:
            return load_calendar_settings() is not None

        try:
            _load_scheduler_inputs(domain)
        except _AVAILABILITY_ERRORS:
            # Debug level: this runs on every /flow poll, but the REASON the
            # availability badge is red should be one log-grep away.
            log.debug("Scheduler availability inputs unavailable", exc_info=True)
            return False
        return True

    @classmethod
    def create(
        cls,
        *,
        domain_id: str | None = None,
        client=None,
    ) -> FlowSession | RefusalSession | None:
        """Build a domain scheduler; live-calendar failures refuse explicitly."""

        selected_domain = domain_id or "plumber"
        domain = DOMAINS.get(selected_domain)
        if domain is None:
            log.error("Scheduler flow unavailable; unknown domain %s", selected_domain)
            return None

        calendar = None
        if domain.uses_live_calendar:
            settings = load_calendar_settings()
            if settings is None:
                log.error(
                    "Scheduler flow unavailable; calendar settings are not configured "
                    "for domain %s",
                    domain.id,
                )
                return RefusalSession(domain)
            try:
                calendar = CalendarClient(settings)
                availability = availability_snapshot(calendar)
                windows = load_free_windows(availability)
                config = region_config_for(domain, availability_digest(availability))
            except (CalendarError, *_AVAILABILITY_ERRORS) as exc:
                log.error(
                    "Scheduler flow unavailable; cannot fetch calendar for domain "
                    "%s: %s",
                    domain.id,
                    exc,
                )
                return RefusalSession(domain)
            except Exception as exc:
                log.exception(
                    "Scheduler flow unavailable; cannot prepare calendar for domain "
                    "%s: %s",
                    domain.id,
                    exc,
                )
                return RefusalSession(domain)
        else:
            try:
                path, config, windows = _load_scheduler_inputs(domain)
            except _AVAILABILITY_ERRORS as exc:
                path = _availability_path()
                log.error("Scheduler flow unavailable; cannot load %s: %s", path, exc)
                return None

        try:
            # Imported lazily because BookingFlow uses the digest/speech helpers
            # in this module.
            from voice.booking import BookingFlow

            runner = GoalRegionRunner(config, windows, client=client)
            booking = BookingFlow(runner, domain, calendar)
            return cls(booking, domain.greeting)
        except Exception as exc:
            log.error("Scheduler flow unavailable; cannot initialize runner: %s", exc)
            return RefusalSession(domain) if domain.uses_live_calendar else None

    @classmethod
    async def create_async(
        cls,
        *,
        domain_id: str | None = None,
        client=None,
    ) -> FlowSession | RefusalSession | None:
        """Create live-calendar sessions in an executor; plumber stays synchronous."""

        selected_domain = domain_id or "plumber"
        domain = DOMAINS.get(selected_domain)
        if domain is None or not domain.uses_live_calendar:
            return cls.create(domain_id=domain_id, client=client)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(cls.create, domain_id=selected_domain, client=client),
        )

    async def reply(self, caller_text: str) -> FlowReply:
        """Run one blocking supervisor turn without blocking the event loop."""

        async with self._turn_lock:
            await self._discard_orphaned_turn()
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, self._booking.turn, caller_text)
            self._inflight_turn = future
            try:
                # Shield keeps the executor future awaitable after browser
                # barge-in cancels this request task; the worker keeps mutating
                # the runner until the next serialized caller drains it.
                turn = await asyncio.shield(future)
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._inflight_turn = None
                raise
            else:
                self._inflight_turn = None

        outcome: FlowOutcome | None = (
            turn.outcome
            if turn.outcome in ("booked", "escape", "budget", "not_booked")
            else None
        )
        return FlowReply(
            text=turn.reply,
            done=turn.done,
            outcome=outcome,
            slots=dict(turn.slots),
            rejected=list(turn.rejected),
            turns_used=turn.turns_used,
            max_turns=turn.max_turns,
            supervisor_ms=turn.supervisor_ms,
            event_id=turn.event_id,
        )

    async def _discard_orphaned_turn(self) -> None:
        future = self._inflight_turn
        if future is None:
            return
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            log.error("Discarded scheduler flow turn was cancelled")
        except Exception:
            log.exception("Discarded scheduler flow turn failed")
        finally:
            if future.done() and self._inflight_turn is future:
                self._inflight_turn = None


def _availability_path() -> Path:
    configured = os.environ.get("NANO_CLAW_FLOW_AVAILABILITY", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_AVAILABILITY_PATH
    return path if path.is_absolute() else REPO_ROOT / path


def _load_scheduler_inputs(
    domain: SchedulingDomain,
) -> tuple[Path, RegionConfig, list[FreeWindow]]:
    path = _availability_path()
    availability = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(availability, dict):
        raise ValueError("availability must be a JSON object")
    # Anchor to the moment the calendar launches: a fixture written last week
    # must not offer last week's days (2026-07-27 regression).
    availability = anchor_availability(availability)
    windows = load_free_windows(availability)
    config = scheduler_region_config(
        availability_digest(availability), domain=domain
    )
    return path, config, windows


_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    21: "twenty first",
    22: "twenty second",
    23: "twenty third",
    24: "twenty fourth",
    25: "twenty fifth",
    26: "twenty sixth",
    27: "twenty seventh",
    28: "twenty eighth",
    29: "twenty ninth",
    30: "thirtieth",
    31: "thirty first",
}


def _spoken_datetime(value) -> str:
    """Render a validated slot time in a form TTS reads naturally."""

    if not isinstance(value, str):
        return "the scheduled date and time"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        # health-ok: TTS formatter guard; the spoken fallback below is the handling
        return "the scheduled date and time"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BUSINESS_TIMEZONE)

    hour = parsed.strftime("%I").lstrip("0")
    minute = f":{parsed.minute:02d}" if parsed.minute else ""
    return (
        f"{parsed:%A %B} {_ORDINALS[parsed.day]} at "
        f"{hour}{minute} {parsed:%p}"
    )
