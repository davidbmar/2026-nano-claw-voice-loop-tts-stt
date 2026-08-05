import { EventEmitter } from 'node:events';
import type { Server } from 'node:http';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createServer, __setProviderManagerForTest } from '../src/api/server';
import type { ProviderRequestOptions } from '../src/providers';

interface TestResponse {
  status: number;
  text: string;
}

function apiRequest(
  server: Server,
  body: Record<string, unknown>,
  stream = false
): Promise<TestResponse> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as any;
  req.method = 'POST';
  req.url = '/api/chat';
  req.headers = {
    'content-type': 'application/json',
    ...(stream && { accept: 'text/event-stream' }),
  };

  return new Promise((resolve) => {
    let status = 0;
    let text = '';
    const res = new EventEmitter() as any;
    res.destroyed = false;
    res.writableEnded = false;
    res.setHeader = () => res;
    res.writeHead = (statusCode: number) => {
      status = statusCode;
      return res;
    };
    res.write = (chunk: string | Buffer) => {
      text += chunk.toString();
      return true;
    };
    res.end = (chunk?: string | Buffer) => {
      if (chunk) text += chunk.toString();
      res.writableEnded = true;
      resolve({ status, text });
      return res;
    };

    const listener = server.listeners('request')[0] as (
      request: typeof req,
      response: typeof res
    ) => void;
    listener(req, res);
  });
}

function json(response: TestResponse): any {
  return JSON.parse(response.text);
}

function finalSse(response: TestResponse): any {
  const frame = response.text
    .split('\n\n')
    .find((candidate) => candidate.startsWith('event: final\n'));
  if (!frame) throw new Error(`Missing final SSE frame: ${response.text}`);
  return JSON.parse(frame.split('\ndata: ')[1]);
}

describe('/api/chat provider request options', () => {
  const originalHome = process.env.HOME;
  let testHome: string;

  beforeEach(() => {
    testHome = mkdtempSync(join(tmpdir(), 'nano-claw-chat-options-'));
    process.env.HOME = testHome;
  });

  afterEach(() => {
    if (originalHome === undefined) delete process.env.HOME;
    else process.env.HOME = originalHome;
    rmSync(testHome, { recursive: true, force: true });
  });

  it('parses and forwards pinning for JSON and SSE while ignoring unknown fields', async () => {
    const completeOptions: Array<ProviderRequestOptions | undefined> = [];
    const streamOptions: Array<ProviderRequestOptions | undefined> = [];
    __setProviderManagerForTest({
      async complete(
        _messages: unknown,
        model: string,
        _temperature: unknown,
        _maxTokens: unknown,
        _tools: unknown,
        options?: ProviderRequestOptions
      ) {
        completeOptions.push(options);
        return { content: 'json answer', finishReason: 'stop', model };
      },
      async *completeStream(
        _messages: unknown,
        model: string,
        _temperature: unknown,
        _maxTokens: unknown,
        _tools: unknown,
        options?: ProviderRequestOptions
      ) {
        streamOptions.push(options);
        yield { type: 'text', delta: 'stream answer' };
        yield { type: 'done', finishReason: 'stop', model };
      },
    });

    const api = createServer();
    try {
      const pinnedJson = await apiRequest(api, {
        message: 'json pin',
        sessionId: `voice-${'1'.repeat(32)}`,
        fallbacks: false,
        firstTokenTimeoutMs: 321,
        futureRoutingField: 'ignored',
      });
      expect(pinnedJson.status).toBe(200);
      expect(json(pinnedJson).debug).toMatchObject({
        fallbacksDisabled: true,
        model: expect.any(String),
      });
      expect(completeOptions[0]).toEqual({
        fallbacks: false,
        firstTokenTimeoutMs: 321,
      });

      const pinnedStream = await apiRequest(
        api,
        {
          message: 'stream pin',
          sessionId: `voice-${'2'.repeat(32)}`,
          fallbacks: false,
        },
        true
      );
      expect(pinnedStream.status).toBe(200);
      expect(finalSse(pinnedStream).debug).toMatchObject({
        fallbacksDisabled: true,
        model: expect.any(String),
      });
      expect(streamOptions[0]).toEqual({ fallbacks: false });

      const unchanged = await apiRequest(api, {
        message: 'normal behavior',
        sessionId: `voice-${'3'.repeat(32)}`,
        fallbacks: true,
        anotherFutureField: { ignored: true },
      });
      expect(unchanged.status).toBe(200);
      expect(json(unchanged).debug).not.toHaveProperty('fallbacksDisabled');
      expect(completeOptions[1]).toBeUndefined();
    } finally {
      api.emit('close');
    }
  });

  it('rejects invalid values for the two known fields before calling a provider', async () => {
    const complete = vi.fn(async () => ({ content: 'must not run' }));
    __setProviderManagerForTest({ complete });
    const api = createServer();
    try {
      const badFallbacks = await apiRequest(api, {
        message: 'bad boolean',
        fallbacks: 'false',
      });
      expect(badFallbacks.status).toBe(400);
      expect(json(badFallbacks).error).toBe('Invalid "fallbacks" field');

      const badTimeout = await apiRequest(api, {
        message: 'bad timeout',
        firstTokenTimeoutMs: 0,
      });
      expect(badTimeout.status).toBe(400);
      expect(json(badTimeout).error).toBe('Invalid "firstTokenTimeoutMs" field');
      expect(complete).not.toHaveBeenCalled();
    } finally {
      api.emit('close');
    }
  });
});
