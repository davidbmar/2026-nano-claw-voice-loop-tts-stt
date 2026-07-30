#!/usr/bin/env bash
# Bring up a delegate test rig beside whatever is already running.
#
# Starts a second riff-builder and a second nano-claw on their own ports, so the
# live phone node (:9090) and your dev server (:8790) are untouched. Prints what
# to do next, then waits; Ctrl-C shuts everything it started back down.
#
#   ./scripts/try_delegate.sh
#
set -uo pipefail

NC_PORT=8080          # this rig's nano-claw console
RB_PORT=8795          # this rig's riff-builder
SINK_PORT=8399        # stands in for Telnyx; nothing reaches a real carrier
DID="+15125550100"    # a pretend phone number, only meaningful to this rig

NC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RB_ROOT="${RB_ROOT:-$HOME/src/riff-builder-goal-driven}"
RUN="$(mktemp -d)"

for p in "$NC_PORT" "$RB_PORT" "$SINK_PORT"; do
  if lsof -ti :"$p" >/dev/null 2>&1; then
    echo "port $p is already in use — stop that first, or edit this script" >&2
    exit 1
  fi
done

cleanup() {
  echo
  echo "shutting down the rig..."
  for p in "$NC_PORT" "$RB_PORT" "$SINK_PORT"; do
    lsof -ti :"$p" 2>/dev/null | xargs kill -9 2>/dev/null
  done
  sleep 1
  for p in "$NC_PORT" "$RB_PORT" "$SINK_PORT"; do
    lsof -ti :"$p" >/dev/null 2>&1 && echo "  :$p STILL RUNNING" || echo "  :$p clear"
  done
  echo "your :8790 and :9090 were never touched."
}
trap cleanup EXIT INT TERM

# The carrier sink: answers any Call Control command with OK, so a fake call id
# never reaches api.telnyx.com with a dummy key.
cat > "$RUN/sink.py" <<'PY'
from aiohttp import web
async def ok(request):
    return web.json_response({"data": {"result": "ok"}})
app = web.Application()
app.router.add_route("*", "/{tail:.*}", ok)
web.run_app(app, port=8399, print=None)
PY

echo "starting riff-builder on :$RB_PORT ..."
RB_BUILDER_DIDS="{\"$DID\":{\"business_name\":\"Test Plumbing\",\"industry\":\"plumbing\"}}" \
  RB_SESSIONS_DIR="$RUN/sessions" \
  RB_NANOCLAW_URL="http://127.0.0.1:$NC_PORT" \
  "$RB_ROOT/.venv/bin/python" -m uvicorn rb.server:app \
  --host 127.0.0.1 --port "$RB_PORT" > "$RUN/riff-builder.log" 2>&1 &

# Wait for riff-builder before minting: the browser needs a CONVERSATION url,
# and unlike the phone it has no start seam to get one. Without this the console
# offers "Turn Delegate" with nowhere to send, and every turn is an apology.
for _ in $(seq 1 90); do
  curl -sf -m 2 -o /dev/null "http://127.0.0.1:$RB_PORT/" 2>/dev/null && break
  sleep 0.5
done
BROWSER_SESSION="$(curl -sf -m 15 -X POST "http://127.0.0.1:$RB_PORT/api/session" \
  -H 'Content-Type: application/json' \
  -d '{"industry":"plumbing","business_name":"Test Plumbing"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])' 2>/dev/null)"
if [ -z "${BROWSER_SESSION:-}" ]; then
  echo "could not create a riff-builder session — is :$RB_PORT healthy?" >&2
  tail -20 "$RUN/riff-builder.log" >&2
  exit 1
fi
BROWSER_DELEGATE="http://127.0.0.1:$RB_PORT/api/session/$BROWSER_SESSION/turn"
echo "browser conversation: $BROWSER_SESSION"

echo "starting the carrier sink on :$SINK_PORT ..."
"$NC_ROOT/.venv-test/bin/python" "$RUN/sink.py" > "$RUN/sink.log" 2>&1 &

TOKEN="$(grep -E '^NANO_CLAW_PHONE_TOKEN=' "$NC_ROOT/.env" | cut -d= -f2- | tr -d '"'"'"'')"

