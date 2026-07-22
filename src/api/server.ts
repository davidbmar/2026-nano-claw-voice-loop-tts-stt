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
import { AgentConfig, AnalysisStyle, ToolCall, StreamEvent, LLMResponse } from '../types';
import { ProviderManager } from '../providers/index';
import {
  Memory,
  assertValidSessionId,
  deleteMemoryFile,
  isEphemeralSessionId,
  isValidSessionId,
  sweepEphemeralMemory,
} from '../agent/memory';
import { ContextBuilder } from '../agent/context';
import { resolveKnowledgeFiles } from '../agent/knowledge';
import { SkillsLoader } from '../agent/skills';
import { ToolRegistry } from '../agent/tools/registry';
import { ShellTool } from '../agent/tools/shell';
import { ReadFileTool, WriteFileTool } from '../agent/tools/file';
import { Config } from '../config/schema';
import { getConfig, createDefaultConfig, mergeEnvConfig } from '../config/index';
import { logger } from '../utils/logger';
import { modelsWithAvailability, DEFAULT_MODEL } from '../agent/models';
import { retrieveTurnEvidence } from '../agent/intelligence';
import {
  analysisStateFromResult,
  deepAcknowledgement,
  DeepReasoningResult,
  DeepRouteDecision,
  detectDeepQuestion,
  guardAnalysisVoiceResponse,
  analysisVoiceWordLimit,
  resolveExistingAnalysisTurn,
  runDeepReasoning,
  streamDeepReasoning,
} from '../agent/deep-reasoning';
import type { AnalysisNavigationDecision } from '../agent/analysis-navigation';

// ── Types ────────────────────────────────────────────────────

interface DebugInfo {
  iteration: number;
  messageCount: number;
  model: string;
  tokenUsage?: {
    prompt: number;
    completion: number;
    total: number;
    cacheRead?: number;
    cacheWrite?: number;
  };
  durationMs: number;
  firstTokenMs?: number;
  finishReason?: string;
  intelligence?: {
    status: 'retrieved' | 'no_match' | 'unavailable';
    evidenceCount: number;
    durationMs: number;
  };
  deepReasoning?: {
    routed: true;
    workflow: DeepReasoningResult['workflow'];
    score: number;
    reasons: string[];
    taskId?: string;
    status: DeepReasoningResult['status'];
    durationMs: number;
    completedSteps: number;
    retrievalQueries: number;
    artifactId?: string;
    topicCount?: number;
    analysisStyle?: AnalysisStyle;
    modelUsage: DeepReasoningResult['modelUsage'];
  };
  analysisNavigation?: {
    action: AnalysisNavigationDecision['action'];
    reason: string;
    selectedTopicIds: string[];
  };
  analysisVoiceGuard?: {
    limit: number;
    replaced: boolean;
  };
}

interface PendingToolState {
  sessionId: string;
  memory: Memory;
  toolCalls: ToolCall[];
  assistantContent: string;
  iteration: number;
  agentConfig: AgentConfig;
}

type ApiResponse =
  | { type: 'final'; response: string; debug: DebugInfo }
  | {
      type: 'tool_pending';
      requestId: string;
      tools: { name: string; args: Record<string, unknown> }[];
      debug: DebugInfo;
    };

// ── Pending state store ──────────────────────────────────────

const pendingRequests = new Map<string, PendingToolState>();

// Clean up stale pending requests after 10 minutes
const PENDING_TTL_MS = 10 * 60 * 1000;
const EPHEMERAL_SESSION_TTL_MS = 24 * 60 * 60 * 1000;
const pendingTimestamps = new Map<string, number>();

function cleanupStale(): void {
  const now = Date.now();
  for (const [id, ts] of pendingTimestamps) {
    if (now - ts > PENDING_TTL_MS) {
      pendingRequests.delete(id);
      pendingTimestamps.delete(id);
    }
  }

  for (const [sessionId, lastUsed] of sessionLastUsed) {
    if (now - lastUsed > EPHEMERAL_SESSION_TTL_MS) deleteSession(sessionId);
  }
  sweepEphemeralMemory(new Set(sessionMemories.keys()), EPHEMERAL_SESSION_TTL_MS, now);
}

