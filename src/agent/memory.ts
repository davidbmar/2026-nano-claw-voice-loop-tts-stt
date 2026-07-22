import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'fs';
import { join } from 'path';
import { Message } from '../types';
import { getMemoryDir } from '../utils/helpers';
import { logger } from '../utils/logger';
import {
  AnalysisConversationState,
  analysisConversationStateForStorage,
  parseAnalysisConversationState,
} from './analysis-navigation';

const EPHEMERAL_SESSION_RE = /^voice-[0-9a-f]{32}$/;
const SAFE_SESSION_RE = /^[A-Za-z0-9_-]{1,64}$/;
const ANALYSIS_SUFFIX = '.analysis.json';

/** Whether a session id is safe to use as a memory filename. */
export function isValidSessionId(sessionId: unknown): sessionId is string {
  return typeof sessionId === 'string' && SAFE_SESSION_RE.test(sessionId);
}

/** Reject unsafe ids before any memory path or filesystem operation occurs. */
export function assertValidSessionId(sessionId: string): void {
  if (!isValidSessionId(sessionId)) throw new Error('Invalid session id');
}

/** Whether a session id belongs to a server-generated browser conversation. */
export function isEphemeralSessionId(sessionId: string): boolean {
  return EPHEMERAL_SESSION_RE.test(sessionId);
}

function memoryPathFor(sessionId: string): string {
  assertValidSessionId(sessionId);
  return join(getMemoryDir(), `${sessionId}.json`);
}

function analysisPathFor(sessionId: string): string {
  assertValidSessionId(sessionId);
  return join(getMemoryDir(), `${sessionId}${ANALYSIS_SUFFIX}`);
}

/** Delete one persisted memory file without constructing/loading the memory. */
export function deleteMemoryFile(sessionId: string): boolean {
  const memoryPath = memoryPathFor(sessionId);
  const analysisPath = analysisPathFor(sessionId);
  let deleted = false;
  for (const path of [memoryPath, analysisPath]) {
    if (!existsSync(path)) continue;
    try {
      unlinkSync(path);
      deleted = true;
    } catch (error) {
      logger.error({ error, sessionId }, 'Failed to delete memory');
    }
  }
  if (deleted) logger.debug({ sessionId }, 'Memory deleted');
  return deleted;
}

/**
 * Delete orphaned anonymous browser memories.
 *
 * `activeSessionIds` protects conversations known to the current API process;
 * the age threshold also protects a live conversation served by another API
 * process during a rolling restart.
 */
export function sweepEphemeralMemory(
  activeSessionIds: ReadonlySet<string>,
  maxAgeMs: number,
  now = Date.now()
): number {
  const memoryDir = getMemoryDir();
  if (!existsSync(memoryDir)) return 0;

  let deleted = 0;
  for (const filename of readdirSync(memoryDir)) {
    if (!filename.endsWith('.json')) continue;
    const sessionId = filename.endsWith(ANALYSIS_SUFFIX)
      ? filename.slice(0, -ANALYSIS_SUFFIX.length)
      : filename.slice(0, -'.json'.length);
    if (!isEphemeralSessionId(sessionId) || activeSessionIds.has(sessionId)) continue;

    const memoryPath = join(memoryDir, filename);
    try {
      if (now - statSync(memoryPath).mtimeMs < maxAgeMs) continue;
      unlinkSync(memoryPath);
      deleted++;
    } catch (error) {
      logger.warn({ error, sessionId }, 'Failed to sweep orphaned memory');
    }
  }

  if (deleted > 0) logger.info({ deleted }, 'Swept orphaned anonymous memories');
  return deleted;
}

/**
 * Memory storage for conversations
 */
export class Memory {
  private sessionId: string;
  private memoryPath: string;
  private analysisPath: string;
  private messages: Message[] = [];
  private analysisState?: AnalysisConversationState;
  private maxMessages: number;
  private deleted = false;

  constructor(sessionId: string, maxMessages = 100) {
    this.sessionId = sessionId;
    this.maxMessages = maxMessages;
    this.memoryPath = memoryPathFor(sessionId);
    this.analysisPath = analysisPathFor(sessionId);

    const memoryDir = getMemoryDir();
    if (!existsSync(memoryDir)) {
      mkdirSync(memoryDir, { recursive: true });
    }

    this.load();
    this.loadAnalysisState();
  }

