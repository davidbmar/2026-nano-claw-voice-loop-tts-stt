# Pipeline Settings (switch STT / LLM / TTS) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One ⚙ Pipeline panel that switches each stage of the voice loop live — STT (Whisper size), LLM (curated, availability-aware, streaming for every provider), and TTS (the existing voice picker) — with no restart.

**Architecture:** Add OpenAI-compatible SSE streaming to the shared `OpenAIProvider` so Gemini/DeepSeek/Groq/OpenAI/Alibaba stream like Anthropic. Expose a curated model catalog with per-model availability at `GET /api/models`. Thread a per-session `model` override to `/api/chat` and a Whisper `size` header to `/transcribe`. The browser panel fetches the catalog, greys unavailable models as "no key", and sends `set_model`/`set_stt`/`set_voice` over the WebSocket.

**Tech Stack:** TypeScript (axios streaming), Python 3.12 (FastAPI STT service, aiohttp voice server, httpx), vanilla JS, vitest + pytest + `node <file>`.

## Global Constraints

- **Model override is per-session and live** — no rebuild/restart. Absent/invalid override falls back to `config.agents.defaults.model`.
- **Availability** = the model's provider has a key configured (`config.providers[provider]?.apiKey`). Unavailable models render greyed, labeled **"no key"**, and are `disabled` in the `<select>` (cannot be selected).
- **Streaming for all** — `OpenAIProvider.completeStream()` parses OpenAI SSE (`choices[0].delta.content` text; `choices[0].delta.tool_calls` accumulated by index; `data: [DONE]` sentinel; `usage` if present). Reuse `readSSEFrames` from `voice`/base.ts.
- **Model-name normalization** — `OpenAIProvider.formatModelName` must strip a leading `provider/` prefix (everything up to and including the first `/`), so `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile`.
- **STT** — Whisper sizes `tiny · base · small · medium`; the STT service caches one `WhisperModel` per size, selected by an `X-Model-Size` header (default `base`).
- **Defaults / persistence** — `base` / `anthropic/claude-haiku-4-5` / `af_heart`; all three persisted in `localStorage`.
- **Config** — add `DASHSCOPE_API_KEY` (+ `MOONSHOT_API_KEY`, `ZHIPUAI_API_KEY`, `MINIMAX_API_KEY`) env injection; fix the Gemini `apiBase` to `https://generativelanguage.googleapis.com/v1beta/openai`.

## File Structure

**TypeScript** — `src/providers/base.ts` (`OpenAIProvider.completeStream` + `parseOpenAIEvents` + `formatModelName` fix), `src/agent/models.ts` (new catalog), `src/api/server.ts` (`GET /api/models`, model override), `src/config/index.ts` (env keys), `src/providers/index.ts` (Gemini apiBase).
**Python** — `stt-service/server.py` (per-size cache), `voice/server.py` (`/api/models` proxy, `set_model`/`set_stt`, model→/api/chat, size→/transcribe), `voice/webrtc.py` (session `model`/`stt_size`, STT header).
**JS** — `voice/web/index.html`, `voice/web/app.js`, `voice/web/styles.css` (⚙ panel), `voice/web/pipeline.js` (new pure helper).
**Tests** — `tests/streaming.test.ts` (OpenAI parser + formatModelName), `tests/models.test.ts` (catalog availability), `tests/python/test_stt_size.py`, `tests/pipeline.test.mjs`.

---

## Task 1: OpenAI-compatible streaming + model-name prefix fix (TS)

**Files:** Modify `src/providers/base.ts`; Test `tests/streaming.test.ts`.

**Interfaces:**
- Consumes: `StreamEvent`, `readSSEFrames` (existing, from the Anthropic work).
- Produces: exported `parseOpenAIEvents(stream: Readable): AsyncGenerator<StreamEvent>`; `OpenAIProvider.completeStream(...)`; `OpenAIProvider.formatModelName` strips a leading `provider/` prefix.

- [ ] **Step 1: Write the failing tests**

Add to `tests/streaming.test.ts`:

