import { describe, expect, it, vi } from 'vitest';
import { ContextBuilder } from '../src/agent/context';
import {
  detectDeepQuestion,
  guardAnalysisVoiceResponse,
  resolveExistingAnalysisTurn,
  runDeepReasoning,
  streamDeepReasoning,
  type DeepReasoningResult,
} from '../src/agent/deep-reasoning';
import {
  createAnalysisConversationState,
  parseAnalysisArtifact,
} from '../src/agent/analysis-navigation';
import type { IntelligenceConfig, Message } from '../src/types';
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
  deepReasoning: {
    enabled: true,
    routingMode: 'auto',
    threshold: 4,
    acknowledgement: 'Let me think deeply about this.',
    maxSteps: 6,
    maxRetrievalQueries: 10,
    pollIntervalMs: 1,
    requestTimeoutMs: 1000,
    taskTimeoutMs: 10000,
    analysisStyle: 'topic_map',
  },
};

function user(content: string): Message[] {
  return [{ role: 'user', content }];
}

describe('deep question routing', () => {
  it('routes explicit and cross-section synthesis requests', () => {
    expect(
      detectDeepQuestion(user('Think deeply about why the proving phase matters.'), intelligence)
        .deep
    ).toBe(true);
    expect(
      detectDeepQuestion(
        user(
          'Compare proving and replication across the chapters, identify the trade-offs, and recommend a sequence.'
        ),
        intelligence
      ).deep
    ).toBe(true);
  });

  it('selects strategy review for judgment, but not for a direct strategy lookup', () => {
    const review = detectDeepQuestion(
      user('Critique this business plan, challenge its assumptions, and recommend what I do next.'),
      intelligence
    );
    expect(review).toMatchObject({ deep: true, workflow: 'strategy_review' });
    expect(review.reasons).toContain('strategy_review');

    const lookup = detectDeepQuestion(user('What is the pricing strategy?'), intelligence);
    expect(lookup).toMatchObject({ deep: false, workflow: 'evidence_analysis' });
  });

  it('uses recent strategy context for a judgmental follow-up', () => {
    const route = detectDeepQuestion(
      [
        { role: 'user', content: 'Explain the pricing strategy in this business plan.' },
        { role: 'assistant', content: 'It uses batches of qualified calls.' },
        { role: 'user', content: 'Do you think that is viable, and what should I change?' },
      ],
      intelligence
    );
    expect(route).toMatchObject({ deep: true, workflow: 'strategy_review' });
  });

  it('uses assistant strategy context for a short document-critique follow-up', () => {
    const route = detectDeepQuestion(
      [
        {
          role: 'assistant',
          content:
            'The document covers lead-generation economics, contractor pricing, and market strategy.',
        },
        { role: 'user', content: 'Tell me only the biggest weaknesses of the doc.' },
      ],
      intelligence
    );

    expect(route).toMatchObject({ deep: true, workflow: 'strategy_review' });
    expect(route.reasons).toContain('strategy_review');
  });

  it('routes a standalone document critique even without a known strategy subject', () => {
    const route = detectDeepQuestion(
      user('What are the biggest weaknesses of this document?'),
      intelligence
    );

    expect(route).toMatchObject({ deep: true, workflow: 'evidence_analysis' });
    expect(route.reasons).toContain('critical_analysis');
    expect(route.reasons).not.toContain('direct_lookup_shape');
  });

  it.each([
    ['What is the pricing strategy?', false, 'evidence_analysis'],
    ['What does the business model charge?', false, 'evidence_analysis'],
    ['How many phases are in the business plan?', false, 'evidence_analysis'],
    ['List the assumptions stated in the business plan.', false, 'evidence_analysis'],
    ['Is this a good business plan?', true, 'strategy_review'],
    ['Are these unit economics realistic?', true, 'strategy_review'],
    ['Stress-test the go-to-market strategy.', true, 'strategy_review'],
    ['Think deeply about whether this business plan is worth pursuing.', true, 'strategy_review'],
    ['Compare the growth plan options and recommend the best path.', true, 'strategy_review'],
  ])('classifies the strategy routing corpus: %s', (question, deep, workflow) => {
    expect(detectDeepQuestion(user(question), intelligence)).toMatchObject({ deep, workflow });
  });

  it('keeps direct document lookups on the fast path', () => {
    expect(detectDeepQuestion(user('What are the twelve phases?'), intelligence).deep).toBe(false);
    expect(detectDeepQuestion(user('Who owns the demand?'), intelligence).deep).toBe(false);
  });

  it('obeys never and always policy modes', () => {
    const never = {
      ...intelligence,
      deepReasoning: { ...intelligence.deepReasoning!, routingMode: 'never' as const },
    };
    const always = {
      ...intelligence,
      deepReasoning: { ...intelligence.deepReasoning!, routingMode: 'always' as const },
    };
    expect(detectDeepQuestion(user('Think deeply about this.'), never).deep).toBe(false);
    expect(detectDeepQuestion(user('What is this?'), always).deep).toBe(true);
  });
});

