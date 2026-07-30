/**
 * Drift guard between nano-claw and the vendored mascot.
 *
 * The mascot's `nano-claw-adapter.js` was written against nano-claw's real
 * renderer call sites — its method list and usage counts were taken from a grep
 * of `voice/web/app.js`. That makes it a safe drop-in TODAY, and silently unsafe
 * the moment either side moves:
 *
 *   - nano-claw starts calling a renderer method the shim does not implement
 *     → the call lands on `undefined` at runtime, with no error at the call site;
 *   - nano-claw emits an emotion or presence name the mascot cannot resolve
 *     → the character just doesn't move, which looks like a rendering bug.
 *
 * A failure here is a genuine divergence between two repos, not a flaky test.
 * See voice/web/vendor/mascot/README.md for the sync procedure.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import {
  NANO_CLAW_RENDERER_METHODS,
  NANO_CLAW_EMOTIONS,
  NANO_CLAW_PRESENCES,
  rendererGaps,
  coverageGaps,
  createRendererShim,
} from '../voice/web/vendor/mascot/nano-claw-adapter.js';
import { EMOTION_PROFILES, PRESENCE_PROFILES } from '../voice/web/emotion-layer.js';

const here = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(resolve(here, '../voice/web/app.js'), 'utf8');

/** A rig stub: every method the shim reaches for, recording nothing. */
function stubRig() {
  const noop = () => true;
  return {
    connectAnalyser: noop,
    disconnectAnalyser: noop,
    pushAudioFrame: noop,
    getChannels: () => ({}),
    setChannels: noop,
    setAutonomic: noop,
    setDynamics: noop,
    stop: noop,
    start: noop,
  };
}

describe('mascot answers nano-claw renderer contract', () => {
  it('implements every method nano-claw is known to call', () => {
    const shim = createRendererShim(stubRig(), { cue: () => true, reset: () => true });
    expect(rendererGaps(shim)).toEqual([]);
  });

  it('resolves every emotion and presence nano-claw can emit', () => {
    const gaps = coverageGaps();
    expect(gaps.emotions).toEqual([]);
    expect(gaps.presences ?? []).toEqual([]);
  });
});

describe('the two repos still agree on vocabulary', () => {
  it("the mascot's emotion list matches nano-claw's EMOTION_PROFILES", () => {
    // Both directions: a name nano-claw has and the mascot lacks means a dead
    // character; the reverse means the mascot supports something we never send,
    // which is harmless but worth noticing on a re-sync.
    expect([...NANO_CLAW_EMOTIONS].sort()).toEqual(Object.keys(EMOTION_PROFILES).sort());
  });

  it("nano-claw's presences are all known to the mascot", () => {
    const ours = Object.keys(PRESENCE_PROFILES);
    const theirs = new Set(NANO_CLAW_PRESENCES);
    expect(ours.filter((p) => !theirs.has(p))).toEqual([]);
  });
});

describe('the method list still reflects what app.js actually calls', () => {
  it('every method the adapter claims nano-claw calls appears in app.js', () => {
    // The adapter's list is a snapshot of a grep. If nano-claw stops calling
    // something the shim still implements that is harmless, but a name that has
    // vanished entirely usually means the call site was renamed — and the
    // replacement is probably NOT in the list.
    const missing = NANO_CLAW_RENDERER_METHODS.filter(
      (m) => !new RegExp(`\\.${m}\\s*\\(`).test(appSource)
    );
    expect(missing).toEqual([]);
  });
});
