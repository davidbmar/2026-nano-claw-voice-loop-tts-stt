# Agent Self-Knowledge: Settings and Health as Introspection Tools

2026-07-28 · branch `call-review-panel` · proposed by David ("the AI doesn't
know what is selected on the panel… can it query the state of the settings as
they are right now?"), scoped to query-on-demand rather than per-turn digest at
David's direction.

**Revision 2** (same day) after an adversarial Codex review returned *needs
rework* with 16 findings, 7 high (`.context/results/089-*`). Revision 1 is in
git history at `f612af7`. The findings did not scatter — they clustered on one
root cause, and this revision restructures around it rather than patching each
symptom.

## Problem

The agent knows the console panel *exists* and what every control *means* —
`data/base/knowledge.md:63-87` enumerates the assistant-mode dropdown, the
barge-in cluster, voice, speed, Whisper model, and the rest. It has no idea what
any of them are **set to**, and no idea whether the machine underneath it is
healthy.

Two questions it cannot answer:

1. *"What are your settings right now?"* — it describes the panel generically
   from static prose, which sounds like an answer and is not one.
2. *"Why do you sound choppy — is the machine overloaded?"* — it cannot
   distinguish a bad configuration from a degraded host. On 2026-07-26 a bumpy
   call was host degradation, not pipeline config; an agent reasoning from
   settings alone would have blamed a perfectly fine barge-in setup.

The blocker is capability, not data. Production voice agents run
`NANO_CLAW_DISABLE_TOOLS=1` — zero tools, no tool-calling — because the
alternative registers `ShellTool`, `ReadFileTool`, and `WriteFileTool` together.
`ReadFileTool` has no path restriction (`src/agent/tools/file.ts:45` is a bare
`readFileSync(path)`) and secrets reach the container through `docker -e` (see
the `-e` block in `run.sh`, roughly lines 251–290), so `/proc/self/environ` is a
live exfiltration target. On a publicly-dialable line, knowledge-only was the
only defensible setting.

**The feature is self-knowledge. The enabling change is a capability model that
lets a deployment expose safe tools without exposing a shell.**

## What the review changed

Revision 1 treated this as "add two tools." The review showed it is "build a
capability boundary, then add two tools through it." Three findings in
particular reframed the work:

- **There are two tool-registration paths, not one.**
  `AgentLoop.registerBuiltInTools()` (`src/agent/loop.ts:76-90`) registers
  shell/read/write whenever `config.tools.enabled !== false`, independent of
  `createToolRegistry()`, and `AgentLoop` executes tool calls immediately with
  no approval pause. Enforcement in one path is not enforcement.
- **There are two approval pauses, not one.** Non-streaming at
  `src/api/server.ts:649-679`, streaming at `~1005-1027`. Voice turns request
  SSE, so revision 1's "bypass the pause at line 657" would have shipped a
  feature that does not work on the only path that matters.
- **An empty advertised schema is documentation, not enforcement.** Providers
  can emit undeclared keys; `safeParseToolArgs()` accepts any parsed object and
  manufactures `_raw` for malformed JSON, and `ToolRegistry.execute()` passes it
  straight through. "Zero parameters" has to be checked where the tool runs.

## Invariant

> A tool reachable by an anonymous caller must expose **no caller-controlled
> target**: zero parameters *enforced at execution*, fixed data sources, and a
> typed output DTO whose every field, error path, and exception is safe to
> publish verbatim.

Read-vs-write is not the safety axis; the most dangerous tool in the registry
today is a read. What predicts danger is whether anything the caller says can
change what the tool touches.

| Tool | Verb | Caller picks target? | Anonymous-safe |
|---|---|---|---|
| `get_settings()` | read | no — zero args, enforced at execution | yes |
| `get_system_health()` | read | no — zero args, enforced at execution | yes |
| `read_file(path)` | read | **yes** | no — `/proc/self/environ` |
| `shell(cmd)` | exec | yes | no |
| `write_file(path, content)` | write | yes | no |

Two corollaries the review forced, both of which revision 1 got wrong:

**The DTO must be safe to publish, not merely narrated safely.** Output goes to
an LLM provider and must be assumed readable verbatim by an adversarial model. A
prompt instruction not to speak raw values is not a security control. A
field-name allowlist is also insufficient — secrets can ride inside *allowed*
fields, and `ToolRegistry.execute()` currently turns a thrown exception's raw
`.message` into tool output, which is how internal URLs, `ECONNREFUSED` targets,
and filesystem paths escape.

**Identifier secrecy is a property of the whole system, never of the
identifier.** Revision 1 proposed `GET /api/runtime/settings?sessionId=…`; the
review rated blind guessing impractical because conversation IDs are 128-bit
UUIDs. Both of us missed that `/api/metrics` published live session IDs to
anonymous callers. It was never guessing. That endpoint is now gated (below),
but **this design must not depend on session IDs being secret** — an ID is a
name, not a credential.

## Prerequisite — SHIPPED, not assumed

Review finding 4 identified a pre-existing hole that undermined the whole
"read-only phase is safe" premise: `POST /api/phone/config`, `/api/phone/vad`,
`/api/voice/flow`, and `/api/voice/region-model` were mounted on the same public
aiohttp app as the console with no authentication, so anyone could change the
live phone line's model/voice/STT and the assistant mode.

Fixed and deployed the same day:

- `5fbbd6b` — those four writes now require `NANO_CLAW_OPERATOR_PASSWORD` via an
  `X-NC-Operator` header (`secrets.compare_digest`), fail closed when unset.
  Adding them to `SENSITIVE_PATH_PREFIXES` alone would **not** have fixed it:
  that constant applies CSRF and response hardening, never authorization, and
  any direct HTTP client can send the three same-origin headers. `/api/phone/incoming`
  is deliberately excluded (Telnyx has its own token; guarding it kills every
  inbound call) with a regression test.
- `10bdfa7` — `/api/metrics` and `/api/costs` now take the operator token.

Both verified live. This design assumes that baseline and does not re-litigate
it. Note for future work: `SENSITIVE_PATH_PREFIXES` does two unrelated jobs
(CSRF + response hardening) and its name invites the assumption that membership
implies authorization. It caused that mistake twice in one session. Worth
renaming.

## Architecture

Two layers. Layer A is most of the work and is reusable by every future tool;
Layer B is the feature.

### Layer A — one capability platform

**A1. Single tool catalog and one startup-resolved policy.** One factory
enumerates every tool with its metadata. One policy object is resolved once at
startup and is immutable thereafter. Both the HTTP API and every `AgentLoop`
consumer (CLI, gateway channels, cron, subagents) obtain tools only through it;
the duplicate registration in `loop.ts` is deleted. The environment allowlist is
authoritative — config may further *disable* a capability but may never *add*
one.

**A2. Deployment allowlist by tool name.**

```bash
NANO_CLAW_TOOLS=get_settings,get_system_health
```

Chosen over a safety tier because **a list fails closed as the codebase grows**:
a tier forces whoever adds `book_appointment(...)` to classify it, and a
misclassification ships live on the next deploy, whereas an unnamed tool is
inert. Unset means no tools — the same fail-closed default as today's prod.
Secondary benefit: `deploy/m1.env.defaults` becomes the complete answer to "what
can this agent do?" without reading TypeScript, and M1/M3/nano-claw can diverge
without sharing a taxonomy.

**A3. `unsafe` and `requiresApproval` on `BaseTool`, both defaulting `true`.**

```typescript
export abstract class BaseTool {
  /** Caller-controlled target (path, command, free text). Default: assume yes. */
  unsafe = true;
  /** Pause the conversation for user approval before executing. */
  requiresApproval = true;
}
```

Kept as separate fields: the first governs whether a tool may be *exposed*, the
second whether a call *interrupts the conversation*. They coincide today;
conflating them would surprise us the first time they do not. Naming an `unsafe`
tool without `NANO_CLAW_ALLOW_UNSAFE_TOOLS` is a **startup error**.

`NANO_CLAW_ALLOW_UNSAFE_TOOLS` defaults to **false in every environment, dev
included** — there is no "trusted" default, because a dev-convenient default is
exactly what leaks to prod via a copied env file or a shared image. Written as
the word `false`, not `0`.

**A4. Runtime zero-argument enforcement.** Advertised schemas get
`additionalProperties: false`, but nothing depends on the provider honouring it.
At execution, a tool with `unsafe === false` requires a plain object with
**exactly zero own keys** and is refused otherwise — extra keys, arrays,
primitives, malformed JSON, and the `_raw` fallback all fail closed before the
tool body runs.

**A5. `ToolExecutionContext` for trusted ambient data.** The current session ID
and anything else the tool legitimately needs arrives through a typed context
supplied by the server, **never through model-supplied arguments**. This is what
lets `get_settings()` be session-scoped while taking zero parameters, and it is
why no caller-selectable session route is needed anywhere.

**A6. One shared tool-call dispatcher** used by both the streaming and
non-streaming loops. Defined behaviour:

- auto-execute only registered tools with `requiresApproval === false`, append
  results with the correct tool-call ID, continue within the iteration limit;
- a batch mixing approval-free and approval-required calls **pauses the whole
  batch before executing anything** (safest interpretation);
- unknown tool names fail closed;
- execution failure and cancellation produce a structured result, never a raw
  exception.

**A7. Boot validation before `listen()`.** `createServer()` currently never
calls `createToolRegistry()` — it is invoked lazily from chat loops, so
revision 1's "loud boot error" would in fact have been a first-caller error,
with `/api/health` passing and the container happily serving. Allowlist names
and the unsafe opt-in are validated at startup against the full catalog, and the
process refuses to start on a bad deployment. The resolved tool names are logged
(names only, no secrets).

**A8. Flag migration contract.** The existing truthy expression is inline and
specific to `NANO_CLAW_DISABLE_TOOLS`, so revision 1's "no parser change needed"
was misleading. Add one shared strict parser. For at least one release, a legacy
`NANO_CLAW_DISABLE_TOOLS=1` **overrides to zero tools** (or a legacy/new
conflict is rejected outright) — silently ignoring an operator's explicit kill
switch is unacceptable. `run.sh` must forward `NANO_CLAW_TOOLS`,
`NANO_CLAW_ALLOW_UNSAFE_TOOLS`, and `NANO_CLAW_VOICE_URL`. Test matrix: unset,
empty, whitespace, duplicates, unknown names, `false`, `0`, mixed case,
legacy-only, and conflicting old/new.

### Layer B — the two tools

**B1. Live runtime registry.** The data does not exist in a reachable shape
today: the browser `Session` is a local variable inside `websocket_handler`, the
history runtime holds only owner metadata (anonymous sockets are absent), phone
keeps `_active_calls: set[str]` rather than a `session_id → PhoneCall` map, and
`_agent_session_id()` is one-way — it mints an ID from a `Session`, it cannot
resolve one back. So: a minimal registry keyed by the server-owned Node session
ID, with registration and removal on browser and phone lifecycle, storing only a
typed snapshot provider. Pre-session, disconnected, and expired IDs return a
fixed `unavailable` DTO.

**B2. Internal-only transport.** The agent (Node) reaching the voice server
(Python) is a genuinely new dependency arrow — today the voice server calls
`POST /api/chat` and Node never calls back. It is **not** exposed as a
caller-selectable route on the public listener. The session ID comes from the
trusted execution context (A5), the endpoint requires an internal service
credential, browser-origin requests are rejected, and a mismatch returns `404`
rather than confirming existence.

**B3. `get_settings()` — zero arguments, one bounded internal call.** Six
setting fields live on the browser `Session` (`voice_id`, `speed`, `model`,
`stt_size`, `analysis_style`, `speech_mode` — `voice/webrtc.py:106-119`; revision
1 said "ten of ~13 controls are already on `Session`", which was wrong). The rest
are globals: assistant mode via `get_flow_profile()`, scheduler model, and the
phone settings. Phone values are globals read at *different times* — some at the
next utterance, some at the next turn, some at the next sentence — so the DTO
carries an explicit **effective-now vs next-turn** label per field rather than
implying they all apply immediately.

Revision 1 described this as "no I/O", which was wrong: the Python snapshot is
memory-only, but the Node tool necessarily performs one bounded internal HTTP
call.

**B4. `get_system_health()` — zero arguments, bounded probes.** Revision 1's
"hard timeout (500ms)" did not bound the work: `kokoro_client.is_healthy()` and
`lux_client.is_healthy()` are synchronous with 3-second timeouts (STT uses 1.5s),
and timing out the Node fetch does not cancel the threads — repeated calls would
exhaust the executor and worsen the exact overload being diagnosed. Instead:
concurrent async probes with per-probe deadlines below the overall budget,
bounded concurrency, single-flight with short-TTL caching so N callers cause one
probe, and per-session rate limiting. A disconnected caller must leave no
unbounded work behind.

**"Host load" needs defining before it can be claimed.** Voice and Node run in a
Linux container while STT/TTS/Lux run on the Mac host, so container metrics may
describe the Docker VM or cgroup rather than the Mac/MPS pressure that actually
degraded the 07-26 call. This design therefore reports **bounded service
readiness and latency plus cgroup-aware coarse saturation**, in qualitative bands
with tested thresholds, and states plainly what cannot be inferred. Exact host
figures are also a fingerprinting oracle and are out of scope; true Mac health
would need a separate narrowly-allowlisted host exporter.

**B5. Output DTOs.** Versioned, with enums, numeric ranges, and maximum lengths
on the leaves. Health returns coarse states (`healthy | degraded | unavailable`)
and fixed reason codes. Every success, timeout, parse failure, non-2xx, and
thrown exception is serialized through the DTO; detailed causes are logged on
the trusted side with redaction. Canonical model/voice labels are fine — those
catalogs are already public. Provider availability, credentials, base URLs,
internal hostnames/ports/paths, PIDs, other callers' data, conversation
identity, and raw exception text are not.

### Input validation at the write boundaries

`get_settings()` echoing stored values creates a **stored injection channel**
unless the writes are validated: `set_model` stores any string, `set_voice`
stores any non-empty string, and `POST /api/phone/config` persists an arbitrary
`model` globally — so attacker text could return to the model in the more
trusted `tool` role, and a planted phone value would be consumed by a *later*
caller's conversation. (`set_stt`, `set_analysis_style`, and `set_speech_mode`
already validate against closed sets.)

