import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const {
    EMOTION_PROFILES,
    PRESENCE_PROFILES,
    applyEmotionLayer,
    inferEmotion,
    resolveEmotionOver,
} = await import("../voice/web/emotion-layer.js");

const BASE = {
    pattern: "nebula",
    primary: "#ff775f",
    secondary: "#ffca5c",
    energy: 0.68,
    rotationAxis: "voice",
    rotationSpeed: 0.22,
};

// Neutral is the identity: the user's tuned base passes through untouched.
assert.deepEqual(resolveEmotionOver(BASE, "neutral", 1), BASE);
assert.deepEqual(applyEmotionLayer(BASE, { emotion: "neutral", presence: "idle" }), BASE);

// Full-intensity joyful lands exactly on the profile's values.
const joyful = resolveEmotionOver(BASE, "joyful", 1);
assert.equal(joyful.primary, EMOTION_PROFILES.joyful.primary);
assert.equal(joyful.pattern, "spectrum");
assert.equal(joyful.energy, EMOTION_PROFILES.joyful.energy);

// Zero intensity leaves the base; below the 0.32 threshold discrete keys hold.
const faint = resolveEmotionOver(BASE, "joyful", 0);
assert.equal(faint.primary, BASE.primary);
assert.equal(faint.pattern, "nebula");
const sub = resolveEmotionOver(BASE, "joyful", 0.2);
assert.equal(sub.pattern, "nebula", "discrete keys switch only at >= 0.32");
assert.notEqual(sub.energy, BASE.energy, "numerics tween continuously");

// Half intensity blends colors channel-wise between base and profile.
const half = resolveEmotionOver({ ...BASE, primary: "#000000" }, "tense", 0.5);
assert.match(half.primary, /^#[0-9a-f]{6}$/);
assert.notEqual(half.primary, "#000000");
assert.notEqual(half.primary, EMOTION_PROFILES.tense.primary);

// Presence merges over the emotion result; unknown names fall back safely.
const listening = applyEmotionLayer(BASE, { emotion: "neutral", presence: "listening" });
assert.equal(listening.pattern, PRESENCE_PROFILES.listening.pattern);
const junk = applyEmotionLayer(BASE, { emotion: "nope", presence: "nope", intensity: 9 });
assert.deepEqual(junk, BASE, "bad live values must never blank the visualization");

// Unknown emotion throws only at the explicit resolve API.
assert.throws(() => resolveEmotionOver(BASE, "sparkly", 1), RangeError);

// Reply inference: outcome words outrank the question rule; empty is neutral.
assert.equal(inferEmotion("You're all set — booked for Tuesday at 2 PM.").emotion, "joyful");
assert.equal(inferEmotion("Unfortunately that slot is taken.").emotion, "somber");
assert.equal(inferEmotion("Would Wednesday at 10 work for you?").emotion, "curious");
assert.equal(inferEmotion("Thanks for calling Austin Plumbing!").emotion, "warm");
assert.equal(inferEmotion("We can send someone right away.").emotion, "urgent");
assert.equal(inferEmotion("Is it booked? No — unfortunately not.").emotion, "somber",
    "regret outranks the trailing-question rule only via rule order when booked doesn't match");
assert.equal(inferEmotion("").emotion, "neutral");
assert.equal(inferEmotion("The visit is scheduled for one hour.").emotion, "neutral");

// The console wires the layer: panel select present, app.js applies it.
const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
assert.ok(html.includes('id="cube-emotion"'), "emotion select in the panel");
const app = await readFile(new URL("../voice/web/app.js", import.meta.url), "utf8");
assert.ok(app.includes("applyEmotionLayer(visualizationSettings, emotionState)"));
assert.ok(app.includes("window.VoiceEmotion"));

console.log("emotion-layer tests passed");