// ── Shared instances ─────────────────────────────────────────

let config: Config;
let providerManager: ProviderManager;
let skillsLoader: SkillsLoader;

let sharedInitialized = false;

function initShared(): void {
  if (sharedInitialized) return;

  try {
    config = getConfig();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/not found/i.test(message)) {
      // No config file on disk (e.g. `nano-claw onboard` never run, or
      // running under test). Degrade to defaults instead of crashing —
      // any env-supplied provider keys still get picked up. Real HTTP
      // requests that need a provider will fail with a clear "No provider
      // configured" error from ProviderManager at call time.
      logger.warn('No nano-claw config file found; using defaults');
      config = mergeEnvConfig(createDefaultConfig());
    } else {
      // Config file exists but is corrupted (invalid JSON) or fails schema
      // validation — this must fail loud, not silently degrade to a looser
      // default config (e.g. restrictToWorkspace: false). Leave
      // `sharedInitialized` false so a retry (e.g. after the caller fixes
      // the file) re-attempts init instead of silently no-op'ing.
      throw error;
    }
  }

  // Preserve a test-injected provider manager (see
  // __setProviderManagerForTest) instead of clobbering it.
  if (!providerManager) providerManager = new ProviderManager(config);
  skillsLoader = new SkillsLoader();
  sharedInitialized = true;
}

/** Test-only: inject a stub provider manager. */
export function __setProviderManagerForTest(pm: unknown): void {
  providerManager = pm as ProviderManager;
}

function createToolRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  const toolsConfig = config.tools;
  // Knowledge-only mode: no tools registered → no tools offered to the LLM,
  // no approval pauses; the agent answers purely from persona + knowledge.
  if (toolsConfig?.enabled === false) return registry;
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

/**
 * Resolve the prompt and knowledge for one optional assistant profile.
 *
 * Known profiles are deliberately isolated from the global knowledge glob.
 * `none` keeps the configured fallback prompt but has no site knowledge. An
 * absent or unknown profile preserves the pre-profile behavior, including
 * environment/config knowledge, for backward compatibility.
 */
export function resolveAgentProfile(
  agentConfig: Config,
  profileId?: string
): Pick<AgentConfig, 'systemPrompt' | 'knowledgeFiles' | 'intelligence'> {
  const profiles = agentConfig.agents?.profiles;
  const knownProfile =
    profileId !== undefined &&
    profileId !== 'none' &&
    profiles !== undefined &&
    Object.prototype.hasOwnProperty.call(profiles, profileId)
      ? profiles[profileId]
      : undefined;

  if (knownProfile) {
    return {
      systemPrompt: knownProfile.systemPrompt,
      knowledgeFiles: [...knownProfile.knowledgeFiles],
      ...(knownProfile.intelligence && { intelligence: knownProfile.intelligence }),
    };
  }

  return {
    systemPrompt: agentConfig.agents?.defaults?.systemPrompt,
    knowledgeFiles: profileId === 'none' ? [] : resolveKnowledgeFiles(agentConfig),
    ...(profileId !== 'none' &&
      agentConfig.agents?.defaults?.intelligence && {
        intelligence: agentConfig.agents.defaults.intelligence,
      }),
  };
}

/**
 * Build the agent config for a turn. If `modelOverride` names a model in the
 * catalog, it wins; otherwise falls back to the configured default. A known
 * `profileId` selects that profile's prompt and isolated knowledge files.
 */
export function getAgentConfig(
  modelOverride?: string,
  profileId?: string,
  analysisStyleOverride?: AnalysisStyle,
  responseMode?: 'text' | 'voice'
): AgentConfig {
  initShared();
  const valid =
    !!modelOverride &&
    modelsWithAvailability(config).some((m) => m.id === modelOverride && m.available);
  const profile = resolveAgentProfile(config, profileId);
  const intelligence =
    profile.intelligence && profile.intelligence.deepReasoning && analysisStyleOverride
      ? {
          ...profile.intelligence,
          deepReasoning: {
            ...profile.intelligence.deepReasoning,
            analysisStyle: analysisStyleOverride,
          },
        }
      : profile.intelligence;
  return {
    model: valid ? modelOverride : config.agents?.defaults?.model || DEFAULT_MODEL,
    temperature: config.agents?.defaults?.temperature || 0.7,
    maxTokens: config.agents?.defaults?.maxTokens || 4096,
    ...profile,
    ...(intelligence && { intelligence }),
    ...(responseMode && { responseMode }),
  };
}

