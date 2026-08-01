/**
 * An unset environment variable must not take the whole config down.
 *
 * `run.sh` passes 76 variables as `-e VAR="$VAR"`, which sends an EMPTY STRING
 * when the host var is unset rather than omitting the variable. `Number('')` is
 * 0 and `Number.isInteger(0)` is true, so the guard that was written to reject
 * garbage accepted an empty string as a legitimate zero — and three of those
 * variables feed schema fields with a minimum.
 *
 * Result on 2026-07-29 22:40: `deepReasoning.threshold` arrived as 0, failed
 * `min(1)`, invalidated the entire config, the Node API never started, the
 * container died every three minutes, and nano.chattychapters.com served 502
 * until a human read `logs/voice_watchdog.ALERT`.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { createDefaultConfig, mergeEnvConfig } from '../src/config/index';
import { ConfigSchema } from '../src/config/schema';

const OWNED = [
  'NANO_CLAW_DEEP_THRESHOLD',
  'NANO_CLAW_DEEP_TIMEOUT_MS',
  'NANO_CLAW_INTELLIGENCE_TIMEOUT_MS',
  'NANO_CLAW_DEEP_REASONING',
  'NANO_CLAW_DEEP_ROUTING',
  'NANO_CLAW_ANALYSIS_STYLE',
] as const;

const saved = new Map<string, string | undefined>();
function setEnv(name: string, value: string | undefined) {
  if (!saved.has(name)) saved.set(name, process.env[name]);
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

afterEach(() => {
  for (const [name, value] of saved) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  saved.clear();
});

/** The exact shape run.sh produces for an unset host variable. */
function emptyLikeRunSh() {
  for (const name of OWNED) setEnv(name, '');
}

describe('numeric env vars that feed schema minimums', () => {
  it('survives every one of them arriving empty, the way run.sh sends them', () => {
    emptyLikeRunSh();

    const merged = mergeEnvConfig(createDefaultConfig());

    // The real regression test: the merged config must still VALIDATE. Before the
    // fix this threw, and a throw here is what killed the container.
    expect(() => ConfigSchema.parse(merged)).not.toThrow();
  });

  it('falls back to the documented default rather than zero', () => {
    setEnv('NANO_CLAW_DEEP_THRESHOLD', '');
    setEnv('NANO_CLAW_INTELLIGENCE_ENABLED', 'true');

    const merged = ConfigSchema.parse(mergeEnvConfig(createDefaultConfig()));
    const threshold = merged.agents?.defaults?.intelligence?.deepReasoning?.threshold;

    // Absent, not 0 — so the schema's own default(4) applies.
    expect(threshold === undefined || threshold >= 1).toBe(true);
    expect(threshold).not.toBe(0);
  });

  it('still honours a real value', () => {
    setEnv('NANO_CLAW_DEEP_THRESHOLD', '7');
    setEnv('NANO_CLAW_INTELLIGENCE_ENABLED', 'true');

    const merged = ConfigSchema.parse(mergeEnvConfig(createDefaultConfig()));
    expect(merged.agents?.defaults?.intelligence?.deepReasoning?.threshold).toBe(7);
  });

  it('still rejects garbage, which is what the original guard was for', () => {
    setEnv('NANO_CLAW_DEEP_THRESHOLD', 'not-a-number');
    setEnv('NANO_CLAW_INTELLIGENCE_ENABLED', 'true');

    const merged = ConfigSchema.parse(mergeEnvConfig(createDefaultConfig()));
    const threshold = merged.agents?.defaults?.intelligence?.deepReasoning?.threshold;
    expect(threshold).not.toBe(0);
    expect(threshold === undefined || threshold >= 1).toBe(true);
  });

  it('treats whitespace as absent too', () => {
    setEnv('NANO_CLAW_DEEP_TIMEOUT_MS', '   ');
    setEnv('NANO_CLAW_INTELLIGENCE_ENABLED', 'true');

    const merged = ConfigSchema.parse(mergeEnvConfig(createDefaultConfig()));
    const timeout = merged.agents?.defaults?.intelligence?.deepReasoning?.taskTimeoutMs;
    // taskTimeoutMs has min(1000); 0 would fail validation outright.
    expect(timeout === undefined || timeout >= 1000).toBe(true);
  });

  it('an empty value does not by itself invent an intelligence override', () => {
    // hasDeepOverride gates whether a deepReasoning block is synthesised at all.
    // An empty string must not count as "the operator asked for something".
    emptyLikeRunSh();
    const merged = mergeEnvConfig(createDefaultConfig());
    const base = createDefaultConfig();
    expect(merged.agents?.defaults?.intelligence?.deepReasoning)
      .toEqual(base.agents?.defaults?.intelligence?.deepReasoning);
  });
});
