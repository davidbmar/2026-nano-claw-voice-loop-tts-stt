/**
 * Drop-in bridge so the mascot can replace nano-claw's talking-cube renderer
 * without changing anything in nano-claw.
 *
 * nano-claw's `voice/web/app.js` already owns the emotion state machine, the
 * auto-inference of emotion from reply text, and a stable control surface:
 *
 *     window.VoiceEmotion = {
 *       set(name, opts),   // opts.intensity; also switches auto OFF
 *       presence(name),
 *       auto(on),
 *       state(),           // { auto, emotion, intensity, presence }
 *       profiles,          // array of emotion names
 *     }
 *
 * So there is no integration to invent — there is a contract to conform to.
 * Installing this bridge makes the mascot answer that contract exactly.
 *
 * Vocabulary check against nano-claw as of 2026-07-30:
 *   emotions  neutral calm curious confused warm joyful confident tense somber
 *             awe urgent                       — all 11 present here
 *   presences idle listening silent thinking confused speaking paused
 *                                             — all 7 present here
 * The mascot's vocabulary is a strict superset on both axes, so every call
 * nano-claw can make resolves, and the extra states (surprised, working,
 * hardWorking, sweating, worried and the rest) are available if nano-claw ever
 * wants them.
 *
 * Two contract details that matter:
 *   - `set` and `presence` return a BOOLEAN, false for an unknown name. They must
 *     not throw: nano-claw calls them from its render path.
 *   - `set` turns auto-inference off, matching nano-claw's own implementation,
 *     so a manual override is not immediately overwritten by the next reply.
 */

import { EMOTIONS, PRESENCES } from './expressions.js';
import { neutralChannels } from './face-channels.js';

/**
 * Every method nano-claw's app.js calls on its renderer, with call counts as of
 * 2026-07-30. Verified by grep rather than assumed — the emotion contract and the
 * renderer contract are separate surfaces, and conforming to one says nothing
 * about the other.
 */
export const NANO_CLAW_RENDERER_METHODS = [
  'pulse',              // 10 — emphasis beat
  'setColors',          //  5 — cube-specific, monochrome mascot ignores it
  'importProfile',      //  4
  'setSpeaking',        //  2
  'setPattern',         //  2 — cube-specific
  'disconnectAnalyser', //  2
  'connectAnalyser',    //  2 — the live TTS AnalyserNode
  'setPanelOpen',       //  1 — host UI hint
  'pushAudioFrame',     //  1
  'getProfile',         //  1
  'destroy',            //  1
  'configure',          //  1
];

/**
 * Wrap the mascot rig so it answers nano-claw's renderer contract.
 *
 * Methods that mean something here are mapped. Cube-specific ones (`setColors`,
 * `setPattern`) are deliberate no-ops returning true: the mascot is monochrome
 * pen-and-ink line art, so a colour or pattern request has no meaning, and
 * throwing would break a host that is entitled to call them.
 */
