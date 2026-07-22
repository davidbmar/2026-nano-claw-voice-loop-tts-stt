#!/bin/bash
# Self-healing voice-path watchdog. Runs the synthetic end-to-end health
# check; on failure it remediates the component the check pinpointed
# (STT/TTS host services or the container), re-checks, and logs the outcome.
# Runs on boot + hourly via com.nanoclaw.voice-watchdog.plist.
#
#   healthcheck exit codes: 0 ok · 2 ws/link · 3 stt · 4 agent · 5 agent-audio · 6 format
#
# Only restarts a service it has CONFIRMED is down (health probe), per the
# server-management discipline — never a blind restart. Format regressions
# (exit 6) are a code bug, not a runtime fault: it alerts, never "fixes".
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv-test/bin/python"
[ -x "$PY" ] || PY="python3"
LOG="$REPO/logs/voice_watchdog.log"
ALERT="$REPO/logs/voice_watchdog.ALERT"       # presence = needs a human
mkdir -p "$REPO/logs"
STT_URL="${STT_CHECK_URL:-http://localhost:8200/health}"
TTS_URL="${TTS_CHECK_URL:-http://localhost:8300/health}"
CONSOLE_URL="${CONSOLE_URL:-http://localhost:9090/}"

ts() { date '+%F %T'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }
up() { curl -sf -o /dev/null --max-time 5 "$1"; }

run_check() {
  PYTHONPATH="$REPO" "$PY" "$REPO/scripts/voice_healthcheck.py" >> "$LOG" 2>&1
  return $?
}

restart_stt() {
  log "remediate: STT down -> restarting"
  lsof -ti :8200 2>/dev/null | xargs kill -9 2>/dev/null
  nohup "$REPO/stt-service/.venv/bin/python" "$REPO/stt-service/server.py" >> /tmp/stt-service.log 2>&1 &
  for _ in $(seq 1 30); do up "$STT_URL" && { log "STT back up"; return 0; }; sleep 2; done
  log "STT did NOT recover"; return 1
}
restart_tts() {
  log "remediate: TTS down -> restarting"
  lsof -ti :8300 2>/dev/null | xargs kill -9 2>/dev/null
  PYTORCH_ENABLE_MPS_FALLBACK=1 nohup "$REPO/tts-service/.venv/bin/python" "$REPO/tts-service/server.py" >> /tmp/tts-service.log 2>&1 &
  for _ in $(seq 1 60); do up "$TTS_URL" && { log "TTS back up"; return 0; }; sleep 2; done
  log "TTS did NOT recover"; return 1
}
restart_container() {
  log "remediate: console down -> restarting container (reuse image)"
  ( cd "$REPO" && set -a && [ -f .env ] && source .env; set +a
    NANO_CLAW_SKIP_BUILD=1 nohup ./run.sh >> /tmp/nano-claw-run.log 2>&1 & )
  for _ in $(seq 1 45); do up "$CONSOLE_URL" && { log "console back up"; return 0; }; sleep 4; done
  log "console did NOT recover"; return 1
}
alert() { log "ALERT: $*"; echo "[$(ts)] $*" > "$ALERT"; }

run_check; code=$?
if [ "$code" -eq 0 ]; then
  log "healthy (full round-trip ok)"
  rm -f "$ALERT"
  exit 0
fi
log "UNHEALTHY: healthcheck exit=$code — diagnosing"

# Remediate by the pinpointed stage; also opportunistically fix any down dep.
case "$code" in
  3) up "$STT_URL"  || restart_stt ;;                       # no transcript -> STT
  4|5) up "$TTS_URL" || restart_tts ;;                      # agent/audio -> TTS
  2) up "$CONSOLE_URL" || restart_container ;;              # link -> container
  6) alert "agent-audio FORMAT wrong (not 48kHz) — code regression, cannot auto-fix"; exit 6 ;;
esac
# Belt-and-suspenders: if any dep is down regardless of the code, fix it.
up "$STT_URL" || restart_stt
up "$TTS_URL" || restart_tts
up "$CONSOLE_URL" || restart_container

sleep 3
run_check; code2=$?
if [ "$code2" -eq 0 ]; then
  log "RECOVERED after remediation"
  rm -f "$ALERT"
  exit 0
fi
alert "voice path STILL FAILING after remediation (exit=$code2) — needs a human"
exit "$code2"
