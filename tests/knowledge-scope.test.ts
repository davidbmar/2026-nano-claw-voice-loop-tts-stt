import { existsSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createAnalysisConversationState,
  parseAnalysisArtifact,
} from '../src/agent/analysis-navigation';
import { retrieveTurnEvidence } from '../src/agent/intelligence';
import {
  collectionScopeKey,
  matchCollectionScopeIntent,
  prepareCollectionScopeTurn,
} from '../src/agent/knowledge-scope';
import { Memory, sweepEphemeralMemory } from '../src/agent/memory';
import { getMemoryDir } from '../src/utils/helpers';
import type { AgentConfig, IntelligenceConfig, Message } from '../src/types';
import { analysisArtifactFixture } from './fixtures/analysis-artifact';

const intelligence: IntelligenceConfig = {
  enabled: true,
  apiUrl: 'http://127.0.0.1:8000',
  tenantId: 'personal',
  principalId: 'nano-claw-test',
  collectionIds: ['owning-the-demand'],
  limit: 5,
  candidatePool: 40,
  maxChars: 16000,
  timeoutMs: 750,
  groundingMode: 'strict',
};

const catalog = {
  tenant_id: 'personal',
  collections: [
    {
      collection_id: 'owning-the-demand',
      display_name: 'Owning the Demand',
      document_count: 1,
    },
    {
      collection_id: 'nano-claw-code',
      display_name: 'Nano Claw Code',
      document_count: 220,
    },
    {
      collection_id: 'riff-design',
      display_name: 'Riff Design',
      document_count: 2,
    },
  ],
  fuzzy_match_threshold: 0.68,
  ambiguity_margin: 0.08,
};

function user(content: string): Message[] {
  return [{ role: 'user', content }];
}

function config(profile = 'intelligence'): AgentConfig {
  return {
    model: 'test-model',
    intelligence,
    intelligenceScopeKey: collectionScopeKey(intelligence, profile),
  };
}

describe('collection scope intent matching', () => {
  it.each([
    ['what can we talk about?', { action: 'available' }],
    ["what's loaded", { action: 'loaded' }],
    [
      'load the riff design and the nano-claw code',
      {
        action: 'load',
        target: 'the riff design and the nano-claw code',
      },
    ],
    ['talk about Owning the Demand', { action: 'load', target: 'Owning the Demand' }],
    ['add Nano Claw Code to the scope', { action: 'add', target: 'Nano Claw Code' }],
    ['drop riff design from the loaded set', { action: 'drop', target: 'riff design' }],
  ])('parses %s', (text, expected) => {
    expect(matchCollectionScopeIntent(text)).toEqual(expected);
  });

  it('does not capture an ordinary knowledge question', () => {
    expect(matchCollectionScopeIntent('How does the routing architecture work?')).toBeUndefined();
  });
});

