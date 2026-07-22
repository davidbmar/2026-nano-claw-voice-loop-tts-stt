import axios from 'axios';
import type { DeepReasoningConfig, IntelligenceConfig, Message } from '../types';
import { logger } from '../utils/logger';
import {
  AnalysisArtifact,
  AnalysisConversationState,
  AnalysisNavigationDecision,
  applyAnalysisNavigation,
  createAnalysisConversationState,
  parseAnalysisArtifact,
  resolveAnalysisFollowUp,
} from './analysis-navigation';

export const DEFAULT_DEEP_REASONING_CONFIG: DeepReasoningConfig = {
  enabled: false,
  routingMode: 'auto',
  threshold: 4,
  acknowledgement: 'Let me think deeply about this.',
  maxSteps: 6,
  maxRetrievalQueries: 10,
  pollIntervalMs: 750,
  requestTimeoutMs: 5000,
  taskTimeoutMs: 240000,
  analysisStyle: 'topic_map',
};

export type DeepReasoningWorkflow = 'evidence_analysis' | 'strategy_review';

export interface DeepRouteDecision {
  deep: boolean;
  score: number;
  reasons: string[];
  workflow: DeepReasoningWorkflow;
}

export interface DeepProgress {
  taskId: string;
  phase: string;
  message: string;
  completedSteps: number;
  maxSteps: number;
  retrievalQueries: number;
}

export interface DeepClaim {
  claimId?: string;
  text: string;
  disposition: 'supported' | 'partially_supported' | 'unsupported' | 'conflicting';
  evidenceIds: string[];
}

export type AnalysisPresentationMode = 'brief' | 'topic' | 'evidence' | 'menu' | 'report';

export interface AnalysisPresentation {
  mode: AnalysisPresentationMode;
  selectedTopicIds: string[];
  reason: string;
}

export interface DeepModelUsage {
  provider: string;
  model: string;
  passNumber: number;
  inputTokens: number;
  cachedInputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  totalTokens: number;
  durationMs: number;
}

export interface ExistingAnalysisTurn {
  decision: AnalysisNavigationDecision;
  state: AnalysisConversationState;
  result?: DeepReasoningResult;
  deepRoute?: DeepRouteDecision;
}

export interface DeepEvidence {
  evidenceId: string;
  title: string;
  sectionPath: string[];
  text: string;
}

export interface DeepReasoningResult {
  status: 'succeeded' | 'failed' | 'cancelled' | 'unavailable';
  workflow: DeepReasoningWorkflow;
  taskId?: string;
  answer?: string;
  claims: DeepClaim[];
  evidence: DeepEvidence[];
  artifact?: AnalysisArtifact;
  presentation?: AnalysisPresentation;
  modelUsage: DeepModelUsage[];
  errorCode?: string;
  durationMs: number;
  completedSteps: number;
  retrievalQueries: number;
}

export interface AnalysisVoiceGuard {
  text: string;
  limit?: number;
  replaced: boolean;
}

export type DeepReasoningEvent =
  { type: 'progress'; progress: DeepProgress } | { type: 'result'; result: DeepReasoningResult };

interface HttpResponse<T> {
  data: T;
}

interface DeepHttpClient {
  post<T>(
    url: string,
    data?: unknown,
    config?: { timeout?: number; signal?: AbortSignal }
  ): Promise<HttpResponse<T>>;
  get<T>(
    url: string,
    config?: {
      timeout?: number;
      signal?: AbortSignal;
      headers?: Record<string, string>;
    }
  ): Promise<HttpResponse<T>>;
}

interface TaskView {
  task_id?: unknown;
  status?: unknown;
  workflow?: unknown;
  progress?: unknown;
  result?: unknown;
}

