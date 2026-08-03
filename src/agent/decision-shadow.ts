/**
 * Decision Core shadow mode (constitution-engine design.md §24 item 12,
 * Milestone 2).
 *
 * When config.decisionCore.shadowEnabled is true (config file or
 * NANO_CLAW_DECISION_SHADOW=true), every user message is classified by the
 * Decision Core sidecar and the decision is LOGGED — nothing about the live
 * interaction changes. The sidecar root comes from config.decisionCore.root,
 * then $DECISION_CORE_ROOT, then ~/src/ai_constitution_engine.
 * Fire-and-forget: the shadow call adds zero latency to the user path and
 * can never throw into the agent loop.
 *
 * The logged `decisionCoreShadow` records are the evaluation dataset for
 * Milestone 3 (classification agreement, latency baselines) and the
 * training data for the Phase-2 Tier-2 classifier.
 */

import { createHash } from 'node:crypto';
import { appendFileSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { DecisionCoreClient, type Decision } from './decision-core-client';
import { logger } from '../utils/logger';
import type { DecisionCoreConfig } from '../config/schema';

/**
 * What the conversation runtime could do today. recommend_* workflow actions
 * are intentionally absent until RIFF is wired — shadow decisions list them
 * under unavailable_actions, which measures exactly what RIFF wiring unlocks.
 */
const SHADOW_AVAILABLE_ACTIONS = [
  'provide_safety_instructions',
  'ask_clarifying_question',
  'escalate_to_human',
];

interface SidecarSlot {
  client: DecisionCoreClient | null;
  starting: Promise<void> | null;
}

/** One sidecar per domain ('' = the engine's default bundle). Domain pins map
 * sessionId prefixes to domains — the config-level form of the per-line
 * domain dropdown (same shape as phone-mode pins). */
const sidecars = new Map<string, SidecarSlot>();
let disabled = false;
let requestCounter = 0;
let shadowConfig: DecisionCoreConfig | null = null;

/** Shadow-internal urgency carry (§9C): never touches nano-claw session state. */
const urgencyBySession = new Map<string, { urgency: Decision['dimensions']['urgency']; seq: number }>();
/** Client-owned per-conversation turn counter (evaluation.md §2.2 invariant). */
const turnSeqBySession = new Map<string, number>();

/**
 * Client metrics file (evaluation.md §2.3): one line per shadow attempt with
 * join fields — the sidecar cannot log its own death, so latency, spawn,
 * timeout, and fail-open observability live client-side. Contains no text.
 */
function metricsPath(): string {
  return (
    process.env.NANO_CLAW_DECISION_METRICS ??
    join(homedir(), '.nano-claw', 'decision-metrics.jsonl')
  );
}

function hash(value: string): string {
  return 'sha256:' + createHash('sha256').update(`nano-claw-m3:${value}`).digest('hex').slice(0, 16);
}

function writeMetric(record: Record<string, unknown>): void {
  try {
    const path = metricsPath();
    mkdirSync(dirname(path), { recursive: true });
    appendFileSync(path, JSON.stringify({ ts: new Date().toISOString(), ...record }) + '\n');
  } catch (error) {
    logger.debug({ error }, 'decision-core metrics write failed');
  }
}

/**
 * Called from AgentLoop with config.decisionCore. Fail-closed: shadow runs
 * only when shadowEnabled is explicitly true (config file or
 * NANO_CLAW_DECISION_SHADOW=true via mergeEnvConfig).
 */
export function configureDecisionShadow(config?: DecisionCoreConfig): void {
  shadowConfig = config ?? null;
}

function resolveRoot(): string {
  return (
    shadowConfig?.root ??
    process.env.DECISION_CORE_ROOT?.trim() ??
    join(homedir(), 'src', 'ai_constitution_engine')
  );
}

export function decisionShadowEnabled(): boolean {
  return !disabled && shadowConfig?.shadowEnabled === true;
}

/** Longest-matching sessionId prefix among the domain pins; '' = default. */
export function resolveDomain(sessionId: string): string {
  const pins = shadowConfig?.domainPins ?? {};
  let best = '';
  let bestLength = -1;
  for (const [prefix, domain] of Object.entries(pins)) {
    if (sessionId.startsWith(prefix) && prefix.length > bestLength) {
      best = domain;
      bestLength = prefix.length;
    }
  }
  return bestLength >= 0 ? best : '';
}

async function ensureClient(domain: string): Promise<DecisionCoreClient | null> {
  if (!decisionShadowEnabled()) return null;
  let slot = sidecars.get(domain);
  if (slot?.client) return slot.client;
  if (!slot) {
    slot = { client: null, starting: null };
    sidecars.set(domain, slot);
  }
  if (!slot.starting) {
    const root = resolveRoot();
    const extraEnv: Record<string, string> = {};
    if (domain) extraEnv.DECISION_CORE_DOMAIN_DIR = join(root, 'policies', domain);
    const candidate = new DecisionCoreClient(root, 250, extraEnv);
    const currentSlot = slot;
    currentSlot.starting = candidate
      .start()
      .then(() => {
        currentSlot.client = candidate;
        writeMetric({
          event: 'spawn',
          eligible: true,
          domain: domain || '(default)',
          bundle_id: candidate.bundleId,
          sidecar_instance: candidate.instanceId,
        });
        logger.info(
          { bundleId: candidate.bundleId, domain: domain || '(default)' },
          'decision-core shadow sidecar started'
        );
      })
      .catch((error: unknown) => {
        disabled = true; // one failed spawn disables shadow for this process
        writeMetric({ event: 'protocol_error', eligible: true, detail: 'spawn_failed' });
        logger.warn({ error, domain }, 'decision-core shadow disabled (sidecar failed to start)');
      });
  }
  await slot.starting;
  return slot.client;
}

/**
 * Fire-and-forget shadow classification of one user message.
 * Safe to call unconditionally — no-op unless decisionCore.shadowEnabled.
 */
export function shadowDecide(sessionId: string, userMessage: string): void {
  if (!decisionShadowEnabled()) return;
  void (async () => {
    try {
      const domain = resolveDomain(sessionId);
      const sidecar = await ensureClient(domain);
      if (!sidecar) return;
      const carried = urgencyBySession.get(sessionId);
      const turnSeq = (turnSeqBySession.get(sessionId) ?? 0) + 1;
      turnSeqBySession.set(sessionId, turnSeq);
      const requestId = `req_shadow_${++requestCounter}`;
      const startedAt = Date.now();
      const decision = await sidecar.decide({
        request_id: requestId,
        conversation_id: sessionId,
        current_message: userMessage,
        available_actions: SHADOW_AVAILABLE_ACTIONS,
        previous_urgency: carried?.urgency ?? null,
        previous_urgency_seq: carried?.seq ?? null,
        turn_seq: turnSeq,
        // TODO: hash the channel user identity once channels surface it here;
        // sessionId is the honest fallback and is flagged as such (§2.2).
        caller_key: hash(sessionId),
      });
      const latencyMs = Date.now() - startedAt;
      urgencyBySession.set(sessionId, {
        urgency: decision.dimensions.urgency,
        seq: decision.decision_seq,
      });
      const failOpen = decision.policy_id === 'default.fail_open';
      writeMetric({
        event: failOpen ? 'fail_open' : 'decision',
        eligible: true,
        domain: domain || '(default)',
        request_id: requestId,
        conversation_hash: hash(sessionId),
        caller_key: hash(sessionId),
        caller_key_fallback: true,
        turn_seq: turnSeq,
        decision_id: decision.decision_id,
        bundle_id: sidecar.bundleId,
        sidecar_instance: sidecar.instanceId,
        e2e_latency_ms: latencyMs,
        cache_hit: false,
      });
      logger.info(
        {
          decisionCoreShadow: {
            sessionId,
            latencyMs,
            decisionId: decision.decision_id,
            policyId: decision.policy_id,
            modeLabel: decision.mode_label,
            dimensions: decision.dimensions,
            requiredActions: decision.required_actions,
            requiredPlaybooks: decision.required_playbooks,
            degraded: decision.degraded,
          },
        },
        'decision-core shadow decision'
      );
    } catch (error) {
      // Shadow mode must never surface into the conversation path.
      writeMetric({ event: 'protocol_error', eligible: true, detail: 'shadow_decide_threw' });
      logger.debug({ error }, 'decision-core shadow decide failed');
    }
  })();
}

/** For tests and graceful shutdown. */
export function stopDecisionShadow(): void {
  for (const slot of sidecars.values()) {
    slot.client?.stop();
  }
  sidecars.clear();
  disabled = false;
  shadowConfig = null;
  urgencyBySession.clear();
  turnSeqBySession.clear();
}
