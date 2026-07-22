import assert from 'node:assert/strict';

await import('../voice/web/barge-in.js');
const BargeInDetector = globalThis.BargeInDetector;
assert.ok(BargeInDetector, 'BargeInDetector must be exported to the global scope');

// Real speech: post-pause voiced evidence commits promptly, without waiting
// for one arbitrarily timed sample at the end of the confirmation window.
const d1 = new BargeInDetector({
  startThreshold: 0.05,
  sustainThreshold: 0.03,
  confirmMs: 300,
  echoGuardMs: 80,
  requiredSpeechMs: 60,
});
assert.equal(d1.sample(0.01, 0), null, 'quiet frame does nothing');
assert.equal(d1.sample(0.08, 20)?.type, 'barge_in', 'loud onset pauses immediately');
assert.equal(d1.sample(0.08, 110), null, 'echo guard does not count the onset tail');
assert.equal(d1.sample(0.08, 140), null, 'voiced evidence accumulates across samples');
assert.equal(d1.sample(0.08, 180)?.type, 'barge_in_commit', 'sustained voice commits early');

// A natural speech dip near the old fixed deadline cannot undo evidence that
// already established a real listener interruption.
const d2 = new BargeInDetector({
  startThreshold: 0.05,
  sustainThreshold: 0.03,
  confirmMs: 400,
  echoGuardMs: 80,
  requiredSpeechMs: 60,
});
assert.equal(d2.sample(0.09, 0)?.type, 'barge_in');
assert.equal(d2.sample(0.07, 100), null);
assert.equal(d2.sample(0.07, 140)?.type, 'barge_in_commit');
assert.equal(d2.sample(0.004, 410), null, 'a later syllable gap cannot revive or reverse it');

// False alarm: loud blip, then silence through the window → false.
const d3 = new BargeInDetector({ startThreshold: 0.05, sustainThreshold: 0.03, confirmMs: 300 });
assert.equal(d3.sample(0.09, 0)?.type, 'barge_in', 'blip pauses');
assert.equal(d3.sample(0.005, 60), null, 'fell silent, still inside window');
assert.equal(d3.sample(0.004, 320)?.type, 'barge_in_false', 'silence through window → false alarm');

// After reset, detector is idle again.
d3.reset();
assert.equal(d3.sample(0.004, 400), null, 'reset clears pending state');

console.log('barge-in detector tests passed');