interface AnalysisSearchView {
  matches?: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function nonempty(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function spokenWordCount(text: string): number {
  const normalized = text.trim();
  return normalized ? normalized.split(/\s+/).length : 0;
}

function spokenList(labels: string[]): string {
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return `${labels[0]} or ${labels[1]}`;
  return `${labels.slice(0, -1).join(', ')}, or ${labels.at(-1)}`;
}

/** Return the hard pre-audio word limit for bounded artifact projections. */
export function analysisVoiceWordLimit(
  result: DeepReasoningResult | undefined
): number | undefined {
  if (result?.status !== 'succeeded' || !result.artifact) return undefined;
  const mode = result.presentation?.mode || 'brief';
  if (mode === 'brief') return 65;
  if (mode === 'menu') return 45;
  return undefined;
}

function deterministicAnalysisSpeech(result: DeepReasoningResult): string {
  const artifact = result.artifact!;
  const selectedIds =
    result.presentation?.selectedTopicIds ||
    artifact.topics.slice(0, 3).map((item) => item.topicId);
  const topics = new Map(artifact.topics.map((topic) => [topic.topicId, topic]));
  const labels = selectedIds.flatMap((topicId) => {
    const topic = topics.get(topicId);
    return topic ? [topic.label] : [];
  });
  const menu = spokenList(
    labels.length ? labels : artifact.topics.slice(0, 3).map((item) => item.label)
  );
  if (result.presentation?.mode === 'menu') {
    return `We can explore ${menu}. Which would you like?`;
  }
  if (result.answer && spokenWordCount(result.answer) <= 65) return result.answer.trim();
  const bottomLine = artifact.bottomLine.replace(/[.!?]+$/, '');
  return `${bottomLine}. We can explore ${menu}.`;
}

/** Prevent an overlong fast-model brief from reaching streaming/TTS output. */
export function guardAnalysisVoiceResponse(
  text: string,
  result: DeepReasoningResult | undefined
): AnalysisVoiceGuard {
  const limit = analysisVoiceWordLimit(result);
  const normalized = text.trim();
  if (limit === undefined || (normalized && spokenWordCount(normalized) <= limit)) {
    return { text, limit, replaced: false };
  }
  return {
    text: deterministicAnalysisSpeech(result!),
    limit,
    replaced: true,
  };
}

function settings(intelligence: IntelligenceConfig): DeepReasoningConfig {
  return { ...DEFAULT_DEEP_REASONING_CONFIG, ...intelligence.deepReasoning };
}

function latestUserText(messages: Message[]): string | undefined {
  return messages
    .filter((message) => message.role === 'user' && message.content.trim())
    .at(-1)
    ?.content.trim();
}

function shouldSearchActiveAnalysis(userText: string): boolean {
  const text = userText.toLowerCase();
  if (/\b(think deeply|deep analysis|analy[sz]e deeply|deep dive|reason through)\b/.test(text)) {
    return false;
  }
  if (
    /\b(document|paper|chapter|section|quote|citation|source passage|where in|what does it say|how many)\b/.test(
      text
    )
  ) {
    return false;
  }
  return /\b(analysis|conclusion|recommendation|assumption|weakness|risk|option|trade-?off|principle|tension|finding|confidence|boundary|condition|what would change|why do you think)\b/.test(
    text
  );
}

function analysisSearchDecision(
  raw: AnalysisSearchView,
  state: AnalysisConversationState
): AnalysisNavigationDecision | undefined {
  if (!Array.isArray(raw.matches)) return undefined;
  const knownTopics = new Set(state.artifact.topics.map((topic) => topic.topicId));
  const candidates: { topicId: string; confidence: number }[] = [];
  for (const rawMatch of raw.matches) {
    if (!isRecord(rawMatch) || !isRecord(rawMatch.node)) continue;
    const node = rawMatch.node;
    if (
      nonempty(node.artifact_id) !== state.artifact.artifactId ||
      nonempty(node.tenant_id) !== state.artifact.tenantId
    ) {
      continue;
    }
    const normalizedScore = Math.max(0, Math.min(1, finiteNumber(rawMatch.normalized_score)));
    const refs: string[] = [];
    if (node.kind === 'topic') {
      const refId = nonempty(node.ref_id);
      if (refId) refs.push(refId);
    }
    if (Array.isArray(node.topic_ids)) {
      refs.push(...node.topic_ids.filter((value): value is string => typeof value === 'string'));
    }
    for (const topicId of refs) {
      if (
        knownTopics.has(topicId) &&
        !candidates.some((candidate) => candidate.topicId === topicId)
      ) {
        candidates.push({ topicId, confidence: normalizedScore });
      }
    }
  }
  if (!candidates.length) return undefined;
  const selected = candidates[0];
  return {
    action: 'open_topic',
    selectedTopicIds: [selected.topicId],
    confidence: Math.max(0.65, selected.confidence),
    reason: 'artifact_node_search',
  };
}

async function searchActiveAnalysis(
  userText: string,
  state: AnalysisConversationState,
  intelligence: IntelligenceConfig,
  signal: AbortSignal | undefined,
  http: DeepHttpClient
): Promise<AnalysisNavigationDecision | undefined> {
  if (!shouldSearchActiveAnalysis(userText)) return undefined;
  try {
    const response = await http.post<AnalysisSearchView>(
      `${intelligence.apiUrl.replace(/\/$/, '')}/v1/analysis/search`,
      {
        text: userText,
        artifact_id: state.artifact.artifactId,
        policy: {
          tenant_id: intelligence.tenantId,
          principal_id: intelligence.principalId,
          permissions: ['knowledge:reason'],
        },
        limit: 3,
      },
      { timeout: settings(intelligence).requestTimeoutMs, signal }
    );
    return analysisSearchDecision(response.data, state);
  } catch (error) {
    logger.warn(
      {
        artifactId: state.artifact.artifactId,
        error: error instanceof Error ? error.message : String(error),
      },
      'Active analysis node search unavailable'
    );
    return undefined;
  }
}

function strategyWorkflow(messages: Message[]): DeepReasoningWorkflow {
  const userMessages = messages
    .filter((message) => message.role === 'user' && message.content.trim())
    .slice(-3)
    .map((message) => message.content.toLowerCase());
  const latest = userMessages.at(-1) || '';
  const recent = userMessages.join(' ');
  const hasStrategySubject =
    /\b(business plan|business model|strateg(?:y|ic)|go-to-market|gtm|unit economics|pricing model|market entry|growth plan|operating plan|competitive advantage)\b/.test(
      recent
    );
  const asksForJudgment =
    /\b(review|critique|assess|evaluat(?:e|ion)|analy[sz]e|advise|advice|recommend|improv(?:e|ement)|viab(?:le|ility)|feasib(?:le|ility)|realistic|sound|good|bad|weakness(?:es)?|blind spots?|stress[- ]test|challenge|compare|trade-?offs?|scenarios?|fail(?:ure)?|think deeply|deep analysis|deep dive|reason through|take a deeper look)\b|\b(what should|should (?:i|we)|would you|does this make sense|what do you think|thoughts on|worth pursuing|best (?:path|option|approach))\b/.test(
      latest
    );
  return hasStrategySubject && asksForJudgment ? 'strategy_review' : 'evidence_analysis';
}

/**
 * Fast local router: explicit requests always route, while auto mode requires
 * multiple complexity signals. It never sends the user's words to another
 * classifier model, so acknowledgement latency stays near zero.
 */
export function detectDeepQuestion(
  messages: Message[],
  intelligence?: IntelligenceConfig
): DeepRouteDecision {
  const workflow = strategyWorkflow(messages);
  if (!intelligence?.enabled) return { deep: false, score: 0, reasons: [], workflow };
  const config = settings(intelligence);
  if (!config.enabled || config.routingMode === 'never') {
    return { deep: false, score: 0, reasons: [], workflow };
  }
  if (config.routingMode === 'always') {
    return { deep: true, score: config.threshold, reasons: ['configured_always'], workflow };
  }

  const text = latestUserText(messages) || '';
  const lowered = text.toLowerCase();
  const words = lowered.match(/[a-z0-9'-]+/g) || [];
  const reasons: string[] = [];
  let score = 0;

  if (workflow === 'strategy_review') {
    score += config.threshold;
    reasons.push('strategy_review');
  }

  if (
    /\b(think deeply|deep analysis|analy[sz]e deeply|deep dive|reason through|take a deeper look)\b/.test(
      lowered
    )
  ) {
    score += config.threshold;
    reasons.push('explicit_deep_request');
  }
  if (/\b(compare|contrast|reconcile|synthesi[sz]e|evaluate)\b/.test(lowered)) {
    score += 3;
    reasons.push('cross_evidence_synthesis');
  }
  if (/\b(trade-?offs?|implications?|tensions?|contradictions?|patterns?)\b/.test(lowered)) {
    score += 2;
    reasons.push('relationship_analysis');
  }
  if (/\b(recommend|prioriti[sz]e|strategy|sequence|decision|should we)\b/.test(lowered)) {
    score += 2;
    reasons.push('judgment_or_recommendation');
  }
  if (
    /\b(across|between|throughout)\b/.test(lowered) &&
    /\b(section|phase|chapter|part)s?\b/.test(lowered)
  ) {
    score += 2;
    reasons.push('cross_section_scope');
  }
  if (/\b(why|how)\b/.test(lowered) && /\b(and|while|versus|vs\.?|then)\b/.test(lowered)) {
    score += 1;
    reasons.push('multi_part_causal_question');
  }
  if ((text.match(/\?/g) || []).length > 1 || /\b(first|second|finally)\b/.test(lowered)) {
    score += 1;
    reasons.push('multiple_subquestions');
  }
  if (words.length >= 24) {
    score += 1;
    reasons.push('long_form_question');
  }
  if (
    workflow !== 'strategy_review' &&
    words.length <= 12 &&
    /^(what is|what are|who |when |where |define |how many |does |is |are )/.test(lowered)
  ) {
    score -= 2;
    reasons.push('direct_lookup_shape');
  }

  return { deep: score >= config.threshold, score, reasons, workflow };
}

function reasoningGoal(messages: Message[]): string | undefined {
  const userMessages = messages.filter(
    (message) => message.role === 'user' && message.content.trim()
  );
  const latest = userMessages.at(-1)?.content.trim();
  if (!latest) return undefined;
  if (/^(and\b|what about\b|how about\b|that\b|those\b|the next\b|it\b)/i.test(latest)) {
    const previous = userMessages.at(-2)?.content.trim();
    if (previous) return `${previous}\nFollow-up: ${latest}`;
  }
  return latest;
}

function conversationContext(messages: Message[]): { role: string; content: string }[] {
  let remaining = 6000;
  const selected: { role: string; content: string }[] = [];
  for (const message of messages
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .slice(-6)) {
    if (remaining <= 0) break;
    const content = message.content.slice(0, remaining);
    remaining -= content.length;
    selected.push({ role: message.role, content });
  }
  return selected;
}

function parseProgress(view: TaskView): DeepProgress | undefined {
  const taskId = nonempty(view.task_id);
  if (!taskId || !isRecord(view.progress)) return undefined;
  return {
    taskId,
    phase: nonempty(view.progress.phase) || 'running',
    message: nonempty(view.progress.message) || 'Deep analysis is running.',
    completedSteps: finiteNumber(view.progress.completed_steps),
    maxSteps: finiteNumber(view.progress.max_steps, 1),
    retrievalQueries: finiteNumber(view.progress.retrieval_queries),
  };
}

function sameStrings(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value) => right.includes(value)) &&
    right.every((value) => left.includes(value))
  );
}

function artifactMatchesTaskResult(
  artifact: AnalysisArtifact,
  taskId: string | undefined,
  tenantId: string,
  workflow: DeepReasoningWorkflow,
  snapshotId: string | undefined,
  claims: DeepClaim[],
  evidence: DeepEvidence[]
): boolean {
  if (
    !taskId ||
    !snapshotId ||
    artifact.taskId !== taskId ||
    artifact.tenantId !== tenantId ||
    artifact.workflow !== workflow ||
    artifact.sourceSnapshotId !== snapshotId
  ) {
    return false;
  }

  const resultClaims = new Map(
    claims.flatMap((claim) => (claim.claimId ? [[claim.claimId, claim] as const] : []))
  );
  if (resultClaims.size !== claims.length || artifact.claims.length !== claims.length) return false;
  for (const claim of artifact.claims) {
    const resultClaim = resultClaims.get(claim.claimId);
    if (
      !resultClaim ||
      resultClaim.text !== claim.text ||
      resultClaim.disposition !== claim.disposition ||
      !sameStrings(resultClaim.evidenceIds, claim.evidenceIds)
    ) {
      return false;
    }
  }

  const knownEvidence = new Set(evidence.map((item) => item.evidenceId));
  const referencedEvidence = [
    ...artifact.claims.flatMap((claim) => claim.evidenceIds),
    ...artifact.findings.flatMap((finding) => finding.evidenceIds),
    ...artifact.topics.flatMap((topic) => topic.evidenceIds),
  ];
  return referencedEvidence.every((evidenceId) => knownEvidence.has(evidenceId));
}

function parseResult(view: TaskView, started: number, tenantId: string): DeepReasoningResult {
  const taskId = nonempty(view.task_id);
  const status = nonempty(view.status);
  const progress = parseProgress(view);
  const rawResult = isRecord(view.result) ? view.result : {};
  const rawWorkflow = nonempty(rawResult.workflow) || nonempty(view.workflow);
  const workflow: DeepReasoningWorkflow =
    rawWorkflow === 'strategy_review' ? 'strategy_review' : 'evidence_analysis';
  const claims: DeepClaim[] = [];
  if (Array.isArray(rawResult.claims)) {
    for (const raw of rawResult.claims) {
      if (!isRecord(raw)) continue;
      const text = nonempty(raw.text);
      const disposition = nonempty(raw.disposition);
      if (
        !text ||
        !disposition ||
        !['supported', 'partially_supported', 'unsupported', 'conflicting'].includes(disposition)
      ) {
        continue;
      }
      claims.push({
        claimId: nonempty(raw.claim_id),
        text,
        disposition: disposition as DeepClaim['disposition'],
        evidenceIds: Array.isArray(raw.evidence_ids)
          ? raw.evidence_ids.filter((value): value is string => typeof value === 'string')
          : [],
      });
    }
  }
  const parsedArtifact = parseAnalysisArtifact(rawResult.analysis_artifact);
  const rawArtifactPresent = rawResult.analysis_artifact !== undefined;
  const modelUsage: DeepModelUsage[] = [];
  if (Array.isArray(rawResult.model_usage)) {
    for (const raw of rawResult.model_usage) {
      if (!isRecord(raw)) continue;
      const provider = nonempty(raw.provider);
      const model = nonempty(raw.model);
      const passNumber = finiteNumber(raw.pass_number);
      if (!provider || !model || passNumber < 1) continue;
      modelUsage.push({
        provider,
        model,
        passNumber,
        inputTokens: finiteNumber(raw.input_tokens),
        cachedInputTokens: finiteNumber(raw.cached_input_tokens),
        outputTokens: finiteNumber(raw.output_tokens),
        reasoningTokens: finiteNumber(raw.reasoning_tokens),
        totalTokens: finiteNumber(raw.total_tokens),
        durationMs: finiteNumber(raw.duration_ms),
      });
    }
  }
  const evidence: DeepEvidence[] = [];
  if (Array.isArray(rawResult.evidence)) {
    for (const raw of rawResult.evidence) {
      if (!isRecord(raw) || !isRecord(raw.citation)) continue;
      const evidenceId = nonempty(raw.evidence_id);
      const title = nonempty(raw.citation.title);
      const locator = isRecord(raw.citation.locator) ? raw.citation.locator : {};
      const text = nonempty(raw.text);
      if (!evidenceId || !title || !text) continue;
      evidence.push({
        evidenceId,
        title,
        sectionPath: Array.isArray(locator.section_path)
          ? locator.section_path.filter((value): value is string => typeof value === 'string')
          : [],
        text,
      });
    }
  }
  const snapshotId = isRecord(rawResult.snapshot)
    ? nonempty(rawResult.snapshot.snapshot_id)
    : undefined;
  const artifact =
    parsedArtifact &&
    artifactMatchesTaskResult(
      parsedArtifact,
      taskId,
      tenantId,
      workflow,
      snapshotId,
      claims,
      evidence
    )
      ? parsedArtifact
      : undefined;
  const artifactRequired = status === 'succeeded' && workflow === 'strategy_review';
  const mappedStatus: DeepReasoningResult['status'] =
    status === 'succeeded' && (!artifactRequired || artifact)
      ? 'succeeded'
      : status === 'cancelled'
        ? 'cancelled'
        : 'failed';
  return {
    status: mappedStatus,
    workflow,
    taskId,
    answer: nonempty(rawResult.answer),
    claims,
    evidence,
    artifact,
    presentation: artifact
      ? {
          mode: 'brief',
          selectedTopicIds: artifact.topics.slice(0, 3).map((item) => item.topicId),
          reason: 'completed_deep_analysis',
        }
      : undefined,
    modelUsage,
    errorCode:
      (rawArtifactPresent || artifactRequired) && !artifact
        ? 'invalid_analysis_artifact'
        : nonempty(rawResult.error_code),
    durationMs: Date.now() - started,
    completedSteps: progress?.completedSteps || 0,
    retrievalQueries: progress?.retrievalQueries || 0,
  };
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error('deep reasoning aborted'));
      return;
    }
    const aborted = () => {
      clearTimeout(timer);
      reject(new Error('deep reasoning aborted'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', aborted);
      resolve();
    }, ms);
    signal?.addEventListener('abort', aborted, { once: true });
  });
}