So, at every write boundary: strict booleans, finite bounded numbers, closed
enums and catalog IDs with length limits, unknown fields rejected. Store
canonical IDs; map them to fixed public labels in the snapshot; report the
**effective** value after fallback rather than an invalid requested one.

### New plumbing: the barge-in trio

Browser barge-in settings live only in `localStorage` and reach the server as
*events* (`barge_in`, `barge_in_commit`, `barge_in_false`), never as settings. A
`set_barge_in` message mirroring `set_model`/`set_voice`
(`voice/server.py:692-738`) is necessary but **not sufficient**: `app.js` must
also send the trio on connect and on reconnect, not only on change, or the
server's view is empty until the user happens to touch a control.

## Prompt changes

`data/base/knowledge.md` gains: **you have tools for your current settings and
health — call them rather than describing the panel from memory.** Without it the
model answers fluently from the static panel description and reports no actual
values; the failure is invisible because the wrong answer sounds fine.

The universal-approval claim must be retired in **three** places, not one:
`knowledge.md:48-49` and two copies inside `docker/default-config.json` (the
default and base-profile system prompts) all state that every tool call is shown
for approval. That becomes false for approval-free self-checks. A
prompt-construction test rejects the old sentence so the copies cannot drift back.

`knowledge.md:86` ("you cannot flip them yourself") remains true and unchanged —
this phase is read-only.

