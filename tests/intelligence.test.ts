import { afterEach, describe, expect, it, vi } from 'vitest';
import { ContextBuilder } from '../src/agent/context';
import { retrieveTurnEvidence } from '../src/agent/intelligence';
import { createDefaultConfig, mergeEnvConfig } from '../src/config/index';
import type { IntelligenceConfig, Message } from '../src/types';
import { SYSTEM_CACHE_MARKER } from '../src/types';

const config: IntelligenceConfig = {
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

const messages: Message[] = [{ role: 'user', content: 'Why are exclusive leads valuable?' }];

afterEach(() => {
  delete process.env.NANO_CLAW_INTELLIGENCE_URL;
  delete process.env.NANO_CLAW_INTELLIGENCE_ENABLED;
  delete process.env.NANO_CLAW_INTELLIGENCE_TENANT;
  delete process.env.NANO_CLAW_INTELLIGENCE_COLLECTIONS;
  delete process.env.NANO_CLAW_INTELLIGENCE_PROFILE;
  delete process.env.NANO_CLAW_INTELLIGENCE_GROUNDING;
  delete process.env.NANO_CLAW_DEEP_REASONING;
  delete process.env.NANO_CLAW_DEEP_ROUTING;
  delete process.env.NANO_CLAW_DEEP_THRESHOLD;
  delete process.env.NANO_CLAW_DEEP_TIMEOUT_MS;
  delete process.env.NANO_CLAW_ANALYSIS_STYLE;
});

describe('retrieveTurnEvidence', () => {
  it('maps platform evidence and sends the policy-scoped request', async () => {
    const post = vi.fn().mockResolvedValue({
      data: {
        evidence: [
          {
            evidence_id: 'ev_1',
            text: 'Exclusive leads improve the buyer close rate.',
            citation: {
              citation_id: 'cite_1',
              title: 'Owning the Demand',
              locator: { section_path: ['Part I', 'A lead is a fish'] },
            },
            score: { rank: 1 },
          },
        ],
      },
    });

    const result = await retrieveTurnEvidence(messages, config, { post } as any);

    expect(result).toMatchObject({ status: 'retrieved', groundingMode: 'strict' });
    expect(result?.items[0]).toMatchObject({
      citationId: 'cite_1',
      title: 'Owning the Demand',
      sectionPath: ['Part I', 'A lead is a fish'],
    });
    expect(post).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/v1/retrieve',
      expect.objectContaining({
        text: 'Why are exclusive leads valuable?',
        policy: expect.objectContaining({
          tenant_id: 'personal',
          permissions: ['knowledge:retrieve'],
        }),
        scope: expect.objectContaining({ collection_ids: ['owning-the-demand'] }),
      }),
      { timeout: 750 }
    );
  });

  it('returns no_match for a valid empty evidence set', async () => {
    const post = vi.fn().mockResolvedValue({ data: { evidence: [] } });

    await expect(retrieveTurnEvidence(messages, config, { post } as any)).resolves.toMatchObject({
      status: 'no_match',
      items: [],
    });
  });

  it('returns unavailable instead of failing the conversation', async () => {
    const post = vi.fn().mockRejectedValue(new Error('connection refused'));

    await expect(retrieveTurnEvidence(messages, config, { post } as any)).resolves.toMatchObject({
      status: 'unavailable',
      items: [],
    });
  });

  it('adds the previous user turn only for a referential follow-up', async () => {
    const post = vi.fn().mockResolvedValue({ data: { evidence: [] } });
    await retrieveTurnEvidence(
      [
        { role: 'user', content: 'What are the first three phases?' },
        { role: 'assistant', content: 'There are three phases.' },
        { role: 'user', content: 'What about the next one?' },
      ],
      config,
      { post } as any
    );

    expect(post.mock.calls[0][1].text).toBe(
      'What are the first three phases?\nFollow-up: What about the next one?'
    );
  });
});

