import assert from "node:assert/strict";

await import("../voice/web/barge-in.js");
const BargeInDetector = globalThis.BargeInDetector;
assert.ok(BargeInDetector, "BargeInDetector must be exported to the global scope");

// Real speech: loud onset, stays loud through the confirm window → commit.
const d1 = new BargeInDetector({ startThreshold: 0.05, sustainThreshold: 0.03, confirmMs: 300 });
assert.equal(d1.sample(0.01, 0), null, "quiet frame does nothing");
assert.equal(d1.sample(0.08, 20)?.type, "barge_in", "loud onset pauses immediately");
assert.equal(d1.sample(0.08, 120), null, "still confirming inside window");
assert.equal(d1.sample(0.08, 340)?.type, "barge_in_commit", "sustained loud → commit");

// False alarm: loud blip, then silence through the window → false.
const d2 = new BargeInDetector({ startThreshold: 0.05, sustainThreshold: 0.03, confirmMs: 300 });
assert.equal(d2.sample(0.09, 0)?.type, "barge_in", "blip pauses");
assert.equal(d2.sample(0.005, 60), null, "fell silent, still inside window");
assert.equal(d2.sample(0.004, 320)?.type, "barge_in_false", "silence through window → false alarm");

// After reset, detector is idle again.
d2.reset();
assert.equal(d2.sample(0.004, 400), null, "reset clears pending state");

console.log("barge-in detector tests passed");
