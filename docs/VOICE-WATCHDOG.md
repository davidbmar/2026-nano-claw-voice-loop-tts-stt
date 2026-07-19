# Self-healing voice-path watchdog

Synthetic end-to-end monitor for the browser voice path. Runs on **boot**
and **every hour** (launchd), and **auto-remediates** the component that
broke.

## What it checks (`scripts/voice_healthcheck.py`)
Drives the real `/ws` WebSocket like a browser: streams a spoken phrase
(generated once with macOS `say`) as PCM16/16k frames, then asserts the
full round-trip: WS connects + selects WS-audio → STT transcribes →
agent replies → agent audio returns at **48 kHz (1920-byte frames)**.
Exit codes pinpoint the failed stage: `2` ws/link · `3` STT · `4` agent
· `5` agent-audio · `6` wrong format.

## What it fixes (`scripts/voice_watchdog.sh`)
On failure it probes the pinpointed dependency and, **only if confirmed
down**, restarts it: STT (`:8200`), TTS (`:8300`), or the container
(reuse-image `NANO_CLAW_SKIP_BUILD=1 ./run.sh`). Then it re-checks:
- recovered → logs `RECOVERED`, clears the alert file
- still failing → writes `logs/voice_watchdog.ALERT` (presence = a human
  is needed) and exits non-zero
- exit `6` (audio not 48 kHz) is a **code regression**, not a runtime
  fault: it alerts and never blindly "fixes".

Never blind-restarts — matches the server-management discipline (confirm
dead → restart → verify back up).

## Logs / alerts
- `logs/voice_watchdog.log` — every run's verdict + remediation actions.
- `logs/voice_watchdog.ALERT` — created only when auto-heal fails; watch
  for this file (or wire it to a notifier).

## Install
```
cp scripts/com.nanoclaw.voice-watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nanoclaw.voice-watchdog.plist
```
Run once by hand: `./scripts/voice_watchdog.sh`
