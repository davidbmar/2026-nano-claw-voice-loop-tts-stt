import { describe, it, expect } from 'vitest';
import { resolveAgentProfile } from '../src/api/server';
import type { Config } from '../src/config/schema';

// The persona/profile selector (task 056): selecting "Replicant PM" vs
// "Space Channel" must swap BOTH the system prompt AND the site knowledge, and
// one persona's knowledge must never leak into another. When a `base` profile
// is registered, every other known profile composes on top of it — persona
// prompt first, base identity layer beneath, base self-knowledge before the
// persona digest. `none` = a plain assistant with no site knowledge;
// unknown/absent = pre-profile behavior.

const BASE_PROMPT = 'You are an AI voice assistant.';
const SC_PROMPT = 'You are the Space Channel assistant.';
const RILEY_PROMPT = 'You are Riley, the Replicant PM assistant.';
const DEFAULT_PROMPT = 'default fallback persona';
const BASE_KNOWLEDGE = '/app/sites/base/knowledge.md';
const SC_KNOWLEDGE = '/app/sites/spacechannel/knowledge.md';
const RPM_KNOWLEDGE = '/app/sites/replicantpm/knowledge.md';

const cfg = {
  agents: {
    defaults: { systemPrompt: DEFAULT_PROMPT, knowledgeFiles: [SC_KNOWLEDGE] },
    profiles: {
      base: { label: 'Base', systemPrompt: BASE_PROMPT, knowledgeFiles: [BASE_KNOWLEDGE] },
      spacechannel: { label: 'Space Channel', systemPrompt: SC_PROMPT, knowledgeFiles: [SC_KNOWLEDGE] },
      replicantpm: { label: 'Replicant PM', systemPrompt: RILEY_PROMPT, knowledgeFiles: [RPM_KNOWLEDGE] },
    },
  },
} as unknown as Config;

describe('resolveAgentProfile', () => {
  it('composes a known profile on top of the base layer: persona prompt first, base beneath', () => {
    const r = resolveAgentProfile(cfg, 'replicantpm');
    expect(r.systemPrompt).toBe(`${RILEY_PROMPT}\n\n${BASE_PROMPT}`);
    expect(r.knowledgeFiles).toEqual([BASE_KNOWLEDGE, RPM_KNOWLEDGE]);
  });

  it('isolates persona knowledge: Replicant PM never sees Space Channel data (base is shared)', () => {
    const rpm = resolveAgentProfile(cfg, 'replicantpm');
    expect(rpm.knowledgeFiles).not.toContain(SC_KNOWLEDGE);
    const sc = resolveAgentProfile(cfg, 'spacechannel');
    expect(sc.systemPrompt).toBe(`${SC_PROMPT}\n\n${BASE_PROMPT}`);
    expect(sc.knowledgeFiles).toEqual([BASE_KNOWLEDGE, SC_KNOWLEDGE]);
    expect(sc.knowledgeFiles).not.toContain(RPM_KNOWLEDGE);
  });

  it('base itself does not self-compose', () => {
    const r = resolveAgentProfile(cfg, 'base');
    expect(r.systemPrompt).toBe(BASE_PROMPT);
    expect(r.knowledgeFiles).toEqual([BASE_KNOWLEDGE]);
  });

  it('composition dedupes a knowledge file listed by both base and the persona', () => {
    const shared = {
      agents: {
        defaults: { systemPrompt: DEFAULT_PROMPT },
        profiles: {
          base: { label: 'Base', systemPrompt: BASE_PROMPT, knowledgeFiles: [BASE_KNOWLEDGE] },
          twin: { label: 'Twin', systemPrompt: SC_PROMPT, knowledgeFiles: [BASE_KNOWLEDGE, SC_KNOWLEDGE] },
        },
      },
    } as unknown as Config;
    expect(resolveAgentProfile(shared, 'twin').knowledgeFiles).toEqual([
      BASE_KNOWLEDGE,
      SC_KNOWLEDGE,
    ]);
  });

  it('without a registered base profile, a known profile resolves unchanged (legacy configs)', () => {
    const legacy = {
      agents: {
        defaults: { systemPrompt: DEFAULT_PROMPT },
        profiles: {
          replicantpm: { label: 'Replicant PM', systemPrompt: RILEY_PROMPT, knowledgeFiles: [RPM_KNOWLEDGE] },
        },
      },
    } as unknown as Config;
    const r = resolveAgentProfile(legacy, 'replicantpm');
    expect(r.systemPrompt).toBe(RILEY_PROMPT);
    expect(r.knowledgeFiles).toEqual([RPM_KNOWLEDGE]);
  });

  it('none = default prompt, NO site knowledge, no base composition', () => {
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
    expect(again.knowledgeFiles).toEqual([BASE_KNOWLEDGE, RPM_KNOWLEDGE]);
  });
});
