import { describe, it, expect, vi } from 'vitest';
import axios from 'axios';
import {
  BaseProvider,
  parseAnthropicEvents,
  parseOpenAIEvents,
  OpenAIProvider,
} from '../src/providers/base';
import type { Message, LLMResponse, ToolDefinition } from '../src/types';
import { Readable } from 'node:stream';
import { __setProviderManagerForTest, stepLoopStream, getAgentConfig } from '../src/api/server';
import { Memory } from '../src/agent/memory';
import {
  createAnalysisConversationState,
  parseAnalysisArtifact,
} from '../src/agent/analysis-navigation';
import { analysisArtifactFixture } from './fixtures/analysis-artifact';

class FakeProvider extends BaseProvider {
  protected getDefaultApiBase(): string {
    return 'http://example.invalid';
  }
  async complete(): Promise<LLMResponse> {
    return { content: 'Hello world.', finishReason: 'stop' };
  }
}

async function collect<T>(gen: AsyncGenerator<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

describe('BaseProvider.completeStream fallback', () => {
  it('yields the full content as one text event then done', async () => {
    const p = new FakeProvider('key');
    const events = await collect(p.completeStream([], 'm'));
    expect(events).toEqual([
      { type: 'text', delta: 'Hello world.' },
      { type: 'done', finishReason: 'stop', usage: undefined },
    ]);
  });
});

function sse(lines: string): Readable {
  return Readable.from([Buffer.from(lines)]);
}

describe('parseAnthropicEvents', () => {
  it('maps text_delta events to text StreamEvents and ends with done', async () => {
    const body =
      'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"there."}}\n\n' +
      'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n' +
      'event: message_stop\ndata: {"type":"message_stop"}\n\n';
    const out: any[] = [];
    for await (const e of parseAnthropicEvents(sse(body))) out.push(e);
    expect(out[0]).toEqual({ type: 'text', delta: 'Hi ' });
    expect(out[1]).toEqual({ type: 'text', delta: 'there.' });
    const done = out[out.length - 1];
    expect(done.type).toBe('done');
    expect(done.finishReason).toBe('end_turn');
    expect(done.usage).toEqual({ promptTokens: 5, completionTokens: 3, totalTokens: 8 });
  });

  it('assembles a tool_use block into a tool_calls event', async () => {
    const body =
      'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"shell","input":{}}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":"}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"ls\\"}"}}\n\n' +
      'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n' +
      'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n\n' +
      'event: message_stop\ndata: {"type":"message_stop"}\n\n';
    const out: any[] = [];
    for await (const e of parseAnthropicEvents(sse(body))) out.push(e);
    const toolEvt = out.find((e) => e.type === 'tool_calls');
    expect(toolEvt).toBeTruthy();
    expect(toolEvt.toolCalls[0]).toEqual({
      id: 't1',
      type: 'function',
      function: { name: 'shell', arguments: '{"cmd":"ls"}' },
    });
  });

  it('reassembles a text_delta whose multi-byte UTF-8 char and SSE frame are split across chunk reads', async () => {
    const body =
      'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n' +
      'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"canción"}}\n\n' +
      'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n' +
      'event: message_stop\ndata: {"type":"message_stop"}\n\n';

    const buf = Buffer.from(body, 'utf8');

    // Byte offset landing in the MIDDLE of the 2-byte 'ó' character.
    const charIdx = body.indexOf('ó');
    const oStartByte = Buffer.byteLength(body.slice(0, charIdx), 'utf8');
    const splitInsideChar = oStartByte + 1;

    // Byte offset landing in the MIDDLE of a later frame (not on a '\n\n' boundary).
    const messageDeltaIdx = body.indexOf('event: message_delta');
    const midFrameCharIdx = messageDeltaIdx + 10;
    const splitInsideFrame = Buffer.byteLength(body.slice(0, midFrameCharIdx), 'utf8');

    const stream = Readable.from([
      buf.subarray(0, splitInsideChar),
      buf.subarray(splitInsideChar, splitInsideFrame),
      buf.subarray(splitInsideFrame),
    ]);

    const out: any[] = [];
    for await (const e of parseAnthropicEvents(stream)) out.push(e);
    const textEvt = out.find((e) => e.type === 'text');
    expect(textEvt).toBeTruthy();
    expect(textEvt.delta).toBe('canción');
  });
});

describe('parseOpenAIEvents', () => {
  function sse(s: string) {
    return require('node:stream').Readable.from([Buffer.from(s)]);
  }

  it('assembles content deltas and ends on [DONE]', async () => {
    const body =
      'data: {"choices":[{"delta":{"content":"Hi "}}]}\n\n' +
      'data: {"choices":[{"delta":{"content":"there."},"finish_reason":null}]}\n\n' +
      'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n' +
      'data: [DONE]\n\n';
    const out: any[] = [];
    for await (const e of parseOpenAIEvents(sse(body))) out.push(e);
    expect(
      out
        .filter((e) => e.type === 'text')
        .map((e) => e.delta)
        .join('')
    ).toBe('Hi there.');
    const done = out.find((e) => e.type === 'done');
    expect(done.finishReason).toBe('stop');
    expect(done.usage).toEqual({ promptTokens: 5, completionTokens: 3, totalTokens: 8 });
  });

  it('assembles a streamed tool_call by index', async () => {
    const body =
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"shell","arguments":""}}]}}]}\n\n' +
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":"}}]}}]}\n\n' +
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n' +
      'data: [DONE]\n\n';
    const out: any[] = [];
    for await (const e of parseOpenAIEvents(sse(body))) out.push(e);
    const t = out.find((e) => e.type === 'tool_calls');
    expect(t.toolCalls[0]).toEqual({
      id: 'c1',
      type: 'function',
      function: { name: 'shell', arguments: '{"cmd":"ls"}' },
    });
  });
});