```ts
import { parseOpenAIEvents, OpenAIProvider } from '../src/providers/base';

describe('parseOpenAIEvents', () => {
  function sse(s: string) { return require('node:stream').Readable.from([Buffer.from(s)]); }

  it('assembles content deltas and ends on [DONE]', async () => {
    const body =
      'data: {"choices":[{"delta":{"content":"Hi "}}]}\n\n' +
      'data: {"choices":[{"delta":{"content":"there."},"finish_reason":null}]}\n\n' +
      'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n' +
      'data: [DONE]\n\n';
    const out: any[] = [];
    for await (const e of parseOpenAIEvents(sse(body))) out.push(e);
    expect(out.filter(e => e.type === 'text').map(e => e.delta).join('')).toBe('Hi there.');
    const done = out.find(e => e.type === 'done');
    expect(done.finishReason).toBe('stop');
    expect(done.usage).toEqual({ promptTokens: 5, completionTokens: 3, totalTokens: 8 });
  });

  it('assembles a streamed tool_call by index', async () => {
    const body =
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"shell","arguments":""}}]}}]}\n\n' +
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":"}}]}}]}\n\n' +
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n' +
      'data: [DONE]\n\n';
    const out: any[] = [];
    for await (const e of parseOpenAIEvents(sse(body))) out.push(e);
    const t = out.find(e => e.type === 'tool_calls');
    expect(t.toolCalls[0]).toEqual({ id: 'c1', type: 'function', function: { name: 'shell', arguments: '{"cmd":"ls"}' } });
  });
});

describe('OpenAIProvider.formatModelName', () => {
  it('strips any leading provider/ prefix', () => {
    const p = new OpenAIProvider('k');
    // formatModelName is protected; exercise via a tiny subclass
    const f = (m: string) => (p as any).formatModelName(m);
    expect(f('groq/llama-3.3-70b-versatile')).toBe('llama-3.3-70b-versatile');
    expect(f('gemini/gemini-2.0-flash')).toBe('gemini-2.0-flash');
    expect(f('gpt-4o-mini')).toBe('gpt-4o-mini');
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/streaming.test.ts`
Expected: FAIL — `parseOpenAIEvents` not exported; `formatModelName` doesn't strip `groq/`.

- [ ] **Step 3: Add `parseOpenAIEvents` to `src/providers/base.ts`**

After `parseAnthropicEvents` (reuse `readSSEFrames`), add:

```ts
/** Parse OpenAI-compatible /chat/completions streaming events into StreamEvents. */
export async function* parseOpenAIEvents(stream: Readable): AsyncGenerator<StreamEvent> {
  let finishReason: string | undefined;
  let usage: LLMResponse['usage'];
  const toolAcc = new Map<number, { id: string; name: string; args: string }>();

  for await (const { data } of readSSEFrames(stream)) {
    if (data === '[DONE]') break;
    let evt: any;
    try { evt = JSON.parse(data); } catch { continue; }
    const choice = evt.choices?.[0];
    if (evt.usage) {
      usage = { promptTokens: evt.usage.prompt_tokens, completionTokens: evt.usage.completion_tokens, totalTokens: evt.usage.total_tokens };
    }
    if (!choice) continue;
    if (choice.finish_reason) finishReason = choice.finish_reason;
    const delta = choice.delta || {};
    if (delta.content) yield { type: 'text', delta: delta.content };
    if (Array.isArray(delta.tool_calls)) {
      for (const tc of delta.tool_calls) {
        const idx = tc.index ?? 0;
        const acc = toolAcc.get(idx) || { id: '', name: '', args: '' };
        if (tc.id) acc.id = tc.id;
        if (tc.function?.name) acc.name = tc.function.name;
        if (tc.function?.arguments) acc.args += tc.function.arguments;
        toolAcc.set(idx, acc);
      }
    }
  }

  if (toolAcc.size > 0) {
    const toolCalls: ToolCall[] = [...toolAcc.values()].map((t) => ({
      id: t.id, type: 'function', function: { name: t.name, arguments: t.args || '{}' },
    }));
    yield { type: 'tool_calls', toolCalls };
  }
  yield { type: 'done', finishReason, usage };
}
```

