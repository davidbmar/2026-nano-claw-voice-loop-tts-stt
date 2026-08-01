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
import type { PendingDeepRequest } from './deep-reasoning';
import {
  AnalysisConversationState,
  analysisConversationStateForStorage,
  parseAnalysisConversationState,
} from './analysis-navigation';

const EPHEMERAL_SESSION_RE = /^voice-[0-9a-f]{32}$/;
const SAFE_SESSION_RE = /^[A-Za-z0-9_-]{1,64}$/;
const ANALYSIS_SUFFIX = '.analysis.json';
const KNOWLEDGE_SCOPE_SUFFIX = '.scope.json';
const DEFAULT_ANALYSIS_SCOPE_KEY = 'default';
const COLLECTION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,239}$/;

export type CollectionScopeMode = 'default' | 'selected' | 'none';

export interface CollectionScopeState {
  mode: CollectionScopeMode;
  collectionIds: string[];
}

interface PersistedCollectionScope {
  mode: 'selected' | 'none';
  collectionIds: string[];
}

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

function collectionScopePathFor(sessionId: string): string {
  assertValidSessionId(sessionId);
  return join(getMemoryDir(), `${sessionId}${KNOWLEDGE_SCOPE_SUFFIX}`);
}

function normalizedCollectionIds(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const normalized: string[] = [];
  for (const item of value) {
    if (typeof item !== 'string') return undefined;
    const collectionId = item.trim();
    if (!COLLECTION_ID_RE.test(collectionId)) return undefined;
    if (!normalized.includes(collectionId)) normalized.push(collectionId);
  }
  return normalized.sort();
}