async function cancelTask(
  apiUrl: string,
  taskId: string,
  intelligence: IntelligenceConfig,
  config: DeepReasoningConfig,
  http: DeepHttpClient
): Promise<void> {
  try {
    await http.post(
      `${apiUrl}/v1/reasoning/tasks/${encodeURIComponent(taskId)}/cancel`,
      {
        policy: {
          tenant_id: intelligence.tenantId,
          principal_id: intelligence.principalId,
          permissions: ['knowledge:reason'],
        },
      },
      { timeout: config.requestTimeoutMs }
    );
  } catch (error) {
    logger.warn(
      { error: error instanceof Error ? error.message : String(error) },
      'Deep reasoning cancellation was not acknowledged'
    );
  }
}

export async function* streamDeepReasoning(
  messages: Message[],
  intelligence: IntelligenceConfig,
  signal?: AbortSignal,
  http: DeepHttpClient = axios,
  routeOverride?: DeepRouteDecision
): AsyncGenerator<DeepReasoningEvent> {
  const config = settings(intelligence);
  const started = Date.now();
  const route = routeOverride || detectDeepQuestion(messages, intelligence);
  const workflow = route.workflow;
  const goal = reasoningGoal(messages);
  if (!goal) {
    yield {
      type: 'result',
      result: {
        status: 'unavailable',
        workflow,
        claims: [],
        evidence: [],
        modelUsage: [],
        errorCode: 'missing_goal',
        durationMs: 0,
        completedSteps: 0,
        retrievalQueries: 0,
      },
    };
    return;
  }

  const apiUrl = intelligence.apiUrl.replace(/\/$/, '');
  let taskId: string | undefined;
  let terminal = false;
  try {
    const submitted = await http.post<TaskView>(
      `${apiUrl}/v1/reasoning/tasks`,
      {
        tenant_id: intelligence.tenantId,
        goal,
        workflow,
        policy: {
          tenant_id: intelligence.tenantId,
          principal_id: intelligence.principalId,
          permissions: ['knowledge:retrieve', 'knowledge:reason'],
        },
        scope: {
          tenant_id: intelligence.tenantId,
          collection_ids: intelligence.collectionIds,
        },
        budget: {
          max_steps: config.maxSteps,
          max_retrieval_queries: config.maxRetrievalQueries,
        },
        output: {
          format: workflow === 'strategy_review' ? 'structured_analysis' : 'structured_answer',
          schema_name: workflow === 'strategy_review' ? 'analysis_artifact_v1' : 'answer_v1',
          response_mode: workflow === 'strategy_review' ? 'progressive_voice' : 'conversational',
          analysis_style: config.analysisStyle,
        },
        context: {
          conversation: conversationContext(messages),
          source_policy: 'indexed_documents_only',
          routing: { score: route.score, reasons: route.reasons },
        },
      },
      { timeout: config.requestTimeoutMs, signal }
    );
    taskId = nonempty(submitted.data.task_id);
    if (!taskId) throw new Error('reasoning API returned no task id');

    while (Date.now() - started < config.taskTimeoutMs) {
      const initialProgress = parseProgress(submitted.data);
      if (initialProgress) yield { type: 'progress', progress: initialProgress };
      const status = nonempty(submitted.data.status);
      if (status && ['succeeded', 'failed', 'cancelled'].includes(status)) {
        terminal = true;
        yield {
          type: 'result',
          result: parseResult(submitted.data, started, intelligence.tenantId),
        };
        return;
      }
      await wait(config.pollIntervalMs, signal);

      const polled = await http.get<TaskView>(
        `${apiUrl}/v1/reasoning/tasks/${encodeURIComponent(taskId)}`,
        {
          timeout: config.requestTimeoutMs,
          signal,
          headers: {
            'X-Tenant-Id': intelligence.tenantId,
            'X-Permissions': 'knowledge:reason',
          },
        }
      );
      submitted.data = polled.data;
    }
    throw new Error('deep reasoning timed out');
  } catch (error) {
    logger.warn(
      {
        hasTask: !!taskId,
        durationMs: Date.now() - started,
        error: error instanceof Error ? error.message : String(error),
      },
      'Deep reasoning unavailable'
    );
    yield {
      type: 'result',
      result: {
        status: signal?.aborted ? 'cancelled' : 'unavailable',
        workflow,
        taskId,
        claims: [],
        evidence: [],
        modelUsage: [],
        errorCode: signal?.aborted ? 'cancelled_by_caller' : 'reasoning_unavailable',
        durationMs: Date.now() - started,
        completedSteps: 0,
        retrievalQueries: 0,
      },
    };
  } finally {
    if (taskId && !terminal) {
      await cancelTask(apiUrl, taskId, intelligence, config, http);
    }
  }
}

