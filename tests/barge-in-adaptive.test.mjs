import assert from "node:assert/strict";
import test from "node:test";

await import("../voice/web/barge-in.js");

const BargeInDetector = globalThis.BargeInDetector;
const AdaptiveBargeInController = globalThis.AdaptiveBargeInController;
const sensitivityLevels = globalThis.BargeInSensitivityLevels;

function nearlyEqual(actual, expected, message) {
    assert.ok(Math.abs(actual - expected) < 1e-12, message || `${actual} != ${expected}`);
}

function controller(options = {}) {
    const detector = new BargeInDetector({
        startThreshold: 0.05,
        sustainThreshold: 0.03,
        confirmMs: 400,
    });
    return new AdaptiveBargeInController(detector, options);
}

function falseAlarm(subject, onsetAt) {
    assert.equal(subject.sample(1, onsetAt)?.type, "barge_in");
    assert.equal(subject.sample(0, onsetAt + 400)?.type, "barge_in_false");
}

test("three false outcomes raise the threshold and keep sustain proportional", () => {
    const subject = controller();

    falseAlarm(subject, 0);
    falseAlarm(subject, 500);
    falseAlarm(subject, 1000);

    nearlyEqual(subject.stats().currentThreshold, 0.05 * 1.4);
    nearlyEqual(subject.detector.sustainThreshold, 0.03 * 1.4);
    assert.equal(subject.stats().adjustments, 1);
});

test("adaptive raises stop at three times the user's base", () => {
    const subject = controller();

    for (let index = 0; index < 15; index += 1) {
        subject.handleOutcome("barge_in_false", index * 10);
    }

    nearlyEqual(subject.stats().currentThreshold, 0.15);
    nearlyEqual(subject.detector.sustainThreshold, 0.09);
    assert.equal(subject.stats().adjustments, 4, "only changes below the cap count");
});

test("sixty false-free seconds recover one inverse-factor step at a time", () => {
    const subject = controller();
    for (let index = 0; index < 6; index += 1) {
        subject.handleOutcome("barge_in_false", index * 10);
    }
    nearlyEqual(subject.stats().currentThreshold, 0.05 * 1.4 * 1.4);

    assert.equal(subject.sample(0, 60049), null);
    nearlyEqual(subject.stats().currentThreshold, 0.05 * 1.4 * 1.4);

    assert.equal(subject.sample(0, 60050), null);
    nearlyEqual(subject.stats().currentThreshold, 0.05 * 1.4);

    assert.equal(subject.sample(0, 120050), null);
    nearlyEqual(subject.stats().currentThreshold, 0.05);
});

test("recovery never crosses the user's base floor", () => {
    const subject = controller();
    for (let index = 0; index < 3; index += 1) {
        subject.handleOutcome("barge_in_false", index);
    }

    subject.sample(0, 60002);
    subject.sample(0, 120002);
    subject.handleOutcome("barge_in_commit", 120003);
    subject.handleOutcome("barge_in_commit", 120004);
    subject.handleOutcome("barge_in_commit", 120005);

    nearlyEqual(subject.stats().currentThreshold, 0.05);
    nearlyEqual(subject.detector.sustainThreshold, 0.03);
});

test("three consecutive commits recover toward base", () => {
    const subject = controller();
    for (let index = 0; index < 6; index += 1) {
        subject.handleOutcome("barge_in_false", index);
    }

    subject.handleOutcome("barge_in_commit", 10);
    subject.handleOutcome("barge_in_commit", 11);
    subject.handleOutcome("barge_in_commit", 12);

    nearlyEqual(subject.stats().currentThreshold, 0.05 * 1.4);
});

test("sensitivity levels and stats expose the public tuning contract", () => {
    assert.deepEqual(sensitivityLevels, {
        low: { startThreshold: 0.09, sustainThreshold: 0.054 },
        medium: { startThreshold: 0.05, sustainThreshold: 0.03 },
        high: { startThreshold: 0.03, sustainThreshold: 0.018 },
    });

    const subject = controller();
    subject.setSensitivity("low");
    subject.handleOutcome("barge_in_commit", 1);
    subject.handleOutcome("barge_in_false", 2);

    assert.deepEqual(subject.stats(), {
        currentThreshold: 0.09,
        baseThreshold: 0.09,
        commits: 1,
        falses: 1,
        adjustments: 0,
    });
    assert.deepEqual(Object.keys(subject.stats()), [
        "currentThreshold",
        "baseThreshold",
        "commits",
        "falses",
        "adjustments",
    ]);
});