Spoken-register note: tools return structured data and the model narrates.
"Barge-in is on and fairly sensitive", not "sensitivity low, threshold 0.35".
This is UX, **not** a security control — see the DTO rule above.

## Non-goals

- **No write path.** Phase 2, where the approval pause becomes the confirmation
  UX rather than an obstacle.
- **No proactive tuning coach.** Answers when asked; does not volunteer opinions.
- **No shared state store.**
- **No per-turn config digest.**
- **No exact host metrics**, and no caller-selectable runtime route, ever.

## Enforcement tests

1. **Zero-argument enforcement at execution** for every `unsafe === false` tool:
   extra keys, arrays, primitives, malformed JSON, and `_raw` all refused.
   Schema-level `additionalProperties: false` asserted separately.
2. **DTO safety**: secret-shaped values planted in *allowed* fields are refused,
   and every error path — timeout, non-2xx, parse failure, thrown exception —
   serializes through the DTO with no raw `.message` escaping.
3. **Every entry point respects the allowlist**, with `NANO_CLAW_TOOLS` unset:
   HTTP API, CLI, gateway channels, cron, and subagents all register zero tools.
   This is the regression test for the `AgentLoop` bypass.
4. **Startup fails** on unknown allowlist names and on an unsafe tool without the
   opt-in — asserted at process/server startup, not by calling a factory.
