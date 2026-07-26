import { describe, it, expect } from 'vitest';
import {
  completeWithFallback,
  streamWithFallback,
  streamWithHedge,
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

describe('streamWithHedge', () => {
  // A generator that yields its first token after `ms`, recording lifecycle
  // so tests can assert whether it was started and whether it was cancelled.
  function racer(ms: number, text: string, log: string[]) {
    return async function* (): AsyncGenerator<StreamEvent> {
      log.push(`start:${text}`);
      try {
        await delay(ms);
        yield { type: 'text', delta: text };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
        log.push(`finish:${text}`);
      } finally {
        if (!log.includes(`finish:${text}`)) log.push(`cancelled:${text}`);
      }
    };
  }

  it('a fast primary wins without the hedge ever starting', async () => {
    const log: string[] = [];
    const out = await collect(
      streamWithHedge(
        [
          { label: 'a', run: racer(5, 'A', log) },
          { label: 'b', run: racer(5, 'B', log) },
        ],
        100,
      ),
    );
    expect(texts(out)).toBe('A');
    expect(log).not.toContain('start:B');
  });

  it('hedge fires at the delay and a faster hedge wins; the loser is cancelled', async () => {
    const log: string[] = [];
    const out = await collect(
      streamWithHedge(
        [
          { label: 'a', run: racer(200, 'A', log) },
          { label: 'b', run: racer(10, 'B', log) },
        ],
        30,
      ),
    );
    expect(texts(out)).toBe('B');
    expect(log).toContain('start:A');
    // gen.return() is queued behind the loser's in-flight await; the cancel
    // lands when that await settles (here: A's 200ms first-token delay).
    await delay(250);
    expect(log).toContain('cancelled:A');
  });

  it('the primary still wins when it beats the started hedge to first token', async () => {
    const log: string[] = [];
    const out = await collect(
      streamWithHedge(
        [
          { label: 'a', run: racer(60, 'A', log) },
          { label: 'b', run: racer(100, 'B', log) },
        ],
        30,
      ),
    );
    expect(texts(out)).toBe('A');
    expect(log).toContain('start:B'); // hedge really was racing
    await delay(150); // cancel lands when B's pending await settles
    expect(log).toContain('cancelled:B');
  });

  it('a pre-commit error starts the next attempt immediately, not at the delay', async () => {
    async function* dies(): AsyncGenerator<StreamEvent> {
      throw new Error('connect failed');
    }
    const log: string[] = [];
    const t0 = Date.now();
    const out = await collect(
      streamWithHedge(
        [
          { label: 'a', run: () => dies() },
          { label: 'b', run: racer(10, 'B', log) },
        ],
        10_000,
      ),
    );
    expect(texts(out)).toBe('B');
    expect(Date.now() - t0).toBeLessThan(2_000); // did not wait out the hedge delay
  });

  it('an empty pre-commit stream drops out and the hedge answers', async () => {
    async function* empty(): AsyncGenerator<StreamEvent> {
      yield { type: 'done', finishReason: 'stop', usage: undefined };
    }
    const log: string[] = [];
    const out = await collect(
      streamWithHedge(
        [
          { label: 'a', run: () => empty() },
          { label: 'b', run: racer(10, 'B', log) },
        ],
        10_000,
      ),
    );
    expect(texts(out)).toBe('B');
  });

  it('throws the last error when every attempt fails before a commit', async () => {
    async function* dies(msg: string): AsyncGenerator<StreamEvent> {
      throw new Error(msg);
    }
    await expect(
      collect(
        streamWithHedge(
          [
            { label: 'a', run: () => dies('a down') },
            { label: 'b', run: () => dies('b down') },
          ],
          10,
        ),
      ),
    ).rejects.toThrow('b down');
  });

  it('a post-commit error propagates (no switching after first token)', async () => {
    async function* firstThenError(): AsyncGenerator<StreamEvent> {
      yield { type: 'text', delta: 'partial ' };
      throw new Error('mid-stream failure');
    }
    const log: string[] = [];
    const seen: StreamEvent[] = [];
    await expect(
      (async () => {
        for await (const e of streamWithHedge(
          [
            { label: 'a', run: () => firstThenError() },
            { label: 'b', run: racer(5, 'B', log) },
          ],
          100,
        )) {
          seen.push(e);
        }
      })(),
    ).rejects.toThrow('mid-stream failure');
    expect(texts(seen)).toBe('partial ');
    expect(texts(seen)).not.toContain('B');
  });
});