- [ ] **Step 4: Fix `OpenAIProvider.formatModelName` + add `completeStream`**

Replace `OpenAIProvider.formatModelName` (base.ts ~446) with a general prefix strip:

```ts
  protected formatModelName(model: string): string {
    // Strip a leading "provider/" prefix (openai/, gemini/, groq/, deepseek/, dashscope/, …).
    const slash = model.indexOf('/');
    return slash === -1 ? model : model.slice(slash + 1);
  }
```

Add `completeStream` to `OpenAIProvider` (mirrors `complete`'s request shaping with `stream:true`):

```ts
  async *completeStream(
    messages: Message[], model: string, temperature = 0.7, maxTokens = 4096, tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const requestData: Record<string, unknown> = {
      model: this.formatModelName(model),
      messages: messages.map((m) => ({
        role: m.role, content: m.content,
        ...(m.name && { name: m.name }),
        ...(m.tool_calls && { tool_calls: m.tool_calls }),
        ...(m.tool_call_id && { tool_call_id: m.tool_call_id }),
      })),
      temperature, max_tokens: maxTokens, stream: true,
      stream_options: { include_usage: true },
    };
    if (tools && tools.length > 0) requestData.tools = tools;
    let response;
    try {
      response = await this.client.post('/chat/completions', requestData, { responseType: 'stream' });
    } catch (error) {
      logger.error({ error }, 'OpenAI API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(`OpenAI API error: ${error.response?.data?.error?.message || error.message}`);
      }
      throw new ProviderError(`OpenAI API error: ${(error as Error).message}`);
    }
    yield* parseOpenAIEvents(response.data as Readable);
  }
```

- [ ] **Step 5: Run tests + build**

Run: `npx vitest run tests/streaming.test.ts` → PASS (new + existing).
Run: `npm run build` → no TS errors.

- [ ] **Step 6: Commit**

```bash
git add src/providers/base.ts tests/streaming.test.ts
git commit -m "feat(api): OpenAI-compatible streaming + strip provider/ model prefix"
```

---

## Task 2: Model catalog + `GET /api/models` (TS)

**Files:** Create `src/agent/models.ts`; Modify `src/api/server.ts`; Test `tests/models.test.ts`.

**Interfaces:**
- Produces: `MODEL_CATALOG: {id,label,provider}[]`; `modelsWithAvailability(config): {id,label,provider,available}[]` (available iff `config.providers[provider]?.apiKey`); `GET /api/models` returns `{models, default}`.

- [ ] **Step 1: Write the failing test**

Create `tests/models.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { MODEL_CATALOG, modelsWithAvailability } from '../src/agent/models';

describe('model catalog', () => {
  it('marks a model available only when its provider key is present', () => {
    const cfg: any = { providers: { anthropic: { apiKey: 'x' }, gemini: {} } };
    const out = modelsWithAvailability(cfg);
    const byId = Object.fromEntries(out.map(m => [m.id, m.available]));
    expect(byId['anthropic/claude-haiku-4-5']).toBe(true);
    expect(byId['gemini/gemini-2.0-flash']).toBe(false); // key object present but no apiKey
    expect(byId['groq/llama-3.3-70b-versatile']).toBe(false); // provider absent
  });
  it('includes the deployed default model', () => {
    expect(MODEL_CATALOG.some(m => m.id === 'anthropic/claude-haiku-4-5')).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/models.test.ts` → FAIL (module missing).

- [ ] **Step 3: Create `src/agent/models.ts`**

```ts
import { Config } from '../config/schema';
import { ProviderConfig } from '../types';

export interface CatalogModel { id: string; label: string; provider: string; }

/** Curated, voice-friendly models. `provider` must match a registry provider name. */
export const MODEL_CATALOG: CatalogModel[] = [
  { id: 'anthropic/claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'anthropic' },
  { id: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5', provider: 'anthropic' },
  { id: 'gemini/gemini-2.0-flash', label: 'Gemini 2.0 Flash', provider: 'gemini' },
  { id: 'deepseek/deepseek-chat', label: 'DeepSeek Chat', provider: 'deepseek' },
  { id: 'groq/llama-3.3-70b-versatile', label: 'Groq Llama 3.3 70B', provider: 'groq' },
  { id: 'dashscope/qwen-plus', label: 'Qwen Plus (Alibaba)', provider: 'dashscope' },
  { id: 'openai/gpt-4o-mini', label: 'GPT-4o mini', provider: 'openai' },
];

export const DEFAULT_MODEL = 'anthropic/claude-haiku-4-5';

export function modelsWithAvailability(config: Config) {
  const providers = (config.providers as Record<string, ProviderConfig>) || {};
  return MODEL_CATALOG.map((m) => ({ ...m, available: !!providers[m.provider]?.apiKey }));
}
```

- [ ] **Step 4: Add `GET /api/models` to `src/api/server.ts`**

Add a handler and route it in `createServer` (near the `/api/chat` routing):

```ts
import { modelsWithAvailability, DEFAULT_MODEL } from '../agent/models';
// ...
function handleModels(res: http.ServerResponse): void {
  initShared();
  sendJson(res, 200, { models: modelsWithAvailability(config), default: DEFAULT_MODEL });
}
```

In the request router (where `GET`/`POST` are dispatched), add before the 404:

```ts
      } else if (method === 'GET' && url === '/api/models') {
        setCorsHeaders(res);
        handleModels(res);
```

- [ ] **Step 5: Run tests + build**

Run: `npx vitest run tests/models.test.ts` → PASS. `npm run build` → clean.

- [ ] **Step 6: Commit**

```bash
git add src/agent/models.ts src/api/server.ts tests/models.test.ts
git commit -m "feat(api): model catalog + GET /api/models with per-provider availability"
```

---

## Task 3: Per-session model override on `/api/chat` (TS)

**Files:** Modify `src/api/server.ts`; Test `tests/streaming.test.ts` (extend `stepLoopStream` test).

**Interfaces:**
- Produces: `getAgentConfig(modelOverride?: string)` uses the override when it names a catalog model; `handleChat`/`handleApprove`/`handleReject` read `body.model`; `stepLoop`/`stepLoopStream` receive the resolved `agentConfig`.

- [ ] **Step 1: Write the failing test**

Add to `tests/streaming.test.ts`:

```ts
import { getAgentConfig } from '../src/api/server';
describe('getAgentConfig model override', () => {
  it('uses a valid catalog model override, else the default', () => {
    expect(getAgentConfig('groq/llama-3.3-70b-versatile').model).toBe('groq/llama-3.3-70b-versatile');
    expect(getAgentConfig('totally-unknown-model').model).toBe(getAgentConfig().model); // falls back
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/streaming.test.ts` → FAIL (`getAgentConfig` not exported / no override param).

- [ ] **Step 3: Add the override to `getAgentConfig` (server.ts)**

Export it and accept an override validated against the catalog:

```ts
import { MODEL_CATALOG } from '../agent/models';
// ...
export function getAgentConfig(modelOverride?: string): AgentConfig {
  initShared();
  const valid = modelOverride && MODEL_CATALOG.some((m) => m.id === modelOverride);
  return {
    model: valid ? (modelOverride as string) : (config.agents?.defaults?.model || 'anthropic/claude-haiku-4-5'),
    temperature: config.agents?.defaults?.temperature || 0.7,
    maxTokens: config.agents?.defaults?.maxTokens || 4096,
    systemPrompt: config.agents?.defaults?.systemPrompt,
  };
}
```

- [ ] **Step 4: Read `body.model` in the chat handlers**

In `handleChat`, parse `model` from the body and pass it through:

```ts
  const body = parseJsonBody(await readBody(req)) as { message?: string; sessionId?: string; model?: string } | null;
  // ...
  const agentConfig = getAgentConfig(body.model);
  if (wantsStream(req)) { await streamLoopToSSE(res, stepLoopStream(memory, agentConfig, 0)); return; }
  const result = await stepLoop(memory, agentConfig, 0);
  sendJson(res, 200, result);
```

For `handleApprove`/`handleReject`: the model is already captured in `pending.agentConfig` when the turn started, so continuations keep the chosen model automatically — no change needed beyond reading `body.model` is unnecessary there. (Confirm `pendingRequests` stores `agentConfig`; it does.)

- [ ] **Step 5: Run tests + build**

Run: `npx vitest run tests/streaming.test.ts tests/models.test.ts` → PASS. `npm run build` → clean.

- [ ] **Step 6: Commit**

```bash
git add src/api/server.ts tests/streaming.test.ts
git commit -m "feat(api): per-request model override on /api/chat (validated against catalog)"
```

---

## Task 4: Config — extra provider keys + Gemini base URL (TS)

**Files:** Modify `src/config/index.ts`, `src/providers/index.ts`.

- [ ] **Step 1: Add env injection for the missing providers**

In `src/config/index.ts::mergeEnvConfig`, after the `GEMINI_API_KEY` block, add:

```ts
  if (process.env.DASHSCOPE_API_KEY) {
    envProviders.dashscope = { apiKey: process.env.DASHSCOPE_API_KEY };
  }
  if (process.env.MOONSHOT_API_KEY) {
    envProviders.moonshot = { apiKey: process.env.MOONSHOT_API_KEY };
  }
  if (process.env.ZHIPUAI_API_KEY) {
    envProviders.zhipu = { apiKey: process.env.ZHIPUAI_API_KEY };
  }
  if (process.env.MINIMAX_API_KEY) {
    envProviders.minimax = { apiKey: process.env.MINIMAX_API_KEY };
  }
```

- [ ] **Step 2: Fix the Gemini OpenAI-compat base URL**

In `src/providers/index.ts`, the `case 'gemini':` uses `.../v1beta`. Change the default apiBase to the OpenAI-compatible path:

```ts
      case 'gemini':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://generativelanguage.googleapis.com/v1beta/openai'
        );
        break;
```

- [ ] **Step 3: Build**

Run: `npm run build` → clean. (No unit test — env-driven; verified in Task 8 integration.)

- [ ] **Step 4: Commit**

```bash
git add src/config/index.ts src/providers/index.ts
git commit -m "feat(config): inject dashscope/moonshot/zhipu/minimax keys; fix Gemini OpenAI base URL"
```

---

## Task 5: STT service — per-size Whisper model cache (Python)

**Files:** Modify `stt-service/server.py`; Test `tests/python/test_stt_size.py`.

**Interfaces:**
- Produces: `/transcribe` honors an `X-Model-Size` header (default `base`); a per-size model cache; `_valid_size(size) -> bool` and `SIZES` list for validation.

- [ ] **Step 1: Write the failing test (pure size validation)**

Create `tests/python/test_stt_size.py`:

```python
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "stt_server", Path(__file__).resolve().parents[2] / "stt-service" / "server.py"
)
# Import without starting uvicorn (module guards run under __main__).
stt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt)


def test_sizes_list():
    assert stt.SIZES == ["tiny", "base", "small", "medium"]


def test_valid_size():
    assert stt._valid_size("small") is True
    assert stt._valid_size("huge") is False
    assert stt._valid_size("") is False
```

> Note: importing `stt-service/server.py` requires `fastapi`/`uvicorn`/`numpy` importable. If the test venv lacks them, mark this test's import guarded; the STT service venv has them. Run with the STT venv's python if needed: `stt-service/.venv/bin/python -m pytest tests/python/test_stt_size.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `stt-service/.venv/bin/python -m pytest tests/python/test_stt_size.py -v`
Expected: FAIL — `SIZES`/`_valid_size` don't exist.

- [ ] **Step 3: Modify `stt-service/server.py`**

Replace the single `MODEL_SIZE` + `_model` + `_get_model()` with a per-size cache, and read the header in `transcribe`:

```python
SIZES = ["tiny", "base", "small", "medium"]
DEFAULT_SIZE = "base"
_models: dict = {}


def _valid_size(size: str) -> bool:
    return size in SIZES


def _get_model(size: str):
    """Load + cache a faster-whisper model per size (auto-downloads on first use)."""
    if size in _models:
        return _models[size]
    from faster_whisper import WhisperModel
    log.info("Loading faster-whisper model: %s ...", size)
    _models[size] = WhisperModel(size, device="cpu", compute_type="int8")
    log.info("Whisper model loaded: %s", size)
    return _models[size]
```

In `transcribe`, read the size header and use it:

```python
    size = request.headers.get("X-Model-Size", DEFAULT_SIZE)
    if not _valid_size(size):
        size = DEFAULT_SIZE
    # ...
    model = _get_model(size)
    # ... (segments, _info = model.transcribe(...))
```

(Keep everything else — the resample, the JSON response — unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `stt-service/.venv/bin/python -m pytest tests/python/test_stt_size.py -v` → PASS.
Run: `python3 -m py_compile stt-service/server.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add stt-service/server.py tests/python/test_stt_size.py
git commit -m "feat(stt): per-size Whisper model cache selected by X-Model-Size header"
```

---

## Task 6: Voice server wiring — /api/models, set_model, set_stt (Python)

**Files:** Modify `voice/server.py`, `voice/webrtc.py`.

**Interfaces:**
- Consumes: nano-claw `GET /api/models`; session attrs.
- Produces: `GET /api/models` proxy; `set_model {modelId}` / `set_stt {size}` WS handlers; `session.model` included on `/api/chat` requests; `session.stt_size` sent as `X-Model-Size` to `/transcribe`.

- [ ] **Step 1: Session attrs (voice/webrtc.py)**

In `Session.__init__` (near `self.voice_id`), add:

```python
        self.model = ""       # "" → server uses its default
        self.stt_size = "base"
```

In `stop_recording` (the STT POST), add the size header:

```python
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Sample-Rate": str(SAMPLE_RATE),
                        "X-Model-Size": self.stt_size,
                    },
