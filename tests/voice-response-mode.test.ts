import { describe, expect, it } from 'vitest';

import { ContextBuilder } from '../src/agent/context';
import { SYSTEM_CACHE_MARKER } from '../src/types';

describe('voice response mode', () => {
  it('adds a spoken response contract after the cache breakpoint', () => {
    const prompt = new ContextBuilder({
      model: 'test',
      responseMode: 'voice',
    }).buildSystemPrompt([], []);

    expect(prompt).toContain('## Spoken response contract');
    expect(prompt).toContain('This answer will be heard, not read.');
    expect(prompt).toContain('Do not use markdown');
    expect(prompt.indexOf('## Spoken response contract')).toBeGreaterThan(
      prompt.indexOf(SYSTEM_CACHE_MARKER)
    );
  });

  it('does not constrain ordinary text clients to spoken prose', () => {
    const prompt = new ContextBuilder({ model: 'test' }).buildSystemPrompt([], []);

    expect(prompt).not.toContain('## Spoken response contract');
  });
});
