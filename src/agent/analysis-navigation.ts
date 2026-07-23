import type { AnalysisStyle } from '../types';

/**
 * Deterministic navigation over one completed deep-analysis artifact.
 *
 * Explicit conversation controls and stable IDs are resolved before lexical or
 * model behavior. Generated analysis remains distinct from source evidence.
 */

export type AnalysisWorkflow = 'evidence_analysis' | 'strategy_review';

export type AnalysisTopicKind =
  | 'assessment'
  | 'opportunity'
  | 'risk'
  | 'option'
  | 'recommendation'
  | 'experiment'
  | 'uncertainty';

export type AnalyticalFindingKind =
  'assessment' | 'inference' | 'risk' | 'option' | 'recommendation' | 'experiment';

export type AnalysisConfidence = 'low' | 'medium' | 'high';

export interface AnalysisClaim {
  claimId: string;
  text: string;
  disposition: 'supported' | 'partially_supported' | 'unsupported' | 'conflicting';
  evidenceIds: string[];
}

export interface AnalyticalFinding {
  findingId: string;
  kind: AnalyticalFindingKind;
  statement: string;
  basisClaimIds: string[];
  evidenceIds: string[];
  confidence: AnalysisConfidence;
  changesIf: string[];
}

export interface AnalysisTopic {
  topicId: string;
  kind: AnalysisTopicKind;
  label: string;
  aliases: string[];
  rank: number;
  voicePreview: string;
  summary: string;
  detail: string;
  findingIds: string[];
  claimIds: string[];
  evidenceIds: string[];
  relatedTopicIds: string[];
  childTopicIds: string[];
}

export interface MissingAnalysisEvidence {
  question: string;
  importance: AnalysisConfidence;
  relatedTopicIds: string[];
}

export interface AnalysisArtifact {
  artifactId: string;
  taskId: string;
  tenantId: string;
  schemaVersion: 'analysis_artifact_v1';
  workflow: AnalysisWorkflow;
  goal: string;
  title: string;
  bottomLine: string;
  recommendedTopicId?: string;
  topics: AnalysisTopic[];
  findings: AnalyticalFinding[];
  claims: AnalysisClaim[];
  missingEvidence: MissingAnalysisEvidence[];
  sourceSnapshotId: string;
  modelPolicy: {
    provider: string;
    model: string;
    thinking: string;
    effort: string;
  };
  promptVersion: string;
  createdAt: string;
}

export interface AnalysisConversationState {
  artifact: AnalysisArtifact;
  taskId: string;
  analysisStyle: AnalysisStyle;
  activeTopicId?: string;
  offeredTopicIds: string[];
  visitedTopicIds: string[];
  updatedAt: string;
}

export type AnalysisNavigationAction =
  | 'open_topic'
  | 'show_evidence'
  | 'list_topics'
  | 'render_report'
  | 'reanalyze'
  | 'show_gaps';

export interface AnalysisNavigationDecision {
  action: AnalysisNavigationAction;
  selectedTopicIds: string[];
  confidence: number;
  reason: string;
  /** Set when the decision adopted an artifact from the registry (debug surface). */
  artifactId?: string;
}

/**
 * Gap-shaped questions ask what the analyzed document does NOT contain. Exported
 * so the registry router and the 060 routing-evaluation corpus share one intent test.
 */
export const ANALYSIS_GAPS_RE =
  /\b(lacks?|lacking|missing|gaps?|not (?:cover(?:ed)?|address(?:ed)?|included?)|doesn'?t (?:cover|address|include)|left out|omit(?:s|ted)?|unresolved|open questions?)\b/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function nonempty(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const items: string[] = [];
  for (const item of value as unknown[]) {
    if (typeof item !== 'string' || !item.trim()) return undefined;
    items.push(item.trim());
  }
  return [...new Set(items)];
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[]): T | undefined {
  return typeof value === 'string' && allowed.includes(value as T) ? (value as T) : undefined;
}

const TOPIC_KINDS: readonly AnalysisTopicKind[] = [
  'assessment',
  'opportunity',
  'risk',
  'option',
  'recommendation',
  'experiment',
  'uncertainty',
];

