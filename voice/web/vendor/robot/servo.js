// A servo, as distinct from a spring.
//
// Every preset in `springs.js` is a mass-spring: exponential approach,
// asymptotic, organic ease-out. That is the right shape for a face and the
// wrong shape for a machine's head. A servo ramps to a commanded rate, CRUISES
// at that rate, ramps down, and locks dead on the target — it arrives exactly,
// then holds perfectly still. The cruise and the lock are the entire "robot"
// read; a spring has neither.
//
// The README's rule that perfectly still reads as dead applies to BIOLOGICAL
// signals — it is why fixated pupils keep a residual drift. The head is
// mechanical and inverts it: a head that eases organically into position and
// never quite stops is the defect here.
//
// Units are channel units (the -1..1 the face schema speaks), not degrees, so
// the same integrator can drive any channel that wants mechanical motion.

/**
 * @param maxRate  cruise speed, channel units per second
 * @param accel    ramp, channel units per second per second
 */
export function createServo({ maxRate = 1.4, accel = 5 } = {}) {
  let pos = 0;
  let vel = 0;
  let target = 0;

  function setTarget(t) {
    if (Number.isFinite(t)) target = t;
  }

  /** Jump instantly, as after a character swap — no sweep across the face. */
  function snap(t) {
    if (Number.isFinite(t)) { pos = t; target = t; vel = 0; }
  }

  function step(dt) {
    // Same clamp discipline as springs.js: a background tab hands us seconds.
    const h = Math.min(Math.max(dt, 0), 0.05);
    if (h === 0) return pos;

    const dist = target - pos;
    if (dist === 0 && vel === 0) return pos;

    const dir = dist >= 0 ? 1 : -1;

    // The fastest speed from which the remaining distance can still be stopped
    // in — the deceleration ramp, computed rather than eased. Cruise is the
    // smaller of this and maxRate, which is what produces the trapezoid:
    // ramp up, flat top, ramp down.
    const vStop = Math.sqrt(2 * accel * Math.abs(dist));
    const vWant = dir * Math.min(maxRate, vStop);

    const dv = vWant - vel;
    const maxDv = accel * h;
    vel += Math.abs(dv) <= maxDv ? dv : (dv >= 0 ? maxDv : -maxDv);

    const before = pos;
    pos += vel * h;

    // Exact arrival. Crossing the target means the move is over: lock, do not
    // oscillate. This is the property no spring preset can have — a spring
    // crossing its target carries momentum by construction.
    if ((target - before) * (target - pos) <= 0) {
      pos = target;
      vel = 0;
    }
    return pos;
  }

  return {
    step,
    setTarget,
    snap,
    get position() { return pos; },
    get velocity() { return vel; },
    get target() { return target; },
  };
}
