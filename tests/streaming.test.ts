import { describe, it, expect } from 'vitest';
import { BaseProvider, parseAnthropicEvents } from '../src/providers/base';
import type { Message, LLMResponse, ToolDefinition } from '../src/types';
import { Readable } from 'node:stream';

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