const FINDING_KINDS: readonly AnalyticalFindingKind[] = [
  'assessment',
  'inference',
  'risk',
  'option',
  'recommendation',
  'experiment',
];

const CONFIDENCE: readonly AnalysisConfidence[] = ['low', 'medium', 'high'];
const DISPOSITIONS: readonly AnalysisClaim['disposition'][] = [
  'supported',
  'partially_supported',
  'unsupported',
  'conflicting',
];

function parseClaim(raw: unknown): AnalysisClaim | undefined {
  if (!isRecord(raw)) return undefined;
  const claimId = nonempty(raw.claim_id);
  const text = nonempty(raw.text);
  const disposition = enumValue(raw.disposition, DISPOSITIONS);
  const evidenceIds = stringArray(raw.evidence_ids);
  if (!claimId || !text || !disposition || !evidenceIds) return undefined;
  return { claimId, text, disposition, evidenceIds };
}

function parseFinding(raw: unknown): AnalyticalFinding | undefined {
  if (!isRecord(raw)) return undefined;
  const findingId = nonempty(raw.finding_id);
  const kind = enumValue(raw.kind, FINDING_KINDS);
  const statement = nonempty(raw.statement);
  const basisClaimIds = stringArray(raw.basis_claim_ids);
  const evidenceIds = stringArray(raw.evidence_ids);
  const confidence = enumValue(raw.confidence, CONFIDENCE);
  const changesIf = stringArray(raw.changes_if);
  if (
    !findingId ||
    !kind ||
    !statement ||
    !basisClaimIds ||
    !evidenceIds ||
    !confidence ||
    !changesIf
  ) {
    return undefined;
  }
  return {
    findingId,
    kind,
    statement,
    basisClaimIds,
    evidenceIds,
    confidence,
    changesIf,
  };
}

function parseTopic(raw: unknown): AnalysisTopic | undefined {
  if (!isRecord(raw)) return undefined;
  const topicId = nonempty(raw.topic_id);
  const kind = enumValue(raw.kind, TOPIC_KINDS);
  const label = nonempty(raw.label);
  const aliases = stringArray(raw.aliases);
  const rank = typeof raw.rank === 'number' && Number.isInteger(raw.rank) ? raw.rank : undefined;
  const voicePreview = nonempty(raw.voice_preview);
  const summary = nonempty(raw.summary);
  const detail = nonempty(raw.detail);
  const findingIds = stringArray(raw.finding_ids);
  const claimIds = stringArray(raw.claim_ids);
  const evidenceIds = stringArray(raw.evidence_ids);
  const relatedTopicIds = stringArray(raw.related_topic_ids);
  const childTopicIds = stringArray(raw.child_topic_ids);
  if (
    !topicId ||
    !kind ||
    !label ||
    rank === undefined ||
    rank < 1 ||
    !voicePreview ||
    !summary ||
    !detail ||
    !aliases ||
    !findingIds ||
    !claimIds ||
    !evidenceIds ||
    !relatedTopicIds ||
    !childTopicIds
  ) {
    return undefined;
  }
  return {
    topicId,
    kind,
    label,
    aliases,
    rank,
    voicePreview,
    summary,
    detail,
    findingIds,
    claimIds,
    evidenceIds,
    relatedTopicIds,
    childTopicIds,
  };
}

function parseMissingEvidence(raw: unknown): MissingAnalysisEvidence | undefined {
  if (!isRecord(raw)) return undefined;
  const question = nonempty(raw.question);
  const importance = enumValue(raw.importance, CONFIDENCE);
  const relatedTopicIds = stringArray(raw.related_topic_ids);
  if (!question || !importance || !relatedTopicIds) return undefined;
  return { question, importance, relatedTopicIds };
}

