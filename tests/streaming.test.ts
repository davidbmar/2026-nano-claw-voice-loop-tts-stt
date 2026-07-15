import { describe, it, expect } from 'vitest';
import { BaseProvider, parseAnthropicEvents, parseOpenAIEvents, OpenAIProvider } from '../src/providers/base';
import type { Message, LLMResponse, ToolDefinition } from '../src/types';
import { Readable } from 'node:stream';
import { __setProviderManagerForTest, stepLoopStream, getAgentConfig } from '../src/api/server';
import { Memory } from '../src/agent/memory';

class FakeProvider extends BaseProvider {
  protected getDefaultApiBase(): string { return 'http://example.invalid'; }
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
  function sse(s: string) { return require('node:stream').Readable.from([Buffer.from(s)]); }

  it('assembles content deltas and ends on [DONE]', async () => {
    const body =
      'data: {"choices":[{"delta":{"content":"Hi "}}]}\n\n' +
      'data: {"choices":[{"delta":{"content":"there."},"finish_reason":null}]}\n\n' +
      'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n' +
      'data: [DONE]\n\n';
    const out: any[] = [];
    for await (const e of parseOpenAIEvents(sse(body))) out.push(e);
    expect(out.filter(e => e.type === 'text').map(e => e.delta).join('')).toBe('Hi there.');
    const done = out.find(e => e.type === 'done');
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
    const t = out.find(e => e.type === 'tool_calls');
    expect(t.toolCalls[0]).toEqual({ id: 'c1', type: 'function', function: { name: 'shell', arguments: '{"cmd":"ls"}' } });
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
    for await (const e of stepLoopStream(mem, { model: 'anthropic/x', temperature: 0.7, maxTokens: 100 } as any, 0)) {
      events.push(e);
    }
    const texts = events.filter((e) => e.type === 'text').map((e) => e.delta).join('');
    expect(texts).toBe('Part one. Part two.');
    const final = events.find((e) => e.type === 'final');
    expect(final.response).toBe('Part one. Part two.');
  });
});

describe('getAgentConfig model override', () => {
  it('uses a valid catalog model override, else the default', () => {
    expect(getAgentConfig('groq/llama-3.3-70b-versatile').model).toBe('groq/llama-3.3-70b-versatile');
    expect(getAgentConfig('totally-unknown-model').model).toBe(getAgentConfig().model); // falls back
  });
});