// ── Stepped agent loop ───────────────────────────────────────

const MAX_ITERATIONS = 10;
const DEEP_FAILURE_RESPONSE =
  "I'm sorry, I couldn't complete the deeper analysis just now. Please try again.";

function deepDebug(
  route: DeepRouteDecision,
  result: DeepReasoningResult
): NonNullable<DebugInfo['deepReasoning']> {
  return {
    routed: true,
    workflow: route.workflow,
    score: route.score,
    reasons: route.reasons,
    taskId: result.taskId,
    status: result.status,
    durationMs: result.durationMs,
    completedSteps: result.completedSteps,
    retrievalQueries: result.retrievalQueries,
    artifactId: result.artifact?.artifactId,
    topicCount: result.artifact?.topics.length,
    analysisStyle: result.artifact
      ? result.artifact.promptVersion.includes('principle_graph')
        ? 'principle_graph'
        : 'topic_map'
      : undefined,
    modelUsage: result.modelUsage,
  };
}

/**
 * Run one LLM call and either return final text or pause at tool calls.
 */
async function stepLoop(
  memory: Memory,
  agentConfig: AgentConfig,
  iteration: number
): Promise<ApiResponse> {
  initShared();
  const toolRegistry = createToolRegistry();

  while (iteration < MAX_ITERATIONS) {
    iteration++;
    const messageCount = memory.getMessages().length;
    const startTime = Date.now();

    const skills = skillsLoader.getSkills();
    const tools = toolRegistry.getDefinitions();
    const messages = memory.getMessages();
    const analysisState = iteration === 1 ? memory.getAnalysisState() : undefined;
    const analysisTurn =
      analysisState && agentConfig.intelligence
        ? await resolveExistingAnalysisTurn(messages, analysisState, agentConfig.intelligence)
        : undefined;
    if (analysisTurn) memory.setAnalysisState(analysisTurn.state);
    const deepRoute =
      analysisTurn?.deepRoute ||
      (iteration === 1
        ? detectDeepQuestion(messages, agentConfig.intelligence)
        : { deep: false, score: 0, reasons: [], workflow: 'evidence_analysis' as const });
    const ranDeepTask = deepRoute.deep && !analysisTurn?.result;
    const deepResult =
      analysisTurn?.result ||
      (ranDeepTask && agentConfig.intelligence
        ? await runDeepReasoning(
            messages,
            agentConfig.intelligence,
            undefined,
            undefined,
            deepRoute
          )
        : undefined);
    if (deepResult && deepResult.status !== 'succeeded') {
      memory.addMessage({ role: 'assistant', content: DEEP_FAILURE_RESPONSE });
      return {
        type: 'final',
        response: DEEP_FAILURE_RESPONSE,
        debug: {
          iteration,
          messageCount,
          model: agentConfig.model,
          durationMs: Date.now() - startTime,
          finishReason: deepResult.errorCode || deepResult.status,
          ...(ranDeepTask && { deepReasoning: deepDebug(deepRoute, deepResult) }),
          ...(analysisTurn && {
            analysisNavigation: {
              action: analysisTurn.decision.action,
              reason: analysisTurn.decision.reason,
              selectedTopicIds: analysisTurn.decision.selectedTopicIds,
            },
          }),
        },
      };
    }
    const completedAnalysisState =
      ranDeepTask && deepResult
        ? analysisStateFromResult(
            deepResult,
            agentConfig.intelligence?.deepReasoning?.analysisStyle
          )
        : undefined;
    if (completedAnalysisState) memory.setAnalysisState(completedAnalysisState);
    const turnEvidence = deepResult
      ? undefined
      : await retrieveTurnEvidence(messages, agentConfig.intelligence);
    const modelTools = deepResult ? [] : tools;
    const contextBuilder = new ContextBuilder(agentConfig);
    const contextMessages = contextBuilder.buildContextMessages(
      messages,
      skills,
      modelTools,
      turnEvidence,
      deepResult
    );

    const response = await providerManager.complete(
      contextMessages,
      agentConfig.model,
      agentConfig.temperature,
      agentConfig.maxTokens,
      modelTools
    );
    const voiceGuard = guardAnalysisVoiceResponse(response.content, deepResult);

    const durationMs = Date.now() - startTime;

    const debug: DebugInfo = {
      iteration,
      messageCount,
      model: agentConfig.model,
      tokenUsage: response.usage
        ? {
            prompt: response.usage.promptTokens,
            completion: response.usage.completionTokens,
            total: response.usage.totalTokens,
            cacheRead: response.usage.cacheReadTokens,
            cacheWrite: response.usage.cacheWriteTokens,
          }
        : undefined,
      durationMs,
      finishReason: voiceGuard.replaced ? 'analysis_voice_limit_fallback' : response.finishReason,
      ...(turnEvidence && {
        intelligence: {
          status: turnEvidence.status,
          evidenceCount: turnEvidence.items.length,
          durationMs: turnEvidence.durationMs,
        },
      }),
      ...(deepResult && ranDeepTask && { deepReasoning: deepDebug(deepRoute, deepResult) }),
      ...(analysisTurn && {
        analysisNavigation: {
          action: analysisTurn.decision.action,
          reason: analysisTurn.decision.reason,
          selectedTopicIds: analysisTurn.decision.selectedTopicIds,
        },
      }),
      ...(voiceGuard.limit !== undefined && {
        analysisVoiceGuard: {
          limit: voiceGuard.limit,
          replaced: voiceGuard.replaced,
        },
      }),
    };

    logger.info(
      {
        iteration,
        messageCount,
        model: agentConfig.model,
        tokenUsage: debug.tokenUsage,
        durationMs,
        finishReason: response.finishReason,
        hasToolCalls: !!(response.toolCalls && response.toolCalls.length > 0),
      },
      'Agent loop iteration complete'
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
        sessionId: memory.getSessionId(),
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
        debug,
      };
    }

    // No tool calls — final response
    memory.addMessage({
      role: 'assistant',
      content: voiceGuard.text,
    });

    return { type: 'final', response: voiceGuard.text, debug };
  }

  return {
    type: 'final',
    response: 'Max iterations reached.',
    debug: {
      iteration: MAX_ITERATIONS,
      messageCount: memory.getMessages().length,
      model: agentConfig.model,
      durationMs: 0,
      finishReason: 'max_iterations',
    },
  };
}