5. **Both dispatch paths**: streaming and non-streaming, plus mixed batches and
   unknown names.
6. **Probe saturation and cancellation**: N concurrent health calls cause one
   probe; a disconnected caller leaves no work running.
7. **Prompt construction** rejects the universal-approval sentence.

## Failure modes

| Failure | Behaviour |
|---|---|
| Voice server unreachable from Node | Bounded deadline → `unavailable` DTO; call continues |
| Probe slower than its deadline | Per-probe deadline; cached/coarse result; no thread leak |
| Unknown name in `NANO_CLAW_TOOLS` | Startup error, refuse to start |
| Unsafe tool named without opt-in | Startup error, refuse to start |
| Legacy `DISABLE_TOOLS=1` plus a new list | Legacy wins (zero tools) or hard conflict error |
| Session ID unknown/expired/pre-session | Fixed `unavailable` DTO, never another session's data |
| Model answers from static panel prose | `knowledge.md` instruction; eval case |
| Adversarial model dumps the DTO verbatim | Acceptable by construction — DTO is publishable |

## Build order

Reordered so no intermediate commit is unsafe or half-broken. Revision 1 exposed
routes before their auth tests and deferred all tests to the end; it also would
have caused an outage if deployment allowlists changed at step 2, since boot
validation would see names for tools that did not exist until step 6.

1. Centralized policy + single catalog (A1, A2, A3), `ToolExecutionContext`
   (A5), strict DTO and argument validators (A4, B5) — **with failing boundary
   tests first**
2. Startup validation (A7) and the flag migration contract (A8)
3. Shared dispatcher across both approval paths (A6)
4. Write-boundary validation + `set_barge_in` including connect/reconnect sync
5. Live runtime registry (B1) and internal-authenticated transport (B2), bounded
   probes (B4) — routes stay internal-only at every intermediate commit
6. The two tools (B3, B4) through the platform
7. Prompt updates across all three copies + evals
8. **Deployment allowlists change last**, as the final enablement step, only
   after everything above passes

## Phase 2 (not in scope)

The write path — "turn off barge-in", "switch to the fast model". Caller-controlled
by definition, so `unsafe` by the rule above, and the approval pause becomes the
confirmation UX. Revisit once phase 1 is live and we know what people actually ask.