describe('ContextBuilder turn evidence', () => {
  it('places per-turn evidence after the stable cache marker', () => {
    const prompt = new ContextBuilder({
      model: 'test',
      intelligence: config,
    }).buildSystemPrompt([], [], {
      status: 'retrieved',
      groundingMode: 'strict',
      durationMs: 2,
      items: [
        {
          evidenceId: 'ev_1',
          citationId: 'cite_1',
          title: 'Owning the Demand',
          sectionPath: ['Part IV', 'Risks'],
          text: 'A copied site can lose its ranking.',
          rank: 1,
        },
      ],
    });

    expect(prompt.indexOf('A copied site can lose its ranking.')).toBeGreaterThan(
      prompt.indexOf(SYSTEM_CACHE_MARKER)
    );
    expect(prompt).toContain('do not read internal citation IDs aloud');
  });

  it('instructs strict profiles to abstain on no match', () => {
    const prompt = new ContextBuilder({ model: 'test' }).buildSystemPrompt([], [], {
      status: 'no_match',
      groundingMode: 'strict',
      durationMs: 1,
      items: [],
    });

    expect(prompt).toContain('document does not appear to cover it');
  });
});

describe('intelligence environment configuration', () => {
  it('enables and scopes retrieval when environment overrides are set', () => {
    process.env.NANO_CLAW_INTELLIGENCE_URL = 'http://127.0.0.1:8000';
    process.env.NANO_CLAW_INTELLIGENCE_TENANT = 'personal';
    process.env.NANO_CLAW_INTELLIGENCE_COLLECTIONS = 'owning-the-demand, notes';
    process.env.NANO_CLAW_INTELLIGENCE_GROUNDING = 'strict';

    const merged = mergeEnvConfig(createDefaultConfig());

    expect(merged.agents.defaults?.intelligence).toMatchObject({
      enabled: true,
      tenantId: 'personal',
      collectionIds: ['owning-the-demand', 'notes'],
      groundingMode: 'strict',
    });
  });

  it('enables bounded deep routing through environment overrides', () => {
    process.env.NANO_CLAW_INTELLIGENCE_URL = 'http://127.0.0.1:8000';
    process.env.NANO_CLAW_DEEP_REASONING = '1';
    process.env.NANO_CLAW_DEEP_ROUTING = 'auto';
    process.env.NANO_CLAW_DEEP_THRESHOLD = '5';
    process.env.NANO_CLAW_DEEP_TIMEOUT_MS = '90000';
    process.env.NANO_CLAW_ANALYSIS_STYLE = 'principle_graph';

    const merged = mergeEnvConfig(createDefaultConfig());

    expect(merged.agents.defaults?.intelligence?.deepReasoning).toMatchObject({
      enabled: true,
      routingMode: 'auto',
      threshold: 5,
      taskTimeoutMs: 90000,
      analysisStyle: 'principle_graph',
    });
  });

  it('grants intelligence only to the explicitly selected assistant profile', () => {
    process.env.NANO_CLAW_INTELLIGENCE_URL = 'http://127.0.0.1:8000';
    process.env.NANO_CLAW_INTELLIGENCE_COLLECTIONS = 'owning-the-demand';
    process.env.NANO_CLAW_INTELLIGENCE_PROFILE = 'intelligence';

    const base = createDefaultConfig();
    base.agents.profiles = {
      spacechannel: {
        label: 'Space Channel',
        systemPrompt: 'Space only',
        knowledgeFiles: ['/space.md'],
      },
      intelligence: {
        label: 'Document Intelligence',
        systemPrompt: 'Documents only',
        knowledgeFiles: [],
      },
    };
    const merged = mergeEnvConfig(base);

    expect(merged.agents.profiles.intelligence.intelligence).toMatchObject({
      enabled: true,
      collectionIds: ['owning-the-demand'],
    });
    expect(merged.agents.profiles.spacechannel.intelligence).toBeUndefined();
  });
});
