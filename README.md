<div align="center">
  <h1>nano-claw voice loop</h1>
  <p>
    A voice-powered AI agent you talk to in your browser. Ask questions, approve tool calls, hear responses — all by voice.
  </p>
  <p>
    <img src="https://img.shields.io/badge/docker-ready-blue" alt="Docker">
    <img src="https://img.shields.io/badge/typescript-5.x-blue" alt="TypeScript">
    <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

<p align="center">
  <img src="docs/voice-full-flow.png" width="600" alt="Voice loop in action — speech, tool approval, agent reply, debug panel">
</p>

## What is this?

You talk to an AI agent through your browser. It listens (Whisper STT), thinks (Claude), speaks back (Kokoro or Piper TTS), and can run tools on your machine with your approval.

**The loop:** You speak → Whisper transcribes → Claude responds → if it needs a tool, you approve/reject → Claude continues → Kokoro or Piper speaks the answer back.

## Architecture

The system runs as **three processes** — the STT and TTS services run natively on your Mac for speed, and everything else runs in Docker:

```
┌─────────────────────────────────────────────────────────────────┐
│  Your Mac (native)                                              │
│                                                                 │
│  ┌──────────────────────────┐  ┌──────────────────────────┐     │
│  │  STT Service              │  │  TTS Service              │     │
│  │  faster-whisper           │  │  Kokoro-82M                │     │
│  │  POST /transcribe         │  │  POST /synthesize          │     │
│  │  port 8200                │  │  port 8300                 │     │
│  └────────────▲─────────────┘  └────────────▲─────────────┘     │
│               │ HTTP                          │ HTTP              │
│  ┌────────────┴──────────────────────────────┴────────────────┐  │
│  │  Docker container                                          │  │
│  │                                                            │  │
│  │  ┌──────────────────┐    ┌─────────────────────────────┐  │  │
│  │  │  nano-claw API   │    │  Voice Server (Python)      │  │  │
│  │  │  (TypeScript)    │    │                             │  │  │
│  │  │                  │    │  WebSocket ←→ Browser       │  │  │
│  │  │  Agent loop      │◄──►│  Piper TTS (fast, local)    │  │  │
│  │  │  Tool execution  │    │  WebRTC audio streaming    │  │  │
│  │  │  Memory          │    │                             │  │  │
│  │  │  port 3001       │    │  port 8080 → 9090          │  │  │
│  │  └──────────────────┘    └─────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Why are STT and TTS separate services?

**Docker Desktop on Mac cannot access the GPU.** Docker runs a Linux VM under the hood, and Apple's Metal GPU is not passed through. This is a hard limitation — there is no workaround.

Whisper running on Docker CPU takes 1-2 seconds for a short clip and 30 seconds to load the model the first time. Running natively on your Mac with Metal acceleration is **3-5x faster**. The same applies to Kokoro — running it natively lets it use Metal via PyTorch's MPS backend instead of falling back to slow Docker CPU inference.

By extracting STT and Kokoro TTS into standalone HTTP services, the voice server in Docker simply POSTs to `http://host.docker.internal:8200/transcribe` and `http://host.docker.internal:8300/synthesize` and gets bytes back. Clean, fast, and the Docker container itself stays simple (no Whisper or Kokoro model to download inside it). Piper stays bundled in the Docker container as the always-available, low-latency fast path.

### Data flow

```
You speak into mic
    → WebRTC audio stream to Docker container
    → Voice server sends audio bytes to STT service (native Mac, port 8200)
    → Whisper transcribes to text (Metal-accelerated)
    → Voice server POSTs text to nano-claw API's /api/chat, requesting
      text/event-stream
    → Claude's reply streams back over SSE, sentence by sentence (may
      request tools)
    → If tool_pending: streaming stops, browser shows approval card, you
      approve/reject
    → If approved: tools execute, loop continues
    → As each sentence arrives, the voice server synthesizes and queues it
      immediately (first audio at the first sentence, not the whole reply)
      and forwards the text to the browser as agent_reply_delta over
      WebSocket, followed by agent_reply_done when finished
    → Voice server routes the selected voice: Kokoro voices go to the native
      TTS service (native Mac, port 8300), Piper voices are synthesized locally
      in the container. If Kokoro is unavailable, it falls back to Piper.
    → WebRTC audio stream back to your browser
You hear the answer

Set NANO_CLAW_STREAM=0 to force the legacy whole-reply path: the API
responds with a single application/json body instead of SSE, and the voice
server speaks (and sends) the full reply at once.
```

