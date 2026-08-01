// Status lights: what the agent is DOING.
//
// The split that makes this worth having: LEDs read PRESENCE, lenses read
// EMOTION. The repo already guarantees those two compose rather than replace,
// but on the mascot they compose onto the same face — which is why a `thinking`
// presence suppresses the emotion's glyph, a footgun the README records as
// having fooled its own author. Separate hardware removes the contest entirely.
//
// Shape is discrete and per-presence; rate is continuous. So "the agent is
// getting busier" is an acceleration of one pattern rather than a jump-cut
// between two.

import { PRESENCES } from './expressions.js';

/**
 * Hard ceiling on flash frequency.
 *
 * A photosensitivity bound, not a style choice. `face-render.js` already carries
 * the rule — "Frequency is held below the WCAG three-flashes-per-second bound" —
 * and runs its flicker at 2.5 Hz. An earlier draft of the table below put
 * `working` at 6.4 Hz and `glitch` at 9.6 Hz while claiming a 4.4 Hz maximum
 * that was itself wrong: three errors in one paragraph, including the
 * accessibility rule this codebase had already established elsewhere.
 *
 * So it is clamped in code and swept by a test, rather than trusted to whoever
 * edits the table next.
 */
export const MAX_HZ = 2.8;

/**
 * Brightness of one lamp, 0..1.
 *
 * Every shape is a function of (index, count, phase) so it works for any cluster
 * size and degrades on its own — `chase` across two lamps IS `alternate`, and
 * falls out of the same function without a special case. The robots have three,
 * two, and none respectively, so this is not hypothetical.
 */
export const SHAPES = {
  off: () => 0,
  solid: () => 1,

  breathe: (i, n, p) => 0.35 + 0.65 * (0.5 - 0.5 * Math.cos(2 * Math.PI * p)),

  // Two quick beats then a rest. Reads as "idle but powered".
  heartbeat: (i, n, p) => {
    if (p < 0.12) return 1;
    if (p < 0.22) return 0.15;
    if (p < 0.34) return 1;
    return 0.1;
  },

  blink: (i, n, p) => (p < 0.5 ? 1 : 0.08),

  // Runs down the stack. One lamp lit at a time, the others banked low so the
  // cluster still reads as powered rather than as mostly broken.
  chase: (i, n, p) => (Math.floor(p * n) === i ? 1 : 0.14),

  alternate: (i, n, p) => (Math.floor(p * 2) === i % 2 ? 1 : 0.14),

  // Rides the audio envelope. Shares its input with the grille voice bar, so
  // lip-sync and the speaking light cannot drift apart.
  voice: (i, n, p, level = 0) => 0.2 + 0.8 * Math.max(0, Math.min(1, level)),

  /**
   * The ONLY irregular shape, and that is load-bearing.
   *
   * Everything else in this system is exactly periodic, because an LED is
   * electronic — a jittery status light reads as faulty, which is a meaning
   * worth reserving for actual faults. Making irregularity itself the error
   * signal means that when something does jitter, it means something.
   *
   * Deterministic despite looking random: driven from the phase, so the same
   * fault produces the same stutter every time. A mechanism that errs the same
   * way reads as broken; one that errs differently every frame reads as noise.
   */
  flicker: (i, n, p) => {
    const s = Math.sin(p * 97.3 + i * 12.9) * 43758.5453;
    const r = s - Math.floor(s);
    return r < 0.22 ? 0.05 : 0.55 + 0.45 * r;
  },
};

/**
 * Presence -> colour, shape and base rate.
 *
 * Total over every key of `PRESENCES` plus `glitch`. A partial table leaves the
 * remainder dark with no declared behaviour, which is the silent-loss failure
 * this repo keeps rediscovering — a state that reports success while nothing
 * moves. `ledTableGaps` below turns that into a test rather than a discovery.
 */
export const PRESENCE_LEDS = {
  idle: { color: '#3ddc84', shape: 'heartbeat', baseHz: 0.25 },
  listening: { color: '#2f9bff', shape: 'breathe', baseHz: 0.45 },
  silent: { color: '#2f9bff', shape: 'solid', baseHz: 0 },
  thinking: { color: '#ffab2e', shape: 'chase', baseHz: 0.85 },
  confused: { color: '#ffab2e', shape: 'alternate', baseHz: 0.7 },
  speaking: { color: '#ffab2e', shape: 'voice', baseHz: 0 },
  paused: { color: '#3ddc84', shape: 'breathe', baseHz: 0.15 },
  working: { color: '#ffab2e', shape: 'chase', baseHz: 1.2 },
  glitch: { color: '#ff2d55', shape: 'flicker', baseHz: 2.0 },
};

/** Presences with no row, and rows naming a presence that no longer exists. */
export function ledTableGaps() {
  const names = new Set([...Object.keys(PRESENCES), 'glitch']);
  return {
    missing: [...names].filter((n) => !PRESENCE_LEDS[n]).sort(),
    stale: Object.keys(PRESENCE_LEDS).filter((n) => !names.has(n)).sort(),
    badShape: Object.entries(PRESENCE_LEDS)
      .filter(([, v]) => !SHAPES[v.shape]).map(([k]) => k).sort(),
  };
}

/**
 * Effective frequency for a presence at a given rate channel.
 *
 * `rate` is a normalised 0..1 like every other channel, scaling the row's base
 * from 0.25x to 4x. Clamped to MAX_HZ at the point of use, so no edit to the
 * table above can push a lamp past the photosensitivity bound.
 */
export function ledHz(row, rate = 0.5) {
  const r = Number.isFinite(rate) ? Math.max(0, Math.min(1, rate)) : 0.5;
  return Math.min(MAX_HZ, (row?.baseHz ?? 0) * (0.25 + 3.75 * r));
}

/**
 * Brightness for every lamp in a cluster.
 *
 * @param presence one of PRESENCES, or 'glitch'
 * @param count    lamps in this character's cluster
 * @param time     seconds
 * @param opts     rate 0..1, audio level 0..1, reduced motion
 */
export function ledLevels(presence, count, time, { rate = 0.5, level = 0, reduced = false } = {}) {
  const row = PRESENCE_LEDS[presence] ?? PRESENCE_LEDS.idle;
  // Under reduced motion the status stays legible and the flashing stops. The
  // information is in the COLOUR as much as the rhythm, so going solid loses
  // far less than going dark would.
  const shape = reduced ? SHAPES.solid : (SHAPES[row.shape] ?? SHAPES.solid);
  const hz = ledHz(row, rate);
  const phase = hz > 0 ? (time * hz) % 1 : 0;
  const out = [];
  for (let i = 0; i < count; i++) out.push(clamp01(shape(i, count, phase, level)));
  return { color: row.color, levels: out, hz };
}

const clamp01 = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