/**
 * Streaming variant of stepLoop — yields text deltas as they arrive, then a
 * terminal `tool_pending` or `final` event (mirrors stepLoop's return value).
 */
export async function* stepLoopStream(
  memory: Memory,
  agentConfig: AgentConfig,
  iteration: number,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent | ApiResponse> {
  initShared();
  const toolRegistry = createToolRegistry();

  while (iteration < MAX_ITERATIONS) {
    iteration++;
    const messageCount = memory.getMessages().length;
    const startTime = Date.now();
    const skills = skillsLoader.getSkills();
    const tools = toolRegistry.getDefinitions();
    const messages = memory.getMessages();
    const analysisState = iteration === 1 ? memory.getAnalysisState() : undefined;
    const analysisTurn =
      analysisState && agentConfig.intelligence
        ? await resolveExistingAnalysisTurn(
            messages,
            analysisState,
            agentConfig.intelligence,
            signal
          )
        : undefined;
    if (analysisTurn) memory.setAnalysisState(analysisTurn.state);
    const deepRoute =
      analysisTurn?.deepRoute ||
      (iteration === 1
        ? detectDeepQuestion(messages, agentConfig.intelligence)
        : { deep: false, score: 0, reasons: [], workflow: 'evidence_analysis' as const });
    const ranDeepTask = deepRoute.deep && !analysisTurn?.result;
    let deepResult: DeepReasoningResult | undefined = analysisTurn?.result;
    if (ranDeepTask && agentConfig.intelligence) {
      yield {
        type: 'deep_started',
        acknowledgement: deepAcknowledgement(agentConfig.intelligence),
        score: deepRoute.score,
        reasons: deepRoute.reasons,
      };
      for await (const event of streamDeepReasoning(
        messages,
        agentConfig.intelligence,
        signal,
        undefined,
        deepRoute
      )) {
        if (event.type === 'progress') {
          yield {
            type: 'deep_progress',
            taskId: event.progress.taskId,
            phase: event.progress.phase,
            message: event.progress.message,
            completedSteps: event.progress.completedSteps,
            maxSteps: event.progress.maxSteps,
            retrievalQueries: event.progress.retrievalQueries,
            currentPass: event.progress.currentPass,
            completedPasses: event.progress.completedPasses,
            maxPasses: event.progress.maxPasses,
            retrievalPlanned: event.progress.retrievalPlanned,
            retrievalCompleted: event.progress.retrievalCompleted,
            evidenceItems: event.progress.evidenceItems,
            model: event.progress.model,
            artifactStatus: event.progress.artifactStatus,
            artifactId: event.progress.artifactId,
            phaseStartedAt: event.progress.phaseStartedAt,
            heartbeatAt: event.progress.heartbeatAt,
          };
        } else {
          deepResult = event.result;
        }
      }
      if (!deepResult || deepResult.status !== 'succeeded') {
        const failed =
          deepResult ||
          ({
            status: 'unavailable',
            workflow: deepRoute.workflow,
            claims: [],
            evidence: [],
            modelUsage: [],
            durationMs: Date.now() - startTime,
            completedSteps: 0,
            retrievalQueries: 0,
          } as DeepReasoningResult);
        memory.addMessage({ role: 'assistant', content: DEEP_FAILURE_RESPONSE });
        yield { type: 'text', delta: DEEP_FAILURE_RESPONSE };
        yield {
          type: 'final',
          response: DEEP_FAILURE_RESPONSE,
          debug: {
            iteration,
            messageCount,
            model: agentConfig.model,
            durationMs: Date.now() - startTime,
            finishReason: failed.errorCode || failed.status,
            deepReasoning: deepDebug(deepRoute, failed),
          },
        };
        return;
      }
    }
    const completedAnalysisState =
      ranDeepTask && deepResult
        ? analysisStateFromResult(
            deepResult,
            agentConfig.intelligence?.deepReasoning?.analysisStyle
          )
        : undefined;
    if (completedAnalysisState) memory.setAnalysisState(completedAnalysisState);
    const turnEvidence = deepResult
      ? undefined
      : await retrieveTurnEvidence(messages, agentConfig.intelligence);
    const modelTools = deepResult ? [] : tools;
    const contextBuilder = new ContextBuilder(agentConfig);
    const contextMessages = contextBuilder.buildContextMessages(
      messages,
      skills,
      modelTools,
      turnEvidence,
      deepResult
    );

    let text = '';
    let toolCalls: ToolCall[] | undefined;
    let finishReason: string | undefined;
    let usage: LLMResponse['usage'];
    let firstTokenAt: number | undefined;
    const holdBoundedVoice = analysisVoiceWordLimit(deepResult) !== undefined;

    for await (const ev of providerManager.completeStream(
      contextMessages,
      agentConfig.model,
      agentConfig.temperature,
      agentConfig.maxTokens,
      modelTools
    )) {
      if (ev.type === 'text') {
        text += ev.delta;
        if (!holdBoundedVoice) {
          if (firstTokenAt === undefined) firstTokenAt = Date.now();
          yield ev; // forward unbounded projections as they arrive
        }
      } else if (ev.type === 'tool_calls') {
        toolCalls = ev.toolCalls;
      } else if (ev.type === 'done') {
        finishReason = ev.finishReason;
        usage = ev.usage;
      }
    }
    const voiceGuard = guardAnalysisVoiceResponse(text, deepResult);
    text = voiceGuard.text;
    if (holdBoundedVoice && text) {
      firstTokenAt = Date.now();
      yield { type: 'text', delta: text };
    }
    if (voiceGuard.replaced) finishReason = 'analysis_voice_limit_fallback';

    const debug: DebugInfo = {
      iteration,
      messageCount,
      model: agentConfig.model,
      tokenUsage: usage
        ? {
            prompt: usage.promptTokens,
            completion: usage.completionTokens,
            total: usage.totalTokens,
            cacheRead: usage.cacheReadTokens,
            cacheWrite: usage.cacheWriteTokens,
          }
        : undefined,
      durationMs: Date.now() - startTime,
      firstTokenMs: firstTokenAt !== undefined ? firstTokenAt - startTime : undefined,
      finishReason,
      ...(turnEvidence && {
        intelligence: {
          status: turnEvidence.status,
          evidenceCount: turnEvidence.items.length,
          durationMs: turnEvidence.durationMs,
        },
      }),
      ...(deepResult && ranDeepTask && { deepReasoning: deepDebug(deepRoute, deepResult) }),
      ...(analysisTurn && {
        analysisNavigation: {
          action: analysisTurn.decision.action,
          reason: analysisTurn.decision.reason,
          selectedTopicIds: analysisTurn.decision.selectedTopicIds,
        },
      }),
      ...(voiceGuard.limit !== undefined && {
        analysisVoiceGuard: {
          limit: voiceGuard.limit,
          replaced: voiceGuard.replaced,
        },
      }),
    };

    if (toolCalls && toolCalls.length > 0) {
      memory.addMessage({ role: 'assistant', content: text, tool_calls: toolCalls });
      const requestId = crypto.randomUUID();
      pendingRequests.set(requestId, {
        sessionId: memory.getSessionId(),
        memory,
        toolCalls,
        assistantContent: text,
        iteration,
        agentConfig,
      });
      pendingTimestamps.set(requestId, Date.now());
      yield {
        type: 'tool_pending',
        requestId,
        tools: toolCalls.map((tc) => ({
          name: tc.function.name,
          args: safeParseToolArgs(tc.function.arguments),
        })),
        debug,
      };
      return;
    }

    memory.addMessage({ role: 'assistant', content: text });
    yield { type: 'final', response: text, debug };
    return;
  }

  yield {
    type: 'final',
    response: 'Max iterations reached.',
    debug: {
      iteration: MAX_ITERATIONS,
      messageCount: memory.getMessages().length,
      model: agentConfig.model,
      durationMs: 0,
      finishReason: 'max_iterations',
    },
  };
}

// ── Session memory cache ─────────────────────────────────────

const sessionMemories = new Map<string, Memory>();
const sessionLastUsed = new Map<string, number>();

export function getMemory(sessionId: string): Memory {
  assertValidSessionId(sessionId);
  let memory = sessionMemories.get(sessionId);
  if (!memory) {
    memory = new Memory(sessionId);
    sessionMemories.set(sessionId, memory);
  }
  if (isEphemeralSessionId(sessionId)) sessionLastUsed.set(sessionId, Date.now());
  return memory;
}

function deleteSession(sessionId: string): void {
  const memory = sessionMemories.get(sessionId);
  if (memory) memory.delete();
  else deleteMemoryFile(sessionId);
  sessionMemories.delete(sessionId);
  sessionLastUsed.delete(sessionId);

  for (const [requestId, pending] of pendingRequests) {
    if (pending.sessionId !== sessionId) continue;
    pendingRequests.delete(requestId);
    pendingTimestamps.delete(requestId);
  }
}

// ── HTTP helpers ─────────────────────────────────────────────

function setCorsHeaders(res: http.ServerResponse): void {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
}

function sendJson(res: http.ServerResponse, statusCode: number, body: unknown): void {
  setCorsHeaders(res);
  res.writeHead(statusCode, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

const STREAM_ENABLED =
  process.env.NANO_CLAW_STREAM !== '0' && process.env.NANO_CLAW_STREAM !== 'false';

function wantsStream(req: http.IncomingMessage): boolean {
  return STREAM_ENABLED && (req.headers['accept'] || '').includes('text/event-stream');
}

function sseWrite(res: http.ServerResponse, event: string, data: unknown): void {
  if (res.destroyed || res.writableEnded) return;
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

async function streamLoopToSSE(
  res: http.ServerResponse,
  gen: AsyncGenerator<StreamEvent | ApiResponse>
): Promise<void> {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  });
  try {
    for await (const ev of gen) {
      if ((ev as StreamEvent).type === 'text')
        sseWrite(res, 'delta', { text: (ev as { delta: string }).delta });
      else if ((ev as StreamEvent).type === 'deep_started') sseWrite(res, 'deep_started', ev);
      else if ((ev as StreamEvent).type === 'deep_progress') sseWrite(res, 'deep_progress', ev);
      else if ((ev as ApiResponse).type === 'tool_pending') sseWrite(res, 'tool_pending', ev);
      else if ((ev as ApiResponse).type === 'final') sseWrite(res, 'final', ev);
    }
  } catch (err) {
    sseWrite(res, 'error', { error: err instanceof Error ? err.message : 'stream error' });
  } finally {
    res.end();
  }
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

function handleModels(res: http.ServerResponse): void {
  initShared();
  // Advertise the deployment's configured default (docker/default-config.json),
  // not the compiled-in constant — the voice UI uses this for fresh browsers.
  sendJson(res, 200, {
    models: modelsWithAvailability(config),
    default: config.agents?.defaults?.model || DEFAULT_MODEL,
  });
}

async function handleChat(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as {
    message?: string;
    sessionId?: string;
    model?: string;
    profile?: unknown;
    analysisStyle?: unknown;
    responseMode?: unknown;
  } | null;
  if (!body || typeof body.message !== 'string' || !body.message.trim()) {
    sendJson(res, 400, { error: 'Missing or empty "message" field' });
    return;
  }
  if (body.sessionId !== undefined && !isValidSessionId(body.sessionId)) {
    sendJson(res, 400, { error: 'Invalid "sessionId" field' });
    return;
  }
  if (
    body.analysisStyle !== undefined &&
    body.analysisStyle !== 'topic_map' &&
    body.analysisStyle !== 'principle_graph'
  ) {
    sendJson(res, 400, { error: 'Invalid "analysisStyle" field' });
    return;
  }
  if (
    body.responseMode !== undefined &&
    body.responseMode !== 'text' &&
    body.responseMode !== 'voice'
  ) {
    sendJson(res, 400, { error: 'Invalid "responseMode" field' });
    return;
  }
  const sessionId = body.sessionId ?? 'default';
  const memory = getMemory(sessionId);

  memory.addMessage({ role: 'user', content: body.message });

  const profile = typeof body.profile === 'string' ? body.profile : undefined;
  const agentConfig = getAgentConfig(
    body.model,
    profile,
    body.analysisStyle as AnalysisStyle | undefined,
    body.responseMode as 'text' | 'voice' | undefined
  );
  if (wantsStream(req)) {
    const controller = new AbortController();
    res.once('close', () => controller.abort());
    await streamLoopToSSE(res, stepLoopStream(memory, agentConfig, 0, controller.signal));
    return;
  }
  const result = await stepLoop(memory, agentConfig, 0);
  sendJson(res, 200, result);
}

async function handleApprove(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as {
    requestId?: string;
    sessionId?: string;
  } | null;
  if (!body || typeof body.requestId !== 'string' || typeof body.sessionId !== 'string') {
    sendJson(res, 400, { error: 'Missing "requestId" or "sessionId" field' });
    return;
  }
  const pending = pendingRequests.get(body.requestId);

  if (!pending || pending.sessionId !== body.sessionId) {
    sendJson(res, 404, { error: 'Unknown or expired requestId' });
    return;
  }

  pendingRequests.delete(body.requestId);
  pendingTimestamps.delete(body.requestId);
  if (isEphemeralSessionId(pending.sessionId)) {
    sessionLastUsed.set(pending.sessionId, Date.now());
  }

  // Execute approved tools
  const toolRegistry = createToolRegistry();
  for (const toolCall of pending.toolCalls) {
    const toolName = toolCall.function.name;
    const toolArgs = safeParseToolArgs(toolCall.function.arguments);

    const toolStart = Date.now();
    const toolResult = await toolRegistry.execute(toolName, toolArgs);
    const toolDuration = Date.now() - toolStart;

    logger.info(
      {
        tool: toolName,
        success: toolResult.success,
        durationMs: toolDuration,
        ...(toolResult.error && { error: toolResult.error }),
      },
      'Tool execution complete'
    );

    pending.memory.addMessage({
      role: 'tool',
      content: toolResult.success ? toolResult.output : `Error: ${toolResult.error}`,
      name: toolName,
      tool_call_id: toolCall.id,
    });
  }

  // Continue the loop
  if (wantsStream(req)) {
    await streamLoopToSSE(
      res,
      stepLoopStream(pending.memory, pending.agentConfig, pending.iteration)
    );
    return;
  }
  const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration);
  sendJson(res, 200, result);
}

async function handleReject(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as {
    requestId?: string;
    sessionId?: string;
  } | null;
  if (!body || typeof body.requestId !== 'string' || typeof body.sessionId !== 'string') {
    sendJson(res, 400, { error: 'Missing "requestId" or "sessionId" field' });
    return;
  }
  const pending = pendingRequests.get(body.requestId);

  if (!pending || pending.sessionId !== body.sessionId) {
    sendJson(res, 404, { error: 'Unknown or expired requestId' });
    return;
  }

  pendingRequests.delete(body.requestId);
  pendingTimestamps.delete(body.requestId);
  if (isEphemeralSessionId(pending.sessionId)) {
    sessionLastUsed.set(pending.sessionId, Date.now());
  }

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
  if (wantsStream(req)) {
    await streamLoopToSSE(
      res,
      stepLoopStream(pending.memory, pending.agentConfig, pending.iteration)
    );
    return;
  }
  const result = await stepLoop(pending.memory, pending.agentConfig, pending.iteration);
  sendJson(res, 200, result);
}

async function handleDeleteSession(
  req: http.IncomingMessage,
  res: http.ServerResponse
): Promise<void> {
  const body = parseJsonBody(await readBody(req)) as { sessionId?: string } | null;
  if (!body || typeof body.sessionId !== 'string' || !isEphemeralSessionId(body.sessionId)) {
    sendJson(res, 400, { error: 'Invalid anonymous sessionId' });
    return;
  }

  deleteSession(body.sessionId);
  sendJson(res, 200, { deleted: true });
}

// ── Server ───────────────────────────────────────────────────

export function createServer(): http.Server {
  initShared();

  // Sweep anonymous files that outlived their cleanup callback. The age bound
  // avoids deleting a live conversation owned by another API process during a
  // rolling restart; periodic cleanup applies the same explicit 24-hour TTL.
  sweepEphemeralMemory(new Set(sessionMemories.keys()), EPHEMERAL_SESSION_TTL_MS);

  // Periodic cleanup of stale pending requests
  const cleanupInterval = setInterval(cleanupStale, 60_000);

  const server = http.createServer((req, res) => {
    void (async () => {
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
        } else if (method === 'GET' && url === '/api/models') {
          setCorsHeaders(res);
          handleModels(res);
        } else if (
          method === 'POST' &&
          (url === '/api/chat' || url === '/api/chat/approve' || url === '/api/chat/reject')
        ) {
          const ct = req.headers['content-type'] || '';
          if (!ct.includes('application/json')) {
            sendJson(res, 415, { error: 'Content-Type must be application/json' });
            return;
          }
          if (url === '/api/chat') await handleChat(req, res);
          else if (url === '/api/chat/approve') await handleApprove(req, res);
          else await handleReject(req, res);
        } else if (method === 'DELETE' && url === '/api/session') {
          const ct = req.headers['content-type'] || '';
          if (!ct.includes('application/json')) {
            sendJson(res, 415, { error: 'Content-Type must be application/json' });
            return;
          }
          await handleDeleteSession(req, res);
        } else {
          sendJson(res, 404, { error: 'Not found' });
        }
      } catch (error) {
        logger.error({ error, url, method }, 'API error');
        sendJson(res, 500, { error: (error as Error).message });
      }
    })();
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
