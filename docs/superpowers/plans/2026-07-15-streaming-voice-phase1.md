# Streaming Voice Replies — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream Claude's reply from the LLM through the API to the voice server so audio (and the on-screen chat text) begins at the first sentence, while Claude is still writing the rest.

**Architecture:** Add a streaming path alongside today's request/response: `AnthropicProvider.completeStream()` (native `/messages` with `stream:true`) → `stepLoopStream()` in `server.ts` yields text deltas + a terminal `tool_pending`/`final` → `/api/chat` emits **SSE** when the caller sends `Accept: text/event-stream` → the Python voice server reads the SSE stream, runs deltas through a `TextChunker`, and synthesizes each speakable chunk with the existing TTS/queue path while forwarding the text to the browser. A feature flag and a base-class fallback keep every non-streaming provider and caller working unchanged.

**Tech Stack:** TypeScript (Node http, axios streaming), Python 3.12 (aiohttp, httpx streaming), vanilla JS, vitest (TS tests), pytest (Python tests), `node <file>` (JS tests).

## Global Constraints

- Deployed provider is **Anthropic native** (`/messages`, model `anthropic/claude-haiku-4-5`). Implement real streaming for `AnthropicProvider`; the base class provides a non-streaming fallback for all other providers.
- Transport between API and voice server: **SSE** (`text/event-stream`). SSE frames: `event: <name>\n` + `data: <json>\n\n`. Event names: `delta`, `tool_pending`, `final`, `error`.
- **First speakable chunk** flushes after `FIRST_CHUNK_WORDS = 6` words even without a sentence boundary; every later chunk flushes only on sentence-ending punctuation (`. ! ?`). Markdown is stripped before speaking (reuse the existing cleaning rules).
- Feature flag `NANO_CLAW_STREAM` (default **on**; `"0"`/`"false"` forces the legacy path). The voice server auto-detects a non-SSE (JSON) response and speaks it whole.
- Reuse the existing per-chunk synthesis: `voice/tts.py::synthesize(text, voice_id, speed)` → 48kHz PCM → existing `AudioQueue`. Do not change the WebRTC/audio path.
- The browser shows streamed text via a new `agent_reply_delta {text}` WebSocket message; audio behavior is unchanged (it already drains the queue).
- Tool calls mid-stream end the turn as `tool_pending` (existing approve/reject flow); barge-in is Phase 2 and out of scope here.

## File Structure

**TypeScript**
- `src/types.ts` — add the `StreamEvent` union type.
- `src/providers/base.ts` — `BaseProvider.completeStream()` default fallback; `AnthropicProvider.completeStream()` real streaming; a shared SSE line-reader helper.
- `src/providers/index.ts` — `ProviderManager.completeStream()` routing.
- `src/api/server.ts` — `stepLoopStream()` + SSE writing in `handleChat`/`handleApprove`/`handleReject`.

**Python**
- `voice/text_chunker.py` — new pure `TextChunker`.
- `voice/webrtc.py` — refactor `speak_text` to expose `enqueue_chunk(text, voice_id, speed)` (synth one chunk → queue) + a `drain(timeout)` helper.
- `voice/server.py` — consume the SSE stream, feed the chunker, emit `agent_reply_delta`.

**JS**
- `voice/web/app.js` — handle `agent_reply_delta`.

**Tests**
- `tests/streaming.test.ts` (vitest) — `StreamEvent`, SSE parsing, `completeStream` fallback, `stepLoopStream`.
- `tests/python/test_text_chunker.py` — chunker behavior.
- `tests/voice-delta.test.mjs` — (optional JS DOM helper; folded into Task 6 if a pure helper is extracted).

---

## Task 1: StreamEvent type + provider streaming fallback + routing

**Files:**
- Modify: `src/types.ts` (after the `LLMResponse` interface, ~line 137)
- Modify: `src/providers/base.ts` (add method to `BaseProvider`, ~line 41)
- Modify: `src/providers/index.ts` (add method to `ProviderManager`, near `complete`, ~line 148-163)
- Test: `tests/streaming.test.ts`

**Interfaces:**
- Produces: `StreamEvent` union; `BaseProvider.completeStream(messages, model, temperature?, maxTokens?, tools?): AsyncGenerator<StreamEvent>`; `ProviderManager.completeStream(...)` with the same signature as `complete` but returning `AsyncGenerator<StreamEvent>`.

- [ ] **Step 1: Write the failing test for the fallback generator**

Create `tests/streaming.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { BaseProvider } from '../src/providers/base';
import type { Message, LLMResponse, ToolDefinition } from '../src/types';

class FakeProvider extends BaseProvider {
  protected getDefaultApiBase(): string { return 'http://example.invalid'; }
  async complete(): Promise<LLMResponse> {
    return { content: 'Hello world.', finishReason: 'stop' };
  }
}

async function collect<T>(gen: AsyncGenerator<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

describe('BaseProvider.completeStream fallback', () => {
  it('yields the full content as one text event then done', async () => {
    const p = new FakeProvider('key');
    const events = await collect(p.completeStream([], 'm'));
    expect(events).toEqual([
      { type: 'text', delta: 'Hello world.' },
      { type: 'done', finishReason: 'stop', usage: undefined },
    ]);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run tests/streaming.test.ts`
