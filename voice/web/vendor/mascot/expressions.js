import { CHANNELS, clampChannels } from './face-channels.js';

// Data only, no logic. A new emotion is a table entry; tuning is editing
// numbers. Values were read off the generated reference expressions in
// assets/reference/.

/** Sustained emotional character of one idea. */
export const EMOTIONS = {
  neutral: {},
  calm: { lidLArch: 0.15, lidRArch: 0.15, mouthCorner: 0.45, mouthOpen: 0.12, eyeScaleY: 0.95, browLY: -0.1, browRY: -0.1, blush: 0.35, binaryRain: 0.35 },
  curious: { lidLArch: 0.9, lidRArch: 0.72, lidLTilt: -0.05, lidRTilt: 0.05, eyeScaleY: 1.12, irisScale: 1.08, highlightAngle: -1.1, browLY: 0.55, browRY: 0.35, browLAngle: 0.25, pupilY: -0.25, pupilX: 0.3, mouthOpen: 0.3, mouthCorner: 0.5, headTilt: 0.35 },
  // Asymmetry is the entire vocabulary of confusion.
  confused: { highlightAngle: -1.2, lidLArch: 0.5, lidRArch: -0.35, lidLTilt: -0.3, lidRTilt: 0.35, browLY: 0.7, browRY: -0.45, browLAngle: 0.35, browRAngle: -0.5, pupilX: -0.55, pupilY: -0.3, eyeLOpen: 0.95, eyeROpen: 0.7, mouthOpen: 0.1, mouthCorner: -0.15, mouthShift: 0.5, headTilt: 0.5, glyphOpacity: 0.9, binaryRain: 0.3 },
  warm: { highlightAngle: -0.9, lidLArch: 0.3, lidRArch: 0.3, lidLTilt: -0.15, lidRTilt: -0.15, mouthCorner: 0.9, mouthOpen: 0.4, blush: 1, eyeLSquint: 0.25, eyeRSquint: 0.25, browLY: 0.1, browRY: 0.1 },
  joyful: { highlightAngle: -1.0, lidLArch: 0.45, lidRArch: 0.45, mouthCorner: 1, mouthOpen: 0.8, mouthWidth: 1.25, tongue: 0.8, blush: 1, eyeLSquint: 0.4, eyeRSquint: 0.4, browLY: 0.45, browRY: 0.45, squash: 0.4, binaryRain: 1.2 },
  confident: { lidLArch: -0.45, lidRArch: -0.45, lidLTilt: 0.35, lidRTilt: 0.35, mouthCorner: 0.7, mouthOpen: 0.3, browLAngle: -0.3, browRAngle: -0.3, browLY: -0.15, browRY: -0.15, eyeScaleY: 0.92, bodyLean: 0.2 },
  tense: { lowerLid: 0.5, highlightAngle: 0.15, lidLArch: -0.7, lidRArch: -0.7, lidLTilt: 0.72, lidRTilt: 0.72, browLAngle: -0.7, browRAngle: -0.7, browLY: -0.4, browRY: -0.4, eyeScaleY: 1.05, eyeLSquint: 0.22, eyeRSquint: 0.22, mouthCorner: -0.4, mouthWidth: 0.8, mouthOpen: 0.15, flicker: 0.35, blush: 0.1 },
  somber: { lowerLid: 0.75, lidLArch: -0.25, lidRArch: -0.25, lidLTilt: -0.95, lidRTilt: -0.95, eyeLSquint: 0.3, eyeRSquint: 0.3, eyeScaleY: 0.88, irisScale: 0.92, highlightAngle: 0.4, browLAngle: 0.55, browRAngle: 0.55, browLY: -0.25, browRY: -0.25, eyeLOpen: 0.72, eyeROpen: 0.72, mouthCorner: -0.55, mouthOpen: 0.06, mouthWaver: 0.5, mouthWidth: 0.85, blush: 0.05, scanlines: 0.3, binaryRain: 0.15 },
  awe: { highlightAngle: -1.25, lidLArch: 0.85, lidRArch: 0.85, browLY: 0.85, browRY: 0.85, eyeScaleY: 1.28, mouthOpen: 0.7, mouthWidth: 0.85, mouthRound: 0.55, mouthCorner: 0.25, pupilY: -0.3, blush: 0.5, binaryRain: 1.5 },
  urgent: { lidLArch: -0.5, lidRArch: -0.5, lidLTilt: 0.5, lidRTilt: 0.5, browLAngle: -0.6, browRAngle: -0.6, browLY: 0.3, browRY: 0.3, eyeScaleY: 1.2, mouthOpen: 0.85, mouthWidth: 1.15, mouthCorner: 0.1, flicker: 0.5, binaryRain: 1.8, bodyLean: 0.35 },

  // --- Extended set -------------------------------------------------------
  // Surprise is distinct from awe: awe is sustained and open, surprise is a
  // recoil. Small iris in a wide eye is the whole trick.
  surprised: { highlightAngle: -1.3, lidLArch: 1, lidRArch: 0.92, browLY: 1, browRY: 0.92, eyeScaleY: 1.3, irisScale: 0.62, mouthOpen: 0.6, mouthWidth: 0.68, mouthCorner: -0.1, mouthRound: 0.9, pupilY: -0.1, bodyLean: -0.4, squash: -0.5, blush: 0.2, binaryRain: 1.6, flicker: 0.3 },

  // `thinking` exists on BOTH axes, as `confused` and `working` already do: it is
  // equally a thing the agent is DOING and a way it can look. Presence-only left
  // the most natural call — emotion('thinking') — failing silently.
  thinking: { lowerLid: 0.8, highlightAngle: -0.95, lidLArch: -0.55, lidRArch: -0.45, lidLTilt: 0.3, lidRTilt: 0.2, eyeLOpen: 0.62, eyeROpen: 0.62, eyeLSquint: 0.45, eyeRSquint: 0.45, pupilX: 0.5, pupilY: -0.6, browLAngle: -0.35, browRAngle: -0.25, browLY: -0.2, browRY: -0.1, mouthOpen: 0.06, mouthCorner: 0, mouthShift: -0.45, binaryRain: 1.7, glyphOpacity: 0.5 },

  pondering: { lowerLid: 0.55, highlightAngle: -1.05, lidLArch: 0.25, lidRArch: -0.2, lidLTilt: -0.25, lidRTilt: 0.15, browLY: 0.3, browRY: 0.12, browLAngle: 0.2, browRAngle: -0.1, eyeLOpen: 0.82, eyeROpen: 0.78, eyeLSquint: 0.2, eyeRSquint: 0.2, pupilX: -0.55, pupilY: -0.62, mouthOpen: 0.07, mouthCorner: 0.05, mouthShift: 0.4, mouthWidth: 0.82, headTilt: 0.42, blush: 0.2, binaryRain: 0.8, glyphOpacity: 0.6 },

  // Working is a task, not a mood — the progress meter is what makes it legible.
  working: { highlightAngle: -0.35, lidLArch: -0.72, lidRArch: -0.72, lidLTilt: 0.32, lidRTilt: 0.32, browLAngle: -0.32, browRAngle: -0.32, browLY: -0.2, browRY: -0.2, eyeLOpen: 0.78, eyeROpen: 0.78, eyeLSquint: 0.34, eyeRSquint: 0.34, pupilY: 0.55, mouthOpen: 0.06, mouthCorner: 0.15, mouthWidth: 0.85, binaryRain: 1.9, scanlines: 0.28, progress: 0.45, blush: 0.15 },

  hardWorking: { lowerLid: 0.6, highlightAngle: 0.25, lidLArch: -0.95, lidRArch: -0.95, lidLTilt: 0.78, lidRTilt: 0.78, browLAngle: -0.72, browRAngle: -0.72, browLY: -0.42, browRY: -0.42, eyeLOpen: 0.7, eyeROpen: 0.7, eyeLSquint: 0.45, eyeRSquint: 0.45, irisScale: 0.86, pupilY: 0.4, mouthOpen: 0.4, mouthWidth: 1.1, mouthCorner: -0.2, teeth: 0.85, effort: 0.7, sweat: 0.4, progress: 0.72, binaryRain: 2, scanlines: 0.35, flicker: 0.25, bodyLean: 0.3, blush: 0.1 },

  sweating: { lowerLid: 0.7, highlightAngle: -0.15, lidLArch: 0.55, lidRArch: 0.5, lidLTilt: -0.62, lidRTilt: -0.58, browLAngle: 0.62, browRAngle: 0.58, browLY: 0.34, browRY: 0.3, eyeScaleY: 1.18, irisScale: 0.8, mouthOpen: 0.52, mouthWidth: 0.95, mouthCorner: -0.35, teeth: 0.3, sweat: 1, effort: 0.35, headTilt: 0.2, binaryRain: 1.5, flicker: 0.4, blush: 0.05 },

  // Worried is anxious and alert. Somber is sad and withdrawn. Different shapes.
  worried: { lowerLid: 0.9, highlightAngle: -0.2, lidLArch: 0.4, lidRArch: 0.35, lidLTilt: -0.85, lidRTilt: -0.78, browLAngle: 0.78, browRAngle: 0.7, browLY: 0.2, browRY: 0.14, eyeScaleY: 1.12, irisScale: 0.86, pupilX: -0.2, mouthOpen: 0.06, mouthWaver: 0.85, mouthWidth: 0.78, mouthCorner: -0.5, mouthShift: -0.2, sweat: 0.22, blush: 0.05, scanlines: 0.2, binaryRain: 0.7 },

  determined: { lowerLid: 0.35, highlightAngle: -0.45, lidLArch: -0.88, lidRArch: -0.88, lidLTilt: 0.85, lidRTilt: 0.85, browLAngle: -0.8, browRAngle: -0.8, browLY: -0.3, browRY: -0.3, eyeScaleY: 0.9, eyeLOpen: 0.76, eyeROpen: 0.76, eyeLSquint: 0.34, eyeRSquint: 0.34, irisScale: 1.05, mouthOpen: 0.25, mouthWidth: 1.05, mouthCorner: 0.15, teeth: 0.55, effort: 0.25, bodyLean: 0.4, binaryRain: 1.3 },

  relieved: { lowerLid: 0.45, highlightAngle: -0.8, lidLArch: 0.35, lidRArch: 0.35, lidLTilt: -0.3, lidRTilt: -0.3, browLY: -0.05, browRY: -0.05, browLAngle: 0.2, eyeLOpen: 0.42, eyeROpen: 0.42, eyeLSquint: 0.4, eyeRSquint: 0.4, mouthOpen: 0.55, mouthWidth: 1.05, mouthCorner: 0.75, blush: 0.75, sweat: 0.18, squash: 0.35, binaryRain: 0.4, scanlines: 0.18 },

  proud: { highlightAngle: -0.95, lidLArch: 0.2, lidRArch: 0.2, lidLTilt: 0.2, lidRTilt: 0.2, browLY: 0.3, browRY: 0.3, eyeLSquint: 0.5, eyeRSquint: 0.5, eyeLOpen: 0.8, eyeROpen: 0.8, mouthOpen: 0.5, mouthWidth: 1.2, mouthCorner: 1, tongue: 0.2, blush: 0.9, bodyLean: -0.28, squash: -0.3, binaryRain: 0.9 },

  sleepy: { lowerLid: 0.85, highlightAngle: 0.7, lidLArch: -0.3, lidRArch: -0.25, lidLTilt: -0.35, lidRTilt: -0.3, browLY: -0.35, browRY: -0.32, eyeLOpen: 0.3, eyeROpen: 0.26, eyeLSquint: 0.15, eyeRSquint: 0.15, pupilY: 0.5, irisScale: 0.9, mouthOpen: 0.12, mouthWidth: 0.8, mouthCorner: 0.2, headTilt: 0.35, binaryRain: 0.12, scanlines: 0.42, glyphOpacity: 0.7, blush: 0.3 },

  // Skepticism is asymmetry plus a narrowed eye — same lever as confusion, but
  // held rather than searching.
  skeptical: { lowerLid: 0.5, highlightAngle: -0.55, lidLArch: 0.3, lidRArch: -0.85, lidLTilt: -0.2, lidRTilt: 0.8, browLY: 0.85, browRY: -0.5, browLAngle: -0.15, browRAngle: -0.35, eyeLOpen: 0.92, eyeROpen: 0.58, eyeLSquint: 0.1, eyeRSquint: 0.55, irisScale: 0.95, pupilX: 0.35, mouthOpen: 0.06, mouthWidth: 0.8, mouthCorner: -0.2, mouthShift: 0.55, headTilt: -0.2, binaryRain: 0.5 },

  excited: { highlightAngle: -1.15, lidLArch: 0.75, lidRArch: 0.75, browLY: 0.8, browRY: 0.8, eyeScaleY: 1.22, irisScale: 1.12, mouthOpen: 0.9, mouthWidth: 1.3, mouthCorner: 1, tongue: 0.9, blush: 1, squash: 0.7, bodyLean: 0.2, binaryRain: 2, flicker: 0.3 },
};

