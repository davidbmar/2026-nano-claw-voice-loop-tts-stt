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

const EPHEMERAL_SESSION_RE = /^voice-[0-9a-f]{32}$/;
const SAFE_SESSION_RE = /^[A-Za-z0-9_-]{1,64}$/;

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

/** Delete one persisted memory file without constructing/loading the memory. */
export function deleteMemoryFile(sessionId: string): boolean {
  const memoryPath = memoryPathFor(sessionId);
  if (!existsSync(memoryPath)) return false;
  try {
    unlinkSync(memoryPath);
    logger.debug({ sessionId }, 'Memory deleted');
    return true;
  } catch (error) {
    logger.error({ error, sessionId }, 'Failed to delete memory');
    return false;
  }
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
    const sessionId = filename.slice(0, -'.json'.length);
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
  private messages: Message[] = [];
  private maxMessages: number;
  private deleted = false;

  constructor(sessionId: string, maxMessages = 100) {
    this.sessionId = sessionId;
    this.maxMessages = maxMessages;
    this.memoryPath = memoryPathFor(sessionId);

    const memoryDir = getMemoryDir();
    if (!existsSync(memoryDir)) {
      mkdirSync(memoryDir, { recursive: true });
    }

    this.load();
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
    this.save();
  }

  /**
   * Permanently dispose this in-memory conversation and its persisted file.
   * Later writes from an already-running request become no-ops, preventing a
   * close/delete race from recreating an anonymous transcript on disk.
   */
  delete(): void {
    this.messages = [];
    this.deleted = true;
    deleteMemoryFile(this.sessionId);
  }

  /** Get the owning session id for approval binding and lifecycle cleanup. */
  getSessionId(): string {
    return this.sessionId;
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