Expected: FAIL — `completeStream` does not exist / `StreamEvent` not exported.

- [ ] **Step 3: Add the `StreamEvent` type**

In `src/types.ts`, immediately after the `LLMResponse` interface (line ~137), add:

```ts
/**
 * One event in a streamed LLM completion.
 */
export type StreamEvent =
  | { type: 'text'; delta: string }
  | { type: 'tool_calls'; toolCalls: ToolCall[] }
  | {
      type: 'done';
      finishReason?: string;
      usage?: { promptTokens: number; completionTokens: number; totalTokens: number };
    };
```

- [ ] **Step 4: Add the fallback generator to `BaseProvider`**

In `src/providers/base.ts`, update the import on line 2 to include `StreamEvent`:

```ts
import { Message, LLMResponse, ToolDefinition, ToolCall, StreamEvent } from '../types';
```

Then inside `class BaseProvider`, right after the abstract `complete(...)` declaration (line ~41), add a concrete default:

```ts
  /**
   * Stream a completion. Default: call complete() once and yield it whole.
   * Providers with native streaming override this.
   */
  async *completeStream(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const res = await this.complete(messages, model, temperature, maxTokens, tools);
    if (res.content) yield { type: 'text', delta: res.content };
    if (res.toolCalls && res.toolCalls.length > 0) {
      yield { type: 'tool_calls', toolCalls: res.toolCalls };
    }
    yield {
      type: 'done',
      finishReason: res.finishReason,
      usage: res.usage,
    };
  }
```

- [ ] **Step 5: Add routing to `ProviderManager`**

In `src/providers/index.ts`, update the import on line 2 to add `StreamEvent`:

```ts
import { Message, LLMResponse, ToolDefinition, ProviderConfig, StreamEvent } from '../types';
```

Immediately after the `complete(...)` method (which ends at line ~164 with `return provider.complete(...)`), add:

```ts
  /**
   * Streaming variant of complete() — routes to the model's provider.
   */
  async *completeStream(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const providerSpec = findProviderByModel(model);
    const provider = this.getProviderInstance(providerSpec.name);
    yield* provider.completeStream(messages, model, temperature, maxTokens, tools);
  }
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `npx vitest run tests/streaming.test.ts`
Expected: PASS (1 passed). Then `npm run build` to confirm the types compile.
Run: `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add src/types.ts src/providers/base.ts src/providers/index.ts tests/streaming.test.ts
git commit -m "feat(api): StreamEvent + completeStream fallback and routing"
```

---

## Task 2: Anthropic native streaming (`AnthropicProvider.completeStream`)

**Files:**
- Modify: `src/providers/base.ts` (add a shared SSE reader + `AnthropicProvider.completeStream`)
- Test: `tests/streaming.test.ts` (add cases)

**Interfaces:**
- Consumes: `StreamEvent` (Task 1).
- Produces: `AnthropicProvider.completeStream(...)` streaming text deltas from Anthropic's `/messages` SSE; a module-local `parseSSEStream(stream): AsyncGenerator<{event:string; data:any}>` helper exported for tests.

- [ ] **Step 1: Write the failing test for SSE parsing + Anthropic event mapping**

Add to `tests/streaming.test.ts`:

```ts
import { parseAnthropicEvents } from '../src/providers/base';
import { Readable } from 'node:stream';

function sse(lines: string): Readable {
  return Readable.from([Buffer.from(lines)]);
}