```

- [ ] **Step 2: Voice server — /api/models proxy + WS handlers (voice/server.py)**

Add a models proxy handler and route it (before the `/{filename}` catch-all):

```python
async def models_handler(request: web.Request) -> web.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{NANO_CLAW_URL}/api/models")
        return web.json_response(resp.json())
# in create_app: app.router.add_get("/api/models", models_handler)
```

Add WS handlers (alongside `set_voice`):

```python
            elif msg_type == "set_model":
                if session:
                    session.model = msg.get("modelId", "") or ""
                    log.info("Model set: %s", session.model or "(default)")

            elif msg_type == "set_stt":
                if session:
                    size = msg.get("size", "base")
                    session.stt_size = size if size in ("tiny", "base", "small", "medium") else "base"
                    log.info("STT size set: %s", session.stt_size)
```

Include the session model on every `/api/chat` request. In `_handle_agent_request`, add `model` to the JSON body when set:

```python
            json={"message": text, "sessionId": SESSION_ID, **({"model": session.model} if session.model else {})},
```

(The tool-decision continuation keeps the model automatically — the API stored it in `pending.agentConfig`.)

- [ ] **Step 3: Verify**

Run: `python3 -m py_compile voice/server.py voice/webrtc.py` → clean. `.venv-test/bin/pytest tests/python -v` → existing suite green.

- [ ] **Step 4: Commit**

```bash
git add voice/server.py voice/webrtc.py
git commit -m "feat(voice): /api/models proxy; set_model/set_stt; thread model + Whisper size"
```

---

## Task 7: Browser ⚙ Pipeline panel (JS)

**Files:** Create `voice/web/pipeline.js`; Modify `voice/web/index.html`, `voice/web/app.js`, `voice/web/styles.css`; Test `tests/pipeline.test.mjs`.

**Interfaces:**
- Produces (`window.Pipeline`): `buildModelOptions(models) -> [{id,label,disabled}]` (unavailable → `label + " — no key"`, `disabled:true`).

- [ ] **Step 1: Write the failing test for the pure helper**

Create `tests/pipeline.test.mjs`:

```javascript
import assert from "node:assert/strict";
await import("../voice/web/pipeline.js");
const Pipeline = globalThis.Pipeline;
assert.ok(Pipeline, "Pipeline global must exist");
const opts = Pipeline.buildModelOptions([
  { id: "anthropic/claude-haiku-4-5", label: "Claude Haiku 4.5", available: true },
  { id: "gemini/gemini-2.0-flash", label: "Gemini 2.0 Flash", available: false },
]);
assert.equal(opts[0].label, "Claude Haiku 4.5");
assert.equal(opts[0].disabled, false);
assert.equal(opts[1].label, "Gemini 2.0 Flash — no key");
assert.equal(opts[1].disabled, true);
console.log("pipeline tests passed");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/pipeline.test.mjs` → FAIL (module missing).

- [ ] **Step 3: Create `voice/web/pipeline.js`**

```javascript
(function (global) {
    "use strict";
    function buildModelOptions(models) {
        return (models || []).map(function (m) {
            return { id: m.id, label: m.available ? m.label : m.label + " — no key", disabled: !m.available };
        });
    }
    global.Pipeline = { buildModelOptions: buildModelOptions };
}(typeof window !== "undefined" ? window : globalThis));
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/pipeline.test.mjs` → "pipeline tests passed".

- [ ] **Step 5: Markup — `index.html`**

Load `pipeline.js` before `app.js`. Add a gear button + panel inside `#controls` (place the existing `#voice-row` inside this panel):

