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
