# Agent Self-Knowledge: Settings and Health as Introspection Tools

2026-07-28 · branch `call-review-panel` · proposed by David ("the AI doesn't
know what is selected on the panel... can it query the state of the settings as
they are right now?"), scoped to query-on-demand rather than per-turn digest at
David's direction.

## Problem

The agent knows the console panel *exists* and what every control *means* —
`data/base/knowledge.md:63-87` enumerates the assistant-mode dropdown, the
barge-in cluster, voice, speed, Whisper model, and the rest. It has no idea what
any of them are **set to**, and no idea whether the machine underneath it is
healthy.

So two questions it cannot answer today:

1. *"What are your settings right now?"* — it will describe the panel generically
   from static prose, which sounds like an answer and is not one.
2. *"Why do you sound choppy — is the machine overloaded?"* — it cannot
   distinguish a bad configuration from a degraded host. On 2026-07-26 a bumpy
   call was diagnosed as host degradation, not pipeline config; an agent
   reasoning from settings alone would have blamed a perfectly fine barge-in
   setup.

The blocker is not data availability. It is capability:

```
deploy/m1.env.defaults:6:NANO_CLAW_DISABLE_TOOLS=1
deploy/m3.env.defaults:6:NANO_CLAW_DISABLE_TOOLS=1
```

Production voice agents run knowledge-only — **zero tools registered, no
tool-calling at all**. And `createToolRegistry()` (`src/api/server.ts:228-244`)
is all-or-nothing: enabling tools registers `ShellTool`, `ReadFileTool`, and
`WriteFileTool` together. `ReadFileTool` has no path restriction whatsoever
(`src/agent/tools/file.ts:45` is a bare `readFileSync(path)`), and `run.sh:287`
passes secrets into the container via `-e`, so `/proc/self/environ` exposes every
API key. On a publicly-dialable voice line, knowledge-only was the only
defensible setting.

**This design breaks that binary.** The feature is self-knowledge; the enabling
change is a capability model that lets a deployment expose safe tools without
exposing a shell.

## Invariant

> A tool reachable by an anonymous caller must expose **no caller-controlled
> target**: zero parameters, fixed data sources, allowlisted output fields.

Read-vs-write is explicitly *not* the safety axis. The most dangerous tool in the
current registry is a read tool. What predicts danger is whether anything the
caller says can change what the tool touches:

| Tool | Verb | Caller picks target? | Anonymous-safe |
|---|---|---|---|
| `get_settings()` | read | no — no parameters | yes, structurally |
| `get_system_health()` | read | no — no parameters | yes, structurally |
| `read_file(path)` | read | **yes** | no — `/proc/self/environ` |
| `shell(cmd)` | exec | yes | no |
| `write_file(path, content)` | write | yes | no |

Safety by construction, not by vigilance: "remember not to add a tool that reads
`.env`" is a policy living in a reviewer's head and it holds until someone adds
`get_config_value(key)` because it looked harmless. A tool with no parameters
offers no lever to pull.

## Decisions

### 1. Query on demand, not a per-turn digest

State is asked for rarely and explicitly. A digest injected every turn costs
tokens and attention permanently — and worse, `kokoro_client.is_healthy()` is an
HTTP probe, so a health digest means running network probes on every turn to
serve a question asked once in a hundred. Health is only meaningful at the
instant of asking.

Rejected alternative — digest after `SYSTEM_CACHE_MARKER` (`context.ts:105`):
cheap for settings, wrong for health, and it does not compose. Every future fact
would make every turn heavier forever. A tool-shaped capability composes: call
history, cost ledger, and next-appointment all become the same shape later.

### 2. Deployment allowlist by tool name, not a safety tier

```bash
NANO_CLAW_TOOLS=get_settings,get_system_health
```

`ToolRegistry` is already `Map<string, BaseTool>` keyed by `tool.name`
(`registry.ts:47,53`), so filtering at registration is a small change.

Chosen over a tier/class enum because **a list fails closed as the codebase
grows**. When someone adds `book_appointment(...)` in six months, a tier forces
them to classify it and a misclassification ships live on the next deploy. With
an allowlist the new tool is inert until a deploy file names it — new capability
defaults to dark, everywhere, always.

Secondary benefits: `deploy/m1.env.defaults` becomes the complete answer to "what
can this agent do?" without reading TypeScript, and M1/M3/nano-claw can diverge
in capability without sharing a taxonomy.

**Unset means no tools.** An absent `NANO_CLAW_TOOLS` registers nothing — the
same fail-closed default as today's prod. Every environment that wants tools
names them explicitly, including local dev; `.env.example` ships with the two
introspection tools listed so a fresh checkout gets self-knowledge and nothing
else.

### 3. `NANO_CLAW_ALLOW_UNSAFE_TOOLS` defaults to false — including dev

An allowlist alone makes safety a *deployment-config* property: a one-line env
edit (`NANO_CLAW_TOOLS=get_settings,read_file`) exposes secrets with no code
review and no test to catch it. The seatbelt is one boolean on the base class,
not a taxonomy:

```typescript
export abstract class BaseTool {
  /** Caller-controlled target (path, command, free text). Default: assume yes. */
  unsafe = true;
}
```

`ShellTool`, `ReadFileTool`, `WriteFileTool` inherit `true`. The two
introspection tools set `false`. Naming an unsafe tool without
`NANO_CLAW_ALLOW_UNSAFE_TOOLS` **refuses it at boot with a loud error** rather
than silently registering or silently dropping it.

Default is **false in every environment, dev included** — there is no
"trusted" default. Dev opts in deliberately, the same as prod would have to.
Written as the word `false` rather than `0` in `.env.example` and all
`deploy/*.env.defaults`, so the intent reads at a glance. The existing truthy
parser (`['1','true','yes']`, `src/config/index.ts:126`) already treats `false`
as falsy; no parser change is needed.

`NANO_CLAW_DISABLE_TOOLS` is retired. `NANO_CLAW_TOOLS=` (empty) is the
knowledge-only mode, which is what `scripts/cross_source_eval/run_eval.py:1009`
becomes.

### 4. Read-only tools skip the approval pause

`src/api/server.ts:657` pauses on **any** tool call — correct for the only tools
that exist today, wrong for a side-effect-free read. In a spoken conversation,
answering "what are your settings?" with an approval dialog is not a slow answer,
it is a broken one.

`BaseTool` gains `requiresApproval`, defaulting to `true`. Tools with
`unsafe = false` set it `false` and bypass the pause at line 657. `unsafe` and
`requiresApproval` stay separate fields: the first governs whether a tool may be
*exposed*, the second whether a call *interrupts the conversation*. They coincide
today; conflating them would surprise us the first time they do not.

### 5. Agent → voice server is a new, deliberate dependency arrow

Today the arrow is one-way: the voice server calls `POST /api/chat`
(`voice/server.py:1319`); the Node agent never calls back. Both tools need state
that lives in the voice server, so the arrow must be added.

Two new read-only aiohttp routes:

- `GET /api/runtime/settings?sessionId=…` — resolves via `_agent_session_id()`
- `GET /api/runtime/health`

Reached from Node via `NANO_CLAW_VOICE_URL`, with a **hard timeout (500ms) and
graceful degradation**: on timeout or error the tool returns a structured
"unavailable" result and the agent says it cannot reach its own status. A
self-check must never hang a live call.

## The two tools

### `get_settings()` — no arguments, no I/O

Reads what is already in memory. Ten of ~13 panel controls are *already* on the
server-side `Session` object (`voice/webrtc.py:106-119`) and nothing has ever
read them for prompt purposes:

```python
self.voice_id  self.speed  self.model  self.stt_size
self.analysis_style  self.speech_mode
```

Plus `get_flow_profile()` (assistant mode), the scheduler/region model, and phone
VAD as server globals.

**Phone sessions are in scope.** A phone caller has no panel, but the settings
are just as real — voice, model, STT, VAD all come from `/api/phone/config` and
`/api/phone/vad`. The tool reports the phone line's configuration for those
sessions, and the narration adapts: a phone caller is told what the line is set
to, never pointed at a control they cannot see.

### `get_system_health()` — no arguments, probes services

Kokoro / LuxTTS / STT reachability (`kokoro_client.is_healthy()`,
`lux_client.is_healthy()` — already implemented and already used for a
user-facing voice warning at `voice/server.py:743-751`), host load, and recent
turn latency against a normal band. This is the tool that gets the 07-26 call
right: degraded host, not bad config.

It costs real I/O, so it is the case where the thinking-cue filler earns its
keep. Kept separate from `get_settings()` precisely because their costs differ —
an instant question should not pay a probe's latency.

## New plumbing: the barge-in trio

Everything else is already server-side. The barge-in settings are pure
`localStorage` and reach the server only as *events* (`barge_in`,
`barge_in_commit`, `barge_in_false`), never as *settings*.

Add a `set_barge_in` WS message mirroring the established `set_model` /
`set_voice` pattern (`voice/server.py:692-738`) exactly, including the
`pending_settings` pre-session path, carrying `enabled`, `sensitivity`, and
`adaptive` onto `Session`.

## Prompt changes

`data/base/knowledge.md` gains an explicit instruction: **you have tools for your
current settings and health — call them rather than describing the panel from
memory.** Without this the model answers fluently from the static panel
description at `knowledge.md:63-87` and reports no actual values. The failure is
invisible because the wrong answer sounds completely fine.

`knowledge.md:86` ("These are changed by the user in the console — you cannot
flip them yourself") **remains true and unchanged**. This phase is read-only.

Spoken-output note: the tools return structured data; the model narrates. It must
not read raw thresholds or millisecond figures aloud. "Barge-in is on and fairly
sensitive" is the register, not "sensitivity low, threshold 0.35."

## Non-goals

- **No write path.** The agent cannot change settings in this phase. That is
  phase 2, and it is exactly where the approval pause becomes a feature.
- **No proactive tuning coach.** The agent answers when asked; it does not
  volunteer configuration opinions. `knowledge.md:91-94` ("do not recite this
  document unprompted") still governs.
- **No shared state store.** One config owner per session, already in memory in
  the right process, already the right lifetime.
- **No per-turn config digest.**

## Enforcement tests

Two small tests, in the spirit of the AST isolation test pattern — the boundary
stops depending on whoever reviews the next PR:

1. **Empty parameter schema.** Every registered tool with `unsafe === false`
   exposes `parameters.properties === {}` and no `required`. Adding a parameter
   to an introspection tool fails the build.
2. **Allowlisted output fields.** `get_system_health()` and `get_settings()`
   return only fields on an explicit allowlist — never a config dump, never an
   env echo, never a provider key. A no-argument tool that returns
   `process.env` is exactly as dangerous as `read_file`.

Plus: boot validation rejects unknown names in `NANO_CLAW_TOOLS` with a startup
error. A typo'd tool name that silently disables self-knowledge presents to the
user as "the AI is being dumb again" — the hardest class of bug to trace.

## Failure modes

| Failure | Behaviour |
|---|---|
| Voice server unreachable from Node | 500ms timeout → "I can't reach my own status right now"; call continues |
| Unknown name in `NANO_CLAW_TOOLS` | Loud boot error, refuse to start |
| Unsafe tool named without the opt-in | Loud boot error, refuse to start |
| Model answers from static panel prose | Mitigated by the `knowledge.md` instruction; covered by an eval case |
| Model reads raw numbers aloud | Spoken-register instruction alongside the tool description |

## Security posture, before and after

**Before:** binary — knowledge-only, or shell plus unrestricted filesystem read
on a public voice line. Prod chose knowledge-only, which is why the agent knows
nothing about itself.

**After:** prod exposes exactly two zero-argument tools with allowlisted output.
Worst case with a fully adversarial LLM is that it tells a caller the TTS service
is healthy. Unsafe tools require a second, deliberately separate opt-in that is
false by default in every environment.

## Build order

1. `BaseTool` gains `unsafe` and `requiresApproval` (defaults `true`/`true`)
2. Allowlist + opt-in flag in `createToolRegistry()`, boot validation, retire
   `NANO_CLAW_DISABLE_TOOLS`
3. Approval bypass at `src/api/server.ts:657` for `requiresApproval === false`
4. `set_barge_in` WS message → `Session`
5. `GET /api/runtime/settings` and `GET /api/runtime/health` on the voice server
6. `get_settings` and `get_system_health` tools in Node, with timeout handling
7. `knowledge.md` instruction + spoken-register note
8. The two enforcement tests, boot-validation test, and an eval case for
   "what are your settings?"

## Phase 2 (not in scope)

The write path — "turn off barge-in", "switch to the fast model". It needs a
different safety story: a caller-controlled target by definition, so it is
`unsafe` by the rule above, and the approval pause becomes the confirmation UX
rather than an obstacle. Revisit once phase 1 is live and we know what people
actually ask for.
