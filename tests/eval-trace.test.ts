import { describe, expect, it } from 'vitest';
import {
  CROSS_SOURCE_EVAL_TRACE_VERSION,
  buildCrossSourceEvalTrace,
  segmentFastClaims,
} from '../src/agent/eval-trace';
import type { DeepReasoningResult } from '../src/agent/deep-reasoning';
import type { IntelligenceConfig } from '../src/types';

const intelligence: IntelligenceConfig = {
  enabled: true,
  apiUrl: 'http://127.0.0.1:8000',
  tenantId: 'eval-tenant',
  principalId: 'eval-runner',
  collectionIds: ['nano-claw-code'],
  limit: 5,
  candidatePool: 40,
  maxChars: 16000,
  timeoutMs: 750,
  groundingMode: 'strict',
  deepReasoning: {
    enabled: true,
    routingMode: 'auto',
    threshold: 4,
    acknowledgement: 'Thinking.',
    maxSteps: 6,
    maxRetrievalQueries: 10,
    pollIntervalMs: 750,
    requestTimeoutMs: 5000,
    taskTimeoutMs: 240000,
    analysisStyle: 'topic_map',
  },
};

describe('cross-source evaluation trace', () => {
  it('segments factual fast-answer sentences without scoring the follow-up question', () => {
    expect(
      segmentFastClaims(
        'Request routing begins in handleChat. It then enters stepLoop for retrieval. Want more?'
      )
    ).toEqual(['Request routing begins in handleChat.', 'It then enters stepLoop for retrieval.']);
  });

  it('preserves file/span lineage and links fast claims to lexical evidence', () => {
    const trace = buildCrossSourceEvalTrace({
      route: 'fast',
      outcome: 'answered',
      response:
        'Request routing begins in handleChat and then enters stepLoop for evidence retrieval.',
      intelligence,
      affirmationPolicy: 'never',
      turnEvidence: {
        status: 'retrieved',
        durationMs: 2,
        groundingMode: 'strict',
        items: [
          {
            evidenceId: 'ev_route',
            citationId: 'cite_route',
            title: 'src/api/server.ts',
            sectionPath: ['handleChat'],
            sourceId: 'src_route',
            documentId: 'doc_route',
            sourceRef: 'eval://nano-claw-code/src/api/server.ts',
            charStart: 120,
            charEnd: 420,
            lineStart: 1040,
            lineEnd: 1100,
            text: 'handleChat adds the input and calls stepLoop, which retrieves turn evidence.',
            rank: 1,
          },
        ],
      },
    });

    expect(trace).toMatchObject({
      version: CROSS_SOURCE_EVAL_TRACE_VERSION,
      route: 'fast',
      outcome: 'answered',
      config: {
        collectionIds: ['nano-claw-code'],
        topK: 5,
        affirmationPolicy: 'never',
      },
    });
    expect(trace.evidence[0]).toMatchObject({
      citationId: 'cite_route',
      sourceRef: 'eval://nano-claw-code/src/api/server.ts',
      lineStart: 1040,
    });
    expect(trace.claims).toEqual([
      {
        text: 'Request routing begins in handleChat and then enters stepLoop for evidence retrieval.',
        evidenceIds: ['ev_route'],
        citationIds: ['cite_route'],
      },
    ]);
  });

  it('uses validated deep claim-to-evidence links without prose matching', () => {
    const deepResult: DeepReasoningResult = {
      status: 'succeeded',
      workflow: 'evidence_analysis',
      claims: [
        {
          claimId: 'claim_1',
          text: 'The API separates retrieval from deeper reasoning.',
          disposition: 'supported',
          evidenceIds: ['ev_1'],
        },
      ],
      evidence: [
        {
          evidenceId: 'ev_1',
          citationId: 'cite_1',
          title: 'src/api/server.ts',
          sectionPath: [],
          sourceRef: 'eval://nano-claw-code/src/api/server.ts',
          text: 'retrieveTurnEvidence is skipped when a deep result is available.',
        },
      ],
      modelUsage: [],
      durationMs: 5,
      completedSteps: 2,
      retrievalQueries: 1,
    };

    const trace = buildCrossSourceEvalTrace({
      route: 'deep',
      outcome: 'answered',
      response: 'A rendered answer that is deliberately unrelated to the scoring implementation.',
      intelligence,
      deepResult,
      affirmationPolicy: 'never',
    });

    expect(trace.claims).toEqual([
      {
        text: 'The API separates retrieval from deeper reasoning.',
        evidenceIds: ['ev_1'],
        citationIds: ['cite_1'],
      },
    ]);
  });
});