/** What the interface is doing right now. */
export const PRESENCES = {
  // Nothing in progress. Distinct from `silent`, which is a pause DURING a turn:
  // idle is between turns, so it is a touch warmer and more open. Present so the
  // vocabulary is a strict superset of nano-claw's, whose default presence is
  // `idle` — see src/nano-claw-adapter.js.
  idle: { mouthOpen: 0.1, mouthCorner: 0.45, eyeScaleY: 1, lidLArch: 0.25, lidRArch: 0.25, blush: 0.4, binaryRain: 0.3, scanlines: 0.1 },
  listening: { lidLArch: 0.4, lidRArch: 0.4, eyeScaleY: 1.05, browLY: 0.2, browRY: 0.2, mouthOpen: 0.08, mouthCorner: 0.4, headTilt: 0.15, binaryRain: 0.4 },
  silent: { mouthOpen: 0.05, mouthCorner: 0.35, eyeScaleY: 0.98, binaryRain: 0.25 },
  thinking: { lowerLid: 0.8, lidLArch: -0.55, lidRArch: -0.45, lidLTilt: 0.3, lidRTilt: 0.2, eyeLOpen: 0.6, eyeROpen: 0.6, eyeLSquint: 0.45, eyeRSquint: 0.45, pupilX: 0.5, pupilY: -0.6, browLAngle: -0.35, browRAngle: -0.25, browLY: -0.2, browRY: -0.1, mouthOpen: 0.06, mouthCorner: 0, mouthShift: -0.45, binaryRain: 1.7, glyphOpacity: 0.5 },
  confused: { lidLArch: 0.5, lidRArch: -0.35, lidLTilt: -0.3, lidRTilt: 0.35, browLY: 0.7, browRY: -0.45, browLAngle: 0.35, browRAngle: -0.5, pupilX: -0.5, mouthShift: 0.5, mouthCorner: -0.15, mouthOpen: 0.1, headTilt: 0.5, glyphOpacity: 0.9 },
  speaking: { mouthOpen: 0.55, mouthCorner: 0.6, binaryRain: 0.9 },
  paused: { lowerLid: 0.4, mouthOpen: 0.04, mouthCorner: 0.3, eyeLOpen: 0.9, eyeROpen: 0.9, binaryRain: 0.2, scanlines: 0.2 },
  // The agent is running a tool or a long task.
  working: { lowerLid: 0.5, lidLArch: -0.72, lidRArch: -0.72, lidLTilt: 0.32, lidRTilt: 0.32, browLAngle: -0.3, browRAngle: -0.3, eyeLSquint: 0.32, eyeRSquint: 0.32, eyeLOpen: 0.8, eyeROpen: 0.8, pupilY: 0.5, mouthOpen: 0.06, mouthWidth: 0.85, binaryRain: 1.9, scanlines: 0.28, progress: 0.45, glyphOpacity: 0.45 },
};

