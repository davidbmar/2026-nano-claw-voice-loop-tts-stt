import { describe, it, expect } from 'vitest';
import { SYSTEM_CACHE_MARKER } from '../src/types';
import { anthropicSystemParam, stripCacheMarker } from '../src/providers/base';
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
