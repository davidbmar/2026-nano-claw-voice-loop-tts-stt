import axios, { AxiosInstance } from 'axios';
import type { AgentConfig, IntelligenceConfig, Message } from '../types';
import type { CollectionScopeState, Memory } from './memory';

export type CollectionScopeAction = 'available' | 'loaded' | 'load' | 'add' | 'drop';

export interface CollectionScopeIntent {
  action: CollectionScopeAction;
  target?: string;
}

export interface CollectionCatalogItem {
  collectionId: string;
  displayName: string;
}

interface CollectionCatalog {
  collections: CollectionCatalogItem[];
  threshold: number;
  ambiguityMargin: number;
}

export interface PreparedCollectionScope {
  scopeKey: string;
  scope: CollectionScopeState;
  intelligence: IntelligenceConfig;
  reply?: string;
  action?: CollectionScopeAction | 'none_loaded';
}

type ScopeHttpClient = Pick<AxiosInstance, 'get'>;

const DEFAULT_MATCH_THRESHOLD = 0.68;
const DEFAULT_AMBIGUITY_MARGIN = 0.08;
const COLLECTION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,239}$/;

function latestUserText(messages: Message[]): string | undefined {
  return messages
    .filter((message) => message.role === 'user' && message.content.trim())
    .at(-1)
    ?.content.trim();
}

