// 50 ms. Guards against backgrounded-tab rAF gaps: an unclamped multi-second
// step makes the integration explode.
const MAX_DT = 0.05;

/**
 * Named transition dynamics.
 *
 * Critical damping is `2 * sqrt(stiffness)`. Sitting under it produces
 * overshoot, which is what makes a snap read as a recoil rather than a glide;
 * sitting over it produces a heavy settle with no bounce at all.
 *
 * BUT these values are tuned against MEASURED output, not the analytic formula.
 * Semi-implicit Euler adds numerical damping proportional to stiffness * dt^2,
 * so at 60fps a stiff spring loses most of its theoretical bounce: `snap` at a
 * nominal zeta of 0.73 measured under 1% overshoot where theory predicted 3.6%.
 * Deriving these from `zeta = c / 2*sqrt(k)` alone produces a table where only
 * the soft rows behave as intended.
 *
 * A single spring for every emotion is why uniform animation reads as
 * mechanical: a surprise and a slide into sorrow have no business arriving at
 * the same speed.
 */
export const DYNAMICS = {
  //                                        measured at 60fps:
  //                                        rise-to-90%   overshoot
  snap:   { stiffness: 320, damping: 19 }, //   100 ms      10.8%
  quick:  { stiffness: 210, damping: 21 }, //   167 ms       1.5%
  normal: { stiffness: 120, damping: 22 }, //   350 ms       0
  slow:   { stiffness: 55,  damping: 17 }, //   617 ms       0
  heavy:  { stiffness: 30,  damping: 14 }, //   967 ms       0
};

export function createSpringSet(initial, { stiffness = 120, damping = 22 } = {}) {
  const values = { ...initial };
  const targets = { ...initial };
  const vel = {};
  for (const k of Object.keys(values)) vel[k] = 0;
  // Named `stiff`/`damp` rather than k/c: `k` is also the loop variable for a
  // channel name a few lines below, and one letter meaning two things in one
  // file is how a future edit introduces a very confusing bug.
  let stiff = stiffness;
  let damp = damping;

  /** Swap transition dynamics. Accepts a DYNAMICS name or explicit values. */
  function setDynamics(spec) {
    const d = typeof spec === 'string' ? DYNAMICS[spec] : spec;
    if (!d) {
      console.warn(`[mascot] unknown dynamics "${spec}"`);
      return false;
    }
    if (Number.isFinite(d.stiffness)) stiff = d.stiffness;
    if (Number.isFinite(d.damping)) damp = d.damping;
    return true;
  }

  function setTarget(partial) {
    if (!partial) return;
    for (const [k, v] of Object.entries(partial)) {
      if (!(k in values)) continue;
      if (typeof v !== 'number' || !Number.isFinite(v)) continue;
      targets[k] = v;
    }
  }

  function snap(partial) {
    if (!partial) return;
    for (const [k, v] of Object.entries(partial)) {
      if (!(k in values)) continue;
      if (typeof v !== 'number' || !Number.isFinite(v)) continue;
      values[k] = v;
      targets[k] = v;
      vel[k] = 0;
    }
  }

  function step(dt) {
    const h = Math.min(Math.max(dt, 0), MAX_DT);
    for (const name of Object.keys(values)) {
      const a = stiff * (targets[name] - values[name]) - damp * vel[name];
      vel[name] += a * h;
      values[name] += vel[name] * h;
    }
    return values;
  }

  return {
    values,
    // Where the springs are HEADING, as distinct from where they are. The rig
    // reports this as the held expression; without it, a caller asking "what
    // expression is set?" mid-transition gets a partial interpolation instead.
    targets,
    setTarget,
    setTargets: setTarget,
    snap,
    step,
    setDynamics,
    getDynamics: () => ({ stiffness: stiff, damping: damp }),
  };
}
