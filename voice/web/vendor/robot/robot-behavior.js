// The listening-and-speaking behaviour layer: small movements that track the
// CONVERSATION rather than the held emotion.
//
// The held emotion answers "how does it feel"; this layer answers "is it
// engaged". A robot that holds one perfect pose while someone talks to it
// reads as a screensaver. What separates a listener from a statue is small and
// specific: the occasional head-cock, eyes that widen slightly toward the
// speaker, a head that ticks along with the rhythm of its own speech, an
// antenna flick when its mood actually changes.
//
// Everything here returns OFFSETS on ordinary channels, composed on top of
// springs, autonomic noise and the servo. Nothing is written back into held
// state, so the layer can be dropped without a trace — and every movement is
// seeded, per the governing rule: electronic commands are deterministic,
// physical mechanisms are repeatable, only biology gets true randomness.

import { mulberry32 } from './autonomic.js';

const clamp01 = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);

export function createBehavior({ seed = 1 } = {}) {
  const rand = mulberry32(seed * 7919 + 3);

  // --- speech envelope -------------------------------------------------------
  // Fast attack, slow release: the lamps flare with a syllable and fade in the
  // gap after it, which is what makes the glow read as THE VOICE rather than
  // as a lamp that happens to flicker.
  let envelope = 0;
  let lastLevel = 0;

  // --- emphasis ticks --------------------------------------------------------
  // A local peak in the voice with a refractory window behind it is a stressed
  // beat. The head answers with a small tick — alternating direction, so a run
  // of beats reads as animated talk rather than repeated nodding at one side.
  let refractory = 0;
  let tick = 0;          // decaying impulse magnitude
  let tickSign = 1;

  // --- listening head-cock ---------------------------------------------------
  // Listeners cock their heads occasionally and HOLD, they do not sway. The
  // offset moves at a constant rate toward its target (a mechanism, not a
  // spring) and re-decides every few seconds.
  let cockTarget = 0;
  let cock = 0;
  let cockTimer = 0;

  // --- accents ---------------------------------------------------------------
  let glowPop = 0;       // brief lens flare, from pulse() or an emotion change
  let flick = 0;         // antenna impulse, decays fast
  let flickSign = 1;

  function onPulse(strength = 1) {
    glowPop = Math.max(glowPop, 0.5 * clamp01(strength));
    tick = Math.max(tick, 0.35 * clamp01(strength));
    tickSign = -tickSign;
  }

  function onEmotionChange(intensity = 1) {
    flick = Math.max(flick, 0.6 + 0.4 * clamp01(intensity));
    flickSign = -flickSign;
    glowPop = Math.max(glowPop, 0.35);
  }

  /**
   * Advance and return channel offsets for this frame.
   *
   * @param dt seconds
   * @param state { presence, audioLevel }
   */
  function update(dt, { presence = null, audioLevel = 0 } = {}) {
    const h = Math.min(Math.max(dt, 0), 0.05);
    const level = clamp01(audioLevel);

    // Envelope: attack in ~80ms, release in ~400ms.
    const k = level > envelope ? Math.min(1, h / 0.08) : Math.min(1, h / 0.4);
    envelope += (level - envelope) * k;

    // Emphasis: a rise that crests above the envelope's recent shoulder, with
    // at least 420ms since the last tick. Cheap peak detection, but speech is
    // forgiving — what matters is that ticks land ON syllable stress and never
    // machine-gun.
    refractory = Math.max(0, refractory - h);
    if (refractory === 0 && level > 0.28 && level > lastLevel + 0.06 && envelope > 0.2) {
      tick = Math.max(tick, 0.28 + 0.22 * level);
      tickSign = -tickSign;
      refractory = 0.6;
    }
    lastLevel = level;
    tick = Math.max(0, tick - h / 0.3);         // ~300ms decay

    // Listening head-cock: only while the presence is actually `listening`.
    if (presence === 'listening') {
      cockTimer -= h;
      if (cockTimer <= 0) {
        // Hold roughly 2.5-5s per attitude; a third of the time return to
        // level, so the cock reads as consideration rather than a stuck pose.
        cockTarget = rand() < 0.34 ? 0 : (rand() < 0.5 ? -1 : 1) * (0.10 + rand() * 0.14);
        cockTimer = 2.5 + rand() * 2.5;
      }
    } else {
      cockTarget = 0;
      cockTimer = 0;
    }
    // Constant-rate approach — a mechanism repositioning, not a sway.
    const dc = cockTarget - cock;
    const step = 0.25 * h;                       // full swing in ~1s
    cock += Math.abs(dc) <= step ? dc : Math.sign(dc) * step;

    glowPop = Math.max(0, glowPop - h / 0.5);    // ~500ms decay
    flick = Math.max(0, flick - h / 0.35);       // ~350ms decay

    const attentive = presence === 'listening' ? 1 : 0;

    return {
      // Head: emphasis ticks while speaking, held cocks while listening.
      // 0.09, down from 0.16: at 0.16 the ticks read as jitter rather than
      // punctuation — reported from live use, which is the review that counts.
      headTilt: tick * 0.09 * tickSign + cock,
      // Lenses: the voice flares the lamps (eyeScaleY drives glow), listening
      // widens them slightly — attention, in hardware.
      eyeScaleY: envelope * 0.14 + glowPop * 0.10 + attentive * 0.05,
      // Antennas: the flick on a mood change, split so the pair scissors.
      browLY: flick * 0.45 * flickSign,
      browRY: flick * 0.45 * -flickSign,
    };
  }

  return { update, onPulse, onEmotionChange };
}
