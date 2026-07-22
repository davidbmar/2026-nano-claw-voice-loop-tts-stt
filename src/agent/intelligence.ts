import axios, { AxiosInstance } from 'axios';
import { AgentConfig, Message } from '../types';
import { logger } from '../utils/logger';

export interface TurnEvidenceItem {
  evidenceId: string;
  citationId: string;
  title: string;
  sectionPath: string[];
  text: string;
  rank: number;
}

export interface TurnEvidence {
  status: 'retrieved' | 'no_match' | 'unavailable';
  items: TurnEvidenceItem[];
  durationMs: number;
  groundingMode: 'augment' | 'strict';
}

interface EvidenceResponse {
  evidence?: unknown;
}

type HttpClient = Pick<AxiosInstance, 'post'>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function parseEvidence(data: EvidenceResponse, maxChars: number): TurnEvidenceItem[] {
  if (!Array.isArray(data.evidence)) return [];
  const items: TurnEvidenceItem[] = [];
  let remaining = maxChars;

  for (const raw of data.evidence) {
    if (!isRecord(raw) || !isRecord(raw.citation) || !isRecord(raw.score)) continue;
    const locator = isRecord(raw.citation.locator) ? raw.citation.locator : {};
    const rawSectionPath = Array.isArray(locator.section_path) ? locator.section_path : [];
    const sectionPath = rawSectionPath.filter(
      (value): value is string => typeof value === 'string'
    );
    const text = stringValue(raw.text);
    const evidenceId = stringValue(raw.evidence_id);
    const citationId = stringValue(raw.citation.citation_id);
    const title = stringValue(raw.citation.title);
    const rank = numberValue(raw.score.rank);
    if (!text || !evidenceId || !citationId || !title || rank === undefined) continue;
    if (remaining <= 0) break;
    const selectedText =
      text.length <= remaining ? text : `${text.slice(0, Math.max(0, remaining - 1))}…`;
    remaining -= selectedText.length;
    if (!selectedText) break;
    items.push({ evidenceId, citationId, title, sectionPath, text: selectedText, rank });
  }
  return items;
}

function latestRetrievalQuestion(messages: Message[]): string | undefined {
  const userMessages = messages.filter(
    (message) => message.role === 'user' && message.content.trim()
  );
  const latest = userMessages.at(-1)?.content.trim();
  if (!latest) return undefined;

  // Give short referential follow-ups one preceding user turn without bloating
  // ordinary queries or involving another model before voice TTFT.
  if (/^(and\b|what about\b|how about\b|that\b|those\b|the next\b|it\b)/i.test(latest)) {
    const previous = userMessages.at(-2)?.content.trim();
    if (previous) return `${previous}\nFollow-up: ${latest}`;
  }
  return latest;
}

export async function retrieveTurnEvidence(
  messages: Message[],
  intelligence: AgentConfig['intelligence'],
  http: HttpClient = axios
): Promise<TurnEvidence | undefined> {
  if (!intelligence?.enabled) return undefined;
  const question = latestRetrievalQuestion(messages);
  if (!question) return undefined;

  const started = Date.now();
  try {
    const response = await http.post<EvidenceResponse>(
      `${intelligence.apiUrl.replace(/\/$/, '')}/v1/retrieve`,
      {
        text: question,
        policy: {
          tenant_id: intelligence.tenantId,
          principal_id: intelligence.principalId,
          permissions: ['knowledge:retrieve'],
        },
        scope: {
          tenant_id: intelligence.tenantId,
          collection_ids: intelligence.collectionIds,
        },
        limit: intelligence.limit,
        candidate_pool: intelligence.candidatePool,
      },
      { timeout: intelligence.timeoutMs }
    );
    const items = parseEvidence(response.data, intelligence.maxChars);
    const result: TurnEvidence = {
      status: items.length > 0 ? 'retrieved' : 'no_match',
      items,
      durationMs: Date.now() - started,
      groundingMode: intelligence.groundingMode,
    };
    logger.info(
      {
        status: result.status,
        evidenceCount: items.length,
        durationMs: result.durationMs,
      },
      'Turn evidence retrieval complete'
    );
    return result;
  } catch (error) {
    const durationMs = Date.now() - started;
    logger.warn(
      {
        durationMs,
        error: error instanceof Error ? error.message : String(error),
      },
      'Turn evidence retrieval unavailable'
    );
    return {
      status: 'unavailable',
      items: [],
      durationMs,
      groundingMode: intelligence.groundingMode,
    };
  }
}
