import type { StreamEvent } from '../types';
import { logger } from '../utils/logger';

/**
 * Model fallback policy (provider-agnostic).
 *
 * A default model that every unauthenticated caller and the phone line inherit
 * is a single point of failure: if it errors or hangs, the whole turn fails.
 * These helpers try an ordered chain of models, moving on when one errors or is
 * too slow — with a critical rule for the streaming (voice) path: once the
 * first token has been emitted we are COMMITTED to that model, because
 * switching mid-reply would make the assistant speak the answer twice.
 *
 * The logic is kept pure (takes attempt thunks, not providers) so it is fully
 * unit-testable without real network providers.
 */

/** Sentinel returned by raceTimeout when the deadline wins the race. */
export const TIMED_OUT = Symbol('timed-out');

export async function raceTimeout<T>(
  p: Promise<T>,
  ms: number,
): Promise<T | typeof TIMED_OUT> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
    timer = setTimeout(() => resolve(TIMED_OUT), ms);
  });
  try {
    return await Promise.race([p, timeout]);
  } finally {
    clearTimeout(timer!);
  }
}

export interface CompleteAttempt<T> {
  label: string;
  run: () => Promise<T>;
}

/**
 * Non-streaming fallback. Try each attempt in order; move on when one throws
 * or (for all but the last) exceeds `timeoutMs`. The LAST attempt gets no
 * deadline — a slow answer beats no answer. Returns the first success; throws
 * the final error if every attempt fails.
 */
export async function completeWithFallback<T>(
  attempts: Array<CompleteAttempt<T>>,
  timeoutMs: number,
): Promise<T> {
  if (attempts.length === 0) throw new Error('completeWithFallback: no attempts');
  let lastError: unknown;
  for (let i = 0; i < attempts.length; i++) {
    const { label, run } = attempts[i];
    const isLast = i === attempts.length - 1;
    try {
      const result = isLast ? await run() : await raceTimeout(run(), timeoutMs);
      if (result === TIMED_OUT) {
        lastError = new Error(`Model ${label} timed out after ${timeoutMs}ms`);
        logger.warn({ model: label, timeoutMs }, 'Model too slow; falling back');
        continue;
      }
      if (i > 0) logger.warn({ model: label }, 'Answered via fallback model');
      return result as T;
    } catch (error) {
      lastError = error;
      logger.warn(
        { model: label, error: (error as Error).message, isLast },
        'Model attempt failed',
      );
      if (isLast) throw error;
    }
  }
  throw lastError;
}

export interface StreamAttempt {
  label: string;
  run: () => AsyncGenerator<StreamEvent>;
}

/** A first meaningful token — text or a tool call — commits us to a stream. */
function isFirstToken(ev: StreamEvent): boolean {
  return ev.type === 'text' || ev.type === 'tool_calls';
}

/**
 * Streaming fallback with a time-to-first-token deadline. For each attempt we
 * wait up to `timeoutMs` for the first meaningful event; if nothing arrives in
 * time — or the stream errors — BEFORE that first event, abandon it and try the
 * next. Once the first event is emitted we are committed: no further switching,
 * and a later error propagates (the partial reply is already spoken).
 */
interface HedgeRacer {
  label: string;
  gen: AsyncGenerator<StreamEvent>;
  /** Pre-first-token events (in practice only an empty reply's `done`). */
  buffer: StreamEvent[];
  settled: boolean;
}

/**
 * Hedged streaming: attempt 0 starts immediately; each later attempt starts
 * `hedgeDelayMs` after the previous one UNLESS a first token has already
 * arrived — and starts at once when a running attempt dies, so an erroring
 * primary never makes the caller wait out the delay. All in-flight attempts
 * race to the first meaningful token; the winner takes the turn and every
 * other attempt is cancelled immediately (its request aborts, so a losing
 * local model stops burning the GPU that TTS/STT need). The no-double-speak
 * rule is identical to streamWithFallback: after the first token we are
 * committed, and a later error propagates. An attempt that ends with no
 * tokens drops out; if every attempt does, the last empty reply is surfaced
 * so usage accounting still sees its `done` event. If every attempt errors
 * before a commit, the last error is thrown.
 */
