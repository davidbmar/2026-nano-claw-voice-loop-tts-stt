#!/usr/bin/env node
/**
 * Run the browser-console tests that `npm test` does not.
 *
 * `tests/*.test.mjs` are plain `node:assert` scripts, not vitest suites. Vitest
 * reports "No test suite found in file" for each and moves on — so they have
 * never run in `npm test`, and a red one is invisible.
 *
 * That is not hypothetical: `voice-ui.test.mjs` was red for eight commits after
 * the delegate mode was added to the console dropdown without updating the list
 * it pins. Nobody knew, because nobody ran it.
 *
 *   node scripts/run-web-tests.mjs
 *   npm run test:web
 *
 * Exit status is 0 only if every file that is expected to pass, passes.
 */
import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const testDir = join(root, 'tests');

/**
 * Files known to fail for reasons that predate this runner, each with why.
 *
 * Quarantined rather than fixed: they are stale SOURCE PINS, not behaviour
 * regressions — the code contains exactly what they assert, in the other quote
 * style, after a reformatting pass. Repairing them is a real cleanup and not
 * one to fold silently into unrelated work.
 *
 * Verified failing at 7e63cf2, before the delegate work began.
 */
const KNOWN_FAILING = new Map([
  ['client-telemetry.test.mjs', 'pins a diagnostic line that was reworded'],
  ['model-select.test.mjs', 'pins source text that has since been reformatted'],
  ['ws-audio-cutover.test.mjs', 'pins double-quoted source; the code uses single quotes'],
  ['ws-audio-gesture.test.mjs', 'pins double-quoted source; the code uses single quotes'],
]);

const files = readdirSync(testDir).filter((f) => f.endsWith('.test.mjs')).sort();
let failed = 0;
let quarantined = 0;

for (const file of files) {
  const result = spawnSync(process.execPath, [join(testDir, file)], {
    encoding: 'utf8',
  });
  const ok = result.status === 0;
  const known = KNOWN_FAILING.get(file);

  if (ok && known) {
    // Worth failing on: a quarantined file that now passes means the list is
    // stale, and a stale quarantine hides the next real failure.
    console.log(`[ FIXED  ] ${file} — passes now; remove it from KNOWN_FAILING`);
    failed += 1;
  } else if (ok) {
    console.log(`[  ok    ] ${file}`);
  } else if (known) {
    console.log(`[ known  ] ${file} — ${known}`);
    quarantined += 1;
  } else {
    console.log(`[ FAIL   ] ${file}`);
    console.log((result.stdout + result.stderr).split('\n').slice(0, 6)
      .map((l) => `           ${l}`).join('\n'));
    failed += 1;
  }
}

console.log(
  `\n${files.length - failed - quarantined} passed, ${failed} failed, ` +
  `${quarantined} known-failing (see KNOWN_FAILING in this file)`,
);
process.exit(failed ? 1 : 0);
