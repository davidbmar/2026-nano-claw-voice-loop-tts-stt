export function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const BLINK_DOWN = 0.055; // seconds to close
const BLINK_UP = 0.11; // seconds to reopen

/**
 * The always-running layer. Blinking lives here rather than in the cue system
 * because a blink issued as a cue would compete with whatever emotion is
 * mid-interpolation and pop; as continuous additive noise it composes with any
 * held expression.
 */
export function createAutonomic({ rng, blinkMeanS = 4, doubleBlinkChance = 0.15 } = {}) {
  const rand = rng ?? mulberry32(1);
  let t = 0;

  // Poisson-distributed inter-blink interval, bounded at both ends.
  //
  // The floor stops blinks stacking. The ceiling matters just as much: a pure
  // exponential tail produces occasional 18-second gaps, which read as frozen.
  // Irregularity is what avoids a mechanical feel; the unbounded tail only
  // contributes dead air. Real intervals run roughly 2-8 seconds.
  const MIN_GAP = 0.9;
  const MAX_GAP = blinkMeanS * 2;
  const nextInterval = () =>
    Math.min(MAX_GAP, Math.max(MIN_GAP, -Math.log(1 - rand()) * blinkMeanS));
  let nextBlinkAt = nextInterval();
  let blinkStart = null;
  let pendingDouble = false;

  // Eye movement: a fast flick to a new point, then a HOLD.
  //
  // This used to interpolate over the whole 0.35-0.85s interval and start the
  // next one the instant it finished, so the pupils were never still. Measured
  // across 20 s of idle: in motion 96.4% of the time, and plotting pupilX
  // against time gave a smooth continuous wave with no flat sections anywhere.
  // That reads as swimming or dreamy rather than alive, which matters because
  // idle is the state this character spends most of its life in.
  //
  // Real fixational movement is the opposite shape: a saccade lasts 20-80 ms and
  // is followed by a fixation of several hundred, so the trace is a staircase.
  // Splitting the interval into a fast jump plus a hold gets that, and it makes
  // the eyes read as attentive — which is what an assistant should look like.
  //
  // The hold is not perfectly frozen. The original concern behind continuous
  // motion was right — "perfectly still pupils read as dead" — so a slow residual
  // drift rides on top, sized at roughly a tenth of the flick and slow enough to
  // stay below the threshold at which the eye reads as moving at all. It is the
  // difference between a held gaze and a photograph.
  let sacTarget = [0, 0];
  let sacFrom = [0, 0];
  let sacT = 0;
  let sacDur = 0.08;   // the flick itself
  let sacHold = 0.4;   // how long to sit there afterwards
  const newSaccade = () => {
    sacFrom = [...sacTarget];
    sacTarget = [(rand() * 2 - 1) * 0.09, (rand() * 2 - 1) * 0.07];
    sacT = 0;
    sacDur = 0.05 + rand() * 0.06;
    sacHold = 0.25 + rand() * 0.5;
  };
  newSaccade();

  /**
   * Breathing, with the rate varying breath to breath.
   *
   * It used to be `sin(t * 2*PI * 0.25)`: 15 breaths a minute, which is the right
   * rate, but the SAME 4.000 s every cycle forever. Measured across 40 s the
   * period variation was exactly 0.0000 s — a metronome.
   *
   * That contradicts a principle this file already states, fifty lines above, as
   * the reason blinks are Poisson-distributed rather than evenly spaced:
   * "irregularity is what avoids a mechanical feel". Real effort went into
   * avoiding regularity in a 165 ms event, while the breath — continuous, always
   * on screen, and 26 px peak to peak — was left perfectly periodic.
   *
   * It matters more since the eyes were given fixations, because the bob became
   * the only exactly periodic signal left in the system, and a single metronome
   * among irregular signals is what an eye picks out.
   *
   * The rate is redrawn each cycle from a band that stays inside the human
   * resting range (12.6–17.4 breaths/min), so the motion never settles into a
   * predictable loop. Phase is advanced incrementally rather than computed from
   * `t`, since the rate now changes underneath it.
   */
  const BREATH_MIN_HZ = 0.21;
  const BREATH_MAX_HZ = 0.29;
  const breathPhase = rand() * Math.PI * 2;
  let breathT = breathPhase;
  let breathHz = BREATH_MIN_HZ + rand() * (BREATH_MAX_HZ - BREATH_MIN_HZ);

  function blinkValue(elapsed) {
    if (elapsed < BLINK_DOWN) return 1 - elapsed / BLINK_DOWN;
    const up = elapsed - BLINK_DOWN;
    if (up < BLINK_UP) return up / BLINK_UP;
    return null; // finished
  }

  function forceBlink() {
    blinkStart = t;
  }

  function step(dt) {
    t += dt;

    if (blinkStart === null && t >= nextBlinkAt) {
      blinkStart = t;
      // The natural double-blink cluster.
      pendingDouble = rand() < doubleBlinkChance;
      nextBlinkAt = t + nextInterval();
    }

    let open = 1;
    if (blinkStart !== null && blinkStart <= t) {
      const v = blinkValue(t - blinkStart);
      if (v === null) {
        blinkStart = null;
        if (pendingDouble) {
          pendingDouble = false;
          blinkStart = t + 0.15 + rand() * 0.25;
        }
      } else {
        open = v;
      }
    }

    sacT += dt;
    // Retarget only once the flick AND the fixation after it have elapsed.
    if (sacT >= sacDur + sacHold) newSaccade();
    // k saturates at 1 during the hold, so the pupil sits on target rather than
    // sliding toward the next one.
    const k = Math.min(1, sacT / sacDur);
    const ease = k * k * (3 - 2 * k);
    // Residual ocular drift, so a held gaze is not a frozen one. Deliberately
    // slower and smaller than the flick — it should be felt, not seen.
    const driftX = Math.sin(t * 1.1 + breathPhase) * 0.008;
    const driftY = Math.cos(t * 0.9 + breathPhase) * 0.006;
    const px = sacFrom[0] + (sacTarget[0] - sacFrom[0]) * ease + driftX;
    const py = sacFrom[1] + (sacTarget[1] - sacFrom[1]) * ease + driftY;

    breathT += dt * 2 * Math.PI * breathHz;
    if (breathT >= Math.PI * 2) {
      // A whole breath done: wrap the phase and draw the next one's rate. Wrapping
      // rather than resetting keeps the waveform continuous across the seam.
      breathT -= Math.PI * 2;
      breathHz = BREATH_MIN_HZ + rand() * (BREATH_MAX_HZ - BREATH_MIN_HZ);
    }
    const bob = Math.sin(breathT);

    return { eyeLOpen: open, eyeROpen: open, pupilX: px, pupilY: py, bodyBob: bob };
  }

  return { step, forceBlink };
}
