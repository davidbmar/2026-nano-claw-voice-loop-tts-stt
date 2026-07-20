import { describe, it, expect } from 'vitest';
import { resolveAgentProfile } from '../src/api/server';
import type { Config } from '../src/config/schema';

// The persona/profile selector (task 056): selecting "Replicant PM" vs
// "Space Channel" must swap BOTH the system prompt AND the site knowledge, and
// one persona's knowledge must never leak into another. `none` = a plain
// assistant with no site knowledge; unknown/absent = pre-profile behavior.

const SC_PROMPT = 'You are the Space Channel assistant.';
const RILEY_PROMPT = 'You are Riley, the Replicant PM assistant.';
const DEFAULT_PROMPT = 'default fallback persona';
const SC_KNOWLEDGE = '/app/sites/spacechannel/knowledge.md';
const RPM_KNOWLEDGE = '/app/sites/replicantpm/knowledge.md';

const cfg = {
  agents: {
    defaults: { systemPrompt: DEFAULT_PROMPT, knowledgeFiles: [SC_KNOWLEDGE] },
    profiles: {
      spacechannel: { label: 'Space Channel', systemPrompt: SC_PROMPT, knowledgeFiles: [SC_KNOWLEDGE] },
      replicantpm: { label: 'Replicant PM', systemPrompt: RILEY_PROMPT, knowledgeFiles: [RPM_KNOWLEDGE] },
    },
  },
} as unknown as Config;

describe('resolveAgentProfile', () => {
  it('selects a known profile’s own prompt and knowledge', () => {
    const r = resolveAgentProfile(cfg, 'replicantpm');
    expect(r.systemPrompt).toBe(RILEY_PROMPT);
    expect(r.knowledgeFiles).toEqual([RPM_KNOWLEDGE]);
  });

  it('isolates knowledge: the Replicant PM persona never sees Space Channel data', () => {
    const rpm = resolveAgentProfile(cfg, 'replicantpm');
    expect(rpm.knowledgeFiles).not.toContain(SC_KNOWLEDGE);
    const sc = resolveAgentProfile(cfg, 'spacechannel');
    expect(sc.systemPrompt).toBe(SC_PROMPT);
    expect(sc.knowledgeFiles).toEqual([SC_KNOWLEDGE]);
    expect(sc.knowledgeFiles).not.toContain(RPM_KNOWLEDGE);
  });

  it('none = default prompt, NO site knowledge', () => {
    const r = resolveAgentProfile(cfg, 'none');
    expect(r.systemPrompt).toBe(DEFAULT_PROMPT);
    expect(r.knowledgeFiles).toEqual([]);
  });

  it('unknown profile falls back to default prompt + global knowledge (back-compat)', () => {
    delete process.env.NANO_CLAW_KNOWLEDGE; // deterministic: config-only knowledge
    const r = resolveAgentProfile(cfg, 'no-such-profile');
    expect(r.systemPrompt).toBe(DEFAULT_PROMPT);
    expect(r.knowledgeFiles).toEqual([SC_KNOWLEDGE]);
  });

  it('undefined profile = pre-profile behavior (global knowledge preserved)', () => {
    delete process.env.NANO_CLAW_KNOWLEDGE;
    const r = resolveAgentProfile(cfg, undefined);
    expect(r.systemPrompt).toBe(DEFAULT_PROMPT);
    expect(r.knowledgeFiles).toEqual([SC_KNOWLEDGE]);
  });

  it('returns a copy — mutating the result cannot corrupt the profile registry', () => {
    const r = resolveAgentProfile(cfg, 'replicantpm');
    r.knowledgeFiles.push('/tmp/injected.md');
    const again = resolveAgentProfile(cfg, 'replicantpm');
    expect(again.knowledgeFiles).toEqual([RPM_KNOWLEDGE]);
  });
});
