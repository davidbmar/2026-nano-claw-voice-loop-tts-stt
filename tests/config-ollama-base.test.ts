/**
 * The ollama endpoint is deployment-specific, so it lives in env rather than
 * baked into config.json inside a running container.
 *
 * The empty-string case is not hypothetical: run.sh forwards ~82 variables as
 * `-e VAR="$VAR"`, which sends an EMPTY STRING when the host var is unset
 * rather than omitting the variable (see config-env-numbers.test.ts for the
 * outage this caused on 2026-07-29). An empty NANO_CLAW_OLLAMA_BASE must fall
 * through to config.json, never overwrite a working endpoint with "".
 */
import { afterEach, describe, expect, it } from 'vitest';

import { createDefaultConfig, mergeEnvConfig } from '../src/config/index';

const HOST_DEFAULT = 'http://host.docker.internal:11434/v1';

function configWithOllama(apiBase: string) {
  const config = createDefaultConfig();
  (config.providers as Record<string, unknown>).ollama = { apiKey: 'ollama', apiBase };
  return config;
}

afterEach(() => {
  delete process.env.NANO_CLAW_OLLAMA_BASE;
});

describe('NANO_CLAW_OLLAMA_BASE', () => {
  it('overrides the configured endpoint so local models can run on another host', () => {
    process.env.NANO_CLAW_OLLAMA_BASE = 'http://192.168.86.29:11434/v1';
    const merged = mergeEnvConfig(configWithOllama(HOST_DEFAULT));
    const ollama = (merged.providers as Record<string, { apiBase?: string }>).ollama;
    expect(ollama.apiBase).toBe('http://192.168.86.29:11434/v1');
  });

  it('keeps the configured endpoint when unset', () => {
    const merged = mergeEnvConfig(configWithOllama(HOST_DEFAULT));
    const ollama = (merged.providers as Record<string, { apiBase?: string }>).ollama;
    expect(ollama.apiBase).toBe(HOST_DEFAULT);
  });

  it('keeps the configured endpoint when forwarded as an empty string', () => {
    process.env.NANO_CLAW_OLLAMA_BASE = '';
    const merged = mergeEnvConfig(configWithOllama(HOST_DEFAULT));
    const ollama = (merged.providers as Record<string, { apiBase?: string }>).ollama;
    expect(ollama.apiBase).toBe(HOST_DEFAULT);
  });

  it('preserves the existing apiKey rather than replacing the provider entry', () => {
    process.env.NANO_CLAW_OLLAMA_BASE = 'http://192.168.86.29:11434/v1';
    const merged = mergeEnvConfig(configWithOllama(HOST_DEFAULT));
    const ollama = (merged.providers as Record<string, { apiKey?: string }>).ollama;
    expect(ollama.apiKey).toBe('ollama');
  });
});
