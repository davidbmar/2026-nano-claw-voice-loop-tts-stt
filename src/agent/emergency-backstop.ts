/**
 * Client-embedded emergency backstop (design.md §9B rule 2).
 *
 * Reads the SAME taxonomy file the server's detector uses — backstop and
 * policy versions ship in one bundle and cannot skew. The guard logic here
 * is a deliberately SIMPLIFIED, conservative subset of the server's
 * (pre-clause negation only, no post-clause resolution guards): when in
 * doubt it fires, which is the safe direction for a backstop whose only
 * effect is adding safety instructions to a fail-open default.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

interface Taxonomy {
  emergency_rules: { rule_id: string; terms: string[] }[];
  escalation_rules: { rule_id: string; terms: string[] }[];
  negators: string[];
}

const cached = new Map<string, Taxonomy>();

function loadTaxonomy(repoRoot: string): Taxonomy {
  let taxonomy = cached.get(repoRoot);
  if (!taxonomy) {
    taxonomy = JSON.parse(
      readFileSync(join(repoRoot, 'policies', 'plumbing', 'taxonomy.v1.json'), 'utf-8'),
    ) as Taxonomy;
    cached.set(repoRoot, taxonomy);
  }
  return taxonomy;
}

export function emergencyBackstop(text: string, repoRoot: string): boolean {
  let taxonomy: Taxonomy;
  try {
    taxonomy = loadTaxonomy(repoRoot);
  } catch {
    return false; // no taxonomy → no backstop; the fail-open default still applies
  }
  const lowered = text.toLowerCase();
  const rules = [...taxonomy.emergency_rules, ...taxonomy.escalation_rules];
  for (const rule of rules) {
    for (const term of rule.terms) {
      const index = lowered.indexOf(term.toLowerCase());
      if (index < 0) continue;
      // Same-clause pre-negation guard only (conservative subset).
      const clause = lowered.slice(Math.max(0, index - 30), index).split(/[,.;!?]/).pop() ?? '';
      const negated = taxonomy.negators.some(
        (negator) => new RegExp(`\\b${negator}\\b|n't`, 'i').test(clause),
      );
      if (!negated) return true;
    }
  }
  return false;
}
