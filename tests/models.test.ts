import { describe, it, expect } from 'vitest';
import { MODEL_CATALOG, modelsWithAvailability } from '../src/agent/models';

describe('model catalog', () => {
  it('marks a model available only when its provider key is present', () => {
    const cfg: any = { providers: { anthropic: { apiKey: 'x' }, gemini: {} } };
    const out = modelsWithAvailability(cfg);
    const byId = Object.fromEntries(out.map(m => [m.id, m.available]));
    expect(byId['anthropic/claude-haiku-4-5']).toBe(true);
    expect(byId['gemini/gemini-2.0-flash']).toBe(false); // key object present but no apiKey
    expect(byId['groq/llama-3.3-70b-versatile']).toBe(false); // provider absent
  });
  it('includes the deployed default model', () => {
    expect(MODEL_CATALOG.some(m => m.id === 'anthropic/claude-haiku-4-5')).toBe(true);
  });
});