/**
 * How fast each emotion ARRIVES, by name from springs.js DYNAMICS.
 *
 * Timing is expression. A surprise is a recoil and must snap with a little
 * overshoot; a slide into sorrow is a settle and must be slow and heavy. Running
 * every emotion through one spring is why uniform animation reads as mechanical
 * even when every pose is correct.
 *
 * Anything unlisted uses `normal`.
 */
export const ATTACK = {
  thinking: 'slow',
  // Recoils and jolts — fast, with overshoot.
  surprised: 'snap',
  excited: 'snap',
  urgent: 'snap',
  joyful: 'quick',
  awe: 'quick',
  tense: 'quick',
  determined: 'quick',
  skeptical: 'quick',

  // Settles — slow, no bounce.
  somber: 'heavy',
  sleepy: 'heavy',
  pondering: 'slow',
  relieved: 'slow',
  calm: 'slow',
  worried: 'slow',
  proud: 'slow',
};

/** Presences arrive at their own pace too. */
export const PRESENCE_ATTACK = {
  idle: 'slow',
  thinking: 'slow',
  paused: 'slow',
  working: 'quick',
  speaking: 'quick',
  confused: 'quick',
};

/**
 * How to GROUP the emotions when presenting them to a person.
 *
 * The table's own order is insertion order — the original eleven, then the twelve
 * added later — which scatters work states among moods and gives a reader no way
 * in. This is presentation only; nothing in the renderer depends on it.
 */
