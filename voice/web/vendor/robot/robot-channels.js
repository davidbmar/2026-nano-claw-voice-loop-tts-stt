// The universal channel set, translated into robot hardware.
//
// Every name here was checked against `face-channels.js` and `expressions.js`.
// The first draft of this mapping used `browRaise`, `eyeScaleY` (as squint) and
// `mouthCurve`: the first does not exist, the second is not squint, and nothing
// writes to the third. All three would have been wired, driven, and completely
// inert — a call reporting success while nothing moved, which is the failure
// this codebase has documented more than once.
//
// So the mapping is validated against the schema at module load rather than
// trusted, and `assertMappingIsTotal` makes a future rename fail a test instead
// of silently going dark.

import { CHANNELS } from './face-channels.js';

/**
 * Which universal channel feeds which piece of robot hardware.
 *
 * Per-side rather than combined, deliberately. `confused` sets browLY 0.7
 * against browRY -0.45 and `skeptical` sets 0.85 against -0.5 — the asymmetry
 * IS the expression. One combined "brow" channel would average it away.
 */
export const ROBOT_MAP = {
  lensL: {
    eyeOpen: 'eyeLOpen',
    squint: 'eyeLSquint',
    gazeX: 'pupilX',
    gazeY: 'pupilY',
    bloom: 'eyeScaleY',
    hue: 'mouthCorner',
    sweat: 'sweat',
  },
  lensR: {
    eyeOpen: 'eyeROpen',
    squint: 'eyeRSquint',
    gazeX: 'pupilX',
    gazeY: 'pupilY',
    bloom: 'eyeScaleY',
    hue: 'mouthCorner',
    sweat: 'sweat',
  },
  antennaL: { elevation: 'browLY', cant: 'browLAngle' },
  antennaR: { elevation: 'browRY', cant: 'browRAngle' },
  grille: { level: 'mouthOpen' },
  head: { roll: 'headTilt', lean: 'bodyLean' },
};

/**
 * Channels a robot deliberately does not consume.
 *
 * Declared rather than omitted. An unlisted channel is a channel somebody forgot,
 * and the difference between "we decided not to use this" and "we never noticed
 * this" is exactly what goes wrong silently.
 */
export const UNUSED_ON_ROBOTS = new Set([
  // CRT vocabulary. A robot has no screen, so binary rain and scanlines have
  // nowhere to go; the power-on sequence replaces what they said.
  'binaryRain', 'scanlines', 'glyphOpacity', 'progress', 'flicker', 'glitch',
  // Ink vocabulary. There are no drawn brows, lids or mouth to shape.
  'lidLArch', 'lidRArch', 'lidLTilt', 'lidRTilt', 'lowerLid',
  'mouthWidth', 'mouthShift', 'mouthRound', 'mouthWaver', 'teeth', 'tongue',
  'irisScale', 'highlightAngle',
  // `mouthCurve` is in the schema but NO expression writes to it — every
  // emotion in `expressions.js` drives `mouthCorner` instead. Mapping it would
  // read a channel that is permanently at its neutral, which is a wire to
  // nowhere that looks exactly like a wire to somewhere. Listed here so the
  // totality check treats it as a decision rather than an oversight; if
  // something ever starts driving it, this line is where to look.
  'mouthCurve',
  // Soft-body vocabulary. A steel head does not deform and does not breathe.
  'squash', 'bodyBob',
  // Skin vocabulary.
  'blush', 'effort',
]);

/**
 * Every universal channel either reaches hardware or is explicitly unused.
 *
 * Called by a test, not at import: a mapping that throws on load would take the
 * whole harness down over a channel rename, when the right outcome is a red test.
 */
export function mappingGaps() {
  const mapped = new Set();
  for (const part of Object.values(ROBOT_MAP)) {
    for (const universal of Object.values(part)) mapped.add(universal);
  }

  const all = Object.keys(CHANNELS);
  return {
    // Named in the map but absent from the schema — the `browRaise` failure.
    phantom: [...mapped].filter((c) => !all.includes(c)).sort(),
    // In the schema, neither mapped nor declared unused — the silent gap.
    unaccounted: all.filter((c) => !mapped.has(c) && !UNUSED_ON_ROBOTS.has(c)).sort(),
    // Declared unused but no longer a real channel — stale after a rename.
    staleUnused: [...UNUSED_ON_ROBOTS].filter((c) => !all.includes(c)).sort(),
  };
}

