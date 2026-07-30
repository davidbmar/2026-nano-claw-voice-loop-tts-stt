# The Emotive Display

How the thing in the middle of the console decides what to feel, and why it is
built out of regular expressions rather than a model.

Written to be extended. If you are here to add an emotion, a rule, or a third
renderer, the sections you want are **Adding a rule** and **Two renderers, one
vocabulary**. If you are here because you think a language model would do this
better, read **Why not a model** first — that experiment has been run.

## What the display is

The centre of the voice console is the agent's face. Two renderers can occupy
it, chosen from **VISUALIZATION → DISPLAY**:

- **Voice constellation** — the particle field (`talking-cube.js`), a 2D canvas
  of drifting points that react to audio.
- **Computer mascot** — a pen-and-ink robot character (`vendor/mascot/`) whose
  screen is its face.

Both speak the same vocabulary, so nothing else in the system needs to know
which one is on screen.

**Eleven emotions.** `neutral`, `calm`, `curious`, `confused`, `warm`,
`joyful`, `confident`, `tense`, `somber`, `awe`, `urgent`.

**Seven presences** — what the pipeline is doing, as distinct from how it
feels: `idle`, `listening`, `silent`, `thinking`, `confused`, `speaking`,
`paused`.

Emotion and presence are independent layers. The agent can be `thinking` and
`somber` at once; that is a different picture from `thinking` and `joyful`.

## Two signals, two rule sets

The display reads **both sides of the conversation**, and it reads them with
different rules.

```
caller stops speaking
  └─ transcript arrives ──▶ inferInboundEmotion(text)   ~0ms
                                └─▶ display reacts WHILE the agent thinks

agent replies
  └─ reply text ─────────▶ inferEmotion(text)           ~0ms
                                └─▶ display reacts as it speaks
```

Before this, only the second half existed: the display sat still until the agent
answered. Now a caller saying *"my basement is flooding right now"* moves it
immediately, which is what makes the agent look like it is **listening** rather
than merely talking.

### Why the rule sets are separate

This is the part most likely to be "simplified" by someone later, so here is the
reason in full.

The outbound rules were written for **agent replies** and are correct there. Run
them on **caller speech** and they misfire in a specific, embarrassing way:

> Caller: *"Wait, sorry, I don't understand what you just said."*

The outbound `somber` rule matches on `sorry` — which in an agent reply means
*"I'm sorry, we're fully booked"*, and in caller speech means *"sorry, could you
repeat that"*. The display would go sad at someone who is merely confused.

`tests/inbound-emotion.test.ts` asserts both halves of that misfire, so anyone
merging the two rule sets gets a failing test rather than a subtly sad robot.

The same asymmetry runs the other way. The outbound comment above
`EMOTION_RULES` notes that a caller's *"is it booked?"* must not read as joy —
affirmative outcome words belong to the agent, not to someone asking whether
something happened.

## Why not a model

Measured 2026-07-30, on the machine this runs on. Recorded here so nobody
re-runs it.

### Could the agent just decide?

The obvious design: give the model a `set_emotion` tool and let it choose. It
was tested against `gemma4:e2b`, one of the local models the console offers.

| Prompt | Tool call | Reply text |
|---|---|---|
| "Before replying, call set_emotion… then reply" | **no** | *"I need more context to answer that question."* |
| "Call set_emotion with emotion=somber. **Do this now.**" | **yes** | **empty** |
| "Call set_emotion for how your reply should feel, and reply in one sentence." | **no** | "What would you like to book?" |

Ollama reports `capabilities: tools` for this model and is telling the truth —
it emits a valid call with the right enum value when ordered to. What it will
not do is *volunteer* one in natural conversation, which is the only shape that
would be useful.

The second row is the more important result. When it did call the tool, the
reply was **empty** — the turn was spent, and getting the actual sentence needs
a second inference. At ~6.1s per turn locally that roughly doubles
time-to-first-word for a cosmetic effect. That cost is structural, not a quirk
of this model: tool-call turns do not carry the reply.

### Could a tiny classifier decide?

Next obvious design: a small model reads the text and returns an emotion. Tested
with ollama's JSON-schema constrained decoding, which is essential — an
off-vocabulary name is silently dropped by `setVisualEmotion`, so the display
just fails to move.

| Input | `gemma3:270m` (~391ms) | `gemma3:1b` (~537ms) | Rules (~0ms) |
|---|---|---|---|
| "third time I've called, nobody has helped" | neutral | **urgent** | **urgent** |
| "my basement is flooding **right now**" | **calm** | **urgent** | **urgent** |
| "wait, sorry, I don't understand" | *concerned*¹ | neutral | **confused** |
| "yeah that works great, thanks so much" | neutral | neutral | **warm** |
| "uh. hmm. i guess. maybe?" | neutral | neutral | **curious** |
| "hi! hoping to get someone out…" | calm | neutral | **neutral** |
| *(silence)* | **calm** | neutral | **neutral** |

¹ not in the vocabulary — `setVisualEmotion` rejects it and nothing happens.

Constrained decoding fixed enum-safety completely (0/7 off-vocabulary for both
models). It did not fix usefulness. `270m` is fast but inert — it answered
`calm` to a flooding basement and `calm` to an empty string. `1b` respects the
vocabulary and catches genuine arousal, but returns `neutral` for most things.

**Five hand-written rules scored 7/7 where `gemma3:1b` scored 3/7, at ~0ms, with
no ollama dependency and nothing to keep running.**