/** Deterministic scope-command parser, exported for the shared routing corpus. */
export function matchCollectionScopeIntent(text: string): CollectionScopeIntent | undefined {
  const normalized = text
    .trim()
    .replace(/[.!?]+$/, '')
    .trim();
  if (
    /^(?:what(?:'s| is) available|what can we talk about|what (?:documents|collections|sources|corpora) (?:are )?available|list (?:the )?(?:available )?(?:documents|collections|sources|corpora))$/i.test(
      normalized
    )
  ) {
    return { action: 'available' };
  }
  if (
    /^(?:what(?:'s| is) loaded|what (?:documents|collections|sources|corpora) (?:are )?loaded|show (?:me )?what(?:'s| is) loaded)$/i.test(
      normalized
    )
  ) {
    return { action: 'loaded' };
  }

  const load = normalized.match(/^(?:please\s+)?(?:load|open|talk about)\s+(.+)$/i);
  if (load) return { action: 'load', target: load[1].trim() };
  const add = normalized.match(
    /^(?:please\s+)?add\s+(.+?)(?:\s+to (?:the )?(?:scope|loaded (?:set|collections?)))?$/i
  );
  if (add) return { action: 'add', target: add[1].trim() };
  const drop = normalized.match(
    /^(?:please\s+)?(?:drop|remove|unload)\s+(.+?)(?:\s+from (?:the )?(?:scope|loaded (?:set|collections?)))?$/i
  );
  if (drop) return { action: 'drop', target: drop[1].trim() };
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function boundedRatio(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
    ? value
    : fallback;
}

function parseCatalog(value: unknown): CollectionCatalog | undefined {
  if (!isRecord(value) || !Array.isArray(value.collections)) return undefined;
  const collections: CollectionCatalogItem[] = [];
  for (const raw of value.collections) {
    if (!isRecord(raw)) continue;
    const collectionId =
      typeof raw.collection_id === 'string' && COLLECTION_ID_RE.test(raw.collection_id)
        ? raw.collection_id
        : undefined;
    const displayName =
      typeof raw.display_name === 'string' && raw.display_name.trim()
        ? raw.display_name.trim()
        : undefined;
    if (!collectionId || !displayName) continue;
    collections.push({ collectionId, displayName });
  }
  return {
    collections,
    threshold: boundedRatio(value.fuzzy_match_threshold, DEFAULT_MATCH_THRESHOLD),
    ambiguityMargin: boundedRatio(value.ambiguity_margin, DEFAULT_AMBIGUITY_MARGIN),
  };
}

async function fetchCatalog(
  intelligence: IntelligenceConfig,
  signal: AbortSignal | undefined,
  http: ScopeHttpClient
): Promise<CollectionCatalog | undefined> {
  try {
    const response = await http.get(`${intelligence.apiUrl.replace(/\/$/, '')}/v1/collections`, {
      timeout: intelligence.timeoutMs,
      signal,
      headers: {
        'X-Tenant-Id': intelligence.tenantId,
        'X-Principal-Id': intelligence.principalId,
        'X-Permissions': 'knowledge:retrieve',
      },
    });
    return parseCatalog(response.data);
  } catch {
    return undefined;
  }
}

function normalizeName(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/^(?:the|a|an)\s+/, '');
}

function levenshtein(left: string, right: string): number {
  if (!left) return right.length;
  if (!right) return left.length;
  const previous = Array.from({ length: right.length + 1 }, (_, index) => index);
  for (let leftIndex = 1; leftIndex <= left.length; leftIndex++) {
    const current = [leftIndex];
    for (let rightIndex = 1; rightIndex <= right.length; rightIndex++) {
      current[rightIndex] = Math.min(
        current[rightIndex - 1] + 1,
        previous[rightIndex] + 1,
        previous[rightIndex - 1] + (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1)
      );
    }
    previous.splice(0, previous.length, ...current);
  }
  return previous[right.length];
}

function similarity(target: string, item: CollectionCatalogItem): number {
  const query = normalizeName(target);
  const names = [item.collectionId, item.displayName].map(normalizeName);
  return Math.max(
    ...names.map((name) => {
      if (query === name) return 1;
      const queryTokens = new Set(query.split(' ').filter(Boolean));
      const nameTokens = new Set(name.split(' ').filter(Boolean));
      const overlap = [...queryTokens].filter((token) => nameTokens.has(token)).length;
      const tokenDice =
        queryTokens.size + nameTokens.size > 0
          ? (2 * overlap) / (queryTokens.size + nameTokens.size)
          : 0;
      const edit =
        Math.max(query.length, name.length) > 0
          ? 1 - levenshtein(query, name) / Math.max(query.length, name.length)
          : 0;
      return Math.max(tokenDice, edit);
    })
  );
}

interface CatalogResolution {
  items: CollectionCatalogItem[];
  unknown?: string;
  ambiguous?: string;
}

function resolveOne(target: string, catalog: CollectionCatalog): CatalogResolution {
  const scored = catalog.collections
    .map((item) => ({ item, score: similarity(target, item) }))
    .sort(
      (left, right) =>
        right.score - left.score || left.item.collectionId.localeCompare(right.item.collectionId)
    );
  const best = scored[0];
  if (!best || best.score < catalog.threshold) return { items: [], unknown: target };
  const second = scored[1];
  if (
    second &&
    second.score >= catalog.threshold &&
    best.score - second.score < catalog.ambiguityMargin
  ) {
    return { items: [], ambiguous: target };
  }
  return { items: [best.item] };
}

function resolveTargets(target: string, catalog: CollectionCatalog): CatalogResolution {
  const whole = resolveOne(target, catalog);
  if (whole.items.length > 0) return whole;

  const parts = target
    .split(/\s+(?:and|plus)\s+|,/i)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length <= 1) return whole;

  const items: CollectionCatalogItem[] = [];
  for (const part of parts) {
    const resolved = resolveOne(part, catalog);
    if (resolved.unknown || resolved.ambiguous) return resolved;
    for (const item of resolved.items) {
      if (!items.some((existing) => existing.collectionId === item.collectionId)) {
        items.push(item);
      }
    }
  }
  return { items };
}