export async function* streamWithHedge(
  attempts: Array<StreamAttempt>,
  hedgeDelayMs: number,
): AsyncGenerator<StreamEvent> {
  if (attempts.length === 0) throw new Error('streamWithHedge: no attempts');

  type Win = { racer: HedgeRacer; firstEvent?: StreamEvent; index: number };
  let decideWin!: (w: Win) => void;
  let decideFail!: (e: unknown) => void;
  const arbitration = new Promise<Win>((resolve, reject) => {
    decideWin = resolve;
    decideFail = reject;
  });
  let decided = false;

  const racers: HedgeRacer[] = [];
  let started = 0;
  let settledCount = 0;
  let lastError: unknown = new Error('streamWithHedge: no attempt produced output');
  let lastEmpty: HedgeRacer | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const scheduleNext = () => {
    if (decided || started >= attempts.length) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(startNext, hedgeDelayMs);
  };

  const startNext = () => {
    if (decided || started >= attempts.length) return;
    const index = started++;
    const attempt = attempts[index];
    const racer: HedgeRacer = {
      label: attempt.label,
      gen: attempt.run(),
      buffer: [],
      settled: false,
    };
    racers.push(racer);
    if (index > 0) {
      logger.warn({ model: attempt.label, hedgeDelayMs }, 'Hedge attempt started');
    }
    void pump(racer, index);
    scheduleNext();
  };

  const pump = async (racer: HedgeRacer, index: number) => {
    try {
      while (true) {
        const step = await racer.gen.next();
        if (decided) return; // lost while awaiting; cancellation is under way
        if (step.done) break; // ended with no first token: an empty reply
        if (isFirstToken(step.value)) {
          decided = true;
          if (timer) clearTimeout(timer);
          decideWin({ racer, firstEvent: step.value, index });
          return; // the main body takes over pulling this generator
        }
        racer.buffer.push(step.value);
      }
      lastEmpty = racer;
    } catch (error) {
      if (decided) return;
      lastError = error;
      logger.warn(
        { model: racer.label, error: (error as Error).message },
        'Hedge attempt failed before first token',
      );
    }
    racer.settled = true;
    settledCount++;
    if (decided) return;
    if (started < attempts.length) {
      startNext(); // a dead racer hands its slot to the next model immediately
    } else if (settledCount === started) {
      decided = true;
      if (timer) clearTimeout(timer);
      if (lastEmpty) {
        decideWin({ racer: lastEmpty, index: racers.indexOf(lastEmpty) });
      } else {
        decideFail(lastError);
      }
    }
  };

  const cancelOthers = (winner?: HedgeRacer) => {
    for (const racer of racers) {
      if (racer === winner || racer.settled) continue;
      void racer.gen.return(undefined as unknown as StreamEvent).catch(() => {});
    }
  };

  startNext();
  let win: Win;
  try {
    win = await arbitration;
  } catch (error) {
    cancelOthers();
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
  cancelOthers(win.racer);
  if (win.index > 0 && win.firstEvent) {
    logger.warn({ model: win.racer.label }, 'Streaming via hedged fallback model');
  }
  try {
    for (const event of win.racer.buffer) yield event;
    if (!win.firstEvent) return; // every attempt was an empty reply
    yield win.firstEvent;
    while (true) {
      const step = await win.racer.gen.next();
      if (step.done) return;
      yield step.value;
    }
  } finally {
    // The consumer may bail mid-reply (barge-in): close the winner too.
    void win.racer.gen.return(undefined as unknown as StreamEvent).catch(() => {});
  }
}

export async function* streamWithFallback(
  attempts: Array<StreamAttempt>,
  timeoutMs: number,
): AsyncGenerator<StreamEvent> {
  if (attempts.length === 0) throw new Error('streamWithFallback: no attempts');
  let lastError: unknown;
  for (let i = 0; i < attempts.length; i++) {
    const { label, run } = attempts[i];
    const isLast = i === attempts.length - 1;
    let emittedFirst = false;
    let gen: AsyncGenerator<StreamEvent> | undefined;
    try {
      gen = run();
      while (true) {
        const step =
          !emittedFirst && !isLast
            ? await raceTimeout(gen.next(), timeoutMs)
            : await gen.next();
        if (step === TIMED_OUT) {
          lastError = new Error(`Model ${label} produced no first token in ${timeoutMs}ms`);
          logger.warn({ model: label, timeoutMs }, 'No first token in time; falling back');
          break; // abandon this attempt; try the next model
        }
        if (step.done) return;
        if (!emittedFirst && isFirstToken(step.value)) {
          emittedFirst = true;
          if (i > 0) logger.warn({ model: label }, 'Streaming via fallback model');
        }
        yield step.value;
      }
    } catch (error) {
      lastError = error;
      logger.warn(
        { model: label, error: (error as Error).message, emittedFirst, isLast },
        'Stream attempt failed',
      );
      if (emittedFirst || isLast) throw error; // committed, or out of options
      // otherwise fall through to the next attempt
    } finally {
      // Close an abandoned (uncommitted) stream so its request can clean up.
      if (gen && !emittedFirst) {
        try {
          await gen.return(undefined as unknown as StreamEvent);
        } catch {
          /* health-ok: best-effort close of an abandoned stream; the failed
             attempt itself was already logged above */
        }
      }
    }
  }
  throw lastError;
}
