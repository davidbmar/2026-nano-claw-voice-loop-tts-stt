import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AgentLoop } from '../src/agent/loop';
import { createDefaultConfig, mergeEnvConfig, requireExplicitBoolean } from '../src/config/index';
import { ConfigSchema } from '../src/config/schema';
import type { ToolDefinition } from '../src/types';

const TOOL_ENV_NAMES = ['NANO_CLAW_ENABLE_TOOLS', 'NANO_CLAW_DISABLE_TOOLS'] as const;
const originalToolEnv = new Map(TOOL_ENV_NAMES.map((name) => [name, process.env[name]] as const));
const originalHome = process.env.HOME;
let testHome: string;

function restoreEnv(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

beforeEach(() => {
  for (const name of TOOL_ENV_NAMES) delete process.env[name];
  testHome = mkdtempSync(join(tmpdir(), 'nano-claw-tool-gate-'));
  process.env.HOME = testHome;
});

afterEach(() => {
  for (const name of TOOL_ENV_NAMES) restoreEnv(name, originalToolEnv.get(name));
  restoreEnv('HOME', originalHome);
  rmSync(testHome, { recursive: true, force: true });
  vi.resetModules();
});

async function apiRegistryToolNames(): Promise<string[]> {
  vi.resetModules();
  const [{ Memory }, { __setProviderManagerForTest, stepLoopStream }] = await Promise.all([
    import('../src/agent/memory'),
    import('../src/api/server'),
  ]);

  let registeredNames: string[] | undefined;
  __setProviderManagerForTest({
    async *completeStream(
      _messages: unknown[],
      _model: string,
      _temperature?: number,
      _maxTokens?: number,
      tools: ToolDefinition[] = []
    ) {
      registeredNames = tools.map((tool) => tool.function.name);
      yield { type: 'text', delta: 'ok' };
      yield { type: 'done', finishReason: 'stop' };
    },
  });

  const memory = new Memory('tool-gate-api');
  memory.addMessage({ role: 'user', content: 'hello' });
  for await (const _event of stepLoopStream(memory, { model: 'test/model' }, 0)) {
    // Consume the stream so the provider observes createToolRegistry's output.
  }

  if (!registeredNames) throw new Error('Fake provider did not receive tool definitions');
  return registeredNames;
}

describe('fail-closed tool gate', () => {
  it('strictly parses the documented boolean matrix', () => {
    const cases: {
      value: string | undefined;
      expected: boolean | 'throws';
    }[] = [
      { value: '1', expected: true },
      { value: 'true', expected: true },
      { value: 'yes', expected: true },
      { value: 'on', expected: true },
      { value: '0', expected: false },
      { value: 'false', expected: false },
      { value: 'no', expected: false },
      { value: 'off', expected: false },
      { value: '', expected: false },
      { value: undefined, expected: false },
      { value: 'TRUE ', expected: 'throws' },
      { value: 'Yes', expected: true },
      { value: 'maybe', expected: 'throws' },
    ];

    for (const testCase of cases) {
      const parse = () => requireExplicitBoolean('TEST_DANGEROUS_CAPABILITY', testCase.value);
      if (testCase.expected === 'throws') {
        expect(parse, JSON.stringify(testCase.value)).toThrow(/TEST_DANGEROUS_CAPABILITY/);
      } else {
        expect(parse(), JSON.stringify(testCase.value)).toBe(testCase.expected);
      }
    }
  });

  it('registers zero API tools when NANO_CLAW_ENABLE_TOOLS is unset', async () => {
    expect(await apiRegistryToolNames()).toEqual([]);
  });

  it('rejects a conflicting legacy kill switch and positive enable at startup', () => {
    process.env.NANO_CLAW_ENABLE_TOOLS = 'true';
    process.env.NANO_CLAW_DISABLE_TOOLS = 'true';

    expect(() => mergeEnvConfig(createDefaultConfig())).toThrow(
      /NANO_CLAW_ENABLE_TOOLS and NANO_CLAW_DISABLE_TOOLS conflict/
    );
  });

  it('keeps an explicit disabled tool gate in the real Docker config fixture', () => {
    const raw = JSON.parse(
      readFileSync(new URL('../docker/default-config.json', import.meta.url), 'utf-8')
    ) as { tools?: Record<string, unknown> };

    expect(raw.tools).toHaveProperty('enabled', false);
    expect(ConfigSchema.parse(raw).tools.enabled).toBe(false);
  });

  it('keeps AgentLoop registration aligned with the API registry resolved state', async () => {
    const states = [
      { enable: 'false', disable: undefined, expected: [] },
      {
        enable: 'true',
        disable: undefined,
        expected: ['shell', 'read_file', 'write_file'],
      },
      { enable: undefined, disable: 'true', expected: [] },
    ] as const;

    for (const state of states) {
      restoreEnv('NANO_CLAW_ENABLE_TOOLS', state.enable);
      restoreEnv('NANO_CLAW_DISABLE_TOOLS', state.disable);

      const apiNames = await apiRegistryToolNames();
      const resolved = mergeEnvConfig(createDefaultConfig());
      const loopNames = new AgentLoop('tool-gate-loop', resolved)
        .getToolRegistry()
        .getDefinitions()
        .map((tool) => tool.function.name);

      expect(apiNames).toEqual(state.expected);
      expect(loopNames).toEqual(apiNames);
    }
  });
});
