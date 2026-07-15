# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