### The honest caveat

Those seven cases and those five rules were written by the same author in the
same sitting, so 7/7 measures self-consistency, not accuracy. The metrics
database held no stored caller utterances to score against. **Before trusting
the rule set, pull real transcripts from the `/calls` panel and re-score it.**
Expect to rewrite half of them.

What the measurement *does* establish is narrower and still useful: a small
local model is not obviously better than rules here, and it costs a dependency,
a running service, and 400-500ms. That is the finding worth not re-discovering.

## Two renderers, one vocabulary

The mascot ships its own adapter, `vendor/mascot/nano-claw-adapter.js`, written
against this console's *actual* renderer calls — its method list and per-method
usage counts were derived from the source, and match a grep of `app.js` exactly:

    pulse 10, setColors 5, importProfile 4, setSpeaking 2, setPattern 2,
    disconnectAnalyser 2, connectAnalyser 2, setPanelOpen 1, pushAudioFrame 1,
    getProfile 1, destroy 1, configure 1

Two of those are deliberate no-ops that return `true`: `setColors` and
`setPattern` mean nothing to monochrome line art, and throwing would break a
host entitled to call them.

Swapping is handled by `switchRenderer()` in `app.js`. Three things have to
survive it or the swap looks broken, and all three are easy to forget:

1. **The live TTS analyser** — reconnect it, or the new renderer never reacts to
   audio.
2. **Current emotion and presence** — re-apply them, or it starts blank
   mid-conversation.
3. **The outgoing renderer's teardown** — the mascot documents a *measured* leak
   of 14 writes plus a stray animation loop per unmount when `destroy()` is
   skipped.

The new renderer is built **before** the old one is torn down, so a failed mount
leaves a working display rather than an empty stage.

### Drift between the repos is the real risk

The adapter is verified against *today's* `app.js`. Add a renderer call here and
the mascot breaks silently — the call lands on `undefined` with no error at the
call site.

`tests/mascot-renderer-contract.test.ts` is the guard. It runs the adapter's own
`rendererGaps()` and `coverageGaps()`, and checks both repos still agree on the
eleven emotion names. **A failure there is a genuine divergence, not a flaky
test.** See `voice/web/vendor/mascot/README.md` for the re-sync procedure.

## Adding a rule

One line in `voice/web/emotion-layer.js`, in `INBOUND_RULES` or `EMOTION_RULES`
depending on which side of the conversation you mean:

```js
{ emotion: "urgent", intensity: 0.85, re: /\b(right now|flooding|emergency)\b/i },
```

Rules are ordered and **first match wins**, so put arousal above the generic
`/\?\s*$/ → curious` rule or every urgent question reads as merely curious.

Two ways to get this wrong, both silent:

- **Emitting a name that is not in `EMOTION_PROFILES`.** `setVisualEmotion`
  returns `false` and does nothing. No error, no log — the display simply stops
  responding to that rule. The test suite asserts every emitted name exists.
- **Routing caller text through `inferEmotion()`** (or agent text through
  `inferInboundEmotion()`). See *Why the rule sets are separate* — this
  produces confidently wrong expressions rather than no expression.

Also worth knowing: an inbound `neutral` deliberately does **not** clear a live
emotion. Most utterances match no rule, and letting every unremarkable sentence
flatten the display makes it less alive, not more.

## Extension points — not built

Written down because they were considered and deliberately deferred.

**Repolling while the caller speaks.** Streaming STT (task 088) commits a
growing transcript prefix during the utterance, and the chat model is idle in
that window, so classification there is genuinely free. It is gated behind
`NANO_CLAW_PHONE_STT_STREAM`, which ships dark, and adding load beside Whisper
risks the STT latency that task 088 existed to fix. At ~0ms per regex the cost
argument for staging disappears — revisit only if the rules prove too coarse.

**Agent-chosen emotion.** See *Why not a model*. Reconsider only for large
models, and only once the capability platform in
`docs/design/2026-07-28-agent-self-knowledge/design.md` clears its review.

**Barge-in sensitivity and adaptive state.** These live only in the browser
(`barge-in.js`); the server never learns them. Reporting them would mean
rendering a client-supplied value into the system prompt — the injection channel
task 094 closed. They are the two left-panel controls the agent still cannot
describe.

**A presence signal during delegate waits.** Delegated turns can take 7-11
seconds with nothing to show, and the display is the only thing on screen while
that happens. The vocabulary already has `thinking`.

**Embedding the display elsewhere.** `TalkingCubeRenderer` is a
dependency-free ES module over a 2D canvas and `emotion-layer.js` is pure, so
both travel well. The console-specific glue in `app.js` does not.

## Where things live

| File | What |
|---|---|
| `voice/web/emotion-layer.js` | Both rule sets, `EMOTION_PROFILES`, `PRESENCE_PROFILES` |
| `voice/web/app.js` | Emotion state, `switchRenderer()`, the two call sites |
| `voice/web/talking-cube.js` | The particle renderer |
| `voice/web/mascot-renderer.js` | Mounts the mascot; resolves its assets |
| `voice/web/vendor/mascot/` | The vendored character (see its README) |
| `tests/inbound-emotion.test.ts` | Rules + the somber misfire guard |
| `tests/mascot-renderer-contract.test.ts` | Cross-repo drift guard |
| `tests/voice-ui.test.mjs` | Console markup pins — run with `npm run test:web` |

`npm run test:all` runs both suites. `npm test` alone is vitest and will not
catch markup regressions.