describe('deep reasoning task client', () => {
  it('submits, emits progress heartbeats, and parses the grounded result', async () => {
    const post = vi.fn().mockResolvedValue({
      data: {
        task_id: 'task_1',
        status: 'queued',
        progress: {
          phase: 'queued',
          message: 'Waiting for a reasoning worker.',
          completed_steps: 0,
          max_steps: 6,
          retrieval_queries: 0,
        },
      },
    });
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        data: {
          task_id: 'task_1',
          status: 'running',
          progress: {
            phase: 'reasoning',
            message: 'Analyzing retrieved evidence, pass 1 of up to 6.',
            completed_steps: 0,
            max_steps: 6,
            retrieval_queries: 5,
            reasoning: { current: 1, completed: 0, maximum: 6 },
            retrieval: { planned: 5, completed: 5, evidence_items: 19 },
            model: {
              provider: 'deepseek',
              model: 'deepseek-v4-pro',
              thinking: 'enabled',
              effort: 'high',
            },
            artifact: { status: 'not_applicable', artifact_id: null },
            phase_started_at: '2026-07-22T01:00:54Z',
            heartbeat_at: '2026-07-22T01:01:34Z',
          },
        },
      })
      .mockResolvedValueOnce({
      data: {
        task_id: 'task_1',
        status: 'succeeded',
        workflow: 'strategy_review',
        progress: {
          phase: 'completed',
          message: 'Deep analysis completed.',
          completed_steps: 2,
          max_steps: 6,
          retrieval_queries: 3,
          reasoning: { current: 2, completed: 2, maximum: 6 },
          retrieval: { planned: 3, completed: 3, evidence_items: 1 },
          artifact: { status: 'indexed', artifact_id: 'analysis_task_1' },
        },
        result: {
          workflow: 'strategy_review',
          answer: 'Proving validates demand before replication systematizes it.',
          analysis_artifact: analysisArtifactFixture('task_1'),
          snapshot: { snapshot_id: 'snapshot_1' },
          model_usage: [
            {
              provider: 'deepseek',
              model: 'deepseek-v4-pro',
              pass_number: 1,
              input_tokens: 100,
              cached_input_tokens: 20,
              output_tokens: 50,
              reasoning_tokens: 30,
              total_tokens: 150,
              duration_ms: 1000,
            },
          ],
          claims: [
            {
              claim_id: 'claim_sequence',
              text: 'The plan places proving demand before replication.',
              disposition: 'supported',
              evidence_ids: ['ev_1'],
            },
          ],
          evidence: [
            {
              evidence_id: 'ev_1',
              text: 'Replication turns a validated method into a system.',
              citation: {
                title: 'Owning the Demand',
                locator: { section_path: ['Replication'] },
              },
            },
          ],
        },
      },
      });
    const events = [];
    for await (const event of streamDeepReasoning(
      user('Critique the business plan strategy and recommend what to validate first.'),
      intelligence,
      undefined,
      { post, get }
    )) {
      events.push(event);
    }

    expect(events.filter((event) => event.type === 'progress')).toHaveLength(3);
    const reasoning = events.find(
      (event) => event.type === 'progress' && event.progress.phase === 'reasoning'
    );
    expect(reasoning).toMatchObject({
      type: 'progress',
      progress: {
        currentPass: 1,
        completedPasses: 0,
        maxPasses: 6,
        retrievalPlanned: 5,
        retrievalCompleted: 5,
        evidenceItems: 19,
        model: {
          provider: 'deepseek',
          name: 'deepseek-v4-pro',
          thinking: 'enabled',
          effort: 'high',
        },
        phaseStartedAt: '2026-07-22T01:00:54Z',
        heartbeatAt: '2026-07-22T01:01:34Z',
      },
    });
    const completed = events.find(
      (event) => event.type === 'progress' && event.progress.phase === 'completed'
    );
    expect(completed).toMatchObject({
      type: 'progress',
      progress: {
        artifactStatus: 'indexed',
        artifactId: 'analysis_task_1',
      },
    });
    const final = events.find((event) => event.type === 'result');
    expect(final).toMatchObject({
      type: 'result',
      result: {
        status: 'succeeded',
        workflow: 'strategy_review',
        completedSteps: 2,
        retrievalQueries: 3,
        claims: [{ evidenceIds: ['ev_1'] }],
        evidence: [{ title: 'Owning the Demand', sectionPath: ['Replication'] }],
        artifact: { artifactId: 'analysis_task_1' },
        modelUsage: [{ reasoningTokens: 30 }],
      },
    });
    if (final?.type !== 'result') throw new Error('missing final deep-analysis result');
    expect(final.result.artifact?.topics[0]).toMatchObject({ label: 'Acquisition risk' });
    expect(post.mock.calls[0][1]).toMatchObject({
      policy: { permissions: ['knowledge:retrieve', 'knowledge:reason'] },
      budget: { max_steps: 6, max_retrieval_queries: 10 },
      workflow: 'strategy_review',
      output: {
        format: 'structured_analysis',
        schema_name: 'analysis_artifact_v1',
        response_mode: 'progressive_voice',
        analysis_style: 'topic_map',
      },
      context: { source_policy: 'indexed_documents_only' },
    });
    expect(get.mock.calls[0][1].headers).toEqual({
      'X-Tenant-Id': 'personal',
      'X-Permissions': 'knowledge:reason',
    });
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('fails closed when task submission is unavailable', async () => {
    const result = await runDeepReasoning(
      user('Think deeply about this.'),
      intelligence,
      undefined,
      {
        post: vi.fn().mockRejectedValue(new Error('connection refused')),
        get: vi.fn(),
      }
    );

    expect(result).toMatchObject({ status: 'unavailable', errorCode: 'reasoning_unavailable' });
  });

  it('fails closed when an artifact does not belong to the returned task', async () => {
    const post = vi.fn().mockResolvedValue({
      data: {
        task_id: 'task_expected',
        status: 'succeeded',
        workflow: 'strategy_review',
        progress: {
          phase: 'completed',
          completed_steps: 1,
          max_steps: 1,
          retrieval_queries: 1,
        },
        result: {
          workflow: 'strategy_review',
          answer: 'A concise answer.',
          analysis_artifact: analysisArtifactFixture('task_wrong'),
          snapshot: { snapshot_id: 'snapshot_1' },
          claims: [
            {
              claim_id: 'claim_sequence',
              text: 'The plan places proving demand before replication.',
              disposition: 'supported',
              evidence_ids: ['ev_1'],
            },
          ],
          evidence: [
            {
              evidence_id: 'ev_1',
              text: 'Replication turns a validated method into a system.',
              citation: {
                title: 'Owning the Demand',
                locator: { section_path: ['Replication'] },
              },
            },
          ],
        },
      },
    });

    const result = await runDeepReasoning(
      user('Critique the business strategy and recommend what to validate.'),
      intelligence,
      undefined,
      { post, get: vi.fn() }
    );

    expect(result).toMatchObject({ status: 'failed', errorCode: 'invalid_analysis_artifact' });
  });

  it('reloads only active-topic source evidence without starting a new task', async () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_evidence'))!;
    const state = {
      ...createAnalysisConversationState(artifact, artifact.taskId),
      activeTopicId: 'topic_acquisition',
    };
    const post = vi.fn();
    const get = vi.fn().mockResolvedValue({
      data: {
        task_id: 'task_evidence',
        status: 'succeeded',
        workflow: 'strategy_review',
        progress: {
          phase: 'completed',
          completed_steps: 2,
          max_steps: 6,
          retrieval_queries: 3,
        },
        result: {
          workflow: 'strategy_review',
          answer: 'A concise compatibility answer.',
          analysis_artifact: analysisArtifactFixture('task_evidence'),
          snapshot: { snapshot_id: 'snapshot_1' },
          claims: [
            {
              claim_id: 'claim_sequence',
              text: 'The plan places proving demand before replication.',
              disposition: 'supported',
              evidence_ids: ['ev_1'],
            },
          ],
          evidence: [
            {
              evidence_id: 'ev_1',
              text: 'Replication turns a validated method into a system.',
              citation: {
                title: 'Owning the Demand',
                locator: { section_path: ['Replication'] },
              },
            },
            {
              evidence_id: 'ev_unrelated',
              text: 'An unrelated passage should not reach this follow-up.',
              citation: {
                title: 'Owning the Demand',
                locator: { section_path: ['Unrelated'] },
              },
            },
          ],
        },
      },
    });

    const turn = await resolveExistingAnalysisTurn(
      user('What evidence supports that?'),
      state,
      intelligence,
      undefined,
      { post, get }
    );

    expect(turn?.decision).toMatchObject({
      action: 'show_evidence',
      selectedTopicIds: ['topic_acquisition'],
    });
    expect(turn?.result?.presentation?.mode).toBe('evidence');
    expect(turn?.result?.evidence.map((item) => item.evidenceId)).toEqual(['ev_1']);
    expect(post).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledOnce();
    expect(get.mock.calls[0][1].headers).toEqual({
      'X-Tenant-Id': 'personal',
      'X-Permissions': 'knowledge:reason',
    });
  });

  it('searches only the active artifact for an analysis-oriented follow-up', async () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_memory'))!;
    const state = createAnalysisConversationState(artifact, artifact.taskId);
    const post = vi.fn().mockResolvedValue({
      data: {
        matches: [
          {
            normalized_score: 1,
            node: {
              artifact_id: artifact.artifactId,
              tenant_id: artifact.tenantId,
              kind: 'finding',
              ref_id: 'finding_gate_scaling',
              topic_ids: ['topic_acquisition'],
            },
          },
        ],
      },
    });
    const get = vi.fn();

    const turn = await resolveExistingAnalysisTurn(
      user('What would change your recommendation?'),
      state,
      intelligence,
      undefined,
      { post, get }
    );

    expect(turn?.decision).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_acquisition'],
      reason: 'artifact_node_search',
    });
    expect(turn?.result?.presentation).toMatchObject({
      mode: 'topic',
      selectedTopicIds: ['topic_acquisition'],
    });
    expect(post).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/v1/analysis/search',
      expect.objectContaining({
        artifact_id: artifact.artifactId,
        policy: expect.objectContaining({ permissions: ['knowledge:reason'] }),
      }),
      expect.objectContaining({ timeout: 1000 })
    );
    expect(get).not.toHaveBeenCalled();
  });

  it('repeats the top artifact topics for a request for the biggest weaknesses', async () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_overview'))!;
    const post = vi.fn();

    const turn = await resolveExistingAnalysisTurn(
      user('Tell me the biggest weaknesses of the document.'),
      createAnalysisConversationState(artifact, artifact.taskId),
      intelligence,
      undefined,
      { post, get: vi.fn() }
    );

    expect(turn?.decision).toMatchObject({
      action: 'list_topics',
      selectedTopicIds: artifact.topics.slice(0, 3).map((topic) => topic.topicId),
      reason: 'analysis_overview',
    });
    expect(turn?.result?.presentation?.mode).toBe('menu');
    expect(post).not.toHaveBeenCalled();
  });

  it('allows document wording in a semantic artifact search', async () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_document_search'))!;
    const post = vi.fn().mockResolvedValue({
      data: {
        matches: [
          {
            normalized_score: 0.9,
            node: {
              artifact_id: artifact.artifactId,
              tenant_id: artifact.tenantId,
              kind: 'finding',
              ref_id: 'finding_gate_scaling',
              topic_ids: ['topic_acquisition'],
            },
          },
        ],
      },
    });

    const turn = await resolveExistingAnalysisTurn(
      user('What conclusion did this document reach about defensibility?'),
      createAnalysisConversationState(artifact, artifact.taskId),
      intelligence,
      undefined,
      { post, get: vi.fn() }
    );

    expect(turn?.decision).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_acquisition'],
      reason: 'artifact_node_search',
    });
    expect(post).toHaveBeenCalledOnce();
  });

  it('does not search analysis memory for an unrelated source-fact lookup', async () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_source'))!;
    const post = vi.fn();

    const turn = await resolveExistingAnalysisTurn(
      user('How many phases are named in the document?'),
      createAnalysisConversationState(artifact, artifact.taskId),
      intelligence,
      undefined,
      { post, get: vi.fn() }
    );

    expect(turn).toBeUndefined();
    expect(post).not.toHaveBeenCalled();
  });
});