  /**
   * Load messages from disk
   */
  private load(): void {
    if (existsSync(this.memoryPath)) {
      try {
        const data = readFileSync(this.memoryPath, 'utf-8');
        const parsed = JSON.parse(data) as Message[];
        this.messages = parsed;
        logger.debug({ sessionId: this.sessionId, count: this.messages.length }, 'Memory loaded');
      } catch (error) {
        logger.error({ error, sessionId: this.sessionId }, 'Failed to load memory');
        this.messages = [];
      }
    }
  }

  /**
   * Save messages to disk
   */
  private save(): void {
    if (this.deleted) return;
    try {
      const data = JSON.stringify(this.messages, null, 2);
      writeFileSync(this.memoryPath, data, 'utf-8');
      logger.debug({ sessionId: this.sessionId, count: this.messages.length }, 'Memory saved');
    } catch (error) {
      logger.error({ error, sessionId: this.sessionId }, 'Failed to save memory');
    }
  }

  /** Load the generated-analysis sidecar without mixing it into the LLM transcript. */
  private loadAnalysisState(): void {
    if (!existsSync(this.analysisPath)) return;
    try {
      const parsed = parseAnalysisConversationState(
        JSON.parse(readFileSync(this.analysisPath, 'utf-8'))
      );
      if (!parsed) throw new Error('invalid analysis state');
      this.analysisState = parsed;
    } catch (error) {
      logger.warn({ error, sessionId: this.sessionId }, 'Failed to load analysis state');
      this.analysisState = undefined;
    }
  }

  private saveAnalysisState(): void {
    if (this.deleted) return;
    if (!this.analysisState) {
      try {
        if (existsSync(this.analysisPath)) unlinkSync(this.analysisPath);
      } catch (error) {
        logger.error({ error, sessionId: this.sessionId }, 'Failed to clear analysis state');
      }
      return;
    }
    try {
      writeFileSync(
        this.analysisPath,
        JSON.stringify(analysisConversationStateForStorage(this.analysisState), null, 2),
        'utf-8'
      );
    } catch (error) {
      logger.error({ error, sessionId: this.sessionId }, 'Failed to save analysis state');
    }
  }

  /**
   * Add a message to memory
   */
  addMessage(message: Message): void {
    if (this.deleted) return;
    this.messages.push(message);

    // Trim old messages if exceeding max
    if (this.messages.length > this.maxMessages) {
      // Keep system messages and trim from user/assistant messages
      const systemMessages = this.messages.filter((m) => m.role === 'system');
      const otherMessages = this.messages
        .filter((m) => m.role !== 'system')
        .slice(-this.maxMessages);
      this.messages = [...systemMessages, ...otherMessages];
    }

    this.save();
  }

  /**
   * Get all messages
   */
  getMessages(): Message[] {
    return [...this.messages];
  }

  /**
   * Get recent messages
   */
  getRecentMessages(count: number): Message[] {
    return this.messages.slice(-count);
  }

  /**
   * Clear all messages
   */
  clear(): void {
    this.messages = [];
    this.analysisState = undefined;
    this.save();
    this.saveAnalysisState();
  }

  /**
   * Permanently dispose this in-memory conversation and its persisted file.
   * Later writes from an already-running request become no-ops, preventing a
   * close/delete race from recreating an anonymous transcript on disk.
   */
  delete(): void {
    this.messages = [];
    this.analysisState = undefined;
    this.deleted = true;
    deleteMemoryFile(this.sessionId);
  }

  /** Get the owning session id for approval binding and lifecycle cleanup. */
  getSessionId(): string {
    return this.sessionId;
  }

  /** Persist one structured analysis map independently from the transcript. */
  setAnalysisState(state: AnalysisConversationState): void {
    if (this.deleted) return;
    this.analysisState = JSON.parse(JSON.stringify(state)) as AnalysisConversationState;
    this.saveAnalysisState();
  }

  /** Return a defensive copy so navigation cannot mutate persisted state accidentally. */
  getAnalysisState(): AnalysisConversationState | undefined {
    return this.analysisState
      ? (JSON.parse(JSON.stringify(this.analysisState)) as AnalysisConversationState)
      : undefined;
  }

  clearAnalysisState(): void {
    this.analysisState = undefined;
    this.saveAnalysisState();
  }

  /**
   * Update the last message
   */
  updateLastMessage(content: string): void {
    if (!this.deleted && this.messages.length > 0) {
      this.messages[this.messages.length - 1].content = content;
      this.save();
    }
  }

  /**
   * Get message count
   */
  getMessageCount(): number {
    return this.messages.length;
  }
}