export async function runDeepReasoning(
  messages: Message[],
  intelligence: IntelligenceConfig,
  signal?: AbortSignal,
  http: DeepHttpClient = axios,
  routeOverride?: DeepRouteDecision
): Promise<DeepReasoningResult> {
  let result: DeepReasoningResult | undefined;
  for await (const event of streamDeepReasoning(
    messages,
    intelligence,
    signal,
    http,
    routeOverride
  )) {
    if (event.type === 'result') result = event.result;
  }
  return (
    result || {
      status: 'unavailable',
      workflow: detectDeepQuestion(messages, intelligence).workflow,
      claims: [],
      evidence: [],
      modelUsage: [],
      errorCode: 'reasoning_unavailable',
      durationMs: 0,
      completedSteps: 0,
      retrievalQueries: 0,
    }
  );
}

/** Resolve a high-confidence follow-up against the current artifact before ordinary routing. */
export async function resolveExistingAnalysisTurn(
  messages: Message[],
  state: AnalysisConversationState,
  intelligence: IntelligenceConfig,
  signal?: AbortSignal,
  http: DeepHttpClient = axios
): Promise<ExistingAnalysisTurn | undefined> {
  const userText = latestUserText(messages);
  if (!userText) return undefined;
  const decision =
    resolveAnalysisFollowUp(userText, state) ||
    (await searchActiveAnalysis(userText, state, intelligence, signal, http));
  if (!decision) return undefined;
  const nextState = applyAnalysisNavigation(state, decision);
  if (decision.action === 'reanalyze') {
    return {
      decision,
      state: nextState,
      deepRoute: {
        deep: true,
        score: settings(intelligence).threshold,
        reasons: ['analysis_state_changed', decision.reason],
        workflow: state.artifact.workflow,
      },
    };
  }
  const sourceResult =
    decision.action === 'show_evidence'
      ? await fetchDeepReasoningResult(state.taskId, intelligence, signal, http)
      : undefined;
  return {
    decision,
    state: nextState,
    result: resultForAnalysisNavigation(nextState, decision, sourceResult),
  };
}