describe('OpenAIProvider.formatModelName', () => {
  it('strips any leading provider/ prefix', () => {
    const p = new OpenAIProvider('k');
    // formatModelName is protected; exercise via a tiny subclass
    const f = (m: string) => (p as any).formatModelName(m);
    expect(f('groq/llama-3.3-70b-versatile')).toBe('llama-3.3-70b-versatile');
    expect(f('gemini/gemini-2.0-flash')).toBe('gemini-2.0-flash');
    expect(f('gpt-4o-mini')).toBe('gpt-4o-mini');
    expect(f('meta-llama/Llama-3.1-8B-Instruct')).toBe('meta-llama/Llama-3.1-8B-Instruct'); // unknown prefix, untouched
    expect(f('dashscope/qwen-plus')).toBe('qwen-plus');
  });
});

describe('stepLoopStream', () => {
  it('forwards text deltas then a final event when there are no tool calls', async () => {
    __setProviderManagerForTest({
      async *completeStream() {
        yield { type: 'text', delta: 'Part one. ' };
        yield { type: 'text', delta: 'Part two.' };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);

    const mem = new Memory('test-stream');
    mem.addMessage({ role: 'user', content: 'hi' });
    const events: any[] = [];
    for await (const e of stepLoopStream(
      mem,
      { model: 'anthropic/x', temperature: 0.7, maxTokens: 100 } as any,
      0
    )) {
      events.push(e);
    }
    const texts = events
      .filter((e) => e.type === 'text')
      .map((e) => e.delta)
      .join('');
    expect(texts).toBe('Part one. Part two.');
    const final = events.find((e) => e.type === 'final');
    expect(final.response).toBe('Part one. Part two.');
  });

  it('emits deep lifecycle events before naturalizing a completed task result', async () => {
    const post = vi.spyOn(axios, 'post').mockResolvedValue({
      data: {
        task_id: 'task_stream',
        status: 'queued',
        progress: {
          phase: 'queued',
          message: 'Waiting for a reasoning worker.',
          completed_steps: 0,
          max_steps: 4,
          retrieval_queries: 0,
        },
      },
    } as any);
    const get = vi.spyOn(axios, 'get').mockResolvedValue({
      data: {
        task_id: 'task_stream',
        status: 'succeeded',
        progress: {
          phase: 'completed',
          message: 'Deep analysis completed.',
          completed_steps: 2,
          max_steps: 4,
          retrieval_queries: 2,
          reasoning: { current: 2, completed: 2, maximum: 4 },
          retrieval: { planned: 2, completed: 2, evidence_items: 1 },
          artifact: { status: 'indexed', artifact_id: 'analysis_task_stream' },
        },
        result: {
          answer:
            'Validate repeatable demand before investing in replication. We can explore Acquisition risk, Pricing economics, or Validation plan.',
          workflow: 'strategy_review',
          analysis_artifact: analysisArtifactFixture('task_stream'),
          snapshot: { snapshot_id: 'snapshot_1' },
          model_usage: [],
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
    } as any);
    __setProviderManagerForTest({
      async *completeStream() {
        yield { type: 'text', delta: Array(70).fill('excess').join(' ') };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);

    try {
      const mem = new Memory('test-deep-stream');
      mem.addMessage({ role: 'user', content: 'Think deeply about the sequence.' });
      const events: any[] = [];
      for await (const event of stepLoopStream(
        mem,
        {
          model: 'anthropic/x',
          temperature: 0.7,
          maxTokens: 100,
          intelligence: {
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
              maxSteps: 4,
              maxRetrievalQueries: 6,
              pollIntervalMs: 1,
              requestTimeoutMs: 1000,
              taskTimeoutMs: 10000,
            },
          },
        },
        0
      )) {
        events.push(event);
      }

      expect(events[0]).toMatchObject({
        type: 'deep_started',
        acknowledgement: 'Let me think deeply about this.',
      });
      expect(events.filter((event) => event.type === 'deep_progress')).toHaveLength(2);
      expect(
        events.find(
          (event) => event.type === 'deep_progress' && event.phase === 'completed'
        )
      ).toMatchObject({
        artifactStatus: 'indexed',
        artifactId: 'analysis_task_stream',
        completedPasses: 2,
        retrievalCompleted: 2,
        evidenceItems: 1,
      });
      const spoken = events
        .filter((event) => event.type === 'text')
        .map((event) => event.delta)
        .join('');
      expect(spoken).toContain('Validate repeatable demand');
      expect(spoken).not.toContain('excess');
      const final = events.find((event) => event.type === 'final');
      expect(final?.debug.deepReasoning).toMatchObject({
        status: 'succeeded',
        completedSteps: 2,
        artifactId: 'analysis_task_stream',
      });
      expect(final?.debug.analysisVoiceGuard).toEqual({ limit: 65, replaced: true });
      expect(final?.debug.finishReason).toBe('analysis_voice_limit_fallback');
      expect(post).toHaveBeenCalledOnce();
      expect(get).toHaveBeenCalledOnce();
    } finally {
      post.mockRestore();
      get.mockRestore();
    }
  });

  it('opens the second offered topic without starting another deep-analysis task', async () => {
    const post = vi.spyOn(axios, 'post');
    const get = vi.spyOn(axios, 'get');
    let systemPrompt = '';
    __setProviderManagerForTest({
      async *completeStream(messages: Message[]) {
        systemPrompt = messages[0]?.content || '';
        yield {
          type: 'text',
          delta: 'The pricing economics need measured acquisition cost and margin together.',
        };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);

    const artifact = parseAnalysisArtifact(analysisArtifactFixture('task_followup'))!;
    const mem = new Memory('test-analysis-followup');
    mem.setAnalysisState(createAnalysisConversationState(artifact, artifact.taskId));
    mem.addMessage({ role: 'user', content: 'Tell me about the second one.' });

    try {
      const events: any[] = [];
      for await (const event of stepLoopStream(
        mem,
        {
          model: 'anthropic/x',
          temperature: 0.7,
          maxTokens: 100,
          intelligence: {
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
              maxSteps: 4,
              maxRetrievalQueries: 6,
              pollIntervalMs: 1,
              requestTimeoutMs: 1000,
              taskTimeoutMs: 10000,
            },
          },
        },
        0
      )) {
        events.push(event);
      }

      expect(events.some((event) => event.type === 'deep_started')).toBe(false);
      expect(events.find((event) => event.type === 'final')?.debug.analysisNavigation).toEqual({
        action: 'open_topic',
        reason: 'ordinal_menu_selection',
        selectedTopicIds: ['topic_economics'],
      });
      expect(systemPrompt).toContain(
        'Measure price, acquisition cost, and margin in the same experiment.'
      );
      expect(systemPrompt).not.toContain(
        'The plan should measure a repeatable channel before scaling it.'
      );
      expect(mem.getAnalysisState()?.activeTopicId).toBe('topic_economics');
      expect(post).not.toHaveBeenCalled();
      expect(get).not.toHaveBeenCalled();
    } finally {
      mem.delete();
      post.mockRestore();
      get.mockRestore();
    }
  });
});

describe('stepLoopStream TTFT', () => {
  it('reports firstTokenMs on the final debug', async () => {
    __setProviderManagerForTest({
      async *completeStream() {
        await new Promise((r) => setTimeout(r, 20));
        yield { type: 'text', delta: 'Hello.' };
        yield { type: 'done', finishReason: 'stop', usage: undefined };
      },
    } as any);
    const mem = new Memory('ttft-test');
    mem.addMessage({ role: 'user', content: 'hi' });
    let finalEvt: any;
    for await (const e of stepLoopStream(
      mem,
      { model: 'anthropic/x', temperature: 0.7, maxTokens: 100 } as any,
      0
    )) {
      if ((e as any).type === 'final') finalEvt = e;
    }
    expect(finalEvt.debug.firstTokenMs).toBeGreaterThanOrEqual(15);
    expect(finalEvt.debug.firstTokenMs).toBeLessThanOrEqual(finalEvt.debug.durationMs);
  });
});

describe('getAgentConfig model override', () => {
  it('honors an available catalog override, else falls back to the default', () => {
    // No provider keys in the test env → every catalog model is unavailable → fall back to default.
    expect(getAgentConfig('groq/llama-3.3-70b-versatile').model).toBe(getAgentConfig().model);
    expect(getAgentConfig('totally-unknown-model').model).toBe(getAgentConfig().model);
  });
});