### Voices

The picker defaults to `af_heart` (Kokoro, American English, grade A). It's
grouped by language — American English, British English, and Spanish — and
each voice shows a quality grade. Click preview (▶) to hear a sample before
picking, or drag the speed slider to change Kokoro's tempo (Piper ignores
speed — it doesn't support it). Piper ("Lessac") stays available as the
fast, low-latency option, and it's what plays automatically if the Kokoro
service is down or unreachable. The voice picker lives inside the **⚙
Pipeline settings** panel — see below.

### Pipeline settings

Click the **⚙** button next to the message box to open the pipeline
settings panel. It switches all three stages of the voice loop live, with no
restart or reconnect needed:

- **STT (Whisper)** — pick the Whisper model size (`tiny`, `base`, `small`,
  `medium`). Bigger sizes transcribe more accurately but take longer; the
  STT service loads and caches a separate model per size, keyed by the
  `X-Model-Size` header sent with each transcription request.
- **LLM** — pick any model from the catalog. A model is selectable only if
  its provider's API key was present in `.env` (or the environment) when
  the container started; otherwise it's greyed out and labeled
  "— no key". The choice is sent as `set_model` over the WebSocket and
  applies starting with your next turn.
- **Voice** — the Kokoro/Piper picker described above, now nested in this
  same panel.

Your STT size, LLM model, and voice selections persist in `localStorage`
and are restored (and re-applied to the session) the next time you load
the page.

#### Recognized provider keys

Set any of these in `.env` to unlock the matching model(s) in the LLM
picker. All of them stream via the same OpenAI-compatible SSE path except
Anthropic, which streams natively:

| Env var             | Provider            | Model(s) in the catalog             |
| ------------------- | ------------------- | ----------------------------------- |
| `ANTHROPIC_API_KEY` | Anthropic           | Claude Haiku 4.5, Claude Sonnet 4.5 |
| `GEMINI_API_KEY`    | Google Gemini       | Gemini 2.0 Flash                    |
| `DEEPSEEK_API_KEY`  | DeepSeek            | DeepSeek Chat                       |
| `GROQ_API_KEY`      | Groq                | Llama 3.3 70B Versatile             |
| `DASHSCOPE_API_KEY` | Alibaba (DashScope) | Qwen Plus                           |
| `OPENAI_API_KEY`    | OpenAI              | GPT-4o mini                         |