/** Persist only a validated structured artifact, never the task's raw evidence excerpts. */
export function analysisStateFromResult(
  result: DeepReasoningResult,
  analysisStyle = DEFAULT_DEEP_REASONING_CONFIG.analysisStyle
): AnalysisConversationState | undefined {
  if (result.status !== 'succeeded' || !result.taskId || !result.artifact) return undefined;
  return createAnalysisConversationState(result.artifact, result.taskId, analysisStyle);
}

/** Re-fetch one authorized completed result when a follow-up explicitly requests source evidence. */
export async function fetchDeepReasoningResult(
  taskId: string,
  intelligence: IntelligenceConfig,
  signal?: AbortSignal,
  http: DeepHttpClient = axios
): Promise<DeepReasoningResult> {
  const started = Date.now();
  try {
    const response = await http.get<TaskView>(
      `${intelligence.apiUrl.replace(/\/$/, '')}/v1/reasoning/tasks/${encodeURIComponent(taskId)}`,
      {
        timeout: settings(intelligence).requestTimeoutMs,
        signal,
        headers: {
          'X-Tenant-Id': intelligence.tenantId,
          'X-Permissions': 'knowledge:reason',
        },
      }
    );
    return parseResult(response.data, started, intelligence.tenantId);
  } catch (error) {
    logger.warn(
      { taskId, error: error instanceof Error ? error.message : String(error) },
      'Completed deep result could not be reloaded'
    );
    return {
      status: signal?.aborted ? 'cancelled' : 'unavailable',
      workflow: 'evidence_analysis',
      taskId,
      claims: [],
      evidence: [],
      modelUsage: [],
      errorCode: signal?.aborted ? 'cancelled_by_caller' : 'reasoning_unavailable',
      durationMs: Date.now() - started,
      completedSteps: 0,
      retrievalQueries: 0,
    };
  }
}