```html
      <button id="settings-btn" type="button" title="Pipeline settings">⚙</button>
      <div id="pipeline-panel" class="hidden">
        <div class="pipe-row"><label for="stt-select">STT (Whisper)</label>
          <select id="stt-select">
            <option value="tiny">tiny</option><option value="base" selected>base</option>
            <option value="small">small</option><option value="medium">medium</option>
          </select></div>
        <div class="pipe-row"><label for="model-select">LLM</label>
          <select id="model-select"></select></div>
        <!-- existing #voice-row (TTS voice + preview + speed) moves here -->
      </div>
```

- [ ] **Step 6: Wire `app.js`**

Add DOM handles + state; fetch `/api/models`; restore `localStorage`; send `set_stt`/`set_model` on load and change. Add near the voice-picker code:

```javascript
var LS_MODEL = "nanoclaw.model", LS_STT = "nanoclaw.stt";
var currentModel = localStorage.getItem(LS_MODEL) || "anthropic/claude-haiku-4-5";
var currentStt = localStorage.getItem(LS_STT) || "base";
var modelSelect = document.getElementById("model-select");
var sttSelect = document.getElementById("stt-select");
var settingsBtn = document.getElementById("settings-btn");
var pipelinePanel = document.getElementById("pipeline-panel");

settingsBtn.addEventListener("click", function () { pipelinePanel.classList.toggle("hidden"); });

function loadModels() {
  fetch("/api/models").then(function (r) { return r.json(); }).then(function (data) {
    modelSelect.innerHTML = "";
    Pipeline.buildModelOptions(data.models).forEach(function (o) {
      var el = document.createElement("option");
      el.value = o.id; el.textContent = o.label; el.disabled = o.disabled;
      modelSelect.appendChild(el);
    });
    // keep stored model if still available, else fall back to default
    var chosen = data.models.find(function (m) { return m.id === currentModel && m.available; });
    currentModel = chosen ? currentModel : data.default;
    modelSelect.value = currentModel;
    sttSelect.value = currentStt;
    sendMsg("set_model", { modelId: currentModel });
    sendMsg("set_stt", { size: currentStt });
  }).catch(function () {});
}

modelSelect.addEventListener("change", function () {
  currentModel = modelSelect.value; localStorage.setItem(LS_MODEL, currentModel);
  sendMsg("set_model", { modelId: currentModel });
});
sttSelect.addEventListener("change", function () {
  currentStt = sttSelect.value; localStorage.setItem(LS_STT, currentStt);
  sendMsg("set_stt", { size: currentStt });
});
```