export function createRendererShim(rig, director) {
  if (!rig) throw new Error('createRendererShim: rig is required');
  const noop = () => true;

  return {
    // --- audio, the part that matters -------------------------------------
    connectAnalyser: (node) => rig.connectAnalyser(node),
    disconnectAnalyser: () => rig.disconnectAnalyser(),
    pushAudioFrame: (frame) => rig.pushAudioFrame(frame ?? {}),
    setAudioLevel: (level, bands) => rig.pushAudioFrame({ level, bands, speaking: true }),

    /** Mouth on or off. Presence stays the host's business. */
    setSpeaking(on) {
      rig.pushAudioFrame({ level: on ? 0.4 : 0, speaking: !!on });
      return true;
    },

    /** An emphasis beat. nano-claw's most-used call, so it maps to a real cue. */
    pulse(opts) {
      const strength = Number(opts?.strength);
      director?.cue('impact', {
        intensity: Number.isFinite(strength) ? Math.max(0, Math.min(1, strength / 1.5)) : 0.7,
      });
      return true;
    },

    // --- profiles: channel sets, versioned separately from the cube's ------
    getProfile() {
      return { schema: 'computer-mascot/profile', version: 1, channels: rig.getChannels() };
    },
    importProfile(profile) {
      let p = profile;
      if (typeof p === 'string') {
        try {
          p = JSON.parse(p);
        } catch {
          return false;
        }
      }
      // A cube profile has none of our channel names, so nothing applies and the
      // face is left alone rather than being reset to a meaningless state.
      const channels = p?.channels ?? p;
      if (!channels || typeof channels !== 'object') return false;
      const known = Object.keys(neutralChannels());
      const usable = Object.fromEntries(
        Object.entries(channels).filter(([k]) => known.includes(k)),
      );
      if (!Object.keys(usable).length) return false;
      rig.setChannels(usable);
      return true;
    },

    /** Settings bag. Only the keys the mascot understands are honoured. */
    configure(settings) {
      if (!settings || typeof settings !== 'object') return false;
      if (settings.autonomic != null) rig.setAutonomic(settings.autonomic);
      if (typeof settings.dynamics === 'string') rig.setDynamics(settings.dynamics);
      return true;
    },

    /**
     * Teardown. Must stop the DIRECTOR's work too, not just the rig's.
     *
     * This used to call only `rig.disconnectAnalyser()` and `rig.stop()`, which
     * ends the render loop and leaves everything else running. The performer
     * schedules its own rAF — one loop for a timeline, another for a stream —
     * and neither belongs to the rig. Measured: with a stream open and a timeline
     * mid-flight, `destroy()` was followed by 14 further writes into the
     * torn-down rig, and the stream drained 14 more syllables it had no reason to
     * speak. Those loops reschedule forever, so a host that mounts and unmounts
     * the avatar leaks one per cycle.
     *
     * `director.reset()` is exactly the right call: it cancels the timeline,
     * closes the stream, and clears held state. Done BEFORE `rig.stop()` so the
     * final writes land on a live rig rather than a dead one.
     */
    destroy() {
      director?.reset();
      rig.disconnectAnalyser();
      rig.stop();
      return true;
    },

    // --- cube-specific, deliberately inert --------------------------------
    setColors: noop,   // monochrome line art
    setPattern: noop,  // no field patterns
    setPanelOpen: noop, // host UI hint
  };
}

/** Report any renderer method nano-claw calls that the shim does not answer. */
export function rendererGaps(shim) {
  return NANO_CLAW_RENDERER_METHODS.filter((m) => typeof shim?.[m] !== 'function');
}

/** Names nano-claw is known to send. Used to verify the superset holds. */
export const NANO_CLAW_EMOTIONS = [
  'neutral', 'calm', 'curious', 'confused', 'warm', 'joyful',
  'confident', 'tense', 'somber', 'awe', 'urgent',
];

export const NANO_CLAW_PRESENCES = [
  'idle', 'listening', 'silent', 'thinking', 'confused', 'speaking', 'paused',
];

/**
 * Report any name nano-claw can send that this build cannot resolve.
 * Empty arrays mean the mascot is a safe drop-in.
 */
export function coverageGaps() {
  return {
    emotions: NANO_CLAW_EMOTIONS.filter((n) => !(n in EMOTIONS)),
    presences: NANO_CLAW_PRESENCES.filter((n) => !(n in PRESENCES)),
  };
}

/**
 * Install `window.VoiceEmotion` backed by a CharacterDirector.
 *
 * @param director  a createDirector() instance
 * @param options.target  where to install (defaults to globalThis)
 * @param options.onChange  called after any accepted change, with the state
 * @returns { uninstall(), state() }
 */