/** Project an existing artifact into the exact material needed for one follow-up. */
export function resultForAnalysisNavigation(
  state: AnalysisConversationState,
  decision: AnalysisNavigationDecision,
  sourceResult?: DeepReasoningResult
): DeepReasoningResult {
  const selected = new Set(decision.selectedTopicIds);
  const topics = state.artifact.topics.filter((topic) => selected.has(topic.topicId));
  const claimIds = new Set(topics.flatMap((topic) => topic.claimIds));
  const evidenceIds = new Set(topics.flatMap((topic) => topic.evidenceIds));
  for (const finding of state.artifact.findings) {
    if (topics.some((topic) => topic.findingIds.includes(finding.findingId))) {
      finding.basisClaimIds.forEach((id) => claimIds.add(id));
      finding.evidenceIds.forEach((id) => evidenceIds.add(id));
    }
  }
  const claims = state.artifact.claims
    .filter((claim) => decision.action === 'render_report' || claimIds.has(claim.claimId))
    .map((claim) => ({ ...claim }));
  claims.forEach((claim) => claim.evidenceIds.forEach((id) => evidenceIds.add(id)));

  const sourceMatches =
    sourceResult?.status === 'succeeded' &&
    sourceResult.artifact?.artifactId === state.artifact.artifactId
      ? sourceResult.evidence.filter((item) => evidenceIds.has(item.evidenceId))
      : [];
  const mode: AnalysisPresentationMode =
    decision.action === 'show_evidence'
      ? 'evidence'
      : decision.action === 'list_topics'
        ? 'menu'
        : decision.action === 'render_report'
          ? 'report'
          : 'topic';
  return {
    status: 'succeeded',
    workflow: state.artifact.workflow,
    taskId: state.taskId,
    claims,
    evidence: sourceMatches,
    artifact: state.artifact,
    presentation: {
      mode,
      selectedTopicIds: decision.selectedTopicIds,
      reason: decision.reason,
    },
    modelUsage: [],
    durationMs: sourceResult?.durationMs || 0,
    completedSteps: 0,
    retrievalQueries: 0,
  };
}

export function deepAcknowledgement(intelligence: IntelligenceConfig): string {
  return settings(intelligence).acknowledgement;
}