Call `loadModels()` in `ws.onopen` (next to the existing `loadVoices()`).

- [ ] **Step 7: Styles — `styles.css`**

Append minimal styles for `#settings-btn`, `#pipeline-panel` (a small bordered panel; `.hidden { display:none }` if not already defined), and `.pipe-row` (flex row, label + control). Keep consistent with the existing `#voice-row`.

- [ ] **Step 8: Verify**

Run: `node tests/pipeline.test.mjs && node tests/voice-ui.test.mjs && node tests/phone-vad.test.mjs && node tests/barge-in.test.mjs` → all pass.
Run: `node --check voice/web/app.js` → clean.

- [ ] **Step 9: Commit**

```bash
git add voice/web/pipeline.js voice/web/index.html voice/web/app.js voice/web/styles.css tests/pipeline.test.mjs
git commit -m "feat(web): ⚙ Pipeline panel — switch STT size, LLM model, and TTS voice"
```

---

## Task 8: Docs + integration verification

**Files:** Modify `README.md`, `CHANGELOG.md`.

- [ ] **Step 1: Docs**

- `README.md`: a "Pipeline settings" section — the ⚙ panel switches STT (Whisper size), LLM (any model whose provider key is in `.env`; others show "no key"), and TTS voice, live. List the recognized keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY`).
- `CHANGELOG.md` `### Added`: pipeline settings + OpenAI-compatible streaming for Gemini/DeepSeek/Groq/OpenAI/Alibaba.

