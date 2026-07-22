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
          /* ignore cleanup errors */
        }
      }
    }
  }
  throw lastError;
}