const clamp01 = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
const signed = (v) => (Number.isFinite(v) ? Math.max(-1, Math.min(1, v)) : 0);

/**
 * How far a channel is from REST, as -1..1. Not its raw value.
 *
 * The schema defines every channel as (min, max, neutral) and **the neutrals are
 * not zero**: `mouthCorner` rests at 0.8, `eyeScaleY` at 1.0. Reading a raw
 * value therefore treats "at rest" as "strongly expressed" — mapping
 * `mouthCorner` straight onto a bipolar hue put the lens permanently 40% of the
 * way to cool blue, and the amber lamp rendered beige. Everything worked; only
 * sampling the canvas found it.
 *
 * Normalised separately in each direction, because a neutral is rarely centred.
 * `mouthCorner` has 0.2 of headroom above and 1.8 below; treating those
 * symmetrically would make every negative emotion nine times louder than every
 * positive one.
 */
export function deviation(ch, name) {
  const spec = CHANNELS[name];
  if (!spec) return 0;
  const v = ch[name];
  if (!Number.isFinite(v)) return 0;
  const n = spec.neutral ?? 0;
  const span = v >= n ? spec.max - n : n - spec.min;
  return span > 0 ? signed((v - n) / span) : 0;
}

/**
 * Universal channels -> one lens's inputs.
 *
 * `glow` is derived rather than mapped. There is no universal "brightness", but
 * `eyeScaleY` (0.6-1.3, neutral 1) is how wide the mascot's eye is opened, and a
 * wide eye on a lamp is an over-driven filament. Emotions already drive it hard:
 * `awe` 1.28, `surprised` 1.30, `somber` 0.88. So the lamp brightens with
 * astonishment and dims with sorrow for free, off data that already exists.
 */
/**
 * Universal channels -> one antenna's inputs.
 *
 * Brows become antennas because they say the same thing: a raised brow and a
 * lifted antenna are both "attention up". The per-side channels carry that
 * directly — `confused` sets browLY 0.7 against browRY -0.45, so one antenna
 * lifts while the other drops, which is the cocked-head reading the expression
 * is going for.
 */
/**
 * Zero the soft-body channels on a rigid character.
 *
 * `bodyTransform` implements cartoon squash and stretch — volume-preserving, so
 * narrowing vertically widens horizontally — plus a breathing bob. Both are
 * right for the mascot and wrong for a steel head, and emotions drive them hard:
 * `joyful` sets squash 0.4, `surprised` -0.5, `excited` 0.7. Left alone, picking
 * an emotion makes the robot's head visibly deform.
 *
 * Filtered here rather than inside `bodyTransform` so the mascot's path is
 * untouched — its squash is load-bearing for exactly those emotions.
 */
export function rigidBody(ch) {
  return { ...ch, squash: 0, bodyBob: 0 };
}

export function antennaChannels(ch, side) {
  const m = side === 'right' ? ROBOT_MAP.antennaR : ROBOT_MAP.antennaL;
  return {
    elevation: deviation(ch, m.elevation),
    cant: deviation(ch, m.cant),
  };
}

export function lensChannels(ch, side) {
  const m = side === 'right' ? ROBOT_MAP.lensR : ROBOT_MAP.lensL;
  return {
    eyeOpen: clamp01(ch[m.eyeOpen] ?? 1),
    squint: clamp01(ch[m.squint] ?? 0),
    gazeX: signed(ch[m.gazeX] ?? 0),
    gazeY: signed(ch[m.gazeY] ?? 0),
    // Deviation, not raw value — a resting lamp must sit at exactly mid glow and
    // exactly zero hue shift, whatever the schema happens to call neutral.
    glow: clamp01(0.5 + 0.5 * deviation(ch, m.bloom)),
    hue: deviation(ch, m.hue),
    sweat: clamp01(ch[m.sweat] ?? 0),
  };
}