- [ ] **Step 2: Full integration verification (controller-run)**

Rebuild TS + image, run with the keys present, then:

```bash
npm run build && docker build -t nano-claw-voice . && docker rm -f nano-claw-voice 2>/dev/null
set -a; source .env; set +a
docker run -d --rm --name nano-claw-voice -p 9090:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  $( [ -n "$GEMINI_API_KEY" ] && echo -e "-e GEMINI_API_KEY=$GEMINI_API_KEY" ) \
  $( [ -n "$DASHSCOPE_API_KEY" ] && echo -e "-e DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY" ) \
  -e STT_SERVICE_URL="http://host.docker.internal:8200" -e TTS_SERVICE_URL="http://host.docker.internal:8300" \
  -v nano-claw-models:/app/voice/models nano-claw-voice

# catalog + availability
curl -s localhost:9090/api/models | python3 -m json.tool | head -40
# streamed reply on a specified model (Anthropic always available)
docker exec nano-claw-voice sh -c 'curl -N -s -X POST localhost:3001/api/chat -H "Accept: text/event-stream" -H "Content-Type: application/json" -d "{\"message\":\"say hi in one sentence\",\"sessionId\":\"m1\",\"model\":\"anthropic/claude-haiku-4-5\"}"' | head
# if GEMINI_API_KEY present, repeat with "model":"gemini/gemini-2.0-flash" and confirm SSE deltas stream
```