export const EMOTION_GROUPS = [
  { label: 'Conversational', names: ['neutral', 'calm', 'curious', 'warm', 'confident', 'skeptical'] },
  { label: 'Reacting', names: ['surprised', 'joyful', 'excited', 'awe', 'proud', 'relieved'] },
  { label: 'Struggling', names: ['confused', 'worried', 'somber', 'tense', 'urgent', 'sleepy'] },
  { label: 'At work', names: ['thinking', 'pondering', 'working', 'hardWorking', 'determined', 'sweating'] },
];

/** Status glyph shown on screen for a given presence or emotion, if any. */
/**
 * The "still processing" indicator: an ellipsis drawn as three chunky squares.
 *
 * As TEXT, '...' failed at the size this actually ships at. Compared side by
 * side on a 93px screen (nano-claw's 400px character), three sub-pixel dots
 * sitting on the baseline survive the downscale as a grey smudge that reads as a
 * rendering artifact rather than a signal.
 *
 * A solid cursor block fixed the legibility but cost the meaning — at full size
 * it reads as a flat grey slab, where '...' said "thinking" immediately. Drawing
 * the ellipsis as three PIXEL BLOCKS keeps both: the shape still says "thinking"
 * and each dot has enough mass to survive a 10:1 downscale. It also puts the
 * glyph in the same visual language as the binary border and the progress meter,
 * which are already chunky blocks rather than fine marks.
 *
 * `face-render.js` draws this one as GEOMETRY rather than text — that is the
 * whole point, and it also means no font coverage question arises. The string is
 * still a real ellipsis so anything that treats glyphs as text degrades sensibly.
 * Exported so the coupling is a named import instead of a magic string.
 */
