import { describe, it, expect } from 'vitest';
import { ProviderManager } from '../src/providers';
import type { Config } from '../src/config/schema';
import type { LLMResponse, Message, StreamEvent } from '../src/types';

// The /calls panel attributes each turn to the model that actually wrote it,
// so the fallback chain must tag responses with the serving model.

const config = {
  providers: {
    ollama: { apiKey: 'local' },
    gemini: { apiKey: 'key' },
  },
  agents: {
    defaults: {
      fallbackModels: ['gemini/gemini-flash-lite-latest'],
      fallbackTimeoutMs: 40,
    },
  },
} as unknown as Config;

const messages: Message[] = [{ role: 'user', content: 'hi' }];

function fakeProvider(opts: { fail?: boolean; text: string }) {
  return {
    async complete(): Promise<LLMResponse> {
      if (opts.fail) throw new Error('primary down');
      return { content: opts.text };
    },
    async *completeStream(): AsyncGenerator<StreamEvent> {
      if (opts.fail) throw new Error('primary down');
      yield { type: 'text', delta: opts.text };
      yield { type: 'done', finishReason: 'stop' };
    },
  };
}

function seeded(primary: { fail?: boolean; text: string }, fallback: { text: string }) {
  const pm = new ProviderManager(config);
  const cache = (pm as unknown as { providerCache: Map<string, unknown> }).providerCache;
  cache.set('ollama', fakeProvider(primary));
  cache.set('gemini', fakeProvider(fallback));
  return pm;
}

async function collect(gen: AsyncGenerator<StreamEvent>): Promise<StreamEvent[]> {
  const out: StreamEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

describe('served-model attribution', () => {
  it('complete() reports the requested model when it answers', async () => {
    const pm = seeded({ text: 'A' }, { text: 'B' });
    const res = await pm.complete(messages, 'ollama/gemma4:e2b');
    expect(res.content).toBe('A');
    expect(res.model).toBe('ollama/gemma4:e2b');
  });

  it('complete() reports the fallback model when the primary fails', async () => {
    const pm = seeded({ fail: true, text: 'A' }, { text: 'B' });
    const res = await pm.complete(messages, 'ollama/gemma4:e2b');
    expect(res.content).toBe('B');
    expect(res.model).toBe('gemini/gemini-flash-lite-latest');
  });

  it('completeStream() tags the done event with the serving model', async () => {
    const pm = seeded({ fail: true, text: 'A' }, { text: 'B' });
    const events = await collect(pm.completeStream(messages, 'ollama/gemma4:e2b'));
    const done = events.find((e) => e.type === 'done') as
      | { type: 'done'; model?: string }
      | undefined;
    expect(done?.model).toBe('gemini/gemini-flash-lite-latest');
  });

  it('completeStream() tags the primary model on the happy path', async () => {
    const pm = seeded({ text: 'A' }, { text: 'B' });
    const events = await collect(pm.completeStream(messages, 'ollama/gemma4:e2b'));
    const done = events.find((e) => e.type === 'done') as
      | { type: 'done'; model?: string }
      | undefined;
    expect(done?.model).toBe('ollama/gemma4:e2b');
  });

  it('hedged streaming preserves served-model attribution', async () => {
    const hedgedConfig = {
      ...config,
      agents: {
        defaults: {
          fallbackModels: ['gemini/gemini-flash-lite-latest'],
          fallbackTimeoutMs: 4000,
          fallbackHedgeMs: 20,
        },
      },
    } as unknown as Config;
    const pm = new ProviderManager(hedgedConfig);
    const cache = (pm as unknown as { providerCache: Map<string, unknown> })
      .providerCache;
    cache.set('ollama', {
      async *completeStream(): AsyncGenerator<StreamEvent> {
        await new Promise((r) => setTimeout(r, 200)); // misses the hedge window
        yield { type: 'text', delta: 'slow local' };
        yield { type: 'done', finishReason: 'stop' };
      },
    });
    cache.set('gemini', fakeProvider({ text: 'hedge wins' }));

    const events = await collect(pm.completeStream(messages, 'ollama/gemma4:e2b'));
    const done = events.find((e) => e.type === 'done') as
      | { type: 'done'; model?: string }
      | undefined;
    expect(done?.model).toBe('gemini/gemini-flash-lite-latest');
    expect(
      events.filter((e) => e.type === 'text').map((e) => (e as { delta: string }).delta)
    ).toEqual(['hedge wins']);
  });
});