describe('session-scoped collection selection', () => {
  const originalHome = process.env.HOME;
  let testHome: string;

  beforeEach(() => {
    testHome = mkdtempSync(join(tmpdir(), 'nano-claw-scope-'));
    process.env.HOME = testHome;
  });

  afterEach(() => {
    if (originalHome === undefined) delete process.env.HOME;
    else process.env.HOME = originalHome;
    rmSync(testHome, { recursive: true, force: true });
  });

  it('lists, loads, adds, drops, and fails closed after dropping the last collection', async () => {
    const memory = new Memory('scope-verbs');
    const agentConfig = config();
    const get = vi.fn().mockResolvedValue({ data: catalog });
    const http = { get } as any;

    const available = await prepareCollectionScopeTurn(
      user('what can we talk about?'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(available?.reply).toBe('Available: Owning the Demand, Nano Claw Code, and Riff Design.');

    const loaded = await prepareCollectionScopeTurn(
      user('load the riff design and the nano-claw code'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(loaded?.scope).toEqual({
      mode: 'selected',
      collectionIds: ['nano-claw-code', 'riff-design'],
    });

    const added = await prepareCollectionScopeTurn(
      user('add owning the demand'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(added?.scope.collectionIds).toEqual([
      'nano-claw-code',
      'owning-the-demand',
      'riff-design',
    ]);

    const droppedTwo = await prepareCollectionScopeTurn(
      user('drop riff design and owning the demand'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(droppedTwo?.scope.collectionIds).toEqual(['nano-claw-code']);

    const droppedLast = await prepareCollectionScopeTurn(
      user('drop nano claw code'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(droppedLast?.scope).toEqual({ mode: 'none', collectionIds: [] });
    expect(droppedLast?.reply).toBe('Nothing is loaded.');

    get.mockClear();
    const blocked = await prepareCollectionScopeTurn(
      user('Explain the architecture.'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(blocked).toMatchObject({ action: 'none_loaded', scope: { mode: 'none' } });
    expect(blocked?.reply).toContain('Nothing is loaded');
    expect(get).not.toHaveBeenCalled();
  });

  it('fuzzy-matches names atomically and leaves scope unchanged on an unknown name', async () => {
    const memory = new Memory('scope-fuzzy');
    const agentConfig = config();
    const http = { get: vi.fn().mockResolvedValue({ data: catalog }) } as any;

    const fuzzy = await prepareCollectionScopeTurn(
      user('load the owning the demand playbook'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(fuzzy?.scope.collectionIds).toEqual(['owning-the-demand']);

    const unknown = await prepareCollectionScopeTurn(
      user('load Mars source code'),
      memory,
      agentConfig,
      undefined,
      http
    );
    expect(unknown?.reply).toContain("I don't have “Mars source code” loaded");
    expect(memory.getCollectionScope(agentConfig.intelligenceScopeKey!, [])).toEqual({
      mode: 'selected',
      collectionIds: ['owning-the-demand'],
    });
  });

  it('plumbs the active set into retrieval without mutating deployment config', async () => {
    const memory = new Memory('scope-retrieval');
    const agentConfig = config();
    const prepared = await prepareCollectionScopeTurn(
      user('load nano claw code'),
      memory,
      agentConfig,
      undefined,
      { get: vi.fn().mockResolvedValue({ data: catalog }) } as any
    );
    const post = vi.fn().mockResolvedValue({ data: { evidence: [] } });

    await retrieveTurnEvidence(
      user('Where is request routing implemented?'),
      prepared!.intelligence,
      { post } as any
    );

    expect(post.mock.calls[0][1].scope.collection_ids).toEqual(['nano-claw-code']);
    expect(intelligence.collectionIds).toEqual(['owning-the-demand']);
  });

  it('persists by tenant/profile and recovers safely from corrupt state', () => {
    const first = new Memory('scope-persist');
    const intelligenceKey = collectionScopeKey(intelligence, 'intelligence');
    const spaceKey = collectionScopeKey(intelligence, 'spacechannel');
    first.setCollectionScope(intelligenceKey, ['nano-claw-code']);

    const reloaded = new Memory('scope-persist');
    expect(reloaded.getCollectionScope(intelligenceKey, ['owning-the-demand'])).toEqual({
      mode: 'selected',
      collectionIds: ['nano-claw-code'],
    });
    expect(reloaded.getCollectionScope(spaceKey, ['owning-the-demand'])).toEqual({
      mode: 'default',
      collectionIds: ['owning-the-demand'],
    });
    expect(
      new Memory('other-session').getCollectionScope(intelligenceKey, ['riff-design'])
    ).toEqual({
      mode: 'default',
      collectionIds: ['riff-design'],
    });

    writeFileSync(
      join(getMemoryDir(), 'scope-corrupt.scope.json'),
      '{"version":1,"scopes":{"bad":{"mode":"selected","collectionIds":[42]}}}',
      'utf-8'
    );
    expect(
      new Memory('scope-corrupt').getCollectionScope(intelligenceKey, ['owning-the-demand'])
    ).toEqual({
      mode: 'default',
      collectionIds: ['owning-the-demand'],
    });
  });

  it('invalidates analysis and pending confirmation only for the changed profile', () => {
    const memory = new Memory('scope-invalidation');
    const firstKey = collectionScopeKey(intelligence, 'intelligence');
    const secondKey = collectionScopeKey(intelligence, 'spacechannel');
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const state = createAnalysisConversationState(artifact, artifact.taskId);
    const pending = {
      goal: 'Analyze the loaded code.',
      reflection: 'You want the loaded code analyzed.',
      workflow: 'strategy_review' as const,
      score: 5,
      reasons: ['strategy_review'],
    };
    memory.setAnalysisState(state, firstKey);
    memory.setPendingDeepRequest(pending, firstKey);

    expect(memory.getAnalysisState(secondKey)).toBeUndefined();
    expect(memory.getPendingDeepRequest(secondKey)).toBeUndefined();
    memory.setCollectionScope(secondKey, ['riff-design']);
    expect(memory.getAnalysisState(firstKey)).toBeDefined();
    expect(memory.getPendingDeepRequest(firstKey)).toEqual(pending);

    memory.setCollectionScope(firstKey, ['nano-claw-code']);
    expect(memory.getAnalysisState(firstKey)).toBeUndefined();
    expect(memory.getPendingDeepRequest(firstKey)).toBeUndefined();
  });

  it('clears, deletes, and sweeps scope sidecars with their session', () => {
    const key = collectionScopeKey(intelligence);
    const cleared = new Memory('scope-clear');
    cleared.setCollectionScope(key, ['nano-claw-code']);
    const clearPath = join(getMemoryDir(), 'scope-clear.scope.json');
    expect(existsSync(clearPath)).toBe(true);
    cleared.clear();
    expect(existsSync(clearPath)).toBe(false);

    const deleted = new Memory('scope-delete');
    deleted.setCollectionScope(key, ['nano-claw-code']);
    const deletePath = join(getMemoryDir(), 'scope-delete.scope.json');
    deleted.delete();
    expect(existsSync(deletePath)).toBe(false);

    const ephemeralId = `voice-${'e'.repeat(32)}`;
    const ephemeral = new Memory(ephemeralId);
    ephemeral.setCollectionScope(key, ['nano-claw-code']);
    const ephemeralPath = join(getMemoryDir(), `${ephemeralId}.scope.json`);
    const stale = new Date(Date.now() - 25 * 60 * 60 * 1000);
    utimesSync(ephemeralPath, stale, stale);
    sweepEphemeralMemory(new Set(), 24 * 60 * 60 * 1000);
    expect(existsSync(ephemeralPath)).toBe(false);
  });
});