Expected: `/api/models` lists the catalog with `available` reflecting the keys present; a model-override chat streams `delta` frames. In the browser, the ⚙ panel switches all three stages; unavailable models show "no key" and are unselectable.

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: pipeline settings (switch STT/LLM/TTS) + multi-provider streaming"
```

---

## Self-Review (completed during authoring)

**Spec coverage:** OpenAI streaming (T1) · prefix fix (T1) · catalog+availability / GET /api/models (T2) · per-session model override (T3) · dashscope+keys / Gemini base URL (T4) · STT size cache (T5) · voice-server proxy+set_model+set_stt+headers (T6) · ⚙ panel with "no key" greying + TTS relocation (T7) · docs+integration (T8). ✓
**Placeholder scan:** every code step has full code; every test step has assertions + run command. ✓
**Type consistency:** `StreamEvent`/`readSSEFrames`/`ToolCall` reused from the Anthropic work; `modelsWithAvailability`/`MODEL_CATALOG`/`DEFAULT_MODEL` consistent across T2/T3/T7; WS message names `set_model`/`set_stt` emitted by T7, handled by T6; `X-Model-Size` written by T6, read by T5; `Pipeline.buildModelOptions` shape matches `/api/models`. ✓

## Out of scope
- New STT/TTS engines (Whisper sizes + existing voices only).
- Storing API keys via the UI (keys stay in `.env`).
- Mid-conversation model changes rewriting prior history (applies to subsequent turns).