export const PIXEL_ELLIPSIS = '…';

export const GLYPHS = {
  confused: '?',
  thinking: PIXEL_ELLIPSIS,
  pondering: PIXEL_ELLIPSIS,
  working: '%',
  hardWorking: '%',
  sleepy: 'z',
  surprised: '!',
  skeptical: '?',
  urgent: '!',
};

/** Brief gestures: additive offsets that decay back to the emotion baseline. */
export const CUES = {
  blink: { channels: {}, durationMs: 180, blink: true },
  doubleBlink: { channels: {}, durationMs: 480, blink: true, double: true },
  lookAround: { channels: { pupilX: 0.85, pupilY: -0.2, headTilt: 0.3 }, durationMs: 1400 },
  glance: { channels: { pupilX: 0.6 }, durationMs: 520 },
  nod: { channels: { bodyBob: 0.8, headTilt: -0.1 }, durationMs: 620 },
  shake: { channels: { headTilt: 0.7 }, durationMs: 640 },
  squint: { channels: { eyeLSquint: 0.7, eyeRSquint: 0.7, browLY: -0.35, browRY: -0.35 }, durationMs: 900 },
  widen: { channels: { eyeScaleY: 0.3, browLY: 0.7, browRY: 0.7 }, durationMs: 700 },
  glitch: { channels: { glitch: 0.9, flicker: 0.8, binaryRain: 1.5 }, durationMs: 420 },
  boot: { channels: { binaryRain: 1.5, scanlines: 0.8, flicker: 0.6, eyeLOpen: -0.8, eyeROpen: -0.8 }, durationMs: 1100 },

  // Inherited VoiceDirector cues.
  impact: { channels: { squash: 0.8, eyeScaleY: 0.2, flicker: 0.6 }, durationMs: 420 },
  turn: { channels: { headTilt: 0.5, pupilX: 0.6 }, durationMs: 560 },
  sweep: { channels: { binaryRain: 1.3 }, durationMs: 700 },
  flash: { channels: { flicker: 0.7 }, durationMs: 300 },
  strobe: { channels: { flicker: 1 }, durationMs: 800 },
  brighten: { channels: { blush: 0.4, mouthCorner: 0.3, binaryRain: 1 }, durationMs: 1300 },
  hush: { channels: { binaryRain: -0.4, scanlines: 0.4, mouthOpen: -0.2 }, durationMs: 900 },
};

