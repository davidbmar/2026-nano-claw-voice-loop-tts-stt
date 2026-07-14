import assert from "node:assert/strict";

await import("../voice/web/phone-vad.js");

const PhoneVadGate = globalThis.PhoneVadGate;
assert.ok(PhoneVadGate, "PhoneVadGate must be exported to the browser/global scope");

const calibrated = new PhoneVadGate();
const thresholds = calibrated.configureFromNoise([0.002, 0.003, 0.004, 0.003]);
assert.ok(thresholds.startThreshold >= 0.015);
assert.ok(thresholds.stopThreshold < thresholds.startThreshold);

const gate = new PhoneVadGate({
    startThreshold: 0.02,
    stopThreshold: 0.01,
    startHoldMs: 100,
    silenceMs: 500,
    minSpeechMs: 200,
    maxSpeechMs: 5000,
});

assert.equal(gate.sample(0.005, 0), null, "room noise must not open a turn");
assert.equal(gate.sample(0.03, 100), null, "one loud sample must not open a turn");
assert.equal(gate.sample(0.03, 210)?.type, "speech_start");
assert.equal(gate.sample(0.004, 400), null, "short pause must not end a turn");
assert.equal(gate.sample(0.004, 910)?.type, "speech_stop");

const maxGate = new PhoneVadGate({
    startThreshold: 0.02,
    stopThreshold: 0.01,
    startHoldMs: 1,
    maxSpeechMs: 1000,
});
maxGate.sample(0.03, 0);
assert.equal(maxGate.sample(0.03, 2)?.type, "speech_start");
assert.equal(maxGate.sample(0.03, 1002)?.reason, "maximum_duration");

console.log("phone VAD tests passed");
