import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { Server } from 'node:http';
import { existsSync, mkdtempSync, readFileSync, rmSync, utimesSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { Memory } from '../src/agent/memory';
import { createServer, getMemory, __setProviderManagerForTest } from '../src/api/server';
import { getMemoryDir } from '../src/utils/helpers';

async function jsonRequest(
  server: Server,
  url: string,
  method: 'POST' | 'DELETE',
  body: Record<string, unknown>
): Promise<{ status: number; body: any }> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as any;
  req.method = method;
  req.url = url;
  req.headers = { 'content-type': 'application/json' };

  return new Promise((resolve, reject) => {
    let status = 0;
    let responseBody = '';
    const res: any = {
      setHeader() {},
      writeHead(statusCode: number) {
        status = statusCode;
        return res;
      },
      end(chunk?: string | Buffer) {
        if (chunk) responseBody += chunk.toString();
        try {
          resolve({ status, body: responseBody ? JSON.parse(responseBody) : null });
        } catch (error) {
          reject(error);
        }
        return res;
      },
    };
    const listener = server.listeners('request')[0] as (
      request: typeof req,
      response: typeof res
    ) => void;
    listener(req, res);
  });
}

describe('anonymous conversation isolation', () => {
  const originalHome = process.env.HOME;
  let testHome: string;

  beforeEach(() => {
    testHome = mkdtempSync(join(tmpdir(), 'nano-claw-conversation-'));
    process.env.HOME = testHome;
  });

  afterEach(() => {
    if (originalHome === undefined) delete process.env.HOME;
    else process.env.HOME = originalHome;
    rmSync(testHome, { recursive: true, force: true });
  });

  it('rejects unsafe session ids before filesystem access and accepts safe shapes', async () => {
    const memoryDir = getMemoryDir();
    const unsafeIds = [
      '../../x',
      'contains.dot',
      'contains space',
      'phone-call/id',
      '',
      'a'.repeat(65),
    ];

    for (const sessionId of unsafeIds) {
      expect(() => getMemory(sessionId)).toThrow('Invalid session id');
      expect(() => new Memory(sessionId)).toThrow('Invalid session id');
    }
    expect(existsSync(memoryDir)).toBe(false);
    expect(existsSync(join(testHome, 'x.json'))).toBe(false);

    const api = createServer();
    try {
      for (const sessionId of unsafeIds) {
        const response = await jsonRequest(api, '/api/chat', 'POST', {
          message: 'must not touch memory',
          sessionId,
        });
        expect(response.status).toBe(400);
        expect(response.body.error).toBe('Invalid "sessionId" field');
      }
      expect(existsSync(memoryDir)).toBe(false);
      expect(existsSync(join(testHome, 'x.json'))).toBe(false);

      const phoneMemory = getMemory('phone-call_control-id_123');
      const voiceMemory = getMemory(`voice-${'f'.repeat(32)}`);
      expect(phoneMemory.getSessionId()).toBe('phone-call_control-id_123');
      expect(voiceMemory.getSessionId()).toBe(`voice-${'f'.repeat(32)}`);
      phoneMemory.delete();
      voiceMemory.delete();
    } finally {
      api.emit('close');
    }
  });

  it('keeps concurrent marker prompts, replies, and memory files separate', async () => {
    const firstSession = `voice-${'a'.repeat(32)}`;
    const secondSession = `voice-${'b'.repeat(32)}`;
    const orphanSession = `voice-${'c'.repeat(32)}`;
    const firstMarker = 'node-marker-alpha';
    const secondMarker = 'node-marker-bravo';

    const orphan = new Memory(orphanSession);
    orphan.addMessage({ role: 'user', content: 'orphaned transcript' });
    const orphanPath = join(getMemoryDir(), `${orphanSession}.json`);
    expect(existsSync(orphanPath)).toBe(true);
    const staleTime = new Date(Date.now() - 25 * 60 * 60 * 1000);
    utimesSync(orphanPath, staleTime, staleTime);

    const prompts = new Map<string, string[]>();
    let arrivals = 0;
    let release!: () => void;
    const bothArrived = new Promise<void>((resolve) => {
      release = resolve;
    });
    __setProviderManagerForTest({
      async complete(messages: Array<{ role: string; content: string }>) {
        const userMessages = messages
          .filter((message) => message.role === 'user')
          .map((message) => message.content);
        const marker = userMessages[userMessages.length - 1];
        prompts.set(marker, userMessages);
        arrivals++;
        if (arrivals === 2) release();
        await bothArrived;
        return { content: `reply:${marker}`, finishReason: 'stop' };
      },
    });

    const api = createServer();
    expect(existsSync(orphanPath)).toBe(false);
    try {
      const [first, second] = await Promise.all([
        jsonRequest(api, '/api/chat', 'POST', {
          message: firstMarker,
          sessionId: firstSession,
        }),
        jsonRequest(api, '/api/chat', 'POST', {
          message: secondMarker,
          sessionId: secondSession,
        }),
      ]);

      expect(first.status).toBe(200);
      expect(second.status).toBe(200);
      expect(first.body.response).toBe(`reply:${firstMarker}`);
      expect(second.body.response).toBe(`reply:${secondMarker}`);
      expect(prompts.get(firstMarker)).toEqual([firstMarker]);
      expect(prompts.get(secondMarker)).toEqual([secondMarker]);

      const firstPath = join(getMemoryDir(), `${firstSession}.json`);
      const secondPath = join(getMemoryDir(), `${secondSession}.json`);
      const firstMemory = readFileSync(firstPath, 'utf-8');
      const secondMemory = readFileSync(secondPath, 'utf-8');
      expect(firstMemory).toContain(firstMarker);
      expect(firstMemory).not.toContain(secondMarker);
      expect(secondMemory).toContain(secondMarker);
      expect(secondMemory).not.toContain(firstMarker);

      expect(
        (await jsonRequest(api, '/api/session', 'DELETE', { sessionId: firstSession })).status
      ).toBe(200);
      expect(
        (await jsonRequest(api, '/api/session', 'DELETE', { sessionId: secondSession })).status
      ).toBe(200);
      expect(existsSync(firstPath)).toBe(false);
      expect(existsSync(secondPath)).toBe(false);
    } finally {
      api.emit('close');
    }
  });

  it('binds a pending tool decision to its originating session', async () => {
    const ownerSession = `voice-${'d'.repeat(32)}`;
    const otherSession = `voice-${'e'.repeat(32)}`;
    __setProviderManagerForTest({
      async complete(messages: Array<{ role: string; content: string }>) {
        if (messages[messages.length - 1]?.role === 'user') {
          return {
            content: '',
            finishReason: 'tool_calls',
            toolCalls: [
              {
                id: 'tool-1',
                type: 'function',
                function: { name: 'shell', arguments: '{"command":"true"}' },
              },
            ],
          };
        }
        return { content: 'tool decision handled', finishReason: 'stop' };
      },
    });

    const api = createServer();
    try {
      const pending = await jsonRequest(api, '/api/chat', 'POST', {
        message: 'request a tool',
        sessionId: ownerSession,
      });
      expect(pending.status).toBe(200);
      expect(pending.body.type).toBe('tool_pending');

      const wrongSession = await jsonRequest(api, '/api/chat/reject', 'POST', {
        requestId: pending.body.requestId,
        sessionId: otherSession,
      });
      expect(wrongSession.status).toBe(404);

      const owner = await jsonRequest(api, '/api/chat/reject', 'POST', {
        requestId: pending.body.requestId,
        sessionId: ownerSession,
      });
      expect(owner.status).toBe(200);
      expect(owner.body.response).toBe('tool decision handled');

      await jsonRequest(api, '/api/session', 'DELETE', { sessionId: ownerSession });
      await jsonRequest(api, '/api/session', 'DELETE', { sessionId: otherSession });
    } finally {
      api.emit('close');
    }
  });
});