/** Parse and defensively verify the platform's versioned analysis artifact. */
export function parseAnalysisArtifact(raw: unknown): AnalysisArtifact | undefined {
  if (!isRecord(raw) || raw.schema_version !== 'analysis_artifact_v1') return undefined;
  const artifactId = nonempty(raw.artifact_id);
  const taskId = nonempty(raw.task_id);
  const tenantId = nonempty(raw.tenant_id);
  const workflow = enumValue(raw.workflow, ['evidence_analysis', 'strategy_review'] as const);
  const goal = nonempty(raw.goal);
  const title = nonempty(raw.title);
  const bottomLine = nonempty(raw.bottom_line);
  const sourceSnapshotId = nonempty(raw.source_snapshot_id);
  const promptVersion = nonempty(raw.prompt_version);
  const createdAt = nonempty(raw.created_at);
  const recommendedTopicId = nonempty(raw.recommended_topic_id);
  if (
    !artifactId ||
    !taskId ||
    !tenantId ||
    !workflow ||
    !goal ||
    !title ||
    !bottomLine ||
    !sourceSnapshotId ||
    !promptVersion ||
    !createdAt ||
    !Array.isArray(raw.topics) ||
    !Array.isArray(raw.findings) ||
    !Array.isArray(raw.claims) ||
    !Array.isArray(raw.missing_evidence) ||
    !isRecord(raw.model_policy)
  ) {
    return undefined;
  }

  const topics = raw.topics.map(parseTopic);
  const findings = raw.findings.map(parseFinding);
  const claims = raw.claims.map(parseClaim);
  const missingEvidence = raw.missing_evidence.map(parseMissingEvidence);
  if (
    topics.length < 3 ||
    topics.length > 7 ||
    topics.some((item) => !item) ||
    findings.some((item) => !item) ||
    claims.some((item) => !item) ||
    missingEvidence.some((item) => !item)
  ) {
    return undefined;
  }

  const parsedTopics = topics as AnalysisTopic[];
  const parsedFindings = findings as AnalyticalFinding[];
  const parsedClaims = claims as AnalysisClaim[];
  const parsedMissing = missingEvidence as MissingAnalysisEvidence[];
  const topicIds = new Set(parsedTopics.map((item) => item.topicId));
  const findingIds = new Set(parsedFindings.map((item) => item.findingId));
  const claimIds = new Set(parsedClaims.map((item) => item.claimId));
  const ranks = new Set(parsedTopics.map((item) => item.rank));
  if (
    topicIds.size !== parsedTopics.length ||
    findingIds.size !== parsedFindings.length ||
    claimIds.size !== parsedClaims.length ||
    ranks.size !== parsedTopics.length ||
    (recommendedTopicId && !topicIds.has(recommendedTopicId))
  ) {
    return undefined;
  }
  for (const topic of parsedTopics) {
    if (
      !topic.findingIds.every((id) => findingIds.has(id)) ||
      !topic.claimIds.every((id) => claimIds.has(id)) ||
      !topic.relatedTopicIds.every((id) => topicIds.has(id)) ||
      !topic.childTopicIds.every((id) => topicIds.has(id))
    ) {
      return undefined;
    }
  }
  for (const finding of parsedFindings) {
    if (!finding.basisClaimIds.every((id) => claimIds.has(id))) return undefined;
  }
  for (const missing of parsedMissing) {
    if (!missing.relatedTopicIds.every((id) => topicIds.has(id))) return undefined;
  }

  const children = new Map(parsedTopics.map((topic) => [topic.topicId, topic.childTopicIds]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (topicId: string): boolean => {
    if (visiting.has(topicId)) return false;
    if (visited.has(topicId)) return true;
    visiting.add(topicId);
    for (const childId of children.get(topicId) || []) {
      if (!visit(childId)) return false;
    }
    visiting.delete(topicId);
    visited.add(topicId);
    return true;
  };
  if (parsedTopics.some((topic) => !visit(topic.topicId))) return undefined;

  const provider = nonempty(raw.model_policy.provider);
  const model = nonempty(raw.model_policy.model);
  const thinking = nonempty(raw.model_policy.thinking);
  const effort = nonempty(raw.model_policy.effort);
  if (!provider || !model || !thinking || !effort) return undefined;

  return {
    artifactId,
    taskId,
    tenantId,
    schemaVersion: 'analysis_artifact_v1',
    workflow,
    goal,
    title,
    bottomLine,
    recommendedTopicId,
    topics: [...parsedTopics].sort((left, right) => left.rank - right.rank),
    findings: parsedFindings,
    claims: parsedClaims,
    missingEvidence: parsedMissing,
    sourceSnapshotId,
    modelPolicy: { provider, model, thinking, effort },
    promptVersion,
    createdAt,
  };
}

/** Create the smallest persistent state needed to resolve later voice turns. */
export function createAnalysisConversationState(
  artifact: AnalysisArtifact,
  taskId: string,
  analysisStyle: AnalysisStyle = artifact.promptVersion.includes('principle_graph')
    ? 'principle_graph'
    : 'topic_map',
  offeredTopicIds?: string[]
): AnalysisConversationState {
  const known = new Set(artifact.topics.map((item) => item.topicId));
  const offered = offeredTopicIds?.filter((id) => known.has(id));
  return {
    artifact,
    taskId,
    analysisStyle,
    offeredTopicIds: offered?.length
      ? offered
      : artifact.topics.slice(0, 3).map((item) => item.topicId),
    visitedTopicIds: [],
    updatedAt: new Date().toISOString(),
  };
}

function analysisArtifactForStorage(artifact: AnalysisArtifact): Record<string, unknown> {
  return {
    artifact_id: artifact.artifactId,
    task_id: artifact.taskId,
    tenant_id: artifact.tenantId,
    schema_version: artifact.schemaVersion,
    workflow: artifact.workflow,
    goal: artifact.goal,
    title: artifact.title,
    bottom_line: artifact.bottomLine,
    recommended_topic_id: artifact.recommendedTopicId,
    topics: artifact.topics.map((topic) => ({
      topic_id: topic.topicId,
      kind: topic.kind,
      label: topic.label,
      aliases: topic.aliases,
      rank: topic.rank,
      voice_preview: topic.voicePreview,
      summary: topic.summary,
      detail: topic.detail,
      finding_ids: topic.findingIds,
      claim_ids: topic.claimIds,
      evidence_ids: topic.evidenceIds,
      related_topic_ids: topic.relatedTopicIds,
      child_topic_ids: topic.childTopicIds,
    })),
    findings: artifact.findings.map((finding) => ({
      finding_id: finding.findingId,
      kind: finding.kind,
      statement: finding.statement,
      basis_claim_ids: finding.basisClaimIds,
      evidence_ids: finding.evidenceIds,
      confidence: finding.confidence,
      changes_if: finding.changesIf,
    })),
    claims: artifact.claims.map((claim) => ({
      claim_id: claim.claimId,
      text: claim.text,
      disposition: claim.disposition,
      evidence_ids: claim.evidenceIds,
    })),
    missing_evidence: artifact.missingEvidence.map((item) => ({
      question: item.question,
      importance: item.importance,
      related_topic_ids: item.relatedTopicIds,
    })),
    source_snapshot_id: artifact.sourceSnapshotId,
    model_policy: artifact.modelPolicy,
    prompt_version: artifact.promptVersion,
    created_at: artifact.createdAt,
  };
}

/** Serialize state using the platform artifact wire format before writing a sidecar. */
export function analysisConversationStateForStorage(
  state: AnalysisConversationState
): Record<string, unknown> {
  return {
    artifact: analysisArtifactForStorage(state.artifact),
    taskId: state.taskId,
    analysisStyle: state.analysisStyle,
    activeTopicId: state.activeTopicId,
    offeredTopicIds: state.offeredTopicIds,
    visitedTopicIds: state.visitedTopicIds,
    updatedAt: state.updatedAt,
  };
}

/** Parse a sidecar state file without trusting its shape or references. */
export function parseAnalysisConversationState(
  raw: unknown
): AnalysisConversationState | undefined {
  if (!isRecord(raw)) return undefined;
  const artifact = parseAnalysisArtifact(raw.artifact);
  const taskId = nonempty(raw.taskId);
  const analysisStyle =
    enumValue(raw.analysisStyle, ['topic_map', 'principle_graph'] as const) ||
    (artifact?.promptVersion.includes('principle_graph') ? 'principle_graph' : 'topic_map');
  const activeTopicId = nonempty(raw.activeTopicId);
  const offeredTopicIds = stringArray(raw.offeredTopicIds);
  const visitedTopicIds = stringArray(raw.visitedTopicIds);
  const updatedAt = nonempty(raw.updatedAt);
  if (!artifact || !taskId || !offeredTopicIds || !visitedTopicIds || !updatedAt) return undefined;
  const known = new Set(artifact.topics.map((item) => item.topicId));
  if (
    taskId !== artifact.taskId ||
    (activeTopicId && !known.has(activeTopicId)) ||
    !offeredTopicIds.every((id) => known.has(id)) ||
    !visitedTopicIds.every((id) => known.has(id))
  ) {
    return undefined;
  }
  return {
    artifact,
    taskId,
    analysisStyle,
    activeTopicId,
    offeredTopicIds,
    visitedTopicIds,
    updatedAt,
  };
}

function normalized(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s'-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const STOP_WORDS = new Set([
  'a',
  'about',
  'and',
  'can',
  'could',
  'explain',
  'go',
  'into',
  'me',
  'more',
  'of',
  'on',
  'please',
  'tell',
  'that',
  'the',
  'this',
  'topic',
  'what',
  'would',
  'you',
]);

function tokens(text: string): Set<string> {
  return new Set(
    normalized(text)
      .split(' ')
      .filter((item) => item && !STOP_WORDS.has(item))
  );
}

function openTopic(
  topicId: string,
  reason: string,
  confidence: number
): AnalysisNavigationDecision {
  return { action: 'open_topic', selectedTopicIds: [topicId], confidence, reason };
}

function ordinalIndex(text: string): number | undefined {
  const patterns: [RegExp, number][] = [
    [/\b(?:the\s+)?(?:first|1st)(?:\s+(?:one|option|topic))?\b/, 0],
    [/\b(?:the\s+)?(?:second|2nd)(?:\s+(?:one|option|topic))?\b/, 1],
    [/\b(?:the\s+)?(?:third|3rd)(?:\s+(?:one|option|topic))?\b/, 2],
  ];
  return patterns.find(([pattern]) => pattern.test(text))?.[1];
}

const REANALYSIS_RE =
  /\b(what if|suppose|assume|assuming|without|instead|compare|contrast|versus|scenario|new evidence|reconsider|challenge|does that change|would that change)\b/;
const EVIDENCE_RE =
  /\b(what evidence|which evidence|show (?:me )?(?:the )?evidence|where (?:does|did) that come from|where in the document|what supports that|source for that)\b/;
const REPORT_RE = /\b(full|complete|written) (?:analysis|report|review)|\bexport (?:the )?report\b/;
const MENU_RE =
  /\b(what else|more (?:options|topics)|other (?:options|topics)|list (?:the )?topics)\b/;
const ANALYSIS_OVERVIEW_RE =
  /\b(?:biggest|main|top|key|major) (?:weaknesses|weak points|risks|problems|issues)\b/;
const EXPAND_RE =
  /\b(tell me|explain|expand|go deeper|more about|what about|discuss|open|walk me through)\b/;

/** Resolve only high-confidence references to the current artifact. */
/** Topics the artifact's missing-evidence entries point at, capped for a spoken answer. */
export function gapRelatedTopicIds(artifact: AnalysisArtifact): string[] {
  const known = new Set(artifact.topics.map((topic) => topic.topicId));
  const related = artifact.missingEvidence
    .flatMap((item) => item.relatedTopicIds)
    .filter((id) => known.has(id));
  return [...new Set(related)].slice(0, 3);
}

export function resolveAnalysisFollowUp(
  userText: string,
  state: AnalysisConversationState
): AnalysisNavigationDecision | undefined {
  const text = normalized(userText);
  if (!text) return undefined;

  if (REPORT_RE.test(text)) {
    return {
      action: 'render_report',
      selectedTopicIds: state.artifact.topics.map((item) => item.topicId),
      confidence: 1,
      reason: 'explicit_report_request',
    };
  }
  if (EVIDENCE_RE.test(text) && state.activeTopicId) {
    return {
      action: 'show_evidence',
      selectedTopicIds: [state.activeTopicId],
      confidence: 1,
      reason: 'active_topic_evidence_request',
    };
  }
  if (REANALYSIS_RE.test(text)) {
    return {
      action: 'reanalyze',
      selectedTopicIds: state.activeTopicId ? [state.activeTopicId] : [],
      confidence: 0.95,
      reason: 'changed_analytical_question',
    };
  }
  if (ANALYSIS_GAPS_RE.test(text)) {
    return {
      action: 'show_gaps',
      selectedTopicIds: gapRelatedTopicIds(state.artifact),
      confidence: 0.9,
      reason: 'analysis_gaps_request',
    };
  }
  if (MENU_RE.test(text) || ANALYSIS_OVERVIEW_RE.test(text)) {
    const unoffered = state.artifact.topics
      .filter((item) => !state.offeredTopicIds.includes(item.topicId))
      .slice(0, 3)
      .map((item) => item.topicId);
    const repeatOverview = ANALYSIS_OVERVIEW_RE.test(text);
    return {
      action: 'list_topics',
      selectedTopicIds:
        !repeatOverview && unoffered.length > 0
          ? unoffered
          : state.artifact.topics.slice(0, 3).map((item) => item.topicId),
      confidence: 1,
      reason: repeatOverview
        ? 'analysis_overview'
        : unoffered.length > 0
          ? 'menu_overflow'
          : 'menu_repeat',
    };
  }

  const ordinal = ordinalIndex(text);
  if (ordinal !== undefined && state.offeredTopicIds[ordinal]) {
    return openTopic(state.offeredTopicIds[ordinal], 'ordinal_menu_selection', 1);
  }
  if (/\b(next|next one|continue)\b/.test(text)) {
    const ordered = state.artifact.topics.map((item) => item.topicId);
    const activeIndex = state.activeTopicId ? ordered.indexOf(state.activeTopicId) : -1;
    const next = ordered.slice(activeIndex + 1).find((id) => !state.visitedTopicIds.includes(id));
    if (next) return openTopic(next, 'next_unvisited_topic', 1);
  }
  if (/\b(go back|previous|back up)\b/.test(text) && state.visitedTopicIds.length > 1) {
    return openTopic(
      state.visitedTopicIds[state.visitedTopicIds.length - 2],
      'previous_visited_topic',
      1
    );
  }
  if (
    state.activeTopicId &&
    /\b(tell me more|go deeper|expand (?:on )?that|explain that|why is that)\b/.test(text)
  ) {
    return openTopic(state.activeTopicId, 'active_topic_reference', 1);
  }

  const expansion = EXPAND_RE.test(text);
  for (const topic of state.artifact.topics) {
    for (const phrase of [topic.label, ...topic.aliases].map(normalized)) {
      if (text === phrase || (expansion && text.includes(phrase))) {
        return openTopic(topic.topicId, 'topic_label_or_alias', 0.98);
      }
    }
  }
  if (!expansion) return undefined;

  const queryTokens = tokens(text);
  let best: { topicId: string; score: number } | undefined;
  let tied = false;
  for (const topic of state.artifact.topics) {
    const topicTokens = tokens([topic.label, ...topic.aliases, topic.summary].join(' '));
    const score = [...queryTokens].filter((token) => topicTokens.has(token)).length;
    if (!best || score > best.score) {
      best = { topicId: topic.topicId, score };
      tied = false;
    } else if (best && score === best.score) {
      tied = true;
    }
  }
  if (best && best.score >= 1 && !tied) {
    return openTopic(best.topicId, 'lexical_topic_match', Math.min(0.9, 0.65 + best.score * 0.1));
  }
  return undefined;
}

/** Apply a resolved action without relying on transcript reconstruction. */
export function applyAnalysisNavigation(
  state: AnalysisConversationState,
  decision: AnalysisNavigationDecision
): AnalysisConversationState {
  const selected = decision.selectedTopicIds.filter((id) =>
    state.artifact.topics.some((topic) => topic.topicId === id)
  );
  const activeTopicId =
    decision.action === 'open_topic' || decision.action === 'show_evidence'
      ? selected[0]
      : state.activeTopicId;
  const visitedTopicIds = activeTopicId
    ? [...state.visitedTopicIds.filter((id) => id !== activeTopicId), activeTopicId]
    : state.visitedTopicIds;
  return {
    ...state,
    activeTopicId,
    offeredTopicIds:
      decision.action === 'list_topics' && selected.length > 0 ? selected : state.offeredTopicIds,
    visitedTopicIds,
    updatedAt: new Date().toISOString(),
  };
}