echo "starting nano-claw on :$NC_PORT ..."
env VOICE_PORT="$NC_PORT" \
  STT_SERVICE_URL=http://127.0.0.1:8200 \
  TTS_SERVICE_URL=http://127.0.0.1:8300 \
  LUX_SERVICE_URL=http://127.0.0.1:8301 \
  NANO_CLAW_PHONE=1 \
  NANO_CLAW_PHONE_TOKEN="$TOKEN" \
  NANO_CLAW_OPERATOR_PASSWORD="${NANO_CLAW_OPERATOR_PASSWORD:-testing}" \
  NANO_CLAW_METRICS_DB="$RUN/metrics.db" \
  NANO_CLAW_PHONE_TAP_DIR="$RUN/taps" \
  TELNYX_API_BASE="http://127.0.0.1:$SINK_PORT" \
  TELNYX_API_KEY=not-a-real-key \
  NANO_CLAW_PHONE_WEBHOOK_BASE="http://127.0.0.1:$NC_PORT" \
  NANO_CLAW_DELEGATE_STARTS="{\"$DID\":{\"start\":\"http://127.0.0.1:$RB_PORT/api/delegate/start\",\"greeting\":\"Thanks for calling Test Plumbing.\",\"voice\":\"af_heart\"}}" \
  NANO_CLAW_DELEGATE_URL="$BROWSER_DELEGATE" \
  NANO_CLAW_VOICE_FLOW=delegate \
  NANO_CLAW_WS_AUDIO=1 \
  NANO_CLAW_EMBED_ORIGINS="http://127.0.0.1:$RB_PORT,http://localhost:$RB_PORT,http://127.0.0.1:8790,http://localhost:8790" \
  "$NC_ROOT/.venv-test/bin/python" -m voice > "$RUN/nano-claw.log" 2>&1 &

for _ in $(seq 1 90); do
  curl -sf -m 2 -o /dev/null "http://127.0.0.1:$NC_PORT/" 2>/dev/null \
    && curl -sf -m 2 -o /dev/null "http://127.0.0.1:$RB_PORT/" 2>/dev/null && break
  sleep 0.5
done

echo
if ! curl -sf -m 3 -o /dev/null "http://127.0.0.1:$NC_PORT/"; then
  echo "nano-claw did not come up. Log:"; tail -20 "$RUN/nano-claw.log"; exit 1
fi
if ! curl -sf -m 3 -o /dev/null "http://127.0.0.1:$RB_PORT/"; then
  echo "riff-builder did not come up. Log:"; tail -20 "$RUN/riff-builder.log"; exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo "  rig is up.  logs: $RUN"
echo
echo "  1. CHECK THE WIRING (one command, ~10s)"
echo "       NANO_CLAW_DELEGATE_STARTS='{\"$DID\":{\"start\":\"http://127.0.0.1:$RB_PORT/api/delegate/start\"}}' \\"
echo "         $NC_ROOT/.venv-test/bin/python $NC_ROOT/scripts/check_delegate_setup.py"
echo
echo "  2. TALK TO IT IN THE BROWSER"
echo "       open http://127.0.0.1:$NC_PORT/"
echo "       MODE is ALREADY 'Turn Delegate' — the rig starts in it."
echo "       (to change it, the operator password is ${NANO_CLAW_OPERATOR_PASSWORD:-testing})"
echo "       APP URL       -> already filled in: this rig minted a conversation"
echo "                        and pointed the console at it"
echo "       then hold the mic button and speak. Every reply comes from"
echo "       riff-builder; nano-claw only supplies the voice."
echo
echo "  3. SIMULATE A PHONE CALL (no carrier, no DID)"
echo "       curl -X POST \"http://127.0.0.1:$NC_PORT/api/phone/incoming?token=$TOKEN\" \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"data\":{\"event_type\":\"call.initiated\",\"payload\":{"
echo "              \"call_control_id\":\"v3:try-1\",\"to\":\"$DID\",\"from\":\"+15125559999\"}}}'"
echo "       LOOPBACK_WS_BASE=ws://localhost:$NC_PORT LOOPBACK_CALL_ID='v3:try-1' \\"
echo "         $NC_ROOT/.venv-test/bin/python $NC_ROOT/scripts/phone_loopback_test.py \\"
echo "         'we do emergency plumbing repairs'"
echo
echo "     watch what the node did:   tail -f $RUN/nano-claw.log"
echo
echo "  Ctrl-C here shuts the rig down."
echo "════════════════════════════════════════════════════════════════"

# `wait -n` returns the moment ANY child exits, so the shell tells us instead of
# being polled. The first version of this slept in a loop, which meant the script
# stayed alive with the terminal still saying "rig is up" while nothing answered
# — the quiet failure this whole feature guards against, in the tool built to
# demonstrate it. The second version polled with curl and hung. This one asks the
# shell the question it already knows the answer to.
wait -n
echo
echo "!! a rig process exited — the rig is no longer serving."
echo "   logs: $RUN"
tail -5 "$RUN/nano-claw.log" 2>/dev/null | sed 's/^/     /'
exit 1
