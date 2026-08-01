#!/usr/bin/env node

import http from 'node:http';

process.env.NANO_CLAW_DEEP_CONFIRM = 'never';
process.env.NANO_CLAW_EVAL_TRACE = '1';

const api = http.createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) body += chunk;
  response.setHeader('Content-Type', 'application/json');
  if (request.method === 'POST' && request.url === '/v1/analysis/search') {
    response.end(JSON.stringify({ matches: [] }));
    return;
  }
  if (request.method === 'POST' && request.url === '/v1/reasoning/tasks') {
    response.end(
      JSON.stringify({
        task_id: 'task_eval_invalid_artifact',
        status: 'succeeded',
        workflow: 'strategy_review',
        progress: {
          phase: 'completed',
          completed_steps: 2,
          max_steps: 6,
          retrieval_queries: 1,
        },
        result: {
          workflow: 'strategy_review',
          answer: 'This result must never be spoken.',
          snapshot: { snapshot_id: 'snapshot_eval_fault' },
          claims: [],
          evidence: [],
          analysis_artifact: { malformed: true },
        },
      })
    );
    return;
  }
  response.statusCode = 404;
  response.end(JSON.stringify({ error: 'not found' }));
});

await new Promise((resolve) => api.listen(0, '127.0.0.1', resolve));
const address = api.address();
const apiUrl = `http://127.0.0.1:${address.port}`;

try {
  const [{ Memory }, serverModule] = await Promise.all([
    import('../../dist/agent/memory.js'),
    import('../../dist/api/server.js'),
  ]);
  serverModule.__setProviderManagerForTest({
    async complete() {
      return {
        content: JSON.stringify({
          goal: 'Critique the business strategy.',
          reflection: 'You want a critique of the business strategy.',
          ambiguities: [],
        }),
        finishReason: 'stop',
      };
    },
  });

  const memory = new Memory(`voice-${'f'.repeat(32)}`);
  memory.addMessage({
    role: 'user',
    content: 'Think deeply and critique the business strategy for weaknesses.',
  });
  const intelligence = {
    enabled: true,
    apiUrl,
    tenantId: 'eval-fault-tenant',
    principalId: 'eval-fault-runner',
    collectionIds: ['fault-fixture'],
    limit: 5,
    candidatePool: 40,
    maxChars: 16000,
    timeoutMs: 1000,
    groundingMode: 'strict',
    deepReasoning: {
      enabled: true,
      routingMode: 'auto',
      threshold: 4,
      acknowledgement: 'Thinking.',
      maxSteps: 6,
      maxRetrievalQueries: 10,
      pollIntervalMs: 100,
      requestTimeoutMs: 1000,
      taskTimeoutMs: 5000,
      analysisStyle: 'topic_map',
    },
  };
  const events = [];
  for await (const event of serverModule.stepLoopStream(
    memory,
    {
      model: 'eval/stub',
      temperature: 0,
      maxTokens: 200,
      intelligence,
      intelligenceScopeKey: 'eval-fault-scope',
    },
    0,
    undefined,
    true
  )) {
    events.push(event);
  }
  const final = events.find((event) => event.type === 'final');
  const expectedFallback =
    "I'm sorry, I couldn't complete the deeper analysis just now. Please try again.";
  const result = {
    id: 'invalid-analysis-artifact',
    passed:
      final?.debug?.finishReason === 'invalid_analysis_artifact' &&
      final?.debug?.evalTrace?.errorCode === 'invalid_analysis_artifact' &&
      final?.debug?.evalTrace?.route === 'deep' &&
      final?.debug?.evalTrace?.outcome === 'fallback' &&
      final?.response === expectedFallback,
    errorCode: final?.debug?.evalTrace?.errorCode,
    finishReason: final?.debug?.finishReason,
    route: final?.debug?.evalTrace?.route,
    outcome: final?.debug?.evalTrace?.outcome,
    spokenFallbackMatched: final?.response === expectedFallback,
  };
  console.log(JSON.stringify(result));
  memory.delete();
  process.exitCode = result.passed ? 0 : 1;
} finally {
  await new Promise((resolve) => api.close(resolve));
}
