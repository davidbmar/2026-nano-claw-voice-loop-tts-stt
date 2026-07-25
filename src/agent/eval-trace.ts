import type { IntelligenceConfig } from '../types';
import type { DeepReasoningResult } from './deep-reasoning';
import type { TurnEvidence, TurnEvidenceItem } from './intelligence';

export const CROSS_SOURCE_EVAL_TRACE_VERSION = 'cross-source-eval-v1' as const;

export type EvalRoute = 'fast' | 'deep' | 'registry' | 'scope';
export type EvalOutcome = 'answered' | 'fallback' | 'affirmation_required' | 'scope_reply';

export interface EvalEvidenceTrace {
  evidenceId: string;
  citationId?: string;
  title: string;
  sectionPath: string[];
  sourceId?: string;
  documentId?: string;
  sourceRef?: string;
  charStart?: number;
  charEnd?: number;
  pageStart?: number;
  pageEnd?: number;
  lineStart?: number;
  lineEnd?: number;
  rank?: number;
  text: string;
}

export interface EvalClaimTrace {
  text: string;
  evidenceIds: string[];
  citationIds: string[];
}

export interface CrossSourceEvalTrace {
  version: typeof CROSS_SOURCE_EVAL_TRACE_VERSION;
  route: EvalRoute;
  outcome: EvalOutcome;
  errorCode?: string;
  claims: EvalClaimTrace[];
  evidence: EvalEvidenceTrace[];
  config: {
    collectionIds: string[];
    topK?: number;
    candidatePool?: number;
    groundingMode?: 'augment' | 'strict';
    analysisStyle?: 'topic_map' | 'principle_graph';
    affirmationPolicy: 'always' | 'low_confidence' | 'never';
  };
}

const CLAIM_STOP_WORDS = new Set([
  'a',
  'about',
  'an',
  'and',
  'are',
  'as',
  'at',
  'be',
  'but',
  'by',
  'can',
  'do',
  'for',
  'from',
  'has',
  'have',
  'how',
  'i',
  'if',
  'in',
  'into',
  'is',
  'it',
  'its',
  'of',
  'on',
  'or',
  'that',
  'the',
  'their',
  'this',
  'to',
  'was',
  'we',
  'what',
  'when',
  'where',
  'which',
  'with',
  'would',
  'you',
]);

function terms(text: string): Set<string> {
  return new Set(
    (text.toLowerCase().match(/[a-z0-9][a-z0-9_-]*/g) || []).filter(
      (term) => term.length > 2 && !CLAIM_STOP_WORDS.has(term)
    )
  );
}

/**
 * Deterministic sentence-level segmentation for fast answers. Questions and
 * fragments shorter than four tokens are presentation, not factual claims.
 */
export function segmentFastClaims(response: string): string[] {
  return response
    .replace(/\r/g, '')
    .split(/(?<=[.!?])\s+|\n+/)
    .map((value) => value.trim())
    .filter(
      (value) =>
        value.length > 0 &&
        !value.endsWith('?') &&
        (value.match(/[A-Za-z0-9][A-Za-z0-9_-]*/g) || []).length >= 4
    );
}

function fastEvidenceForClaim(
  claim: string,
  evidence: readonly EvalEvidenceTrace[]
): EvalEvidenceTrace[] {
  const claimTerms = terms(claim);
  if (claimTerms.size === 0) return [];
  const scored = evidence
    .map((item) => {
      const evidenceTerms = terms(`${item.title} ${item.sectionPath.join(' ')} ${item.text}`);
      const shared = [...claimTerms].filter((term) => evidenceTerms.has(term)).length;
      return { item, shared, ratio: shared / claimTerms.size };
    })
    .filter(({ shared, ratio }) => shared >= 2 && ratio >= 0.12)
    .sort(
      (left, right) =>
        right.shared - left.shared ||
        right.ratio - left.ratio ||
        (left.item.rank || Number.MAX_SAFE_INTEGER) - (right.item.rank || Number.MAX_SAFE_INTEGER)
    );
  if (scored.length === 0) return [];
  const best = scored[0].shared;
  return scored.filter((entry) => entry.shared === best).map((entry) => entry.item);
}

function turnEvidenceTrace(item: TurnEvidenceItem): EvalEvidenceTrace {
  return {
    evidenceId: item.evidenceId,
    citationId: item.citationId,
    title: item.title,
    sectionPath: [...item.sectionPath],
    sourceId: item.sourceId,
    documentId: item.documentId,
    sourceRef: item.sourceRef,
    charStart: item.charStart,
    charEnd: item.charEnd,
    pageStart: item.pageStart,
    pageEnd: item.pageEnd,
    lineStart: item.lineStart,
    lineEnd: item.lineEnd,
    rank: item.rank,
    text: item.text,
  };
}

function deepEvidenceTrace(item: DeepReasoningResult['evidence'][number]): EvalEvidenceTrace {
  return {
    evidenceId: item.evidenceId,
    citationId: item.citationId,
    title: item.title,
    sectionPath: [...item.sectionPath],
    sourceId: item.sourceId,
    documentId: item.documentId,
    sourceRef: item.sourceRef,
    charStart: item.charStart,
    charEnd: item.charEnd,
    pageStart: item.pageStart,
    pageEnd: item.pageEnd,
    lineStart: item.lineStart,
    lineEnd: item.lineEnd,
    text: item.text,
  };
}

export interface BuildEvalTraceInput {
  route: EvalRoute;
  outcome: EvalOutcome;
  response: string;
  intelligence?: IntelligenceConfig;
  turnEvidence?: TurnEvidence;
  deepResult?: DeepReasoningResult;
  errorCode?: string;
  affirmationPolicy: 'always' | 'low_confidence' | 'never';
}

/** Build the opt-in trace used by the checked-in cross-source evaluation. */
export function buildCrossSourceEvalTrace(input: BuildEvalTraceInput): CrossSourceEvalTrace {
  const evidence = input.deepResult
    ? input.deepResult.evidence.map(deepEvidenceTrace)
    : (input.turnEvidence?.items || []).map(turnEvidenceTrace);
  const evidenceById = new Map(evidence.map((item) => [item.evidenceId, item]));
  const claims = input.deepResult?.claims.length
    ? input.deepResult.claims.map((claim) => {
        const cited = claim.evidenceIds.flatMap((evidenceId) => {
          const item = evidenceById.get(evidenceId);
          return item ? [item] : [];
        });
        return {
          text: claim.text,
          evidenceIds: [...claim.evidenceIds],
          citationIds: cited.flatMap((item) => (item.citationId ? [item.citationId] : [])),
        };
      })
    : input.outcome === 'answered'
      ? segmentFastClaims(input.response).map((claim) => {
          const cited = fastEvidenceForClaim(claim, evidence);
          return {
            text: claim,
            evidenceIds: cited.map((item) => item.evidenceId),
            citationIds: cited.flatMap((item) => (item.citationId ? [item.citationId] : [])),
          };
        })
      : [];

  return {
    version: CROSS_SOURCE_EVAL_TRACE_VERSION,
    route: input.route,
    outcome: input.outcome,
    errorCode: input.errorCode,
    claims,
    evidence,
    config: {
      collectionIds: [...(input.intelligence?.collectionIds || [])],
      topK: input.intelligence?.limit,
      candidatePool: input.intelligence?.candidatePool,
      groundingMode: input.intelligence?.groundingMode,
      analysisStyle: input.intelligence?.deepReasoning?.analysisStyle,
      affirmationPolicy: input.affirmationPolicy,
    },
  };
}
