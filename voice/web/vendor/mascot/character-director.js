import {
  EMOTIONS,
  PRESENCES,
  CUES,
  GLYPHS,
  ATTACK,
  PRESENCE_ATTACK,
  resolveEmotion,
  resolvePresence,
  resolveCue,
} from './expressions.js';
import { createPerformer } from './performance.js';
import { neutralChannels, mergeChannels } from './face-channels.js';

/**
 * The semantic layer. Mirrors talking_visualization's VoiceDirector so nano-claw
 * can emit one command stream that drives either surface.
 *
 * Presence and emotion are separate channels of state on purpose: silence does
 * not prove the model is thinking, so `thinking` and `confused` must be set
 * intentionally rather than inferred.
 */
export function createDirector(rig, { now, onState = null } = {}) {
  const state = { presence: null, emotion: 'neutral', intensity: 1 };

  // Presence wins over emotion: it describes what the agent is doing, which is
  // the more actionable signal when both would show a glyph.
  function syncGlyph() {
    rig.setGlyph?.(GLYPHS[state.presence] ?? GLYPHS[state.emotion] ?? null);
  }

  /**
   * Recompute the FULL channel set from neutral, then the held emotion, then
   * the held presence.
   *
   * Emotions must be complete states, not diffs. Pushing only the channels a
   * table happens to mention lets residue accumulate: after hardWorking the
   * character keeps its sweat, strain marks and progress meter through relieved
   * and proud, because none of those tables mention `effort` or `sweat`. No
   * caller can be expected to know it must clear four unrelated channels from
   * two emotions ago.
   *
   * Presence is applied last and therefore wins on overlap: it describes the
   * current activity, which is the more immediate signal — a speaking mouth has
   * to open regardless of the emotional colour underneath it.
   */
  /** Report held state to a listener. Must never break the render path. */
  function notifyState() {
    if (!onState) return;
    try {
      onState({ ...state });
    } catch (err) {
      console.warn('[mascot] onState listener threw', err);
    }
  }

  function recompose(attack) {
    // Timing is part of the expression, so the dynamics change with the state,
    // not just the target. Presence wins here for the same reason it wins on
    // channels: it describes the current activity.
    if (attack) rig.setDynamics?.(attack);
    const e = resolveEmotion(state.emotion, state.intensity) ?? {};
    const p = state.presence ? (resolvePresence(state.presence) ?? {}) : {};
    rig.setChannels(mergeChannels(neutralChannels(), e, p));
    syncGlyph();
    notifyState();
  }

  function presence(name) {
    if (!resolvePresence(name)) return false;
    state.presence = name;
    recompose(PRESENCE_ATTACK[name] ?? ATTACK[state.emotion] ?? 'normal');
    return true;
  }

  function emotion(name, { intensity = 1 } = {}) {
    if (!resolveEmotion(name, intensity)) return false;
    state.emotion = name;
    state.intensity = intensity;
    recompose(
      (state.presence && PRESENCE_ATTACK[state.presence]) ?? ATTACK[name] ?? 'normal',
    );
    return true;
  }

  /** Drop the presence overlay, leaving only the held emotion. */
  function clearPresence() {
    state.presence = null;
    recompose(ATTACK[state.emotion] ?? 'normal');
    return true;
  }

  function expression({ presence: p, emotion: e, intensity } = {}) {
    let ok = true;
    if (e) ok = emotion(e, { intensity }) && ok;
    if (p) ok = presence(p) && ok;
    return ok;
  }

  function cue(name, { intensity = 1 } = {}) {
    const c = resolveCue(name, intensity);
    if (!c) return false;
    // Blinks belong to the autonomic layer, not the cue stack — as a cue a blink
    // would fight whatever emotion is mid-interpolation and pop.
    if (c.blink) {
      rig.blink();
      if (c.double) setTimeout(() => rig.blink(), 220);
      return true;
    }
    rig.addCue(c);
    return true;
  }

  const performer = createPerformer({ rig, director: { emotion, presence, cue }, now });

  /**
   * Clear everything: channels, held state, and any timeline or stream in flight.
   *
   * Cancelling the performer is essential, not tidiness. Without it a running
   * timeline keeps firing into the freshly-reset face — a queued keyframe several
   * seconds out would resurrect an expression the caller believed they had
   * cleared, with no command in between to explain it.
   */
  function reset() {
    performer.cancel();
    performer.closeStream();
    rig.resetChannels();
    state.presence = null;
    state.emotion = 'neutral';
    state.intensity = 1;
    syncGlyph();
    // Tell the listener, or the status text goes STALE — which is the exact
    // failure the announcer exists to prevent. Every other path that changes
    // held state goes through recompose(), which notifies; reset() mutates the
    // state object directly and so was silently exempt.
    //
    // It matters most here of all: `reset` is the documented way to tidy up
    // between turns, so the announcement left behind was always the last thing
    // the agent was doing. Driving a full conversational turn and ending it left
    // a screen reader saying "Speaking" indefinitely after the turn was over.
    notifyState();
    return true;
  }

  /** Set channels this instant, with no spring lag — for frame-by-frame driving. */
  function frame(channels) {
    if (!channels || typeof channels !== 'object') return false;
    rig.snapChannels(channels);
    return true;
  }

  /**
   * Power on. The character arrives dark with its eyes shut and comes up.
   *
   * The `boot` cue — binary rain, scanlines, a flicker, eyes pulled closed —
   * existed and was tuned from the beginning, and was never once fired. Without
   * it the mascot simply materialises mid-expression, which is the one moment a
   * CRT character has no excuse to waste.
   *
   * Firing the cue ALONE is not enough, and the reason is worth stating because
   * it is easy to get backwards. A cue is an additive offset that decays, so it
   * necessarily starts at zero and ramps in: measured, the eyes went 0.97 -> 0.24
   * -> 1.0, which reads as a blink with static over it rather than a screen
   * coming on. The interesting state of an entrance is the state it STARTS in,
   * and an impulse cannot express that. So the eyes are snapped shut first —
   * `snapChannels` sets an initial condition with no transit — and only then does
   * the cue run and the expression spring in over the top.
   *
   * Under reduced motion the burst is dropped entirely rather than damped. It is
   * decorative rather than informational, so nothing is lost by cutting it, and
   * `flicker` is the one channel here with a photosensitivity dimension.
   */
  function wake({ presence: p = 'listening', emotion: e = 'warm', intensity = 0.8 } = {}) {
    if (rig.isReducedMotion?.()) {
      expression({ presence: p, emotion: e, intensity });
      return true;
    }
    // The state a powered-off CRT is actually in: eyes shut, no rain, no
    // scanlines, mouth at rest. There is no brightness channel — the screen fill
    // is the character's colour, not a variable — so "off" is expressed by every
    // screen-borne channel being at zero rather than by dimming.
    rig.snapChannels({
      eyeLOpen: 0,
      eyeROpen: 0,
      binaryRain: 0,
      scanlines: 0,
      glyphOpacity: 0,
      mouthOpen: 0,
    });
    cue('boot', { intensity: 1 });
    expression({ presence: p, emotion: e, intensity });
    return true;
  }

  async function one(cmd) {
    if (!cmd || typeof cmd !== 'object') return { ok: false, error: 'command must be an object' };
    switch (cmd.action) {
      case 'presence':
        return { ok: cmd.presence ? presence(cmd.presence) : clearPresence() };
      case 'emotion':
        return { ok: emotion(cmd.emotion, { intensity: cmd.intensity }) };
      case 'expression':
        return { ok: expression(cmd) };
      case 'cue':
        return { ok: cue(cmd.cue, { intensity: cmd.intensity }) };
      case 'frame':
        return { ok: frame(cmd.channels) };
      case 'wake':
        return { ok: wake(cmd) };
      case 'perform':
        return await performer.perform({ id: cmd.id, keys: cmd.keys, durationMs: cmd.durationMs });
      case 'speak':
        return await performer.speak({
          text: cmd.text,
          durationMs: cmd.durationMs,
          emotion: cmd.emotion,
          intensity: cmd.intensity,
        });
      case 'stop':
        performer.cancel();
        performer.closeStream();
        return { ok: true };
      case 'stream.open':
        return performer.openStream({
          id: cmd.id, emotion: cmd.emotion, intensity: cmd.intensity,
          rateMs: cmd.rateMs, autoPresence: cmd.autoPresence,
        });
      case 'stream.text':
        return performer.pushText({
          text: cmd.text, emotion: cmd.emotion,
          intensity: cmd.intensity, emphasis: cmd.cue,
        });
      case 'stream.close':
        return performer.closeStream();
      case 'reset':
        return { ok: reset() };
      default:
        return { ok: false, error: `unknown action "${cmd.action}"` };
    }
  }

  async function command(cmd) {
    if (Array.isArray(cmd)) {
      const out = [];
      for (const c of cmd) out.push(await one(c));
      return out;
    }
    return one(cmd);
  }

  // Hand-maintained, and therefore drift-prone: this list is the ONLY way an AI
  // driver learns what it can send, so an action missing here is an action that
  // does not exist as far as the caller is concerned. Guarded against the
  // command switch by tests/character-director.test.js.
  const ACTIONS = [
    'presence', 'emotion', 'expression', 'cue',
    'frame', 'wake', 'perform', 'speak', 'stop', 'reset',
    'stream.open', 'stream.text', 'stream.close',
  ];

  const getCapabilities = () => ({
    emotions: Object.keys(EMOTIONS),
    presences: Object.keys(PRESENCES),
    cues: Object.keys(CUES),
    actions: ACTIONS,
  });

  const getToolDefinition = () => ({
    name: 'control_mascot_expression',
    description:
      'Control the animated computer mascot. ' +
      'Hold ONE emotion per thought and change it when the idea changes, not per sentence. ' +
      'Set presence when the ACTIVITY changes; presence and emotion compose, and presence wins on ' +
      'overlap. Send action:"presence" with no name to drop the overlay when an activity ends. ' +
      'Use at most one or two cues per paragraph — they are punctuation, and overlapping cues sum. ' +
      'Do NOT cue "blink": the character already blinks on its own, and a regular cadence reads as ' +
      'mechanical. ' +
      'confused, working and thinking work as either an emotion or a presence; listening, silent, ' +
      'speaking, paused and idle are presence-only. ' +
      'Each emotion arrives at its own speed — surprised in ~100ms, somber over ~967ms — so match ' +
      'any "perform" keyframe spacing to the emotion in use. ' +
      'Use "perform" for a timed keyframe sequence and "speak" for a complete utterance. For ' +
      'dictation or token-streamed replies use stream.open, then stream.text per chunk, then ' +
      'stream.close; the mouth starts moving before the sentence is known, and the oldest ' +
      'syllables are dropped to stay in sync rather than falling behind. ' +
      'Use action:"reset" between turns — it also cancels any timeline or stream still running, ' +
      'which emotion:"neutral" does not.',
    input_schema: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ACTIONS },
        presence: { type: 'string', enum: Object.keys(PRESENCES) },
        emotion: { type: 'string', enum: Object.keys(EMOTIONS) },
        cue: { type: 'string', enum: Object.keys(CUES) },
        intensity: {
          type: 'number',
          minimum: 0,
          maximum: 1,
          description:
            'How far from the RESTING face, which is a slight smile rather than a blank. So a ' +
            'low intensity on a negative emotion still reads as content: the mouth only turns ' +
            'down above 0.59 for somber, 0.62 for worried, 0.67 for tense, 0.70 for sweating. ' +
            'Use 0.8+ for those four or the face keeps smiling. Intensity is also relative to ' +
            "each emotion's own full strength, not an absolute scale — hardWorking at 0.2 is " +
            'already stronger than calm at 1.0.',
        },
        text: { type: 'string', description: 'For "speak" and "stream.text": the words being spoken.' },
        durationMs: { type: 'number', description: 'For "speak" and "perform": total length.' },
        rateMs: { type: 'number', description: 'For "stream.open": milliseconds per syllable.' },
        autoPresence: {
          type: 'boolean',
          description:
            'For "stream.open": switch presence between speaking and silent automatically. Never ' +
            'overwrites an intentional thinking or confused.',
        },
        channels: {
          type: 'object',
          description: 'For "frame": raw channel values, applied instantly with no easing.',
          additionalProperties: { type: 'number' },
        },
        keys: {
          type: 'array',
          description: 'For "perform": keyframes, each with an "at" time in milliseconds.',
          items: {
            type: 'object',
            properties: {
              at: { type: 'number' },
              emotion: { type: 'string', enum: Object.keys(EMOTIONS) },
              presence: { type: 'string', enum: Object.keys(PRESENCES) },
              cue: { type: 'string', enum: Object.keys(CUES) },
              intensity: { type: 'number', minimum: 0, maximum: 1 },
              channels: { type: 'object', additionalProperties: { type: 'number' } },
              hard: { type: 'boolean', description: 'Snap instead of easing to the channel values.' },
            },
            required: ['at'],
          },
        },
      },
      required: ['action'],
    },
  });

  return {
    presence,
    clearPresence,
    emotion,
    expression,
    cue,
    frame,
    wake,
    reset,
    perform: performer.perform,
    speak: performer.speak,
    openStream: performer.openStream,
    pushText: performer.pushText,
    closeStream: performer.closeStream,
    stop: () => {
      performer.cancel();
      performer.closeStream();
    },
    isPerforming: performer.isRunning,
    isStreaming: performer.isStreaming,
    command,
    getCapabilities,
    getToolDefinition,
    getState: () => ({ ...state, performing: performer.getState() }),
  };
}