describe('parseAnthropicEvents', () => {
  it('maps text_delta events to text StreamEvents and ends with done', async () => {
    const body =
      'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"there."}}\n\n' +
      'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n' +
      'event: message_stop\ndata: {"type":"message_stop"}\n\n';
    const out: any[] = [];
    for await (const e of parseAnthropicEvents(sse(body))) out.push(e);
    expect(out[0]).toEqual({ type: 'text', delta: 'Hi ' });
    expect(out[1]).toEqual({ type: 'text', delta: 'there.' });
    const done = out[out.length - 1];
    expect(done.type).toBe('done');
    expect(done.finishReason).toBe('end_turn');
    expect(done.usage).toEqual({ promptTokens: 5, completionTokens: 3, totalTokens: 8 });
  });

  it('assembles a tool_use block into a tool_calls event', async () => {
    const body =
      'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"shell","input":{}}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":"}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"ls\\"}"}}\n\n' +
      'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n' +
      'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n\n' +
      'event: message_stop\ndata: {"type":"message_stop"}\n\n';
    const out: any[] = [];
    for await (const e of parseAnthropicEvents(sse(body))) out.push(e);
    const toolEvt = out.find((e) => e.type === 'tool_calls');
    expect(toolEvt).toBeTruthy();
    expect(toolEvt.toolCalls[0]).toEqual({
      id: 't1',
      type: 'function',
      function: { name: 'shell', arguments: '{"cmd":"ls"}' },
    });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/streaming.test.ts`
Expected: FAIL — `parseAnthropicEvents` not exported.

- [ ] **Step 3: Add the SSE reader + Anthropic event parser to `base.ts`**

At the top of `src/providers/base.ts` (after the imports), add a reusable line reader and the Anthropic parser as exported module functions:

```ts
import type { Readable } from 'node:stream';

/**
 * Read a Node Readable SSE body and yield {event, data} frames.
 * Frames are separated by a blank line; `event:` and `data:` lines accumulate.
 */
export async function* readSSEFrames(
  stream: Readable
): AsyncGenerator<{ event: string; data: string }> {
  let buffer = '';
  for await (const chunk of stream) {
    buffer += chunk.toString('utf8');
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = 'message';
      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) yield { event, data: dataLines.join('\n') };
    }
  }
}

/**
 * Parse Anthropic /messages streaming events into StreamEvents.
 */
export async function* parseAnthropicEvents(stream: Readable): AsyncGenerator<StreamEvent> {
  let promptTokens = 0;
  let completionTokens = 0;
  let finishReason: string | undefined;
  // tool_use accumulation, keyed by content block index
  const toolAcc = new Map<number, { id: string; name: string; json: string }>();

  for await (const { data } of readSSEFrames(stream)) {
    if (data === '[DONE]') break;
    let evt: any;
    try {
      evt = JSON.parse(data);
    } catch {
      continue;
    }
    switch (evt.type) {
      case 'message_start':
        promptTokens = evt.message?.usage?.input_tokens ?? 0;
        break;
      case 'content_block_start':
        if (evt.content_block?.type === 'tool_use') {
          toolAcc.set(evt.index, { id: evt.content_block.id, name: evt.content_block.name, json: '' });
        }
        break;
      case 'content_block_delta':
        if (evt.delta?.type === 'text_delta' && evt.delta.text) {
          yield { type: 'text', delta: evt.delta.text };
        } else if (evt.delta?.type === 'input_json_delta') {
          const acc = toolAcc.get(evt.index);
          if (acc) acc.json += evt.delta.partial_json ?? '';
        }
        break;
      case 'message_delta':
        finishReason = evt.delta?.stop_reason ?? finishReason;
        completionTokens = evt.usage?.output_tokens ?? completionTokens;
        break;
      case 'message_stop':
        // handled after the loop
        break;
    }
  }

  if (toolAcc.size > 0) {
    const toolCalls: ToolCall[] = [...toolAcc.values()].map((t) => ({
      id: t.id,
      type: 'function',
      function: { name: t.name, arguments: t.json || '{}' },
    }));
    yield { type: 'tool_calls', toolCalls };
  }
  yield {
    type: 'done',
    finishReason,
    usage: { promptTokens, completionTokens, totalTokens: promptTokens + completionTokens },
  };
}
```

> Note: `ToolCall` and `StreamEvent` are already imported at the top of `base.ts` after Task 1. If your `ToolCall` type's `function.arguments` is typed as `string`, the above matches it.

- [ ] **Step 4: Override `completeStream` in `AnthropicProvider`**

Find `AnthropicProvider` in `src/providers/base.ts` (its `complete` posts to `/messages`, ~line 195). Add a `completeStream` method to that class, reusing its existing request-shaping (system extraction, `formatAnthropicMessages`, tool mapping) but with `stream: true` and `responseType: 'stream'`:

```ts
  async *completeStream(
    messages: Message[],
    model: string,
    temperature = 0.7,
    maxTokens = 4096,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
    const nonSystemMessages = messages.filter((m) => m.role !== 'system');
    const anthropicMessages = this.formatAnthropicMessages(nonSystemMessages);

    const requestData: Record<string, unknown> = {
      model: this.formatModelName(model),
      messages: anthropicMessages,
      temperature,
      max_tokens: maxTokens,
      stream: true,
    };
    if (systemMessage) requestData.system = systemMessage;
    if (tools && tools.length > 0) {
      requestData.tools = tools.map((t) => ({
        name: t.function.name,
        description: t.function.description,
        input_schema: t.function.parameters,
      }));
    }

    const response = await this.client.post('/messages', requestData, {
      responseType: 'stream',
      headers: { 'anthropic-version': '2023-06-01', 'x-api-key': this.apiKey },
    });
    yield* parseAnthropicEvents(response.data as Readable);
  }
```

- [ ] **Step 5: Run to verify it passes**

Run: `npx vitest run tests/streaming.test.ts`
Expected: PASS (all cases, including the two new `parseAnthropicEvents` cases).
Run: `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add src/providers/base.ts tests/streaming.test.ts
git commit -m "feat(api): Anthropic native streaming (parseAnthropicEvents + completeStream)"
```

---

## Task 3: `stepLoopStream` + SSE endpoints in `server.ts`

**Files:**
- Modify: `src/api/server.ts` (add `stepLoopStream`; branch `handleChat`/`handleApprove`/`handleReject` on `Accept`)
- Test: `tests/streaming.test.ts` (add a `stepLoopStream` case with a fake provider)

**Interfaces:**
- Consumes: `providerManager.completeStream` (Task 1/2); the existing `pendingRequests`, `createToolRegistry`, `ContextBuilder`, `Memory`.
- Produces: `stepLoopStream(memory, agentConfig, iteration): AsyncGenerator<StreamEvent | {type:'tool_pending'|'final', ...}>` and SSE responses on `/api/chat`, `/api/chat/approve`, `/api/chat/reject` when `Accept: text/event-stream`.

- [ ] **Step 1: Write the failing test for `stepLoopStream`**

Add to `tests/streaming.test.ts` (this test injects a fake provider by monkeypatching the module's `providerManager`; export a small hook for it):

```ts
import { __setProviderManagerForTest, stepLoopStream } from '../src/api/server';
import { Memory } from '../src/agent/memory';

describe('stepLoopStream', () => {
  it('forwards text deltas then a final event when there are no tool calls', async () => {
    __setProviderManagerForTest({
      async *completeStream() {
        yield { type: 'text', delta: 'Part one. ' };
        yield { type: 'text', delta: 'Part two.' };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);

    const mem = new Memory('test-stream');
    mem.addMessage({ role: 'user', content: 'hi' });
    const events: any[] = [];
    for await (const e of stepLoopStream(mem, { model: 'anthropic/x', temperature: 0.7, maxTokens: 100 } as any, 0)) {
      events.push(e);
    }
    const texts = events.filter((e) => e.type === 'text').map((e) => e.delta).join('');
    expect(texts).toBe('Part one. Part two.');
    const final = events.find((e) => e.type === 'final');
    expect(final.response).toBe('Part one. Part two.');
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/streaming.test.ts`
Expected: FAIL — `stepLoopStream` / `__setProviderManagerForTest` not exported.

- [ ] **Step 3: Add a test hook for the provider manager**

In `src/api/server.ts`, find where `providerManager` is created (a module-level `const providerManager = new ProviderManager(config)` near the top). Change it to a reassignable binding and add a test setter:

```ts
let providerManager = new ProviderManager(config);

/** Test-only: inject a stub provider manager. */
export function __setProviderManagerForTest(pm: unknown): void {
  providerManager = pm as ProviderManager;
}
```

- [ ] **Step 4: Add `stepLoopStream`**

In `src/api/server.ts`, add after `stepLoop` (line ~201). It mirrors `stepLoop` but consumes `completeStream`, forwarding `text` events and assembling the final/tool_pending terminal event:

```ts
export async function* stepLoopStream(
  memory: Memory,
  agentConfig: AgentConfig,
  iteration: number
): AsyncGenerator<StreamEvent | ApiResponse> {
  const toolRegistry = createToolRegistry();

  while (iteration < MAX_ITERATIONS) {
    iteration++;
    const messageCount = memory.getMessages().length;
    const startTime = Date.now();
    const skills = skillsLoader.getSkills();
    const tools = toolRegistry.getDefinitions();
    const contextBuilder = new ContextBuilder(agentConfig);
    const contextMessages = contextBuilder.buildContextMessages(memory.getMessages(), skills, tools);

    let text = '';
    let toolCalls: ToolCall[] | undefined;
    let finishReason: string | undefined;
    let usage: LLMResponse['usage'];

    for await (const ev of providerManager.completeStream(
      contextMessages, agentConfig.model, agentConfig.temperature, agentConfig.maxTokens, tools
    )) {
      if (ev.type === 'text') {
        text += ev.delta;
        yield ev; // forward the delta to the SSE writer
      } else if (ev.type === 'tool_calls') {
        toolCalls = ev.toolCalls;
      } else if (ev.type === 'done') {
        finishReason = ev.finishReason;
        usage = ev.usage;
      }
    }

    const debug: DebugInfo = {
      iteration,
      messageCount,
      model: agentConfig.model,
      tokenUsage: usage
        ? { prompt: usage.promptTokens, completion: usage.completionTokens, total: usage.totalTokens }
        : undefined,
      durationMs: Date.now() - startTime,
      finishReason,
    };

    if (toolCalls && toolCalls.length > 0) {
      memory.addMessage({ role: 'assistant', content: text, tool_calls: toolCalls });
      const requestId = crypto.randomUUID();
      pendingRequests.set(requestId, { memory, toolCalls, assistantContent: text, iteration, agentConfig });
      pendingTimestamps.set(requestId, Date.now());
      yield {
        type: 'tool_pending',
        requestId,
        tools: toolCalls.map((tc) => ({ name: tc.function.name, args: safeParseToolArgs(tc.function.arguments) })),
        debug,
      };
      return;
    }

    memory.addMessage({ role: 'assistant', content: text });
    yield { type: 'final', response: text, debug };
    return;
  }

  yield { type: 'final', response: 'Max iterations reached.', debug: { iteration: MAX_ITERATIONS, messageCount: memory.getMessages().length, model: agentConfig.model, durationMs: 0, finishReason: 'max_iterations' } };
}
```

Add `StreamEvent` and `LLMResponse` to the `../types` import at the top of `server.ts` (line 13 currently imports `AgentConfig, ToolCall`):

```ts
import { AgentConfig, ToolCall, StreamEvent, LLMResponse } from '../types';
```

- [ ] **Step 5: Run to verify the unit test passes**

Run: `npx vitest run tests/streaming.test.ts`
Expected: PASS (the `stepLoopStream` case included).

- [ ] **Step 6: Add SSE writing to the HTTP handlers**

In `src/api/server.ts`, add an SSE helper near `sendJson` (~line 224):

```ts
const STREAM_ENABLED = process.env.NANO_CLAW_STREAM !== '0' && process.env.NANO_CLAW_STREAM !== 'false';

function wantsStream(req: http.IncomingMessage): boolean {
  return STREAM_ENABLED && (req.headers['accept'] || '').includes('text/event-stream');
}

function sseWrite(res: http.ServerResponse, event: string, data: unknown): void {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

async function streamLoopToSSE(
  res: http.ServerResponse,
  gen: AsyncGenerator<StreamEvent | ApiResponse>
): Promise<void> {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  });
  try {
    for await (const ev of gen) {
      if ((ev as StreamEvent).type === 'text') sseWrite(res, 'delta', { text: (ev as { delta: string }).delta });
      else if ((ev as ApiResponse).type === 'tool_pending') sseWrite(res, 'tool_pending', ev);
      else if ((ev as ApiResponse).type === 'final') sseWrite(res, 'final', ev);
    }
  } catch (err) {
    sseWrite(res, 'error', { error: err instanceof Error ? err.message : 'stream error' });
  } finally {
    res.end();
  }
}
```

Then branch `handleChat` (line ~268). Replace its final two lines (`const result = await stepLoop(...)` / `sendJson(...)`) with:

```ts
  if (wantsStream(req)) {
    await streamLoopToSSE(res, stepLoopStream(memory, getAgentConfig(), 0));
    return;
  }
  const result = await stepLoop(memory, getAgentConfig(), 0);
  sendJson(res, 200, result);
```

Apply the same branch to `handleApprove` and `handleReject` — replace their trailing `const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration); sendJson(res, 200, result);` with:

```ts
  if (wantsStream(req)) {
    await streamLoopToSSE(res, stepLoopStream(pending.memory, pending.agentConfig, pending.iteration));
    return;
  }
  const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration);
  sendJson(res, 200, result);
```

- [ ] **Step 7: Build and verify types**

Run: `npm run build`
Expected: no TypeScript errors.

- [ ] **Step 8: Commit**

```bash
git add src/api/server.ts tests/streaming.test.ts
git commit -m "feat(api): stepLoopStream + SSE on /api/chat when Accept: text/event-stream"
```

---

## Task 4: `TextChunker` (Python, pure)

**Files:**
- Create: `voice/text_chunker.py`
- Test: `tests/python/test_text_chunker.py`

**Interfaces:**
- Produces: `class TextChunker` with `push(delta: str) -> list[str]` (returns any newly-complete speakable chunks, markdown-stripped) and `flush() -> str` (the trailing remainder, stripped; empty string if none). Module constant `FIRST_CHUNK_WORDS = 6`.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_text_chunker.py`:

```python
from voice.text_chunker import TextChunker


def test_first_chunk_flushes_after_six_words_without_a_boundary():
    c = TextChunker()
    out = []
    for word in "one two three four five six seven".split():
        out += c.push(word + " ")
    # First chunk emitted once >=6 words accumulate, even mid-sentence.
    assert out, "expected an early first chunk"
    assert len(out[0].split()) >= 6


def test_later_chunks_only_on_sentence_boundary():
    c = TextChunker()
    c.push("This is the first sentence that is quite long already. ")
    # consume whatever the first-chunk rule emitted
    c2 = TextChunker()
    got = c2.push("Short one. And another one! Third?")
    joined = " ".join(got)
    assert "Short one." in joined
    assert "And another one!" in joined
    # "Third?" has no trailing space/flush yet unless boundary seen; it ends with ?
    assert "Third?" in joined


def test_markdown_is_stripped():
    c = TextChunker()
    got = c.push("**Bold** and `code` and a [link](http://x). ")
    joined = " ".join(got) + c.flush()
    assert "*" not in joined
    assert "`" not in joined
    assert "http" not in joined


def test_flush_returns_remainder():
    c = TextChunker()
    c.push("First sentence here now please. ")  # emits first chunk
    c.push("Trailing without terminator")
    assert c.flush().strip() == "Trailing without terminator"
    assert c.flush() == ""  # nothing left after flush
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/pytest tests/python/test_text_chunker.py -v`
Expected: FAIL — `voice/text_chunker.py` does not exist.

- [ ] **Step 3: Create `voice/text_chunker.py`**

```python
"""Turn a stream of text deltas into speakable chunks for incremental TTS.

Rules:
- The FIRST chunk of a reply flushes as soon as FIRST_CHUNK_WORDS words have
  accumulated, even without a sentence boundary — so audio starts fast.
- Every later chunk flushes only on sentence-ending punctuation (. ! ?).
- Markdown is stripped so TTS reads clean prose.
"""

from __future__ import annotations

import re

FIRST_CHUNK_WORDS = 6

_SENTENCE_END = re.compile(r".*?[.!?](?:\s|$)", re.DOTALL)


def _clean(text: str) -> str:
    """Strip markdown formatting (shared intent with webrtc._clean_for_speech)."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


class TextChunker:
    def __init__(self) -> None:
        self._buf = ""
        self._first_done = False

    def push(self, delta: str) -> list[str]:
        """Add a delta; return any speakable chunks now complete."""
        self._buf += delta
        chunks: list[str] = []

        # Flush all complete sentences.
        while True:
            m = _SENTENCE_END.match(self._buf)
            if not m:
                break
            raw = m.group(0)
            self._buf = self._buf[m.end():]
            cleaned = _clean(raw)
            if cleaned:
                chunks.append(cleaned)
                self._first_done = True

        # Eager first chunk: if nothing spoken yet and enough words piled up.
        if not self._first_done and len(self._buf.split()) >= FIRST_CHUNK_WORDS:
            cleaned = _clean(self._buf)
            self._buf = ""
            if cleaned:
                chunks.append(cleaned)
                self._first_done = True

        return chunks

    def flush(self) -> str:
        """Return and clear the trailing remainder."""
        cleaned = _clean(self._buf)
        self._buf = ""
        if cleaned:
            self._first_done = True
        return cleaned
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv-test/bin/pytest tests/python/test_text_chunker.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add voice/text_chunker.py tests/python/test_text_chunker.py
git commit -m "feat(voice): TextChunker — eager first chunk, sentence chunks after"
```

---

## Task 5: Voice server SSE consumption + incremental speak

**Files:**
- Modify: `voice/webrtc.py` (extract `enqueue_chunk`; add `speak_stream` producer/consumer)
- Modify: `voice/server.py` (stream from nano-claw; feed chunker; emit `agent_reply_delta`)
- Test: manual/integration (no new unit test — exercised end-to-end in Task 6)

**Interfaces:**
- Consumes: `voice.text_chunker.TextChunker` (Task 4); the SSE endpoint (Task 3); `voice.tts.synthesize`.
- Produces: `Session.enqueue_chunk(text, voice_id, speed)` (synth one chunk → 48k PCM → `AudioQueue`); `Session.begin_stream()` / `Session.end_stream(timeout)` (set the generator + drain); `voice/server.py` streaming request path.

- [ ] **Step 1: Refactor `speak_text` in `voice/webrtc.py` into reusable pieces**

Replace the body of `speak_text` (lines ~162-192) so the per-chunk synth is a public method and draining is separate. Keep `speak_text` working (whole-text path) by delegating:

```python
    def begin_stream(self) -> None:
        """Attach the TTS generator so enqueued chunks start playing immediately."""
        self._audio_source.set_generator(self._tts_generator)

    def enqueue_chunk(self, text: str, voice_id: str = "", speed: float = 1.0) -> int:
        """Synthesize one already-clean chunk and enqueue it. Returns bytes queued."""
        from voice.tts import synthesize
        pcm_48k = synthesize(text, voice_id, speed)
        if pcm_48k:
            self._audio_queue.enqueue(pcm_48k)
        return len(pcm_48k)

    async def end_stream(self, total_bytes: int) -> None:
        """Wait for the queue to drain, then detach the generator (mirrors speak_text tail)."""
        loop = asyncio.get_running_loop()
        playback_seconds = total_bytes / (SAMPLE_RATE * 2)
        deadline = loop.time() + max(5.0, min(120.0, playback_seconds + 5.0))
        while self._audio_queue.available and loop.time() < deadline and not self._closed:
            await asyncio.sleep(0.02)
        if self._audio_queue.available:
            log.warning("TTS playback drain timed out with %d bytes queued", self._audio_queue.available)
            self._audio_queue.clear()
        await asyncio.sleep(0.15)
        self._audio_source.clear_generator()

    async def speak_text(self, text: str, voice_id: str = "", speed: float = 1.0):
        """Whole-text path (non-streaming fallback): clean, split, enqueue, drain."""
        self.begin_stream()
        text = self._clean_for_speech(text)
        sentences = self._split_sentences(text)
        loop = asyncio.get_running_loop()
        total_bytes = 0
        for sentence in sentences:
            total_bytes += await loop.run_in_executor(None, self.enqueue_chunk, sentence, voice_id, speed)
        await self.end_stream(total_bytes)
```

> `enqueue_chunk` expects already-clean text; the streaming path (server.py) cleans via `TextChunker`, and `speak_text` cleans via `_clean_for_speech` before splitting, so double-cleaning is avoided.

- [ ] **Step 2: Add the streaming request path in `voice/server.py`**

Add imports near the top:

```python
from voice.text_chunker import TextChunker
```

Replace `_handle_agent_request` (lines ~122-139) with a streaming version that falls back to the old JSON path when the response isn't SSE:

```python
async def _handle_agent_request(ws, session, client, text):
    """Stream nano-claw's reply as SSE; synthesize + forward chunks as they arrive."""
    try:
        async with client.stream(
            "POST",
            f"{NANO_CLAW_URL}/api/chat",
            json={"message": text, "sessionId": SESSION_ID},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                data = json.loads(await resp.aread())
                await _process_api_response(ws, session, data)
                return
            await _consume_sse(ws, session, resp)
    except Exception:
        log.exception("nano-claw streaming call failed")
        error_text = "Sorry, I couldn't reach the agent."
        await ws.send_json({"type": "agent_reply", "text": error_text})
        await _speak_with_events(ws, session, error_text)


async def _consume_sse(ws, session, resp):
    """Parse SSE frames, speaking each chunk and forwarding text to the browser."""
    chunker = TextChunker()
    loop = asyncio.get_running_loop()
    total_bytes = 0
    spoke_any = False
    event = ""
    data_lines: list[str] = []

    async def speak_chunk(chunk: str):
        nonlocal total_bytes
        await ws.send_json({"type": "agent_reply_delta", "text": chunk})
        total_bytes += await loop.run_in_executor(
            None, session.enqueue_chunk, chunk, session.voice_id, session.speed
        )

    session.begin_stream()
    await ws.send_json({"type": "agent_audio_start"})

    async for raw in resp.aiter_lines():
        if raw == "":  # frame boundary
            payload = "\n".join(data_lines)
            data_lines = []
            ev, event = event, ""
            if not payload:
                continue
            obj = json.loads(payload)
            if ev == "delta":
                for chunk in chunker.push(obj.get("text", "")):
                    spoke_any = True
                    await speak_chunk(chunk)
            elif ev == "tool_pending":
                await ws.send_json({"type": "tool_pending", "requestId": obj["requestId"], "tools": obj["tools"]})
                await ws.send_json({"type": "agent_audio_end"})
                return
            elif ev == "final":
                tail = chunker.flush()
                if tail:
                    spoke_any = True
                    await speak_chunk(tail)
                if obj.get("debug"):
                    await ws.send_json({"type": "debug", **obj["debug"]})
                await ws.send_json({"type": "agent_reply_done"})
            elif ev == "error":
                await ws.send_json({"type": "agent_reply", "text": "Error from agent."})
            continue
        if raw.startswith("event:"):
            event = raw[6:].strip()
        elif raw.startswith("data:"):
            data_lines.append(raw[5:].strip())

    await session.end_stream(total_bytes)
    if not session._closed:
        await ws.send_json({"type": "agent_audio_end"})
```

> This reuses the session's selected `voice_id`/`speed` (from the Phase-1 Kokoro feature) and the existing `AudioQueue`/WebRTC path. The `agent_audio_start`/`agent_audio_end` gating that the browser already understands is preserved.

- [ ] **Step 3: Verify Python imports still parse**

Run: `python3 -m py_compile voice/webrtc.py voice/server.py`
Expected: no output (success). (`aiohttp`/`aiortc`/`httpx` need not be importable to py_compile-check syntax.)
Run: `.venv-test/bin/pytest tests/python -v`
Expected: all existing + Task 4 tests pass (no regressions; server/webrtc have no unit tests).

- [ ] **Step 4: Commit**

```bash
git add voice/webrtc.py voice/server.py
git commit -m "feat(voice): consume SSE from nano-claw, speak chunks as they stream"
```

---

## Task 6: Browser incremental text + integration verification + docs

**Files:**
- Modify: `voice/web/app.js` (handle `agent_reply_delta` / `agent_reply_done`)
- Modify: `README.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: WS messages `agent_reply_delta {text}`, `agent_reply_done` (Task 5).

- [ ] **Step 1: Handle streamed text in `app.js`**

In `voice/web/app.js`, the `handleMessage` switch (line ~382) currently has a `case "agent_reply"` that calls `addBubble(msg.text, "agent")`. Add streaming cases that build the bubble incrementally. Add near the other `case` blocks:

```javascript
        case "agent_reply_delta":
            clearThinking();
            appendAgentDelta(msg.text);
            setAgentSpeaking(true);
            setPhoneStatus("Claude is speaking to the phone...");
            break;

        case "agent_reply_done":
            finalizeAgentBubble();
            break;
```

Add the helper functions near `addBubble` (define a module-level `var streamingBubble = null;`):

```javascript
var streamingBubble = null;

function appendAgentDelta(text) {
    if (!streamingBubble) {
        streamingBubble = addBubble("", "agent");
    }
    // addBubble returns the bubble element; append text with a leading space if needed
    streamingBubble.textContent = (streamingBubble.textContent + " " + text).trim();
    chatLog.scrollTop = chatLog.scrollHeight;
}

function finalizeAgentBubble() {
    streamingBubble = null;
}
```

> If `addBubble` does not currently return the created element, update it to `return bubble;` (its last statement creates and appends a bubble element — return that node). Keep all existing callers working (they can ignore the return value).

- [ ] **Step 2: Confirm `addBubble` returns its element**

Read `addBubble` in `app.js`. If it ends by appending a `div` (e.g. `chatLog.appendChild(bubble)`), add `return bubble;` as its final line. Verify no existing caller relies on it returning `undefined` (they use it for side effects only).

Run: `node -e "require('fs').readFileSync('voice/web/app.js','utf8')"` (sanity read) and `node --check voice/web/app.js`
Expected: `node --check` prints nothing (valid syntax).

- [ ] **Step 3: Full streaming integration verification (services running)**

With the native TTS service (port 8300), STT (8200), and the container (9090) running (rebuild the image so the new voice server + web assets are in it, and restart the nano-claw API build so `dist/` has the streaming code):

```bash
# rebuild TS + image so streaming code is live
npm run build
docker build -t nano-claw-voice .
docker rm -f nano-claw-voice 2>/dev/null
set -a; source .env; set +a
docker run -d --rm --name nano-claw-voice -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" \
  -e TTS_SERVICE_URL="http://host.docker.internal:8300" \
  -v nano-claw-models:/app/voice/models nano-claw-voice

# confirm the API streams SSE (deltas arrive before the reply finishes)
curl -N -s -X POST localhost:9090/api/chat \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -d '{"message":"In three short sentences, describe the ocean.","sessionId":"t"}' | head -20
```

Expected: multiple `event: delta` frames stream in over time, then `event: final`. In the browser at `http://localhost:9090`, the chat bubble fills sentence-by-sentence and audio starts on the first sentence (noticeably sooner than before on a multi-sentence reply). Confirm a tool request still shows the approval card (streaming ends at `event: tool_pending`).

- [ ] **Step 4: Verify the non-streaming fallback**

```bash
# force legacy path
docker rm -f nano-claw-voice 2>/dev/null
set -a; source .env; set +a
docker run -d --rm --name nano-claw-voice -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" -e NANO_CLAW_STREAM=0 \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" \
  -e TTS_SERVICE_URL="http://host.docker.internal:8300" \
  -v nano-claw-models:/app/voice/models nano-claw-voice
curl -s -o /dev/null -w "%{content_type}\n" -X POST localhost:9090/api/chat \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -d '{"message":"hi","sessionId":"t2"}'
```

Expected: `application/json` (not `text/event-stream`) — the voice server's `_handle_agent_request` detects JSON and speaks the whole reply as before. The app still works end-to-end.

- [ ] **Step 5: Update docs**

- `README.md`: in the data-flow section, note that Claude's reply now **streams** — the voice server reads SSE from `/api/chat` and synthesizes each sentence as it arrives (first audio at the first sentence), with `NANO_CLAW_STREAM=0` to force the legacy whole-reply path.
- `CHANGELOG.md`: add under `### Added`: "Streaming voice replies — Claude's answer is spoken sentence-by-sentence as it's generated (Anthropic native streaming → SSE → incremental TTS), so audio starts at the first sentence. Text also streams into the chat log. `NANO_CLAW_STREAM=0` forces the legacy path."

- [ ] **Step 6: Commit**

```bash
git add voice/web/app.js README.md CHANGELOG.md
git commit -m "feat(web): stream reply text into the chat log; docs for streaming"
```

---

## Self-Review (completed during authoring)

**Spec coverage (Phase 1 portion of the design):**
- Provider streaming (Anthropic native) → Tasks 1-2. ✓
- API SSE + stepLoopStream + tool_pending handling → Task 3. ✓
- First-chunk-after-6-words + sentence chunks + markdown strip → Task 4 (`TextChunker`). ✓
- Voice server SSE consumption + incremental synth reusing existing queue → Task 5. ✓
- Text streams into chat log (`agent_reply_delta`) → Task 6. ✓
- Feature flag `NANO_CLAW_STREAM` + non-SSE fallback → Task 3 (flag), Task 5 (voice-server JSON detection), Task 6 Step 4 (verified). ✓
- Kokoro/Piper per-chunk fallback still applies → Task 5 reuses `synthesize` unchanged. ✓

**Placeholder scan:** every code step shows complete code; every test step shows assertions + the exact run command. No TBD/TODO. ✓

**Type consistency:** `StreamEvent` union defined in Task 1 and consumed identically in Tasks 2-3; `completeStream` signature matches across base/provider/manager; `enqueue_chunk(text, voice_id, speed)` defined in Task 5 and called with the session's `voice_id`/`speed`; SSE event names (`delta`/`tool_pending`/`final`/`error`) written in Task 3 and parsed in Task 5. ✓

## Out of scope (Phase 2 / other)
- Barge-in (pause/confirm/resume + backoff) — separate Phase 2 plan.
- OpenAI-compatible provider streaming — base-class fallback covers them (non-streamed); add later if a non-Anthropic provider is deployed.
- Word-level streaming beyond the eager first chunk.