/**
 * Reject an intensity that cannot be placed anywhere in range.
 *
 * NaN is the one input here that fails silently, and it fails badly. Every other
 * bad value lands somewhere sensible — `Infinity` clamps to 1, `-5` clamps to 0,
 * `null` takes the default, even the string `'0.5'` coerces — because
 * `Math.max(0, Math.min(1, x))` handles them. `Math.max(0, Math.min(1, NaN))` is
 * NaN, and `??` does not catch it because NaN is not nullish.
 *
 * What that produced was worse than a no-op in both directions:
 *
 *   - On an emotion, every scaled channel became NaN, `clampChannels` dropped
 *     all of them, and `resolveEmotion` returned `{}` — which is truthy, so the
 *     call reported success while the face did not move. It also wrote NaN into
 *     the held intensity, so the emotion was DESTROYED: a later `presence()`
 *     recomposed and the emotion contributed nothing.
 *   - On a cue, the NaN went straight into the rendered channel set. Measured:
 *     `cue('nod', { intensity: NaN })` returned true and left `headTilt` and
 *     `bodyBob` as NaN for the cue's full 620 ms.
 *
 * An agent computing an intensity — a ratio, a parse, a division — can produce
 * NaN without any bug on its side being obvious. So this follows the convention
 * already used for unknown names: warn, return null, change nothing. Rejecting
 * rather than defaulting matters because it leaves the previously held
 * expression intact instead of quietly wiping it.
 */
function validIntensity(value, what, name) {
  if (value == null) return 1;
  const n = Number(value);
  // NaN specifically, not merely non-finite. The distinction is whether the
  // value can be ORDERED against the range: Infinity cannot be represented but
  // clamps unambiguously to 1, and an agent producing it plainly meant "as much
  // as possible". NaN compares false against everything, so there is no
  // defensible place to put it — which is exactly why the clamp let it through.
  if (Number.isNaN(n)) {
    console.warn(`[mascot] ${what} "${name}": intensity must be a number, got ${value}`);
    return null;
  }
  return Math.max(0, Math.min(1, n));
}

/** Interpolate an absolute target set from neutral toward its full value. */
function scaleToward(channels, k) {
  const out = {};
  for (const [name, target] of Object.entries(channels)) {
    const spec = CHANNELS[name];
    if (!spec) continue;
    out[name] = spec.neutral + (target - spec.neutral) * k;
  }
  return clampChannels(out);
}

/**
 * Explain a miss instead of just reporting one.
 *
 * Several states live on both axes (confused, working, thinking), so a caller
 * reaching for the wrong one is the likeliest mistake there is. Saying which axis
 * the name IS on turns a dead end into a signpost.
 */
function missHint(name, wanted) {
  const onOther = wanted === 'emotion' ? name in PRESENCES : name in EMOTIONS;
  if (!onOther) return `[mascot] unknown ${wanted} "${name}"`;
  const other = wanted === 'emotion' ? 'presence' : 'emotion';
  // Both sides of the sentence need the right article, not just one.
  const article = (w) => (w === 'emotion' ? 'an emotion' : 'a presence');
  return `[mascot] "${name}" is ${article(other)}, not ${article(wanted)} — use action:"${other}"`;
}

export function resolveEmotion(name, intensity = 1) {
  const e = EMOTIONS[name];
  if (!e) {
    console.warn(missHint(name, 'emotion'));
    return null;
  }
  const k = validIntensity(intensity, 'emotion', name);
  if (k === null) return null;
  return scaleToward(e, k);
}

export function resolvePresence(name) {
  const p = PRESENCES[name];
  if (!p) {
    console.warn(missHint(name, 'presence'));
    return null;
  }
  return clampChannels(p);
}

/**
 * Cue values are OFFSETS added on top of the emotion baseline, not absolute
 * targets, so intensity scales them directly rather than interpolating toward
 * neutral.
 */
export function resolveCue(name, intensity = 1) {
  const c = CUES[name];
  if (!c) {
    console.warn(`[mascot] unknown cue "${name}"`);
    return null;
  }
  const k = validIntensity(intensity, 'cue', name);
  if (k === null) return null;
  const channels = {};
  for (const [ch, v] of Object.entries(c.channels)) channels[ch] = v * k;
  return { channels, durationMs: c.durationMs, blink: !!c.blink, double: !!c.double };
}
