#!/usr/bin/env bash
# Supervisor for the nano-claw phone node (512-FLOW-101 on this Mac).
#
# Designed to be driven by launchd (com.nanoclaw.phone-node) every 120s:
#   --ensure    start whatever is down; no-op when healthy or DRAINED (default)
#   --drain     stop everything and set the DRAINED flag (watchdog respects it)
#   --undrain   clear the flag, then ensure
#   --status    print component health and exit
#
# Components: docker container (via run.sh, which also owns the native
# STT/TTS services) + the dedicated cloudflared tunnel (nano-claw-phone →
# nano.chattychapters.com → :9090). Pattern follows riff's proven
# scripts/phone-node-start.sh (drain flag, ensure watchdog, nohup children
# that outlive the supervisor).
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="/tmp/nano-claw-phone-node"
DRAIN_FLAG="$LOG_DIR/DRAINED"
STARTED_AT_FILE="$LOG_DIR/stack-started-at"
# Each node has its own dedicated tunnel (M3: nano-claw-phone.yml,
# M1: nano-claw-m1.yml) — auto-detect, or override via env.
TUNNEL_CONFIG="${NANO_CLAW_TUNNEL_CONFIG:-$(ls "$HOME"/.cloudflared/nano-claw-*.yml 2>/dev/null | head -1)}"
TUNNEL_NAME="$(awk '/^tunnel:/ {print $2; exit}' "$TUNNEL_CONFIG" 2>/dev/null)"
BOOT_GRACE_S=180   # don't judge a stack that's still booting

mkdir -p "$LOG_DIR"
cd "$ROOT"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

docker_up()    { docker info >/dev/null 2>&1; }
container_up() { [ -n "$(docker ps -q --filter name='^nano-claw-voice$' 2>/dev/null)" ]; }
voice_up()     { curl -sf -m 3 http://localhost:9090/api/models >/dev/null 2>&1; }
stt_up()       { curl -sf -m 3 http://localhost:8200/health >/dev/null 2>&1; }
tts_up()       { curl -sf -m 3 http://localhost:8300/health >/dev/null 2>&1; }
tunnel_up()    { pgrep -f "$TUNNEL_CONFIG" >/dev/null 2>&1; }

stack_healthy() { container_up && voice_up && stt_up && tts_up; }

first_boot() {
  # No image or no service venvs yet: run.sh is doing a from-scratch build
  # (docker build + torch installs + model downloads) that can take many
  # minutes. Without the longer grace the watchdog would kill and restart
  # the build every cycle, forever.
  ! docker image inspect nano-claw-voice >/dev/null 2>&1 \
    || [ ! -d "$ROOT/stt-service/.venv" ] \
    || [ ! -d "$ROOT/tts-service/.venv" ]
}

stack_booting() {
  [ -f "$STARTED_AT_FILE" ] || return 1
  local started now grace
  started=$(cat "$STARTED_AT_FILE" 2>/dev/null || echo 0)
  now=$(date +%s)
  grace="$BOOT_GRACE_S"
  first_boot && grace=1200
  [ $((now - started)) -lt "$grace" ] && pgrep -f "bash $ROOT/run.sh" >/dev/null 2>&1
}

status() {
  local ok=0
  for check in docker_up container_up voice_up stt_up tts_up tunnel_up; do
    if $check; then echo "  $check: UP"; else echo "  $check: DOWN"; ok=1; fi
  done
  [ -f "$DRAIN_FLAG" ] && echo "  DRAINED (watchdog will not restart)"
  return $ok
}

stop_stack() {
  log "stopping run.sh stack (its exit trap stops STT/TTS)"
  pkill -f "bash $ROOT/run.sh" 2>/dev/null || true
  sleep 2
  local cid
  cid=$(docker ps -q --filter name='^nano-claw-voice$' 2>/dev/null)
  [ -n "$cid" ] && docker rm -f "$cid" >/dev/null 2>&1
  # Escalate on the service ports per the kill conventions
  for port in 8200 8300; do
    lsof -ti ":$port" 2>/dev/null | xargs kill -9 2>/dev/null || true
  done
  rm -f "$STARTED_AT_FILE"
}

stop_tunnel() {
  log "stopping cloudflared connector"
  pkill -f "$TUNNEL_CONFIG" 2>/dev/null || true
}

start_stack() {
  log "starting run.sh stack (skip-build fast path)"
  date +%s > "$STARTED_AT_FILE"
  NANO_CLAW_SKIP_BUILD=1 nohup bash "$ROOT/run.sh" >> "$LOG_DIR/run-sh.log" 2>&1 &
}

start_tunnel() {
  if [ -z "$TUNNEL_CONFIG" ] || [ -z "$TUNNEL_NAME" ]; then
    log "no nano-claw-*.yml tunnel config found in ~/.cloudflared — cannot start connector"
    return 0
  fi
  log "starting cloudflared connector ($TUNNEL_NAME)"
  nohup cloudflared tunnel --config "$TUNNEL_CONFIG" run "$TUNNEL_NAME" \
    >> "$LOG_DIR/cloudflared.log" 2>&1 &
}

ensure() {
  if [ -f "$DRAIN_FLAG" ]; then
    log "DRAINED — not ensuring (use --undrain to resume)"
    return 0
  fi
  if ! docker_up; then
    # Docker Desktop does not start at login, so after a reboot the whole
    # node stays down until a human launches it (real outage: 75 min on
    # 2026-07-22). DRAINED already expresses "off on purpose", so it is
    # safe for the watchdog to own this dependency too. -g keeps the app
    # from stealing foreground focus.
    log "docker daemon not available — launching Docker Desktop"
    open -ga Docker
    for _ in $(seq 1 20); do
      sleep 3
      docker_up && break
    done
    if ! docker_up; then
      log "docker daemon still not up after 60s — will retry next cycle"
      return 0
    fi
    log "docker daemon up"
  fi
  if ! tunnel_up; then
    start_tunnel
  fi
  if stack_healthy; then
    return 0
  fi
  if stack_booting; then
    log "stack is booting (grace ${BOOT_GRACE_S}s) — leaving it alone"
    return 0
  fi
  log "stack unhealthy — restarting"
  status || true
  stop_stack
  start_stack
  # Wait for health so the next watchdog tick doesn't see a half-booted stack
  for _ in $(seq 1 30); do
    sleep 3
    if stack_healthy; then
      log "stack healthy"
      return 0
    fi
  done
  log "stack still unhealthy after restart window — next cycle will retry"
  return 0
}

case "${1:---ensure}" in
  --ensure)  ensure ;;
  --drain)   touch "$DRAIN_FLAG"; stop_stack; stop_tunnel; log "drained" ;;
  --undrain) rm -f "$DRAIN_FLAG"; log "undrained"; ensure ;;
  --status)  status ;;
  *) echo "usage: $0 [--ensure|--drain|--undrain|--status]" >&2; exit 2 ;;
esac