export function installVoiceEmotionBridge(director, { target = globalThis, onChange = null } = {}) {
  if (!director) throw new Error('installVoiceEmotionBridge: director is required');

  /**
   * The bridge owns `auto` and the sticky intensity default, and NOTHING else.
   *
   * It used to own a full mirror — emotion, intensity and presence — updated by
   * its own three methods and by nothing else. So anything that drove the
   * character another way desynchronised it: `wake()` at boot, `reset()` between
   * turns, an emotion keyframe in a `perform` timeline, the harness panel.
   *
   * Reporting a stale state was the smaller half. `presence()` also
   * short-circuited on a repeat by comparing against the mirror, which turned a
   * stale READ into a dropped WRITE: at boot the bridge applied `idle`, `wake()`
   * then moved the director to `listening`, and nano-claw's next
   * `presence('idle')` compared 'idle' against the stale 'idle', returned true,
   * and never called the director. The character stayed listening while
   * nano-claw believed it was idle.
   *
   * The lesson is not "update the mirror in more places" — that is how it got
   * here, since there is already a workaround at the install site applying the
   * declared initial state so the first `presence('idle')` would not be dropped.
   * `wake()` was added afterwards and broke it. A second source of truth has to
   * be kept in step by every future caller, forever. So the reported state is
   * now DERIVED from the director, which cannot drift by construction.
   */
  const own = { auto: true, lastIntensity: 0.7 };
  const previous = target.VoiceEmotion;

  /** The contract's state shape, read from the director rather than remembered. */
  const readState = () => {
    const s = director.getState();
    return {
      auto: own.auto,
      emotion: s.emotion,
      intensity: s.intensity,
      // nano-claw's contract types presence as a string; the director uses null
      // for "no overlay", which is the same thing it calls `idle`.
      presence: s.presence ?? 'idle',
    };
  };

  const notify = () => {
    if (onChange) {
      try {
        onChange(readState());
      } catch (err) {
        // A listener must never break the render path.
        console.warn('[mascot] VoiceEmotion onChange threw', err);
      }
    }
  };

  function set(name, opts) {
    if (!(name in EMOTIONS)) return false;
    // Matches nano-claw: a manual set disables auto-inference so the next reply
    // does not immediately overwrite it.
    own.auto = false;
    if (opts && opts.intensity != null) {
      const v = Number(opts.intensity);
      if (Number.isFinite(v)) own.lastIntensity = Math.max(0, Math.min(1, v));
    }
    // Sticky by contract: nano-claw calls set(name) with no intensity when it has
    // no confidence value, and expects the previous one to carry.
    if (!director.emotion(name, { intensity: own.lastIntensity })) return false;
    notify();
    return true;
  }

  function presence(name) {
    if (!(name in PRESENCES)) return false;
    // No short-circuit on a repeat. It saved one recompose (~16us) and cost
    // correctness: comparing against a remembered value silently dropped real
    // state changes whenever anything else had moved the character. Applying it
    // unconditionally is idempotent.
    if (!director.presence(name)) return false;
    notify();
    return true;
  }

  function auto(on) {
    own.auto = on !== false;
    notify();
    return own.auto;
  }

  const bridge = {
    set,
    presence,
    auto,
    state: readState,
    // nano-claw reads this to populate its emotion dropdown. Offering the full
    // set lets the UI expose the richer vocabulary for free.
    profiles: Object.keys(EMOTIONS),
    presences: Object.keys(PRESENCES),

    /**
     * Emotion applied by nano-claw's own inference rather than by a user, so it
     * must NOT clear the auto flag. nano-claw calls setVisualEmotion directly for
     * this internally; exposed here so a host can route inference through the
     * bridge and keep the flag honest.
     */
    infer(name, opts) {
      if (!own.auto) return false;
      const ok = set(name, opts);
      // set() clears auto by contract; inference must not, so put it back.
      own.auto = true;
      return ok;
    },
  };

  target.VoiceEmotion = bridge;

  // Apply nano-claw's declared starting point rather than merely asserting it,
  // so a host that never calls set()/presence() still gets a defined state.
  // This no longer has to guard against a dropped first call — presence() is
  // unconditional now — but applying it is still more honest than claiming it.
  director.emotion('neutral', { intensity: own.lastIntensity });
  director.presence('idle');

  return {
    state: readState,
    uninstall() {
      if (target.VoiceEmotion === bridge) {
        if (previous === undefined) delete target.VoiceEmotion;
        else target.VoiceEmotion = previous;
      }
    },
  };
}
