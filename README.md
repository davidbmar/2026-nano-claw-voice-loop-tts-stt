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

You talk to an AI agent through your browser. It listens (Whisper STT), thinks (Claude), speaks back (Kokoro TTS), and can run tools on your machine with your approval. Everything runs in a single Docker container.

**The loop:** You speak → Whisper transcribes → Claude responds → if it needs a tool, you approve/reject → Claude continues → Kokoro speaks the answer back.

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
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

### 3. Build and run

```bash
./run.sh
```

This single command:
- Builds the Docker image (TypeScript API server + Python voice server)
- Downloads the Whisper STT model on first run (~75 MB, cached in a Docker volume)
- Starts the container

### 4. Open your browser

Go to **http://localhost:9090**

Allow microphone access when prompted. Once it says "Connected", you're ready.

### 5. Talk

**Hold** the blue button and speak. **Release** to send. The agent will think, optionally request tool approval, and speak its answer back.

### 6. Stop

Press `Ctrl-C` in the terminal. The container cleans itself up (`--rm`).

## How It Works

<p align="center">
  <img src="docs/voice-ui-connected.png" width="500" alt="Connected and ready to talk">
</p>

### Voice conversation

Hold the button, ask a question, release. Your speech is transcribed and shown as a blue bubble. The agent's reply appears as a gray bubble and is spoken aloud through your browser.

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

## Architecture

Everything runs inside one Docker container:

```
┌─────────────────────────────────────────────────────────┐
│  Docker container                                       │
│                                                         │
│  ┌──────────────────┐    ┌───────────────────────────┐  │
│  │  nano-claw API   │    │  Voice Server (Python)    │  │
│  │  (TypeScript)    │    │                           │  │
│  │                  │    │  WebSocket ←→ Browser     │  │
│  │  Agent loop      │◄──►│  Whisper STT (speech→text)│  │
│  │  Tool execution  │    │  Kokoro TTS (text→speech) │  │
│  │  Memory          │    │  WebRTC audio streaming   │  │
│  │                  │    │                           │  │
│  │  port 3001       │    │  port 8080 → 9090        │  │
│  │  (internal)      │    │  (exposed)               │  │
│  └──────────────────┘    └───────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

| Component | Role |
|-----------|------|
| **nano-claw API** | Agent loop — LLM calls (Claude), tool execution, conversation memory |
| **Voice server** | WebSocket bridge between browser and API, plus STT/TTS processing |
| **Whisper** | Speech-to-text — runs locally in the container, no external API |
| **Kokoro** | Text-to-speech — runs locally, streams audio back via WebRTC |
| **Browser UI** | Push-to-talk, chat bubbles, tool approval cards, debug panel |

### Data flow

```
You speak into mic
    → WebRTC audio stream to container
    → Whisper transcribes to text
    → POST /api/chat to nano-claw API
    → Claude generates response (may request tools)
    → If tool_pending: browser shows approval card, waits
    → If approved: POST /api/chat/approve, tools execute, loop continues
    → Final text response sent back via WebSocket
    → Kokoro converts to speech
    → WebRTC audio stream back to your browser
You hear the answer
```

## Docker Details

### What `run.sh` does

1. Loads `.env` if present
2. Stops and removes any old `nano-claw-voice` container
3. Prunes dangling Docker images
4. `docker build -t nano-claw-voice .` — multi-stage build (Node.js builder + Python runtime)
5. `docker run -it --rm -p 9090:8080 -e ANTHROPIC_API_KEY=... nano-claw-voice`

### Manual Docker commands

If you prefer to run the steps yourself:

```bash
# Build
docker build -t nano-claw-voice .

# Run
docker run -it --rm \
  -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v nano-claw-models:/app/voice/models \
  nano-claw-voice
```

The `-v nano-claw-models:/app/voice/models` volume caches the Whisper model so it doesn't re-download on each run.

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
| No sound from agent | Click somewhere on the page first (browsers require user interaction before playing audio) |
| Slow first response | The Whisper model downloads on first run (~75 MB). Subsequent runs use the cached volume. |
| "nano-claw API did not become ready" | Check your `ANTHROPIC_API_KEY` is valid. Check Docker logs: `docker logs $(docker ps -q)` |
| Container won't start | Make sure Docker is running and port 9090 isn't in use |

## Server Logs

The Docker terminal shows structured logs from both servers. Look for:

```
voice-server INFO  iter=1 msgs=2 model=anthropic/claude-sonnet-4-5
    tokens={'prompt': 897, 'completion': 68, 'total': 965} duration=2131ms finish=tool_use

(nano-claw): Agent loop iteration complete
    iteration: 1  durationMs: 2131  finishReason: "tool_use"

(nano-claw): Tool execution complete
    tool: "shell"  success: true  durationMs: 342
```

## Project Structure

```
├── src/
│   ├── api/server.ts        # HTTP API — agent loop with tool confirmation
│   ├── agent/               # Core agent: loop, memory, context, tools
│   ├── providers/           # LLM providers (Anthropic, OpenRouter, OpenAI, etc.)
│   ├── cli/                 # CLI commands including `serve`
│   └── config/              # Configuration with Zod validation
├── voice/
│   ├── server.py            # aiohttp WebSocket server — bridges browser ↔ API
│   ├── stt.py               # Speech-to-text (faster-whisper)
│   ├── tts.py               # Text-to-speech (Kokoro)
│   ├── webrtc.py            # WebRTC session management
│   └── web/                 # Browser UI
│       ├── index.html
│       ├── app.js           # WebSocket client, WebRTC, push-to-talk, debug panel
│       └── styles.css
├── Dockerfile               # Multi-stage build (Node.js + Python)
├── docker/
│   ├── entrypoint.sh        # Starts both servers
│   └── default-config.json  # Default agent config for Docker
├── run.sh                   # One-command build + run
└── docs/                    # Screenshots and debug panel guide
```

## License

MIT — see [LICENSE](LICENSE)
