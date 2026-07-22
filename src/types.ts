/**
 * Core type definitions for nano-claw
 */

/**
 * Message role in conversation
 */
export type MessageRole = 'system' | 'user' | 'assistant' | 'tool';

/**
 * Message in conversation
 */
export interface Message {
  role: MessageRole;
  content: string;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}

/**
 * Tool call structure
 */
export interface ToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string;
  };
}

/**
 * Tool definition
 */
export interface ToolDefinition {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: {
      type: 'object';
      properties: Record<string, unknown>;
      required?: string[];
    };
  };
}

/**
 * Tool execution result
 */
export interface ToolResult {
  success: boolean;
  output: string;
  error?: string;
}

/**
 * Agent configuration
 */
export interface AgentConfig {
  model: string;
  temperature?: number;
  maxTokens?: number;
  systemPrompt?: string;
  knowledgeFiles?: string[];
  intelligence?: IntelligenceConfig;
}

/** Local evidence retrieval performed before the conversational model call. */
export interface IntelligenceConfig {
  enabled: boolean;
  apiUrl: string;
  tenantId: string;
  principalId: string;
  collectionIds: string[];
  limit: number;
  candidatePool: number;
  maxChars: number;
  timeoutMs: number;
  groundingMode: 'augment' | 'strict';
  deepReasoning?: DeepReasoningConfig;
}

export type AnalysisStyle = 'topic_map' | 'principle_graph';

/** Asynchronous, evidence-grounded reasoning used only for routed complex turns. */
export interface DeepReasoningConfig {
  enabled: boolean;
  routingMode: 'auto' | 'always' | 'never';
  threshold: number;
  acknowledgement: string;
  maxSteps: number;
  maxRetrievalQueries: number;
  pollIntervalMs: number;
  requestTimeoutMs: number;
  taskTimeoutMs: number;
  analysisStyle: AnalysisStyle;
}

/**
 * Session information
 */
export interface Session {
  id: string;
  userId: string;
  channelType: string;
  createdAt: Date;
  lastActivity: Date;
  metadata?: Record<string, unknown>;
}

/**
 * Skill definition
 */
export interface Skill {
  name: string;
  description: string;
  content: string;
  path: string;
}

/**
 * Provider configuration
 */
export interface ProviderConfig {
  apiKey?: string;
  apiBase?: string;
  enabled?: boolean;
}

/**
 * Channel message
 */
export interface ChannelMessage {
  id: string;
  sessionId: string;
  userId: string;
  content: string;
  channelType: string;
  timestamp: Date;
  metadata?: Record<string, unknown>;
}

/**
 * Cron job definition
 */
export interface CronJob {
  id: string;
  name: string;
  schedule: string;
  task: string;
  enabled: boolean;
  lastRun?: Date;
  nextRun?: Date;
}

/**
 * LLM response
 */
export interface LLMResponse {
  content: string;
  toolCalls?: ToolCall[];
  finishReason?: string;
  usage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    /** Prompt tokens served from the provider's prompt cache (Anthropic). */
    cacheReadTokens?: number;
    /** Prompt tokens written to the provider's prompt cache (Anthropic). */
    cacheWriteTokens?: number;
  };
}

/**
 * Marker the ContextBuilder places after the stable system-prompt prefix
 * (persona + knowledge). Providers that support prompt caching split here and
 * mark the prefix cacheable; all other providers must strip it before sending.
 */
export const SYSTEM_CACHE_MARKER = '\n[[cache-breakpoint]]\n';

/**
 * One event in a streamed LLM completion.
 */
export type StreamEvent =
  | { type: 'text'; delta: string }
  | {
      type: 'deep_started';
      acknowledgement: string;
      score: number;
      reasons: string[];
    }
  | {
      type: 'deep_progress';
      taskId: string;
      phase: string;
      message: string;
      completedSteps: number;
      maxSteps: number;
      retrievalQueries: number;
    }
  | { type: 'tool_calls'; toolCalls: ToolCall[] }
  | {
      type: 'done';
      finishReason?: string;
      usage?: { promptTokens: number; completionTokens: number; totalTokens: number };
    };

/**
 * Agent execution context
 */
export interface AgentContext {
  sessionId: string;
  userId: string;
  channelType: string;
  messages: Message[];
  skills: Skill[];
  tools: ToolDefinition[];
  config: AgentConfig;
}