A model whose key is missing still shows up in the list (so you can see
what's available) but stays disabled with "— no key" until the key is
added and the container is restarted — the catalog's availability is
computed once at startup, not re-checked live.

### Barge-in (experimental)

Set `NANO_CLAW_BARGE_IN=1` to expose browser interruption controls.
The listener must then explicitly enable **BARGE-IN** in the UI. Talking over a
reply pauses playback immediately; sustained input commits the interruption and
drops the reply, while a short false alarm resumes after randomized backoff.

The current browser detector is an experimental RMS energy gate. It cannot
distinguish the listener from the assistant's voice leaking from an open speaker
back into the mic. The server capability may remain enabled while the browser
toggle stays off for speaker playback. Use a headset for intentional voice
interruption testing, or click the deterministic **Stop audio** button shown during
playback. The separate `NANO_CLAW_PHONE_BARGE_IN` flag controls the phone gateway
and is unaffected.

The planned open-source path reuses WebRTC Audio Processing for acoustic echo
cancellation, Silero VAD for fast speech onset, and the existing local
faster-whisper service for transcript confirmation. RMS or VAD may pause quickly,
but must not cancel a reply until confirmed words differ from the assistant's
active TTS text. A matched echo or empty transcript resumes playback.

Regardless of the flag, replies don't queue: with the agent-reply spawn model,
a second text message sent while a reply is still streaming is dropped
("one reply at a time") rather than queued for later.

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
- [Python 3.10+](https://www.python.org/) on your Mac (for the STT service)
- At least one supported LLM provider key (DeepSeek is the default model)

### 1. Clone

```bash
git clone https://github.com/davidbmar/2026-nano-claw-voice-loop-tts-stt.git
cd 2026-nano-claw-voice-loop-tts-stt
```

### 2. Set a provider API key

```bash
export DEEPSEEK_API_KEY=...
```

Or create a `.env` file in the project root:

```
DEEPSEEK_API_KEY=...
```

Anthropic, Gemini, Groq, DashScope, OpenAI, and OpenRouter credentials are also accepted. The
selected/default model must be available through a configured provider.

### 3. Run

```bash
./run.sh
```

This single command:

- Starts the STT service natively on your Mac (for Metal GPU acceleration)
- Builds the Docker image (TypeScript API server + Python voice server + TTS)
- Starts the Docker container, which calls the local STT service for transcription
- The Whisper model downloads (~75 MB) on the first transcription and is cached after that

### 4. Open your browser

Go to **http://localhost:9090**

Allow microphone access when prompted. Once it says "Connected", you're ready.

### 5. Talk

Click **Start Hands-Free Phone Mode** once. The browser calibrates the room,
detects each spoken turn automatically, waits for silence, sends the turn to
Claude, speaks the answer, and resumes listening. Click the same button again
to stop.

For a phone test, place a separate phone on speaker beside the Mac. Start phone
mode before dialing so the initial room-noise calibration runs before the
greeting begins.

### 6. Stop

Press `Ctrl-C` — this stops both the Docker container and the STT service.

## How It Works

<p align="center">
  <img src="docs/voice-ui-connected.png" width="500" alt="Connected and ready to talk">
</p>

### Voice conversation

Start phone mode and speak naturally. Your completed turn is transcribed and
shown as a blue bubble. The agent's reply appears as a gray bubble and is
spoken aloud through your browser. The microphone stays gated during playback
so the agent does not answer its own voice.

### Tool approval

When the agent wants to run a command (shell, read/write files), it pauses and shows you exactly what it wants to do. You approve or reject.

<p align="center">
  <img src="docs/voice-tool-approval.png" width="500" alt="Tool approval card showing shell command">
</p>

### Full conversation flow

A typical interaction: you ask a question → the agent calls a tool → you approve → the agent gives you the answer.

<p align="center">
  <img src="docs/voice-conversation.png" width="500" alt="Complete conversation with tool call and response">
</p>

## Debug Panel

Click the **DEBUG** button to see real-time observability into the agent loop. Every LLM call shows iteration, message count, model, token usage, timing, and finish reason.

<p align="center">
  <img src="docs/debug-panel-expanded.png" width="600" alt="Debug panel showing iteration logs">
</p>

Click any row for a detailed breakdown explaining what each field means:

<p align="center">
  <img src="docs/debug-detail-modal.png" width="500" alt="Debug detail modal with explanations">
</p>

### Reading a debug row

```
iter 1  msgs 2  model anthropic/claude-sonnet-4-5  tok 897/68/965  dur 2131ms  finish tool_use
```

| Field      | What it means                                                                                       |
| ---------- | --------------------------------------------------------------------------------------------------- |
| **iter**   | Which pass through the agent loop (1 = first call, 2 = after tool result, etc.)                     |
| **msgs**   | Messages in conversation history — grows as tool calls and results are added                        |
| **model**  | The LLM model used                                                                                  |
| **tok**    | Tokens: prompt / completion / total — this determines API cost                                      |
| **dur**    | Wall-clock time for the LLM call (network + inference)                                              |
| **finish** | Why the LLM stopped: `end_turn` = final answer, `tool_use` = wants a tool, `max_tokens` = hit limit |

See **[docs/DEBUG-PANEL.md](docs/DEBUG-PANEL.md)** for the full observability guide.

## Metrics

Every completed conversational turn is logged to a local SQLite database at
`/app/data/metrics.db`. The `nano-claw-data` Docker volume persists the database
across container restarts. Each row includes the model, STT timing, LLM
time-to-first-token and total time, TTS timing, end-to-end latency, token usage,
and estimated cost.

`GET /api/metrics` returns the 50 most recent turns and per-model averages:

```bash
curl http://localhost:9090/api/metrics
```

You can also inspect a mounted database directly with `sqlite3`:

```bash
sqlite3 /app/data/metrics.db \
  'SELECT model, llm_ttft_ms, e2e_ms, est_cost_usd FROM turns ORDER BY id DESC LIMIT 5;'
```

## Site Knowledge (persona grounding)

The agent can answer questions about a website from a crawled snapshot instead
of stale training data. Space Channel (spacechannel.com) is the first site;
the pipeline is generic:

```bash
# 1. Crawl once — pages + JSON data feeds → data/<site>/site_index.json
.venv-test/bin/python scripts/crawl_site.py https://www.spacechannel.com/ \
  --feed https://www.spacechannel.com/data/launches.json  # repeat per feed

# 2. Distill → data/<site>/knowledge.md (~10k-token digest the LLM answers from)
python3 scripts/build_knowledge.py spacechannel

# 3. Run — run.sh auto-detects data/*/knowledge.md, mounts data/ read-only
#    at /app/sites, and injects the digest into the system prompt
./run.sh
```

To keep it fresh, re-crawl on a schedule using the base URL + feeds recorded
in the existing index (fails loudly for cron; a broken crawl never replaces
the last good digest):

```bash
scripts/refresh_site.sh spacechannel
```

How it works:

- **No lookup tool.** Tool calls pause the voice loop for approval, so
  knowledge rides in the system prompt: between the persona and the timestamp,
  where the Anthropic provider marks it as a prompt-cache prefix. After the
  first turn the ~10k-token digest is served from cache (watch
  `cacheRead`/`cacheWrite` in the Debug panel token counts).
- **Deterministic digests.** `build_knowledge.py` classifies launches as
  flown vs upcoming, renders vague NET dates honestly (`month precision`),
  precomputes rollups (next launch, per-provider counts), and surfaces each
  feed's own timestamp — a site can serve data older than the crawl. Empty
  feeds are marked EMPTY rather than omitted, which is what keeps the model
  from improvising.
- **Authored overview.** Crawls of JS-rendered SPAs capture data feeds, not
  page content. `docs/knowledge/<site>.md` (committed) describes what the
  site _is_; the builder prepends it to every digest.
- **Detail files.** `data/<site>/knowledge/<feed>.md` are small per-feed
  files safe to read into a conversation. The raw `site_index.json` is
  builder input only — at ~474KB it should never enter the context window.
- Knowledge files are configured via `agents.defaults.knowledgeFiles` in the
  config or the `NANO_CLAW_KNOWLEDGE` env var (comma-separated paths), and
  are re-read automatically when their mtime changes — a cron refresh lands
  on the next turn without a restart.

## Component Details

| Component         | Where it runs                | Role                                                                 |
| ----------------- | ---------------------------- | -------------------------------------------------------------------- |
| **STT Service**   | Mac native (port 8200)       | Speech-to-text via faster-whisper, Metal GPU accelerated             |
| **nano-claw API** | Docker (port 3001, internal) | Agent loop — LLM calls (Claude), tool execution, conversation memory |
| **Voice server**  | Docker (port 8080 → 9090)    | WebSocket bridge, TTS, WebRTC audio                                  |
| **Piper TTS**     | Docker                       | Text-to-speech — runs locally, streams audio via WebRTC              |
| **Browser UI**    | Your browser                 | Hands-free phone VAD, chat bubbles, tool approval cards, debug panel |

## Docker Details

### What `run.sh` does

1. Loads `.env` if present
2. Stops and removes any old `nano-claw-voice` container
3. Prunes dangling Docker images
4. `docker build -t nano-claw-voice .`
5. `docker run -it --rm -p 9090:8080` with API key and STT service URL

### Manual Docker commands

```bash
# Build
docker build -t nano-claw-voice .

# Run (make sure STT service is running on port 8200 first)
docker run -it --rm \
  -p 9090:8080 \
  -e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" \
  -e NANO_CLAW_KNOWLEDGE="/app/sites/spacechannel/knowledge.md" \
  -v nano-claw-models:/app/voice/models \
  -v nano-claw-data:/app/data \
  -v "$(pwd)/data":/app/sites:ro \
  nano-claw-voice
```

### Inside the container

The entrypoint starts two processes:

1. `node dist/cli/index.js serve --port 3001` — the TypeScript API server
2. `python -m voice` — the Python voice server on port 8080

The voice server waits for the API to be healthy before starting.

## Troubleshooting

| Problem                              | Fix                                                                                                             |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| "Mic access denied"                  | Allow microphone in browser permissions, use Chrome/Firefox/Safari                                              |
| Stuck on "Connecting..."             | Check that the Docker container is running (`docker ps`)                                                        |
| Transcription fails                  | Make sure the STT service is running: `curl http://localhost:8200/health`                                       |
| No sound from agent                  | Click somewhere on the page first (browsers require user interaction before playing audio)                      |
| "nano-claw API did not become ready" | Check that the provider key for the configured model is valid. Check Docker logs: `docker logs $(docker ps -q)` |
| Container won't start                | Make sure Docker is running and port 9090 isn't in use                                                          |
| STT service won't start              | Check Python 3.10+ is installed: `python3 --version`                                                            |

## Server Logs

**STT service terminal** shows transcription requests:

```
INFO:     POST /transcribe — 4.56s audio, 0.8s inference → "How much disk space do I have?"
```

**Docker terminal** shows agent loop iterations:

```
voice-server INFO  iter=1 msgs=2 model=anthropic/claude-sonnet-4-5
    tokens={'prompt': 897, 'completion': 68, 'total': 965} duration=2131ms finish=tool_use

(nano-claw): Tool execution complete
    tool: "shell"  success: true  durationMs: 342
```

## Project Structure

```
├── stt-service/
│   ├── server.py              # Standalone STT service (FastAPI + faster-whisper)
│   ├── requirements.txt       # Python deps for STT service
│   └── run.sh                 # Convenience launcher
├── src/
│   ├── api/server.ts          # HTTP API — agent loop with tool confirmation
│   ├── agent/                 # Core agent: loop, memory, context, tools
│   ├── providers/             # LLM providers (Anthropic, OpenRouter, OpenAI, etc.)
│   ├── cli/                   # CLI commands including `serve`
│   └── config/                # Configuration with Zod validation
├── voice/
│   ├── server.py              # aiohttp WebSocket server — bridges browser ↔ API
│   ├── stt.py                 # STT module (used by stt-service, not Docker)
│   ├── tts.py                 # Text-to-speech (Piper)
│   ├── webrtc.py              # WebRTC session + STT service client
│   └── web/                   # Browser UI
│       ├── index.html
│       ├── app.js             # WebSocket client, WebRTC, hands-free phone mode, debug panel
│       └── styles.css
├── Dockerfile                 # Multi-stage build (Node.js + Python)
├── docker/
│   ├── entrypoint.sh          # Starts both servers
│   └── default-config.json    # Default agent config for Docker
├── run.sh                     # One-command Docker build + run
└── docs/                      # Screenshots and debug panel guide
```

## License

MIT — see [LICENSE](LICENSE)
