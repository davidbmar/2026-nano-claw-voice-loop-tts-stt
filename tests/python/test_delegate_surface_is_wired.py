"""Every delegate capability must be called by production code, not only tested.

Five separate defects this session had one shape: a mechanism written and
tested, and never invoked.

- `voice/turn_delegate.py` existed for a commit before either hop called it.
- `hangup_after_playback` was built, tested, and wired nowhere — while three
  existing hangups clipped their own last words.
- `close()` cancelling an in-flight turn passed its tests because they cancelled
  the task by hand; nothing checked `close()` issued it.
- `_compose_greeting` gained a per-line disclosure the media handler never
  passed.
- `record: false` parsed and silenced the disclosure while the tap opened anyway.

Each was found by a vacuity check that failed to fail — sabotage the code, watch
the suite stay green. That works but needs someone to think of it every time.
This asks the question once, for the whole surface.

It cannot tell whether a call site is CORRECT — only that one exists. That is
the cheap half, and the half that was missing all five times.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The delegate surface, by the module that defines it. A capability listed here
# is one the seam depends on; if it stops being called, either something is
# unwired or the entry belongs deleted — both worth a failing test.
SURFACE: dict[str, tuple[str, ...]] = {
    "voice/turn_delegate.py": (
        "call_delegate",            # the turn hop itself
        "start_conversation",       # one conversation per call
        "validate_delegate_url",    # SSRF guard at set time
        "resolve_returned_url",     # same-origin rule for a returned URL
        "safe_url_for_log",         # keeps session ids out of logs
        "terminal",                 # the end-of-call signal — parsed and then
                                    # ignored is exactly how "Goodbye" strands
                                    # a caller on a live leg (2026-08-03)
    ),
    "voice/phone.py": (
        "hangup_after_playback",    # ends a call without clipping it
        "route_for",                # this call's conversation
        "routing_for",              # its URL
        "conversation_key_for",     # the digest sent instead of the carrier id
        "delegate_starts",          # per-DID configuration
        "recording_notice",         # per-line disclosure
        "configured_voice",
        "configured_speed",
        "greeting_line",            # per-line greeting
        "speak_whole_reply",        # truthful recording of what was heard
    ),
    "voice/flow_session.py": (
        "is_delegate_mode",
        "default_delegate_url",
        "set_default_delegate_url",
        "delegate_allowed_hosts",
        "is_transcribe_mode",       # transcribe mode's only gate
    ),
    "voice/server.py": (
        "resolve_delegate_url",
        "_handle_delegate_request",  # the browser hop
        "_handle_transcribe_request",  # the j-space probe hop
    ),
    # Transcribe mode is the same shape of risk this file was written for: a
    # capability that is easy to build, easy to test in isolation, and silently
    # inert if the dispatch call is ever dropped. An unwired probe does not
    # error — it just answers normally, in a mode whose whole promise is that
    # it does not.
    "voice/gemma_probe.py": (
        "send_to_gemma",
        "record_exchange",
        "probe_base_url",
        "probe_model",
    ),
}


def _production_uses() -> dict[str, list[str]]:
    """Names USED anywhere in shipped code — `voice/` and `scripts/`, not tests.

    A definition is a FunctionDef; a use is a Call or an attribute load. The
    defining module counts: `phone.py` calling its own method is a real call
    site, and excluding it made a first draft of this report every property as
    unwired.
    """
    uses: dict[str, list[str]] = {}
    for path in [*(ROOT / "voice").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]:
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name:
                    uses.setdefault(name, []).append(rel)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                uses.setdefault(node.attr, []).append(rel)
    return uses


@pytest.mark.parametrize(
    "capability",
    [name for names in SURFACE.values() for name in names],
)
def test_the_capability_is_called_by_production_code(capability):
    """Written, tested, and never called is the same as absent — except it
    passes its tests."""
    uses = _production_uses()

    assert uses.get(capability), (
        f"{capability!r} is defined and tested but nothing in voice/ or scripts/ "
        f"calls it. Either a hop is unwired, or this capability is dead and the "
        f"entry in SURFACE should go with it.")


def test_every_listed_capability_still_exists():
    """The other direction: a renamed capability leaves this list asserting
    something about nothing, which would pass forever."""
    defined = set()
    for module in SURFACE:
        tree = ast.parse((ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                # Dataclass fields are capabilities too (DelegateReply.terminal).
                defined.add(node.target.id)

    listed = {name for names in SURFACE.values() for name in names}
    missing = sorted(listed - defined)
    assert not missing, (
        f"{missing} are listed here but no longer defined — renamed or removed, "
        f"leaving this file guarding names that do not exist")


# ── what the gateway ASSERTS must match the channel it is on ────────────────

def test_no_gateway_call_contradicts_its_own_channel():
    """`who` is the gateway's statement about which human is speaking, and the
    app is entitled to act on it — riff-builder gates voice approval on
    `who == "owner"`.

    Both hops shipped hardcoding "caller", so the browser told the app its
    OPERATOR was a caller. The app answered them in the third person and their
    spoken approvals did nothing. Found by someone using it, not by a test:
    every value was valid, the code did what it said, and what it said was wrong
    about the situation.

    This is that mistake made checkable. A call on `channel="browser"` claiming a
    caller, or on `channel="phone"` claiming an owner, is a contradiction the
    author has to resolve deliberately.
    """
    import ast

    contradictions = []
    for rel in ("voice/server.py", "voice/phone.py",
                "scripts/check_delegate_setup.py"):
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("call_delegate", "start_conversation"):
                continue
            kw = {k.arg: getattr(k.value, "value", "<expr>") for k in node.keywords}
            who = kw.get("who", "caller")          # the signature's default
            channel = kw.get("channel")
            if channel == "browser" and who not in ("owner", "operator"):
                contradictions.append(f"{rel}:{node.lineno} browser channel says who={who!r}")
            if channel == "phone" and who != "caller":
                contradictions.append(f"{rel}:{node.lineno} phone channel says who={who!r}")

    assert not contradictions, (
        "the gateway is telling the app something its own channel contradicts: "
        + "; ".join(contradictions))
