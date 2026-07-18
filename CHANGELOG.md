# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Default chat LLM is now `gemini/gemini-flash-lite-latest` (~2× cheaper than
  Haiku for 24h continuous use once prompt caching is accounted for; the
  goal-region supervisor stays on claude-haiku-4-5 per the eval sweep).
  `/api/models` now advertises the configured default instead of the
  compiled-in constant.
- The phone line (512-FLOW-101) speaks `lux_george` — the LuxTTS clone of the
  previous `bm_george` Kokoro voice (`NANO_CLAW_PHONE_VOICE` in `.env`).

### Added

- Live phone-line controls in the web UI: the ⚙ panel is split into "This
  browser" (STT/LLM/Voice/Speed for the page session) and "Phone line"
  (Voice/LLM/STT/Speed/VAD/Flow) sections. Phone voice, STT, and speed are
  runtime overrides served by `GET/POST /api/phone/config` that apply live —
  next sentence / next utterance — even mid-call; the LLM applies on the next
  agent turn; VAD and Flow apply per call. The panel shows a "N call live"
  indicator; a restart returns to the `.env` values. UI files are now served
  with `Cache-Control: no-cache` so deploys can't leave stale tabs running
  controls that silently do nothing.
- LuxTTS voice-cloning engine as an optional dropdown voice: new native
  `lux-service/` (port 8301, isolated venv) mirrors the Kokoro service and
  serves 48kHz cloned speech; `voice/lux_client.py` + `engine: "luxtts"`
  routing in `voice/tts.py` with Piper fallback when the service is down.
  Ships 20 voices — one clone per Kokoro voice (Spanish references included;
  they speak English with the cloned timbre) — prewarmed at service startup.
  The phone line (512-FLOW-101) speaks `lux_george` via NANO_CLAW_PHONE_VOICE.
  Supply-chain
  audited before adoption (`.gstack/security-reports/2026-07-17-luxtts-supply-chain.json`);
  `lux-service/setup.sh` pins LuxTTS/LinaCodec commits + the HF model
  revision and pickle-scans weights before the server will load them.
- Supply-chain pinning across all services: `requirements.lock` (full pip
  freeze of the known-good envs) for `lux-service/`, `tts-service/`,
  `stt-service/`, and the container (`voice/requirements.lock`); run scripts
  and the Dockerfile install from locks when present. Dockerfile now copies
  `package-lock.json` and uses `npm ci` (it previously resolved npm deps fresh
  on every build). lux-service prefetches its models (LuxTTS + whisper-base)
  at pinned revisions in setup.sh and runs with `HF_HUB_OFFLINE=1` — nothing
  auto-updates at runtime.
- Site knowledge pipeline: `scripts/crawl_site.py` (site → `data/<site>/site_index.json`),
  `scripts/build_knowledge.py` (index → `knowledge.md` digest + per-feed detail files),
  and `scripts/refresh_site.sh` for cron re-crawls. Space Channel is the first site.
- Knowledge injection into the system prompt via `agents.defaults.knowledgeFiles`
  or `NANO_CLAW_KNOWLEDGE`; `run.sh` auto-detects `data/*/knowledge.md` and mounts
  `data/` read-only at `/app/sites`. Digests re-load on mtime change.
- Anthropic prompt caching: the stable system-prompt prefix (persona + knowledge)
  is marked with a `cache_control` breakpoint; cache read/write token counts are
  surfaced in `/api/chat` debug output.
- Hand-authored site overviews under `docs/knowledge/<site>.md`, prepended to
  digests to cover what feed crawls of SPA sites miss.

## [0.1.0] - 2026-02-11

### Added

- Initial TypeScript + Node.js implementation of nano-claw
- Core agent system with LLM integration
- Multi-provider support (OpenRouter, Anthropic, OpenAI, DeepSeek, Groq, Gemini, etc.)
- Provider registry pattern for easy addition of new LLM providers
- Conversation memory system with persistent storage
- Context builder for prompt construction
- Skills loader for Markdown-based skills
- Tool system with built-in tools:
  - Shell command execution
  - File read/write operations
- Agent loop with tool execution capability
- Configuration system with Zod validation
- Environment variable support for API keys
- CLI with commands:
  - `onboard` - Initialize configuration
  - `agent` - Chat with the agent (interactive and single-message modes)
  - `status` - Show system status
- Comprehensive bilingual documentation (English + Chinese)
- Project structure following nanobot architecture
- TypeScript strict mode with full type safety
- ESM module system
- Logging with pino
- Error handling utilities

### Infrastructure

- TypeScript 5.x configuration
- Node.js >= 18 support
- Package management with npm/pnpm
- ESLint + Prettier for code quality
- Build system with TypeScript compiler
- MIT License

### Documentation

- Comprehensive README (English + Chinese)
- Contributing guide
- Example skills (weather, GitHub)
- OpenRouter setup guide
- API type definitions

## [Unreleased]

### Added
- SQLite telemetry for every conversational turn, including per-stage timing,
  token usage, estimated cost, persisted storage, per-model metrics, and live
  LLM time-to-first-token in the Debug panel.
- Pipeline settings (⚙ panel): switch STT (Whisper model size), LLM (any
  cataloged model whose provider key is set in `.env`), and TTS voice — all
  live, no restart. `GET /api/models` reports the catalog with per-model
  `available` reflecting which provider keys are configured; models without
  a key show "— no key" and are unselectable. `POST /api/chat` accepts a
  per-request `model` override, validated against the catalog. Recognized
  keys: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`,
  `GROQ_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY`. Selections persist
  in `localStorage` and are re-applied to the session via `set_model` /
  `set_stt` WebSocket messages.
- OpenAI-compatible SSE streaming, extended to every OpenAI-compatible
  provider (Gemini, DeepSeek, Groq, Alibaba DashScope, OpenAI) — previously
  only Anthropic streamed. Text deltas and tool-call deltas are parsed
  incrementally so replies from any of these providers speak
  sentence-by-sentence like the Anthropic path.
- Barge-in (opt-in, `NANO_CLAW_BARGE_IN=1`): interrupt Claude mid-reply — playback
  pauses on your voice, your speech becomes the next turn, and a false alarm
  resumes the reply after a randomized exponential backoff. Regardless of the
  flag, a second text message sent while a reply is still streaming is
  dropped rather than queued (one reply at a time).
- Kokoro-82M TTS as a native Mac service (port 8300) with a browser voice picker
  (American/British English + Spanish), quality-grade labels, per-voice preview,
  and a speed slider. Piper remains as the fast, low-latency option. Selecting a
  Kokoro voice while the service is down falls back to Piper automatically.
- Streaming voice replies — Claude's answer is spoken sentence-by-sentence as it's
  generated (Anthropic native streaming → SSE → incremental TTS), so audio starts
  at the first sentence. Text also streams into the chat log. `NANO_CLAW_STREAM=0`
  forces the legacy path.

### Planned Features

- Gateway server for channel integrations
- Chat channel implementations:
  - Telegram
  - Discord
  - WhatsApp
  - Feishu
  - Slack
  - Email
  - QQ
  - DingTalk
  - Mochat
- Message bus for routing
- Session management
- Cron job scheduler
- Heartbeat mechanism
- Subagent for background tasks
- Additional built-in tools:
  - Web search
  - API requests
  - Database operations
- Built-in skills library
- Unit tests
- Integration tests
- CI/CD pipeline
- Docker support
- Package publishing to npm
