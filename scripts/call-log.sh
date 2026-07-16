#!/usr/bin/env bash
# Merged phone-call log across both nodes. Any call served by the failover
# node (nano-m1) IS a failover event — the node column makes them obvious.
#
# Usage: scripts/call-log.sh [limit]
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
token="$(grep '^NANO_CLAW_PHONE_TOKEN=' "$root/.env" | cut -d= -f2)"
limit="${1:-25}"

fetch() {
  curl -sf -m 8 "https://$1/api/calls?token=$token" 2>/dev/null || echo '{"node":"'"$1"' (unreachable)","calls":[]}'
}

{
  fetch nano.chattychapters.com
  fetch nano-m1.chattychapters.com
} | python3 -c "
import json, sys

rows = []
for line in sys.stdin:
    if not line.strip():
        continue
    data = json.loads(line)
    for c in data.get('calls', []):
        c['node'] = c.get('node') or data.get('node', '?')
        rows.append(c)

rows.sort(key=lambda c: c.get('answered_at') or '', reverse=True)
rows = rows[:$limit]
if not rows:
    print('No calls logged yet.')
    raise SystemExit

fmt = '{:<20} {:<16} {:<28} {:<20} {:<6}'
print(fmt.format('ANSWERED (UTC)', 'CALLER', 'NODE', 'ENDED', 'TURNS'))
for c in rows:
    failover = ' *FAILOVER*' if 'm1' in (c.get('node') or '') else ''
    print(fmt.format(
        c.get('answered_at') or '?',
        c.get('caller') or '?',
        (c.get('node') or '?') + failover,
        c.get('ended_at') or '(in progress)',
        str(c.get('turns') or 0),
    ))
"
