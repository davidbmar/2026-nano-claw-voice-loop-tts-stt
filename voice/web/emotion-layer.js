// Emotion vocabulary ported from
// /Users/davidmar/src/talking_visualization/src/voice-director.js on 2026-07-18.
// Only the profiles and blending math were ported — the upstream
// VoiceVisualDirector shell (segments, timelines, streaming cues) was not,
// because nano-claw's app.js already owns a settings-layer system
// (base → emotion → caller → moment → speaking) and two configure() writers
// would fight over the renderer.
//
// One deliberate semantic change from upstream: emotions blend FROM the
// user's tuned base profile TOWARD the emotion profile. Upstream blends from
// its own fixed "neutral" profile, which would discard the user's saved look
// (e.g. the nebula/#ff775f default) whenever the layer engaged. Here
// "neutral" is the identity: base settings pass through untouched.

const TWEEN_NUMERIC = new Set([
    "energy",
    "response",
    "speed",
    "bloom",
    "opacity",
    "density",
    "spread",
    "rotationSpeed",
    "idleLevel",
]);

const TWEEN_COLORS = new Set(["primary", "secondary"]);

const clamp = (value, min = 0, max = 1) => Math.max(min, Math.min(max, Number(value) || 0));
const lerp = (a, b, amount) => a + (b - a) * amount;

function parseHex(hex) {
    const value = String(hex || "").replace("#", "");
    const full = value.length === 3 ? value.split("").map((c) => c + c).join("") : value;
    const num = Number.parseInt(full, 16);
    if (!Number.isFinite(num) || full.length !== 6) return { r: 0, g: 0, b: 0 };
    return { r: (num >> 16) & 0xff, g: (num >> 8) & 0xff, b: num & 0xff };
}

function mixHex(from, to, amount) {
    const a = parseHex(from);
    const b = parseHex(to);
    const channel = (left, right) => Math.round(lerp(left, right, amount))
        .toString(16)
        .padStart(2, "0");
    return `#${channel(a.r, b.r)}${channel(a.g, b.g)}${channel(a.b, b.b)}`;
}

// Verbatim from upstream EMOTION_PROFILES ("neutral" omitted — here neutral
// means "leave the user's base profile alone", so it has no target values).
export const EMOTION_PROFILES = Object.freeze({
    neutral: Object.freeze({}),
    calm: Object.freeze({
        primary: "#49e4d3", secondary: "#247fc7", pattern: "focus",
        energy: 0.46, response: 0.58, speed: 0.22, bloom: 0.34,
        opacity: 0.64, rotationSpeed: 0.1, rotationAxis: "y",
    }),
    curious: Object.freeze({
        primary: "#b779ff", secondary: "#35d9ff", pattern: "helix",
        energy: 0.68, response: 0.82, speed: 0.58, bloom: 0.5,
        opacity: 0.76, rotationSpeed: 0.28, rotationAxis: "voice",
    }),
    confused: Object.freeze({
        primary: "#c586ff", secondary: "#ffbd5a", pattern: "helix",
        energy: 0.7, response: 0.76, speed: 0.66, bloom: 0.52,
        opacity: 0.78, rotationSpeed: 0.38, rotationAxis: "voice",
    }),
    warm: Object.freeze({
        primary: "#ffad72", secondary: "#ff6f91", pattern: "wave",
        energy: 0.62, response: 0.7, speed: 0.38, bloom: 0.56,
        opacity: 0.78, rotationSpeed: 0.16, rotationAxis: "y",
    }),
    joyful: Object.freeze({
        primary: "#52f5a8", secondary: "#31d7ff", pattern: "spectrum",
        energy: 0.84, response: 0.92, speed: 0.74, bloom: 0.66,
        opacity: 0.86, rotationSpeed: 0.4, rotationAxis: "voice",
    }),
    confident: Object.freeze({
        primary: "#72ddff", secondary: "#d8f7ff", pattern: "scan",
        energy: 0.74, response: 0.8, speed: 0.48, bloom: 0.42,
        opacity: 0.82, rotationSpeed: 0.2, rotationAxis: "y",
    }),
    tense: Object.freeze({
        primary: "#ff6a62", secondary: "#ffb24d", pattern: "scan",
        energy: 0.9, response: 0.97, speed: 0.94, bloom: 0.58,
        opacity: 0.86, rotationSpeed: 0.56, rotationAxis: "voice",
    }),
    somber: Object.freeze({
        primary: "#7181d8", secondary: "#315d89", pattern: "wave",
        energy: 0.4, response: 0.62, speed: 0.28, bloom: 0.32,
        opacity: 0.56, rotationSpeed: 0.09, rotationAxis: "z",
    }),
    awe: Object.freeze({
        primary: "#d86cff", secondary: "#31d7ff", pattern: "nebula",
        energy: 0.8, response: 0.86, speed: 0.56, bloom: 0.84,
        opacity: 0.84, rotationSpeed: 0.32, rotationAxis: "voice",
    }),
    urgent: Object.freeze({
        primary: "#ff655a", secondary: "#ffcb54", pattern: "scan",
        energy: 0.95, response: 0.99, speed: 1.08, bloom: 0.66,
        opacity: 0.9, rotationSpeed: 0.66, rotationAxis: "voice",
    }),
});

