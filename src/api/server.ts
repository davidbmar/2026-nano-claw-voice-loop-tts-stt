/**
 * HTTP API server with stepped agent loop.
 *
 * Exposes the agent loop over HTTP with tool confirmation:
 * - POST /api/chat         — start agent loop, returns text or pending tools
 * - POST /api/chat/approve — approve pending tools, continue loop
 * - POST /api/chat/reject  — reject tools, LLM retries without them
 * - GET  /api/health       — health check
 */

import http from 'node:http';
import crypto from 'node:crypto';
import { AgentConfig, ToolCall } from '../types';
import { ProviderManager } from '../providers/index';
import { Memory } from '../agent/memory';
import { ContextBuilder } from '../agent/context';
import { SkillsLoader } from '../agent/skills';
import { ToolRegistry } from '../agent/tools/registry';
import { ShellTool } from '../agent/tools/shell';
import { ReadFileTool, WriteFileTool } from '../agent/tools/file';
import { Config } from '../config/schema';
import { getConfig } from '../config/index';
import { logger } from '../utils/logger';

// ── Types ────────────────────────────────────────────────────

interface PendingToolState {
  memory: Memory;
  toolCalls: ToolCall[];
  assistantContent: string;
  iteration: number;
  agentConfig: AgentConfig;
}

type ApiResponse =
  | { type: 'final'; response: string }
  | { type: 'tool_pending'; requestId: string; tools: { name: string; args: Record<string, unknown> }[] };

// ── Pending state store ──────────────────────────────────────

const pendingRequests = new Map<string, PendingToolState>();

// Clean up stale pending requests after 10 minutes
const PENDING_TTL_MS = 10 * 60 * 1000;
const pendingTimestamps = new Map<string, number>();

function cleanupStale(): void {
  const now = Date.now();
  for (const [id, ts] of pendingTimestamps) {
    if (now - ts > PENDING_TTL_MS) {
      pendingRequests.delete(id);
      pendingTimestamps.delete(id);
    }
  }
}

// ── Shared instances ─────────────────────────────────────────

let config: Config;
let providerManager: ProviderManager;
let skillsLoader: SkillsLoader;

function initShared(): void {
  config = getConfig();
  providerManager = new ProviderManager(config);
  skillsLoader = new SkillsLoader();
}

function createToolRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  const toolsConfig = config.tools;
  registry.register(
    new ShellTool(
      toolsConfig?.restrictToWorkspace,
      toolsConfig?.allowedCommands,
      toolsConfig?.deniedCommands
    )
  );
  registry.register(new ReadFileTool());
  registry.register(new WriteFileTool());
  return registry;
}

function getAgentConfig(): AgentConfig {
  return {
    model: config.agents?.defaults?.model || 'anthropic/claude-opus-4-5',
    temperature: config.agents?.defaults?.temperature || 0.7,
    maxTokens: config.agents?.defaults?.maxTokens || 4096,
    systemPrompt: config.agents?.defaults?.systemPrompt,
  };
}

// ── Stepped agent loop ───────────────────────────────────────

const MAX_ITERATIONS = 10;

/**
 * Run one LLM call and either return final text or pause at tool calls.
 */
async function stepLoop(
  memory: Memory,
  agentConfig: AgentConfig,
  iteration: number
): Promise<ApiResponse> {
  const toolRegistry = createToolRegistry();

  while (iteration < MAX_ITERATIONS) {
    iteration++;
    logger.debug({ iteration }, 'Stepped loop iteration');

    const skills = skillsLoader.getSkills();
    const tools = toolRegistry.getDefinitions();
    const contextBuilder = new ContextBuilder(agentConfig);
    const contextMessages = contextBuilder.buildContextMessages(
      memory.getMessages(),
      skills,
      tools
    );

    const response = await providerManager.complete(
      contextMessages,
      agentConfig.model,
      agentConfig.temperature,
      agentConfig.maxTokens,
      tools
    );

    if (response.toolCalls && response.toolCalls.length > 0) {
      // Add assistant message with tool calls to memory
      memory.addMessage({
        role: 'assistant',
        content: response.content || '',
        tool_calls: response.toolCalls,
      });

      // Pause — return tool calls for browser approval
      const requestId = crypto.randomUUID();
      pendingRequests.set(requestId, {
        memory,
        toolCalls: response.toolCalls,
        assistantContent: response.content || '',
        iteration,
        agentConfig,
      });
      pendingTimestamps.set(requestId, Date.now());

      return {
        type: 'tool_pending',
        requestId,
        tools: response.toolCalls.map((tc) => ({
          name: tc.function.name,
          args: safeParseToolArgs(tc.function.arguments),
        })),
      };
    }

    // No tool calls — final response
    memory.addMessage({
      role: 'assistant',
      content: response.content,
    });

    return { type: 'final', response: response.content };
  }

  return { type: 'final', response: 'Max iterations reached.' };
}

// ── Session memory cache ─────────────────────────────────────

const sessionMemories = new Map<string, Memory>();

function getMemory(sessionId: string): Memory {
  let memory = sessionMemories.get(sessionId);
  if (!memory) {
    memory = new Memory(sessionId);
    sessionMemories.set(sessionId, memory);
  }
  return memory;
}

