/**
 * Decision Core shadow integration: client fail-open guarantees, emergency
 * backstop, and the real MCP sidecar (skipped when the sibling
 * ai_constitution_engine checkout is absent, e.g. CI).
 */

import { describe, it, expect, afterAll, beforeAll } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DecisionCoreClient } from '../src/agent/decision-core-client';
import { emergencyBackstop } from '../src/agent/emergency-backstop';
import {
  configureDecisionShadow,
  decisionShadowEnabled,
  stopDecisionShadow,
} from '../src/agent/decision-shadow';
import { mergeEnvConfig } from '../src/config/index';
import { ConfigSchema } from '../src/config/schema';

const ENGINE_ROOT = join(process.env.HOME ?? '', 'src', 'ai_constitution_engine');
const engineAvailable = existsSync(join(ENGINE_ROOT, 'src', 'decision_core', 'mcp_server.py'));

describe('emergencyBackstop', () => {
  let fixtureRoot: string;

  beforeAll(() => {
    fixtureRoot = mkdtempSync(join(tmpdir(), 'dc-backstop-'));
    mkdirSync(join(fixtureRoot, 'policies', 'plumbing'), { recursive: true });
    writeFileSync(
      join(fixtureRoot, 'policies', 'plumbing', 'taxonomy.v1.json'),
      JSON.stringify({
        emergency_rules: [{ rule_id: 'EMG-001', terms: ['burst pipe', 'flooding'] }],
        escalation_rules: [{ rule_id: 'ESC-GAS', terms: ['smell gas'] }],
        negators: ['no', 'not'],
      }),
    );
  });

  afterAll(() => {
    rmSync(fixtureRoot, { recursive: true, force: true });
  });

  it('fires on emergency and escalation terms', () => {
    expect(emergencyBackstop('A burst pipe in my basement!', fixtureRoot)).toBe(true);
    expect(emergencyBackstop('I smell gas near the heater', fixtureRoot)).toBe(true);
  });

  it('respects same-clause negation and stays quiet on routine text', () => {
    expect(emergencyBackstop('there is no flooding here', fixtureRoot)).toBe(false);
    expect(emergencyBackstop('book me an appointment next week', fixtureRoot)).toBe(false);
  });

  it('returns false (not throws) when the taxonomy is missing', () => {
    expect(emergencyBackstop('a burst pipe!', join(fixtureRoot, 'nope'))).toBe(false);
  });
});

describe('DecisionCoreClient fail-open (§9B)', () => {
  it('decide() never rejects even when the sidecar cannot start', async () => {
    const client = new DecisionCoreClient('/path/that/does/not/exist', 100);
    // start() may reject — the caller (decision-shadow) handles that; decide()
    // on a dead client must still resolve with the fail-open default.
    await client.start().catch(() => undefined);
    const decision = await client.decide({
      request_id: 'r1',
      conversation_id: 'c1',
      current_message: 'hello',
    });
    expect(decision.degraded).toBe(true);
    expect(decision.policy_id).toBe('default.fail_open');
    client.stop();
  });
});

describe('decision shadow mode gating (fail-closed)', () => {
  it('is disabled by default, even when a root path is known', () => {
    stopDecisionShadow();
    expect(decisionShadowEnabled()).toBe(false);
    configureDecisionShadow({ shadowEnabled: false, root: ENGINE_ROOT });
    expect(decisionShadowEnabled()).toBe(false);
    stopDecisionShadow();
  });

  it('enables only on an explicit positive flag', () => {
    configureDecisionShadow({ shadowEnabled: true, root: ENGINE_ROOT });
    expect(decisionShadowEnabled()).toBe(true);
    stopDecisionShadow();
  });

  it('resolves the flag from config and NANO_CLAW_DECISION_SHADOW env', () => {
    const savedFlag = process.env.NANO_CLAW_DECISION_SHADOW;
    const savedRoot = process.env.DECISION_CORE_ROOT;

    delete process.env.NANO_CLAW_DECISION_SHADOW;
    delete process.env.DECISION_CORE_ROOT;
    const base = ConfigSchema.parse({});
    expect(mergeEnvConfig(base).decisionCore.shadowEnabled).toBe(false);

    process.env.NANO_CLAW_DECISION_SHADOW = 'true';
    process.env.DECISION_CORE_ROOT = '/some/engine/path';
    const merged = mergeEnvConfig(base);
    expect(merged.decisionCore.shadowEnabled).toBe(true);
    expect(merged.decisionCore.root).toBe('/some/engine/path');

    // Config-file flag works without any env vars.
    delete process.env.NANO_CLAW_DECISION_SHADOW;
    delete process.env.DECISION_CORE_ROOT;
    const fromFile = mergeEnvConfig(
      ConfigSchema.parse({ decisionCore: { shadowEnabled: true, root: '/cfg/path' } }),
    );
    expect(fromFile.decisionCore.shadowEnabled).toBe(true);
    expect(fromFile.decisionCore.root).toBe('/cfg/path');

    if (savedFlag !== undefined) process.env.NANO_CLAW_DECISION_SHADOW = savedFlag;
    if (savedRoot !== undefined) process.env.DECISION_CORE_ROOT = savedRoot;
  });
});

describe.skipIf(!engineAvailable)('DecisionCoreClient against the real sidecar', () => {
  let client: DecisionCoreClient;

  beforeAll(async () => {
    client = new DecisionCoreClient(ENGINE_ROOT);
    await client.start();
  }, 15000);

  afterAll(() => {
    client?.stop();
  });

  it('classifies a burst pipe as emergency with the shutoff playbook', async () => {
    const decision = await client.decide({
      request_id: 'req_it1',
      conversation_id: 'conv_it',
      current_message: 'A pipe burst in my basement!',
      available_actions: ['provide_safety_instructions', 'ask_clarifying_question'],
    });
    expect(decision.policy_id).toBe('plumbing.emergency');
    expect(decision.dimensions.urgency).toBe('critical');
    expect(decision.required_playbooks).toContain('plumbing.safety.water_shutoff.v1');
  });

  it('routes vague reports to clarify-first', async () => {
    const decision = await client.decide({
      request_id: 'req_it2',
      conversation_id: 'conv_it2',
      current_message: 'Something is wrong with my sink.',
      available_actions: ['provide_safety_instructions', 'ask_clarifying_question'],
    });
    expect(decision.policy_id).toBe('plumbing.ambiguous');
    expect(decision.required_actions).toContain('ask_clarifying_question');
  });

  it('records outcomes without throwing', async () => {
    await expect(
      client.recordOutcome({ decision_id: `dec_test_${Date.now()}`, outcome: { abandoned: false } }),
    ).resolves.toBeUndefined();
  });
});