/** Delete one persisted memory file without constructing/loading the memory. */
export function deleteMemoryFile(sessionId: string): boolean {
  const memoryPath = memoryPathFor(sessionId);
  const analysisPath = analysisPathFor(sessionId);
  const collectionScopePath = collectionScopePathFor(sessionId);
  let deleted = false;
  for (const path of [memoryPath, analysisPath, collectionScopePath]) {
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
    const suffix = filename.endsWith(ANALYSIS_SUFFIX)
      ? ANALYSIS_SUFFIX
      : filename.endsWith(KNOWLEDGE_SCOPE_SUFFIX)
        ? KNOWLEDGE_SCOPE_SUFFIX
        : '.json';
    const sessionId = filename.slice(0, -suffix.length);
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
  private collectionScopePath: string;
  private messages: Message[] = [];
  private analysisState?: AnalysisConversationState;
  private analysisScopeKey = DEFAULT_ANALYSIS_SCOPE_KEY;
  private pendingDeepRequest?: PendingDeepRequest;
  private pendingDeepScopeKey = DEFAULT_ANALYSIS_SCOPE_KEY;
  private collectionScopes = new Map<string, PersistedCollectionScope>();
  private maxMessages: number;
  private deleted = false;

  constructor(sessionId: string, maxMessages = 100) {
    this.sessionId = sessionId;
    this.maxMessages = maxMessages;
    this.memoryPath = memoryPathFor(sessionId);
    this.analysisPath = analysisPathFor(sessionId);
    this.collectionScopePath = collectionScopePathFor(sessionId);

    const memoryDir = getMemoryDir();
    if (!existsSync(memoryDir)) {
      mkdirSync(memoryDir, { recursive: true });
    }

    this.load();
    this.loadAnalysisState();
    this.loadCollectionScopes();
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
      const raw = JSON.parse(readFileSync(this.analysisPath, 'utf-8')) as unknown;
      const envelope =
        typeof raw === 'object' &&
        raw !== null &&
        'version' in raw &&
        (raw as { version?: unknown }).version === 2 &&
        'scopeKey' in raw &&
        typeof (raw as { scopeKey?: unknown }).scopeKey === 'string' &&
        'state' in raw
          ? (raw as { scopeKey: string; state: unknown })
          : undefined;
      const parsed = parseAnalysisConversationState(envelope?.state ?? raw);
      if (!parsed) throw new Error('invalid analysis state');
      this.analysisState = parsed;
      this.analysisScopeKey = envelope?.scopeKey || DEFAULT_ANALYSIS_SCOPE_KEY;
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
        JSON.stringify(
          {
            version: 2,
            scopeKey: this.analysisScopeKey,
            state: analysisConversationStateForStorage(this.analysisState),
          },
          null,
          2
        ),
        'utf-8'
      );
    } catch (error) {
      logger.error({ error, sessionId: this.sessionId }, 'Failed to save analysis state');
    }
  }

  /** Load persisted per-tenant/profile collection choices. */
  private loadCollectionScopes(): void {
    if (!existsSync(this.collectionScopePath)) return;
    try {
      const raw = JSON.parse(readFileSync(this.collectionScopePath, 'utf-8')) as unknown;
      if (
        typeof raw !== 'object' ||
        raw === null ||
        !('version' in raw) ||
        (raw as { version?: unknown }).version !== 1 ||
        !('scopes' in raw) ||
        typeof (raw as { scopes?: unknown }).scopes !== 'object' ||
        (raw as { scopes?: unknown }).scopes === null
      ) {
        throw new Error('invalid collection scope sidecar');
      }
      const scopes = (raw as { scopes: Record<string, unknown> }).scopes;
      for (const [scopeKey, value] of Object.entries(scopes)) {
        if (!scopeKey || typeof value !== 'object' || value === null) {
          throw new Error('invalid collection scope entry');
        }
        const mode = (value as { mode?: unknown }).mode;
        const collectionIds = normalizedCollectionIds(
          (value as { collectionIds?: unknown }).collectionIds
        );
        if (
          (mode !== 'selected' && mode !== 'none') ||
          !collectionIds ||
          (mode === 'selected' && collectionIds.length === 0) ||
          (mode === 'none' && collectionIds.length !== 0)
        ) {
          throw new Error('invalid collection scope entry');
        }
        this.collectionScopes.set(scopeKey, { mode, collectionIds });
      }
    } catch (error) {
      logger.warn({ error, sessionId: this.sessionId }, 'Failed to load collection scopes');
      this.collectionScopes.clear();
    }
  }

  private saveCollectionScopes(): void {
    if (this.deleted) return;
    if (this.collectionScopes.size === 0) {
      try {
        if (existsSync(this.collectionScopePath)) unlinkSync(this.collectionScopePath);
      } catch (error) {
        logger.error({ error, sessionId: this.sessionId }, 'Failed to clear collection scopes');
      }
      return;
    }
    try {
      writeFileSync(
        this.collectionScopePath,
        JSON.stringify(
          {
            version: 1,
            scopes: Object.fromEntries(
              [...this.collectionScopes.entries()].sort(([left], [right]) =>
                left.localeCompare(right)
              )
            ),
          },
          null,
          2
        ),
        'utf-8'
      );
    } catch (error) {
      logger.error({ error, sessionId: this.sessionId }, 'Failed to save collection scopes');
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
    this.pendingDeepRequest = undefined;
    this.collectionScopes.clear();
    this.save();
    this.saveAnalysisState();
    this.saveCollectionScopes();
  }

  /**
   * Permanently dispose this in-memory conversation and its persisted file.
   * Later writes from an already-running request become no-ops, preventing a
   * close/delete race from recreating an anonymous transcript on disk.
   */
  delete(): void {
    this.messages = [];
    this.analysisState = undefined;
    this.pendingDeepRequest = undefined;
    this.collectionScopes.clear();
    this.deleted = true;
    deleteMemoryFile(this.sessionId);
  }

  /** Get the owning session id for approval binding and lifecycle cleanup. */
  getSessionId(): string {
    return this.sessionId;
  }

  /**
   * One-turn reflect-hydrate-affirm state (task 063). Deliberately in-memory
   * only: it must not survive a server restart the way analysis state does.
   */
  getPendingDeepRequest(scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): PendingDeepRequest | undefined {
    return this.pendingDeepScopeKey === scopeKey ? this.pendingDeepRequest : undefined;
  }

  setPendingDeepRequest(request: PendingDeepRequest, scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): void {
    if (this.deleted) return;
    this.pendingDeepRequest = request;
    this.pendingDeepScopeKey = scopeKey;
  }

  clearPendingDeepRequest(scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): void {
    if (this.pendingDeepScopeKey !== scopeKey) return;
    this.pendingDeepRequest = undefined;
  }

  /** Bind the one-turn deep-confirmation store to a tenant/profile scope key. */
  pendingDeepStore(scopeKey: string): {
    getPendingDeepRequest(): PendingDeepRequest | undefined;
    setPendingDeepRequest(request: PendingDeepRequest): void;
    clearPendingDeepRequest(): void;
  } {
    return {
      getPendingDeepRequest: () => this.getPendingDeepRequest(scopeKey),
      setPendingDeepRequest: (request) => this.setPendingDeepRequest(request, scopeKey),
      clearPendingDeepRequest: () => this.clearPendingDeepRequest(scopeKey),
    };
  }

  /** Persist one structured analysis map independently from the transcript. */
  setAnalysisState(state: AnalysisConversationState, scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): void {
    if (this.deleted) return;
    this.analysisState = JSON.parse(JSON.stringify(state)) as AnalysisConversationState;
    this.analysisScopeKey = scopeKey;
    this.saveAnalysisState();
  }

  /** Return a defensive copy so navigation cannot mutate persisted state accidentally. */
  getAnalysisState(scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): AnalysisConversationState | undefined {
    return this.analysisState && this.analysisScopeKey === scopeKey
      ? (JSON.parse(JSON.stringify(this.analysisState)) as AnalysisConversationState)
      : undefined;
  }

  clearAnalysisState(scopeKey = DEFAULT_ANALYSIS_SCOPE_KEY): void {
    if (this.analysisState && this.analysisScopeKey !== scopeKey) return;
    this.analysisState = undefined;
    this.saveAnalysisState();
  }

  /** Resolve a persisted selection or the caller's immutable deployment default. */
  getCollectionScope(
    scopeKey: string,
    defaultCollectionIds: readonly string[]
  ): CollectionScopeState {
    const selected = this.collectionScopes.get(scopeKey);
    if (selected) {
      return { mode: selected.mode, collectionIds: [...selected.collectionIds] };
    }
    return {
      mode: 'default',
      collectionIds: normalizedCollectionIds([...defaultCollectionIds]) || [],
    };
  }

  /**
   * Persist an explicit collection set. An empty set means deliberately none,
   * never the platform's legacy empty-set/unfiltered behavior.
   */
  setCollectionScope(scopeKey: string, collectionIds: readonly string[]): CollectionScopeState {
    if (this.deleted || !scopeKey) {
      return { mode: 'none', collectionIds: [] };
    }
    const normalized = normalizedCollectionIds([...collectionIds]);
    if (!normalized) throw new Error('Invalid collection ids');
    const next: PersistedCollectionScope =
      normalized.length > 0
        ? { mode: 'selected', collectionIds: normalized }
        : { mode: 'none', collectionIds: [] };
    const current = this.collectionScopes.get(scopeKey);
    if (
      current?.mode === next.mode &&
      current.collectionIds.length === next.collectionIds.length &&
      current.collectionIds.every((value, index) => value === next.collectionIds[index])
    ) {
      return { mode: next.mode, collectionIds: [...next.collectionIds] };
    }
    this.collectionScopes.set(scopeKey, next);
    this.invalidateKnowledgeState(scopeKey);
    this.saveCollectionScopes();
    return { mode: next.mode, collectionIds: [...next.collectionIds] };
  }

  /** Forget an explicit choice so the next turn uses deployment defaults again. */
  resetCollectionScope(
    scopeKey: string,
    defaultCollectionIds: readonly string[]
  ): CollectionScopeState {
    if (this.collectionScopes.delete(scopeKey)) {
      this.invalidateKnowledgeState(scopeKey);
      this.saveCollectionScopes();
    }
    return this.getCollectionScope(scopeKey, defaultCollectionIds);
  }

  private invalidateKnowledgeState(scopeKey: string): void {
    if (this.analysisScopeKey === scopeKey) {
      this.analysisState = undefined;
      this.saveAnalysisState();
    }
    if (this.pendingDeepScopeKey === scopeKey) this.pendingDeepRequest = undefined;
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