describe('deep result naturalization', () => {
  it('replaces an overlong initial naturalization before it can reach audio', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const result: DeepReasoningResult = {
      status: 'succeeded',
      workflow: 'strategy_review',
      taskId: artifact.taskId,
      answer:
        'Validate repeatable demand before investing in replication. We can explore Acquisition risk, Pricing economics, or Validation plan.',
      claims: [],
      evidence: [],
      artifact,
      presentation: {
        mode: 'brief',
        selectedTopicIds: artifact.topics.slice(0, 3).map((topic) => topic.topicId),
        reason: 'completed_deep_analysis',
      },
      modelUsage: [],
      durationMs: 0,
      completedSteps: 1,
      retrievalQueries: 5,
    };

    const guarded = guardAnalysisVoiceResponse(Array(70).fill('excess').join(' '), result);

    expect(guarded).toMatchObject({ limit: 65, replaced: true });
    expect(guarded.text).toBe(
      'Validate repeatable demand before investing in replication. Which should we explore first: Acquisition risk, Pricing economics, or Validation plan?'
    );
    expect(guarded.text.trim().split(/\s+/).length).toBeLessThanOrEqual(65);
  });

  it('repairs a short artifact brief that trails off without a navigation question', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const result: DeepReasoningResult = {
      status: 'succeeded',
      workflow: 'strategy_review',
      taskId: artifact.taskId,
      claims: [],
      evidence: [],
      artifact,
      presentation: {
        mode: 'brief',
        selectedTopicIds: artifact.topics.slice(0, 3).map((topic) => topic.topicId),
        reason: 'completed_deep_analysis',
      },
      modelUsage: [],
      durationMs: 0,
      completedSteps: 1,
      retrievalQueries: 5,
    };

    const guarded = guardAnalysisVoiceResponse(
      'Validate demand first. Acquisition risk. Pricing economics. Validation plan.',
      result
    );

    expect(guarded.replaced).toBe(true);
    expect(guarded.text.endsWith('?')).toBe(true);
    expect(guarded.text).toContain('Which should we explore first');
  });

  it('places validated claims after the stable cache marker and forbids new facts', () => {
    const prompt = new ContextBuilder({ model: 'fast-model' }).buildSystemPrompt(
      [],
      [],
      undefined,
      {
        status: 'succeeded',
        workflow: 'evidence_analysis',
        taskId: 'task_1',
        answer: 'Proving precedes replication.',
        claims: [
          {
            text: 'Proving precedes replication.',
            disposition: 'supported',
            evidenceIds: ['ev_1'],
          },
        ],
        evidence: [
          {
            evidenceId: 'ev_1',
            title: 'Owning the Demand',
            sectionPath: ['Sequence'],
            text: 'First prove demand, then replicate the method.',
          },
        ],
        modelUsage: [],
        durationMs: 100,
        completedSteps: 2,
        retrievalQueries: 2,
      }
    );

    expect(prompt).toContain('Completed deep analysis');
    expect(prompt.toLowerCase()).toContain('do not add factual claims');
    expect(prompt.indexOf('Proving precedes replication.')).toBeGreaterThan(
      prompt.indexOf('[[cache-breakpoint]]')
    );
  });

  it('exposes only the bottom line and first three topic previews in the initial voice brief', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const prompt = new ContextBuilder({ model: 'fast-model' }).buildSystemPrompt(
      [],
      [],
      undefined,
      {
        status: 'succeeded',
        workflow: 'strategy_review',
        taskId: artifact.taskId,
        claims: [],
        evidence: [],
        artifact,
        modelUsage: [],
        durationMs: 100,
        completedSteps: 2,
        retrievalQueries: 2,
      }
    );

    expect(prompt).toContain('Validate repeatable demand before investing in replication.');
    expect(prompt).toContain('Acquisition risk:');
    expect(prompt).toContain('Pricing economics:');
    expect(prompt).toContain('Validation plan:');
    expect(prompt).toContain('end by explicitly asking which topic');
    expect(prompt).not.toContain('Market positioning:');
    expect(prompt).not.toContain('The plan should measure a repeatable channel before scaling it.');
    expect(prompt).not.toContain('What is measured acquisition cost by channel?');
  });

  it('permits evidence-based critique without treating an inference as a quoted fact', () => {
    const prompt = new ContextBuilder({ model: 'fast-model' }).buildSystemPrompt([], [], {
      status: 'retrieved',
      groundingMode: 'strict',
      items: [
        {
          rank: 1,
          evidenceId: 'ev_1',
          citationId: 'cite_1',
          title: 'Plan',
          sectionPath: ['Economics'],
          text: 'The plan assumes acquisition cost remains fixed.',
        },
      ],
      durationMs: 1,
    });

    expect(prompt).toContain('you may draw reasoned conclusions');
    expect(prompt).toContain('rather than claiming the document explicitly states them');
  });

  it('exposes only the selected topic when navigating an existing analysis', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const prompt = new ContextBuilder({ model: 'fast-model' }).buildSystemPrompt(
      [],
      [],
      undefined,
      {
        status: 'succeeded',
        workflow: 'strategy_review',
        taskId: artifact.taskId,
        claims: [],
        evidence: [],
        artifact,
        presentation: {
          mode: 'topic',
          selectedTopicIds: ['topic_economics'],
          reason: 'ordinal_menu_selection',
        },
        modelUsage: [],
        durationMs: 0,
        completedSteps: 0,
        retrievalQueries: 0,
      }
    );

    expect(prompt).toContain('Measure price, acquisition cost, and margin in the same experiment.');
    expect(prompt).not.toContain('The plan should measure a repeatable channel before scaling it.');
    expect(prompt).not.toContain('Test whether the narrower promise improves qualified demand.');
    expect(prompt).toContain('making these moves in order');
    expect(prompt).toContain('Next topics to offer:');
    expect(prompt).toContain(
      'Acquisition risk: The repeatable acquisition channel remains unproven.'
    );
    expect(prompt).toContain('Validation plan: Run a bounded paid-demand test before replication.');
    expect(prompt).not.toContain('Market positioning:');
  });
});