// Verbatim from upstream PRESENCE_PROFILES. "idle" is identity here for the
// same base-preserving reason as neutral.
export const PRESENCE_PROFILES = Object.freeze({
    idle: Object.freeze({}),
    listening: Object.freeze({
        pattern: "spectrum", energy: 0.52, response: 0.96, speed: 0.34,
        bloom: 0.38, rotationSpeed: 0.12, rotationAxis: "y", autoRotate: true,
    }),
    silent: Object.freeze({
        pattern: "focus", energy: 0.24, response: 0.26, speed: 0.16,
        bloom: 0.2, opacity: 0.46, rotationSpeed: 0.035,
        rotationAxis: "y", autoRotate: true,
    }),
    thinking: Object.freeze({
        pattern: "helix", energy: 0.6, response: 0.52, speed: 0.48,
        bloom: 0.5, rotationSpeed: 0.25, rotationAxis: "voice", autoRotate: true,
    }),
    confused: Object.freeze({
        primary: "#c586ff", secondary: "#ffbd5a", pattern: "helix",
        energy: 0.68, response: 0.7, speed: 0.68, bloom: 0.5,
        rotationSpeed: 0.38, rotationAxis: "voice", autoRotate: true,
    }),
    speaking: Object.freeze({
        response: 0.9, rotationSpeed: 0.24, rotationAxis: "voice", autoRotate: true,
    }),
    paused: Object.freeze({
        energy: 0.34, response: 0.42, bloom: 0.26,
        rotationSpeed: 0.05, rotationAxis: "y", autoRotate: true,
    }),
});

/** Blend the base settings toward one emotion profile by intensity (0..1).
 *  Numerics lerp, colors mix in RGB, discrete keys (pattern, rotationAxis)
 *  switch once intensity crosses the upstream 0.32 threshold. */
export function resolveEmotionOver(base, name, intensity) {
    const profile = EMOTION_PROFILES[name];
    if (!profile) {
        throw new RangeError(
            `Unknown emotion "${name}". Try ${Object.keys(EMOTION_PROFILES).join(", ")}.`,
        );
    }
    const amount = clamp(intensity);
    const result = Object.assign({}, base);
    for (const key of Object.keys(profile)) {
        const from = base[key];
        const to = profile[key];
        if (TWEEN_NUMERIC.has(key)) result[key] = lerp(Number(from) || 0, Number(to), amount);
        else if (TWEEN_COLORS.has(key)) result[key] = mixHex(from, to, amount);
        else result[key] = amount >= 0.32 ? to : from;
    }
    return result;
}

/** Apply the emotion layer, then the presence layer, over base settings.
 *  state: { emotion, intensity, presence }. Unknown names fall back to
 *  identity rather than throwing — a bad live value must never blank the
 *  visualization. */
export function applyEmotionLayer(base, state) {
    const emotion = EMOTION_PROFILES[state && state.emotion] ? state.emotion : "neutral";
    const presence = PRESENCE_PROFILES[state && state.presence] ? state.presence : "idle";
    const intensity = clamp(state && state.intensity != null ? state.intensity : 0.7);
    let settings = resolveEmotionOver(base, emotion, intensity);
    const presenceProfile = PRESENCE_PROFILES[presence];
    if (Object.keys(presenceProfile).length) {
        settings = Object.assign({}, settings, presenceProfile);
    }
    return settings;
}

// Ordered: first match wins, so outcome words (joyful/somber) outrank
// the generic question → curious rule.
const EMOTION_RULES = [
    // Affirmative shapes only — a caller's "is it booked?" must not read as joy.
    { emotion: "joyful", intensity: 0.85, re: /\b(?:you'?re all set|all set|is booked|booked (?:you|for|it)|confirmed|great news|see you (?:then|on|at)|perfect[,!. ])/i },
    { emotion: "somber", intensity: 0.6, re: /\b(?:sorry|unfortunately|apologi[sz]e|can(?:'|no)t|unable|no availability|fully booked)\b/i },
    { emotion: "urgent", intensity: 0.7, re: /\b(?:right away|immediately|as soon as possible|urgent|emergency)\b/i },
    { emotion: "warm", intensity: 0.7, re: /\b(?:welcome|hi there|hello|good (?:morning|afternoon|evening)|thanks for calling|happy to help)\b/i },
    { emotion: "curious", intensity: 0.65, re: /\?\s*$/ },
];

/** Infer an emotion for an agent reply. Pure and deliberately boring:
 *  keyword/shape rules, no model call — this runs on every reply. */
export function inferEmotion(text) {
    const value = String(text || "").trim();
    if (!value) return { emotion: "neutral", intensity: 0.65 };
    for (const rule of EMOTION_RULES) {
        if (rule.re.test(value)) return { emotion: rule.emotion, intensity: rule.intensity };
    }
    return { emotion: "neutral", intensity: 0.65 };
}
