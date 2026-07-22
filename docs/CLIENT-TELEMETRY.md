# Self-hosted client telemetry

The voice console can send its existing browser lifecycle diagnostics to the
nano-claw server. This is opt-in, same-origin telemetry intended for debugging
remote and mobile sessions; it does not use an external analytics service.

## Enabling it

Telemetry is off by default. Add one of these query flags and reload the voice
console:

- `?telemetry` sends lifecycle diagnostics without changing the page.
- `?diag` shows the existing tappable diagnostic overlay and sends those same
  lines to the server.

For example, use `https://nano.chattychapters.com/?telemetry`. Remove the flag
and reload to stop shipping. The flag is evaluated only in the browser and is
not persisted in local storage.

`pageLog()` queues each line and sends a batch after 10 events or two seconds,
whichever happens first. A final partial batch is attempted on `pagehide` with
Fetch's `keepalive` option. Delivery is best-effort: network, validation, and
rate-limit failures are discarded and never change voice-console behavior.

## Endpoint and security

`POST /api/client-log` accepts this shape:

```json
{
  "events": [
    {
      "t": "2026-07-18T12:00:00.000Z",
      "tag": "ws",
      "msg": "WS OPEN gen=1"
    }
  ],
  "conv": "voice-server-issued-id",
  "ua": "browser user agent"
}
```

The endpoint uses the existing mutation-request guard. A request must have an
allowed same-origin `Origin`, `Sec-Fetch-Site: same-origin`, and
`X-NC-Auth: 1`. The console therefore uses keepalive Fetch rather than
`navigator.sendBeacon`, which cannot attach the custom guard header. The page's
CSP already permits the request through `connect-src 'self'`; no third-party
origin is added.

Requests are limited to 16 KiB and 50 events. Messages are truncated to 500
characters; tags, timestamps, and user agents are bounded too. A token bucket
allows a burst of 10 batches and refills at one batch per second, keyed by the
validated live socket when possible and otherwise by trusted client IP.
Malformed requests return `400`, guard failures `403`, oversized requests
`413`, and rate-limited requests `429`. Accepted batches return `204`.

`conv` is only a correlation hint. The server checks it against its live socket
registry and source IP and logs the registry's server-created conversation id,
never an unverified browser value. Missing, stale, forged, or source-IP-mismatched
hints produce a logged conversation id of `null`.

## What is logged

Each accepted event is written through the dedicated standard logger named
`client` as one compact JSON object. It contains the trusted source IP, verified
server conversation id when available, user agent, client timestamp, category,
and lifecycle message. Current categories and lines cover:

- WebSocket connecting/open/error/close and close code;
- `hello_ack` and selected audio transport;
- `getUserMedia` success or failure;
- mic-audio ready or format error;
- first agent-audio frame and Web Audio context resume state.

Telemetry does **not** include asked text, transcriptions, agent replies, saved
history, tool content, microphone samples, or audio. The implementation ships
only `pageLog()` calls, and those calls are reserved for lifecycle diagnostics.
Developers must never pass transcript or conversation content to `pageLog()`.
Source IP and user agent are operational metadata and should still be treated as
sensitive log data.

## Reading and retaining logs

Telemetry is not inserted into the metrics database and nano-claw creates no
separate client-log file. It follows the same stdout/stderr destination,
access controls, rotation, and retention as the voice server. With the standard
container name, follow only records containing telemetry JSON with:

```sh
docker logs -f nano-claw-voice 2>&1 | grep '"tag":'
```

The voice-server logging prefix precedes each JSON object. To extract historical
records, use the container runtime or service manager's normal log tooling and
filter on logger name `client` or the JSON `tag` field.

nano-claw does not impose an additional retention period or delete deployment
logs. Operators should configure Docker, journald, or their log collector to
the retention period appropriate for their environment, and restrict access as
they do for other server logs. Disabling the query flag stops new client events;
it does not erase records already retained by the logging system.
