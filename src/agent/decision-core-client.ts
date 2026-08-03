/**
 * NanoClaw-side Decision Core client (design.md §9A/§9B, §12.4).
 *
 * Spawns the Python Decision Core as an MCP stdio sidecar and exposes
 * decide()/recordOutcome(). Copy this file (and emergency-backstop.ts) into
 * nano-claw/src/agent/ and call from AgentLoop.processMessage().
 *
 * This client carries the two §9B duties that must live with the caller:
 *   1. FAIL-OPEN: if the sidecar is down, slow (>latencyBudgetMs), or returns
 *      garbage, the conversation continues under a baked-in default decision —
 *      the Decision Core is never the reason a booking fails.
 *   2. EMERGENCY BACKSTOP: before applying the fail-open default, the local
 *      lexicon check (emergency-backstop.ts, reading the SAME taxonomy file
 *      as the server — no version skew) can upgrade the default to the
 *      emergency-conservative variant.
 *
 * No external deps beyond Node builtins; zod validation can be layered on in
 * nano-claw where zod already exists.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface, type Interface } from 'node:readline';
import { emergencyBackstop } from './emergency-backstop';

export interface Situation {
  request_id: string;
  conversation_id: string;
  current_message: string;
  available_actions?: string[];
  user_intent?: string | null;
  conversation_summary?: string | null;
  active_workflow?: string | null;
  known_constraints?: Record<string, unknown>;
  previous_urgency?: 'none' | 'elevated' | 'critical' | null;
  previous_urgency_seq?: number | null;
}

export interface Decision {
  schema_version: string;
  decision_id: string;
  decision_seq: number;
  snapshot_hash: string;
  policy_id: string;
  policy_version: string;
  dimensions: {
    urgency: 'none' | 'elevated' | 'critical';
    ambiguity: { level: 'low' | 'high'; missing_information: string[] };
    intent_stance: 'execute' | 'explore' | 'decide' | 'learn';
  };
  mode_label: string;
  directives: Record<string, { value: unknown; class: string }>;
  required_actions: string[];
  required_playbooks: string[];
  explanation: unknown[];
  degraded: boolean;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

const FAIL_OPEN_DECISION: Omit<Decision, 'decision_id' | 'snapshot_hash'> = {
  schema_version: '0.2.0',
  decision_seq: -1,
  policy_id: 'default.fail_open',
  policy_version: '1.0.0',
  dimensions: {
    urgency: 'none',
    ambiguity: { level: 'high', missing_information: ['service_type'] },
    intent_stance: 'execute',
  },
  mode_label: 'normal_execution',
  directives: {
    require_confirmation_before_booking: { value: true, class: 'mandatory' },
    ask_targeted_clarifying_question: { value: true, class: 'advisory' },
  },
  required_actions: [],
  required_playbooks: [],
  explanation: [{ claim: 'client-side fail-open default applied' }],
  degraded: true,
};

export class DecisionCoreClient {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private reader: Interface | null = null;
  private pending = new Map<number, PendingRequest>();
  private nextId = 1;

  constructor(
    private readonly repoRoot: string, // path to ai_constitution_engine checkout
    private readonly latencyBudgetMs = 250, // §9B: caller enforces the budget
  ) {}

  async start(): Promise<void> {
    this.proc = spawn('python3', ['-m', 'decision_core.mcp_server'], {
      cwd: this.repoRoot,
      env: { ...process.env, PYTHONPATH: `${this.repoRoot}/src` },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.reader = createInterface({ input: this.proc.stdout });
    this.reader.on('line', (line) => this.onLine(line));
    this.proc.on('exit', () => this.failAllPending(new Error('decision-core exited')));
    // A failed spawn (bad path, missing python3) emits 'error' — unhandled it
    // would CRASH the host process, the opposite of §9B. Same for stdin EPIPE.
    this.proc.on('error', (error) => {
      this.proc = null;
      this.failAllPending(error instanceof Error ? error : new Error(String(error)));
    });
    this.proc.stdin.on('error', () => this.failAllPending(new Error('decision-core stdin closed')));
    // Startup gets its own generous budget: a cold python3 spawn can take
    // longer than the per-decision latency budget and must not fail start().
    await this.request(
      'initialize',
      {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'nano-claw', version: '0.4.x' },
      },
      5000,
    );
    this.notify('notifications/initialized');
  }

  /**
   * §9B contract: this NEVER rejects. Sidecar failure, timeout, or malformed
   * output yields the fail-open default — upgraded by the local emergency
   * backstop when the message matches the bundle lexicon.
   */
  async decide(situation: Situation): Promise<Decision> {
    try {
      const result = (await this.request('tools/call', {
        name: 'decide',
        arguments: situation,
      })) as { isError: boolean; structuredContent: Decision };
      if (!result || result.isError) throw new Error('decision-core tool error');
      return result.structuredContent;
    } catch {
      const backstopHit = emergencyBackstop(situation.current_message, this.repoRoot);
      const fallback: Decision = {
        ...FAIL_OPEN_DECISION,
        decision_id: `dec_failopen_${Date.now()}`,
        snapshot_hash: 'sha256:unavailable',
      };
      if (backstopHit) {
        fallback.mode_label = 'emergency_execution';
        fallback.dimensions = { ...fallback.dimensions, urgency: 'critical' };
        fallback.directives = {
          ...fallback.directives,
          prioritize_safety: { value: true, class: 'safety_mandatory' },
        };
        fallback.required_playbooks = ['plumbing.safety.water_shutoff.v1'];
      }
      return fallback;
    }
  }

  /** Best-effort, idempotent server-side; a lost outcome is logged, not fatal. */
  async recordOutcome(outcome: Record<string, unknown>): Promise<void> {
    try {
      await this.request('tools/call', { name: 'record_outcome', arguments: outcome });
    } catch {
      /* §12.6: outcome loss is an observability gap, never a conversation failure */
    }
  }

  stop(): void {
    this.failAllPending(new Error('client stopped'));
    this.proc?.kill();
    this.proc = null;
  }

  // -- JSON-RPC plumbing ---------------------------------------------------

  private request(method: string, params: unknown, timeoutMs = this.latencyBudgetMs): Promise<unknown> {
    const id = this.nextId++;
    const message = JSON.stringify({ jsonrpc: '2.0', id, method, params });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`decision-core timeout after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      if (!this.proc?.stdin.writable) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error('decision-core not running'));
        return;
      }
      try {
        this.proc.stdin.write(message + '\n');
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  private notify(method: string): void {
    this.proc?.stdin.write(JSON.stringify({ jsonrpc: '2.0', method }) + '\n');
  }

  private onLine(line: string): void {
    let message: { id?: number; result?: unknown; error?: { message: string } };
    try {
      message = JSON.parse(line);
    } catch {
      return; // stray stdout noise must not kill the client
    }
    if (message.id === undefined || message.id === null) return;
    const entry = this.pending.get(message.id);
    if (!entry) return;
    this.pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.error) entry.reject(new Error(message.error.message));
    else entry.resolve(message.result);
  }

  private failAllPending(error: Error): void {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    this.pending.clear();
  }
}