function spokenList(items: readonly string[]): string {
  if (items.length === 0) return 'nothing';
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(', ')}, and ${items.at(-1)}`;
}

function fallbackDisplayName(collectionId: string): string {
  return collectionId
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function displayLoaded(scope: CollectionScopeState): string {
  if (scope.mode === 'none') return 'Nothing is loaded.';
  if (scope.mode === 'default' && scope.collectionIds.length === 0) {
    return 'Everything available is loaded by default.';
  }
  return `Loaded: ${spokenList(scope.collectionIds.map(fallbackDisplayName))}.`;
}

/** Stable isolation key: collection/analysis state never crosses tenant or profile. */
export function collectionScopeKey(
  intelligence: IntelligenceConfig,
  profileId = 'default'
): string {
  return JSON.stringify([intelligence.tenantId, profileId]);
}

/**
 * Resolve one session turn before analysis routing or retrieval. Scope commands
 * are deterministic and explicit-none fails closed without calling the model.
 */
export async function prepareCollectionScopeTurn(
  messages: Message[],
  memory: Memory,
  agentConfig: AgentConfig,
  signal?: AbortSignal,
  http: ScopeHttpClient = axios
): Promise<PreparedCollectionScope | undefined> {
  const configured = agentConfig.intelligence;
  if (!configured?.enabled) return undefined;
  // Direct/library callers that predate profile selection keep the historical
  // default analysis bucket. Live API/AgentLoop paths always supply a keyed
  // tenant/profile identity.
  const scopeKey = agentConfig.intelligenceScopeKey || 'default';
  let scope = memory.getCollectionScope(scopeKey, configured.collectionIds);
  const intent = matchCollectionScopeIntent(latestUserText(messages) || '');

  if (!intent) {
    // The deployment's document space can be empty in the same way a session
    // can: documents exist but the customer has ticked none. That only speaks
    // for the turn while the session has not chosen its own scope by voice —
    // an explicit "load X" still wins for as long as the session lasts.
    const emptyByDeployment = !!configured.documentScopeEmpty && scope.mode === 'default';
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      ...((scope.mode === 'none' || emptyByDeployment) && {
        reply: 'Nothing is loaded. Say “what’s available” or “load” followed by a collection name.',
        action: 'none_loaded' as const,
      }),
    };
  }

  if (intent.action === 'loaded') {
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      reply: displayLoaded(scope),
      action: intent.action,
    };
  }

  const catalog = await fetchCatalog(configured, signal, http);
  if (!catalog) {
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      reply: "I couldn't check the available collections just now. Nothing changed.",
      action: intent.action,
    };
  }
  if (intent.action === 'available') {
    const available = catalog.collections.map((item) => item.displayName);
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      reply:
        available.length > 0
          ? `Available: ${spokenList(available)}.`
          : "I don't have any ingested collections available.",
      action: intent.action,
    };
  }

  const resolved = resolveTargets(intent.target || '', catalog);
  if (resolved.unknown) {
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      reply: `I don't have “${resolved.unknown}” loaded. Available: ${spokenList(
        catalog.collections.map((item) => item.displayName)
      )}.`,
      action: intent.action,
    };
  }
  if (resolved.ambiguous) {
    return {
      scopeKey,
      scope,
      intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
      reply: `“${resolved.ambiguous}” matches more than one collection. Please use one of these names: ${spokenList(
        catalog.collections.map((item) => item.displayName)
      )}.`,
      action: intent.action,
    };
  }

  const resolvedIds = resolved.items.map((item) => item.collectionId);
  const currentIds =
    scope.mode === 'default' && scope.collectionIds.length === 0
      ? catalog.collections.map((item) => item.collectionId)
      : scope.collectionIds;
  const nextIds =
    intent.action === 'load'
      ? resolvedIds
      : intent.action === 'add'
        ? [...new Set([...currentIds, ...resolvedIds])]
        : currentIds.filter((collectionId) => !resolvedIds.includes(collectionId));
  scope = memory.setCollectionScope(scopeKey, nextIds);
  const labels = scope.collectionIds.map(
    (collectionId) =>
      catalog.collections.find((item) => item.collectionId === collectionId)?.displayName ||
      fallbackDisplayName(collectionId)
  );
  return {
    scopeKey,
    scope,
    intelligence: { ...configured, collectionIds: [...scope.collectionIds] },
    reply: scope.mode === 'none' ? 'Nothing is loaded.' : `Loaded: ${spokenList(labels)}.`,
    action: intent.action,
  };
}
