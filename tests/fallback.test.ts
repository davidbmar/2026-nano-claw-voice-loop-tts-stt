import { describe, it, expect } from 'vitest';
import {
  completeWithFallback,
  streamWithFallback,
  raceTimeout,
  TIMED_OUT,
} from '../src/providers/fallback';
import type { StreamEvent } from '../src/types';

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function collect(gen: AsyncGenerator<StreamEvent>): Promise<StreamEvent[]> {
  const out: StreamEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}
const texts = (evts: StreamEvent[]) =>
  evts.filter((e) => e.type === 'text').map((e) => (e as { delta: string }).delta).join('');

async function* fast(text: string): AsyncGenerator<StreamEvent> {
  yield { type: 'text', delta: text };
  yield { type: 'done', finishReason: 'stop', usage: undefined };
}

describe('raceTimeout', () => {
  it('returns the value when it resolves in time', async () => {
    expect(await raceTimeout(Promise.resolve(42), 100)).toBe(42);
  });
  it('returns TIMED_OUT when the promise is too slow', async () => {
    expect(await raceTimeout(delay(50).then(() => 42), 10)).toBe(TIMED_OUT);
  });
});

describe('completeWithFallback', () => {
  it('returns the primary result when it succeeds', async () => {
    const res = await completeWithFallback(
      [
        { label: 'a', run: async () => 'A' },
        { label: 'b', run: async () => 'B' },
      ],
      100,
    );
    expect(res).toBe('A');
  });

  it('falls back to the next model when the primary throws', async () => {
    const res = await completeWithFallback(
      [
        { label: 'a', run: async () => { throw new Error('boom'); } },
        { label: 'b', run: async () => 'B' },
      ],
      100,
    );
    expect(res).toBe('B');
  });

  it('falls back when the primary is too slow', async () => {
    const res = await completeWithFallback(
      [
        { label: 'a', run: async () => { await delay(80); return 'A'; } },
        { label: 'b', run: async () => 'B' },
      ],
      20,
    );
    expect(res).toBe('B');
  });

  it('does not time out the LAST attempt — a slow last answer still wins', async () => {
    const res = await completeWithFallback(
      [
        { label: 'a', run: async () => { throw new Error('x'); } },
        { label: 'b', run: async () => { await delay(60); return 'B'; } },
      ],
      20,
    );
    expect(res).toBe('B');
  });

  it('throws the last error when every attempt fails', async () => {
    await expect(
      completeWithFallback(
        [
          { label: 'a', run: async () => { throw new Error('e1'); } },
          { label: 'b', run: async () => { throw new Error('e2'); } },
        ],
        100,
      ),
    ).rejects.toThrow('e2');
  });
});

describe('streamWithFallback', () => {
  it('streams the primary when it produces a first token in time', async () => {
    const out = await collect(
      streamWithFallback(
        [
          { label: 'a', run: () => fast('hello') },
          { label: 'b', run: () => fast('backup') },
        ],
        100,
      ),
    );
    expect(texts(out)).toBe('hello');
  });

  it('falls back when the primary emits no first token in time', async () => {
    async function* stalled(): AsyncGenerator<StreamEvent> {
      await delay(80);
      yield { type: 'text', delta: 'late' };
      yield { type: 'done', finishReason: 'stop', usage: undefined };
    }
    const out = await collect(
      streamWithFallback(
        [
          { label: 'a', run: () => stalled() },
          { label: 'b', run: () => fast('backup') },
        ],
        20,
      ),
    );
    expect(texts(out)).toBe('backup');
  });

  it('does NOT switch once the first token was emitted (no double-speak)', async () => {
    async function* firstThenError(): AsyncGenerator<StreamEvent> {
      yield { type: 'text', delta: 'partial ' };
      throw new Error('mid-stream failure');
    }
    const seen: StreamEvent[] = [];
    await expect(
      (async () => {
        for await (const e of streamWithFallback(
          [
            { label: 'a', run: () => firstThenError() },
            { label: 'b', run: () => fast('backup') },
          ],
          100,
        )) {
          seen.push(e);
        }
      })(),
    ).rejects.toThrow('mid-stream failure');
    // Committed to 'a': saw its partial text, never the backup.
    expect(texts(seen)).toBe('partial ');
  });

  it('falls back on a pre-first-token error', async () => {
    async function* errorsImmediately(): AsyncGenerator<StreamEvent> {
      throw new Error('connect failed');
    }
    const out = await collect(
      streamWithFallback(
        [
          { label: 'a', run: () => errorsImmediately() },
          { label: 'b', run: () => fast('backup') },
        ],
        100,
      ),
    );
    expect(texts(out)).toBe('backup');
  });
});
