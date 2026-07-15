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
    → Voice server POSTs text to nano-claw API
    → Claude generates response (may request tools)
    → If tool_pending: browser shows approval card, you approve/reject
    → If approved: tools execute, loop continues
    → Final text sent back via WebSocket
    → Voice server routes the selected voice: Kokoro voices go to the native
      TTS service (native Mac, port 8300), Piper voices are synthesized locally
      in the container. If Kokoro is unavailable, it falls back to Piper.
    → WebRTC audio stream back to your browser
You hear the answer
```

### Voices

The picker defaults to `af_heart` (Kokoro, American English, grade A). It's
grouped by language — American English, British English, and Spanish — and
each voice shows a quality grade. Click preview (▶) to hear a sample before
picking, or drag the speed slider to change Kokoro's tempo (Piper ignores
speed — it doesn't support it). Piper ("Lessac") stays available as the
fast, low-latency option, and it's what plays automatically if the Kokoro
service is down or unreachable.

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
- [Python 3.10+](https://www.python.org/) on your Mac (for the STT service)
- An [Anthropic API key](https://console.anthropic.com/)

### 1. Clone

```bash
git clone https://github.com/davidbmar/2026-nano-claw-voice-loop-tts-stt.git
cd 2026-nano-claw-voice-loop-tts-stt
```

### 2. Set your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or create a `.env` file in the project root:
```
ANTHROPIC_API_KEY=sk-ant-...
```

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

| Field | What it means |
|-------|---------------|
| **iter** | Which pass through the agent loop (1 = first call, 2 = after tool result, etc.) |
| **msgs** | Messages in conversation history — grows as tool calls and results are added |
| **model** | The LLM model used |
| **tok** | Tokens: prompt / completion / total — this determines API cost |
| **dur** | Wall-clock time for the LLM call (network + inference) |
| **finish** | Why the LLM stopped: `end_turn` = final answer, `tool_use` = wants a tool, `max_tokens` = hit limit |

See **[docs/DEBUG-PANEL.md](docs/DEBUG-PANEL.md)** for the full observability guide.

## Component Details

| Component | Where it runs | Role |
|-----------|--------------|------|
| **STT Service** | Mac native (port 8200) | Speech-to-text via faster-whisper, Metal GPU accelerated |
| **nano-claw API** | Docker (port 3001, internal) | Agent loop — LLM calls (Claude), tool execution, conversation memory |
| **Voice server** | Docker (port 8080 → 9090) | WebSocket bridge, TTS, WebRTC audio |
| **Piper TTS** | Docker | Text-to-speech — runs locally, streams audio via WebRTC |
| **Browser UI** | Your browser | Hands-free phone VAD, chat bubbles, tool approval cards, debug panel |

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
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" \
  -v nano-claw-models:/app/voice/models \
  nano-claw-voice
```

### Inside the container

The entrypoint starts two processes:
1. `node dist/cli/index.js serve --port 3001` — the TypeScript API server
2. `python -m voice` — the Python voice server on port 8080

The voice server waits for the API to be healthy before starting.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Mic access denied" | Allow microphone in browser permissions, use Chrome/Firefox/Safari |
| Stuck on "Connecting..." | Check that the Docker container is running (`docker ps`) |
| Transcription fails | Make sure the STT service is running: `curl http://localhost:8200/health` |
| No sound from agent | Click somewhere on the page first (browsers require user interaction before playing audio) |
| "nano-claw API did not become ready" | Check your `ANTHROPIC_API_KEY` is valid. Check Docker logs: `docker logs $(docker ps -q)` |
| Container won't start | Make sure Docker is running and port 9090 isn't in use |
| STT service won't start | Check Python 3.10+ is installed: `python3 --version` |

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