// ── HTTP helpers ─────────────────────────────────────────────

function setCorsHeaders(res: http.ServerResponse): void {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
}

function sendJson(res: http.ServerResponse, statusCode: number, body: unknown): void {
  setCorsHeaders(res);
  res.writeHead(statusCode, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

const MAX_BODY_BYTES = 1024 * 1024; // 1 MB

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let totalBytes = 0;
    req.on('data', (chunk: Buffer) => {
      totalBytes += chunk.length;
      if (totalBytes > MAX_BODY_BYTES) {
        req.destroy();
        reject(new Error('Request body too large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString()));
    req.on('error', reject);
  });
}

function parseJsonBody(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function safeParseToolArgs(argsJson: string): Record<string, unknown> {
  try {
    return JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    return { _raw: argsJson };
  }
}

// ── Route handlers ───────────────────────────────────────────

async function handleChat(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as { message?: string; sessionId?: string } | null;
  if (!body || typeof body.message !== 'string' || !body.message.trim()) {
    sendJson(res, 400, { error: 'Missing or empty "message" field' });
    return;
  }
  const sessionId = body.sessionId || 'default';
  const memory = getMemory(sessionId);

  memory.addMessage({ role: 'user', content: body.message });

  const result = await stepLoop(memory, getAgentConfig(), 0);
  sendJson(res, 200, result);
}

async function handleApprove(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as { requestId?: string; sessionId?: string } | null;
  if (!body || typeof body.requestId !== 'string') {
    sendJson(res, 400, { error: 'Missing "requestId" field' });
    return;
  }
  const pending = pendingRequests.get(body.requestId);

  if (!pending) {
    sendJson(res, 404, { error: 'Unknown or expired requestId' });
    return;
  }

  pendingRequests.delete(body.requestId);
  pendingTimestamps.delete(body.requestId);

  // Execute approved tools
  const toolRegistry = createToolRegistry();
  for (const toolCall of pending.toolCalls) {
    const toolName = toolCall.function.name;
    const toolArgs = safeParseToolArgs(toolCall.function.arguments);

    logger.info({ tool: toolName, args: toolArgs }, 'Executing approved tool');
    const toolResult = await toolRegistry.execute(toolName, toolArgs);

    pending.memory.addMessage({
      role: 'tool',
      content: toolResult.success ? toolResult.output : `Error: ${toolResult.error}`,
      name: toolName,
      tool_call_id: toolCall.id,
    });
  }

  // Continue the loop
  const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration);
  sendJson(res, 200, result);
}

async function handleReject(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as { requestId?: string; sessionId?: string } | null;
  if (!body || typeof body.requestId !== 'string') {
    sendJson(res, 400, { error: 'Missing "requestId" field' });
    return;
  }
  const pending = pendingRequests.get(body.requestId);

  if (!pending) {
    sendJson(res, 404, { error: 'Unknown or expired requestId' });
    return;
  }

  pendingRequests.delete(body.requestId);
  pendingTimestamps.delete(body.requestId);

  // Add tool rejection messages so LLM knows tools were denied
  for (const toolCall of pending.toolCalls) {
    pending.memory.addMessage({
      role: 'tool',
      content: 'Tool execution was rejected by the user.',
      name: toolCall.function.name,
      tool_call_id: toolCall.id,
    });
  }

  // Continue loop — LLM will respond without tool results
  const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration);
  sendJson(res, 200, result);
}

// ── Server ───────────────────────────────────────────────────

export function createServer(): http.Server {
  initShared();

  // Periodic cleanup of stale pending requests
  const cleanupInterval = setInterval(cleanupStale, 60_000);

  const server = http.createServer(async (req, res) => {
    const url = req.url || '';
    const method = req.method || '';

    // CORS preflight
    if (method === 'OPTIONS') {
      setCorsHeaders(res);
      res.writeHead(204);
      res.end();
      return;
    }

    try {
      if (method === 'GET' && url === '/api/health') {
        sendJson(res, 200, { status: 'ok' });
      } else if (method === 'POST' && (url === '/api/chat' || url === '/api/chat/approve' || url === '/api/chat/reject')) {
        const ct = req.headers['content-type'] || '';
        if (!ct.includes('application/json')) {
          sendJson(res, 415, { error: 'Content-Type must be application/json' });
          return;
        }
        if (url === '/api/chat') await handleChat(req, res);
        else if (url === '/api/chat/approve') await handleApprove(req, res);
        else await handleReject(req, res);
      } else {
        sendJson(res, 404, { error: 'Not found' });
      }
    } catch (error) {
      logger.error({ error, url, method }, 'API error');
      sendJson(res, 500, { error: (error as Error).message });
    }
  });

  server.on('close', () => clearInterval(cleanupInterval));

  return server;
}

/**
 * Start the API server (used by entrypoint and CLI).
 */
export async function startServer(port: number): Promise<void> {
  const server = createServer();

  await new Promise<void>((resolve) => {
    server.listen(port, () => resolve());
  });

  logger.info({ port }, 'nano-claw API server listening');
}
