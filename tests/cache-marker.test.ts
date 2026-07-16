import { describe, it, expect } from 'vitest';
import { SYSTEM_CACHE_MARKER } from '../src/types';
import { anthropicSystemParam, anthropicUsage, stripCacheMarker } from '../src/providers/base';
import { ContextBuilder } from '../src/agent/context';

describe('anthropicSystemParam', () => {
  it('splits at the marker and marks the stable prefix cacheable', () => {
    const sys = `PERSONA AND KNOWLEDGE${SYSTEM_CACHE_MARKER}Current time: now`;
    const out = anthropicSystemParam(sys);
    expect(Array.isArray(out)).toBe(true);
    const blocks = out as Array<Record<string, unknown>>;
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({
      type: 'text',
      text: 'PERSONA AND KNOWLEDGE',
      cache_control: { type: 'ephemeral' },
    });
    expect(blocks[1]).toMatchObject({ type: 'text', text: 'Current time: now' });
    expect(blocks[1]).not.toHaveProperty('cache_control');
  });

  it('passes marker-less prompts through unchanged', () => {
    expect(anthropicSystemParam('plain prompt')).toBe('plain prompt');
  });

  it('degrades to a plain volatile string when the stable part is empty', () => {
    expect(anthropicSystemParam(`${SYSTEM_CACHE_MARKER}tail only`)).toBe('tail only');
  });
});

describe('anthropicUsage', () => {
  it('folds cache read/write tokens back into promptTokens for cost telemetry', () => {
    const usage = anthropicUsage({
      input_tokens: 200,
      output_tokens: 50,
      cache_read_input_tokens: 10000,
      cache_creation_input_tokens: 0,
    });
    expect(usage.promptTokens).toBe(10200);
    expect(usage.totalTokens).toBe(10250);
    expect(usage.cacheReadTokens).toBe(10000);
    expect(usage).not.toHaveProperty('cacheWriteTokens');
  });

  it('is a no-op when caching is not in play', () => {
    const usage = anthropicUsage({ input_tokens: 300, output_tokens: 20 });
    expect(usage).toEqual({ promptTokens: 300, completionTokens: 20, totalTokens: 320 });
  });
});

describe('stripCacheMarker', () => {
  it('removes the marker for providers without prompt caching', () => {
    const sys = `head${SYSTEM_CACHE_MARKER}tail`;
    const clean = stripCacheMarker(sys);
    expect(clean).not.toContain('[[cache-breakpoint]]');
    expect(clean).toContain('head');
    expect(clean).toContain('tail');
  });
});

describe('ContextBuilder cache marker placement', () => {
  it('puts persona before the marker and the timestamp after it', () => {
    const builder = new ContextBuilder({
      model: 'test',
      systemPrompt: 'You are the persona.',
    } as any);
    const prompt = builder.buildSystemPrompt([], []);
    const marker = prompt.indexOf(SYSTEM_CACHE_MARKER);
    expect(marker).toBeGreaterThan(prompt.indexOf('You are the persona.'));
    expect(prompt.indexOf('Current time:')).toBeGreaterThan(marker);
  });
});
