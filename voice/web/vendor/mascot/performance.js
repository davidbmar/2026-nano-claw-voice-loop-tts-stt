/**
 * Frame-accurate sequencing on top of the semantic layer.
 *
 * The director's presence/emotion/cue calls are the right granularity for
 * conversational state, but they cannot express *timing* — "look surprised, then
 * 400ms later start talking, and land a nod on the last word". That needs a
 * clock.
 *
 * Two levels are provided:
 *   - `perform(script)` — a keyframe timeline, driven by rAF.
 *   - `speak(text)`     — derives a mouth track from text, for dictation.
 *
 * Both are cancellable and at most one runs at a time, because two timelines
 * fighting over the same channels produces incoherent motion rather than a
 * blend.
 */

const VOWELS = /[aeiouy]+/gi;

/**
 * Mouth shape per vowel. Openness and width are independent: "ee" is a *wide*
 * mouth but barely open, while "oo" is narrow but rounded. Classifying on a
 * character class like /[ae]/ conflates them and makes "ee" render as a yawn.
 */
const VISEMES = {
  a: { open: 0.8, width: 1.2 },   // ah  — open and wide
  e: { open: 0.5, width: 1.18 },  // eh/ee — wide but flatter
  i: { open: 0.34, width: 1.04 }, // ih  — nearly closed
  y: { open: 0.34, width: 1.04 },
  o: { open: 0.62, width: 0.74 }, // oh  — open and rounded
  u: { open: 0.44, width: 0.64 }, // oo  — small and rounded
};

/**
 * Split text into rough syllable groups and assign each a mouth shape.
 *
 * This is not phoneme-accurate lip-sync — that needs real viseme timing from a
 * TTS engine. It is a plausible mouth track derived from spelling alone, which
 * is what is available when driving from a text stream.
 */
export function mouthTrack(text, durationMs) {
  const clean = String(text ?? '').trim();
  if (!clean) return [];

  const words = clean.split(/\s+/);
  const units = [];
  for (const word of words) {
    const groups = word.match(VOWELS) ?? [];
    const syllables = Math.max(1, groups.length);
    for (let i = 0; i < syllables; i++) {
      const g = (groups[i] ?? 'a').toLowerCase();
      const shape = VISEMES[g[0]] ?? VISEMES.a;
      units.push({ ...shape, stress: i === 0 });
    }
    // A short closure between words keeps it from reading as one long vowel.
    units.push({ open: 0.08, width: 0.9, gap: true });
  }

  const total = units.length || 1;
  const step = durationMs / total;
  return units.map((u, i) => ({
    at: Math.round(i * step),
    channels: {
      mouthOpen: u.open,
      mouthWidth: u.width,
      // Stressed syllables lift the brows slightly — speech is not just a mouth.
      ...(u.stress ? { browLY: 0.16, browRY: 0.16 } : {}),
    },
  }));
}

/**
 * The channels a running speech performance writes every tick.
 *
 * A caller driving the face frame-by-frame needs this. `frame()` during speech
 * is accepted and returns true, but anything it sets on these channels is
 * overwritten on the very next tick — measured: mouthOpen 0.95 was back to 0.51
 * within 400 ms, while pupilX, headTilt and sweat all held exactly. Silently
 * discarding input is the same failure the level-of-detail floors had: the
 * caller believes it communicated something it did not.
 *
 * The rest of the face stays available during speech, which is the useful half
 * of the rule — an agent can drive brows, eyes and body while the mouth speaks.
 *
 * Reported as `getState().performing.owns` rather than only documented, and
 * derived back from actual speaker output in the tests so it cannot drift.
 */
export const SPOKEN_CHANNELS = Object.freeze([
  'mouthOpen',
  'mouthWidth',
  // Only on stressed syllables, but a caller still cannot rely on holding them.
  'browLY',
  'browRY',
]);

/** Punctuation implies emphasis; this is where cues come from in a text stream. */
export function cuesFromText(text, durationMs) {
  const clean = String(text ?? '');
  if (!clean) return [];
  const out = [];
  const len = clean.length || 1;
  const marks = [
    [/!/g, 'impact'],
    [/\?/g, 'glance'],
    [/[:—]|--/g, 'brighten'],
  ];
  for (const [re, cue] of marks) {
    for (const m of clean.matchAll(re)) {
      out.push({ at: Math.round((m.index / len) * durationMs), cue });
    }
  }
  return out.sort((a, b) => a.at - b.at);
}

/** Sentinel for "every channel", used when a keyframe pushes a whole state. */
export const ALL_CHANNELS = '*';

/**
 * Which channels a running timeline will still overwrite.
 *
 * Only the keyframes that have not fired yet count: a timeline's claim shrinks
 * as it plays, and reporting the whole thing would tell a caller its frames are
 * doomed when in fact the key that would have clobbered them is already past.
 *
 * A key carrying an `emotion` or `presence` returns ALL_CHANNELS, because those
 * push a COMPLETE channel set by design — the fix that stopped emotions leaving
 * residue also means they overwrite everything, including anything a caller has
 * driven by hand.
 */
export function pendingOwnership(active) {
  if (!active) return [];
  const owned = new Set();
  for (let i = active.i; i < active.keys.length; i++) {
    const key = active.keys[i];
    if (key.emotion || key.presence) return [ALL_CHANNELS];
    for (const name of Object.keys(key.channels ?? {})) owned.add(name);
  }
  return [...owned];
}

/**
 * How late a keyframe's CUE may be and still be worth firing.
 *
 * A backgrounded tab stops `requestAnimationFrame` entirely — it is not
 * throttled, it stops — while `performance.now()` keeps running. So the first
 * tick after the tab comes back can be arbitrarily far past its keys, and the
 * catch-up loop fires every overdue one in a single frame. Measured with a
 * five-minute gap on the harness's own `grind` arc: five emotions and THREE
 * cues in one frame.
 *
 * For state that is right. An emotion is held, so replaying the sequence and
 * landing on the last one is exactly what a viewer should see — the arc ended
 * at `proud`, so show `proud`.
 *
 * For cues it is wrong, and the difference is not a detail. A cue is punctuation
 * for a moment: a nod at the point something was agreed, an impact on a stressed
 * word. Fired half a minute late it punctuates nothing, and three of them
 * arriving together read as one compound twitch that means nothing at all.
 *
 * This codebase already draws that line everywhere else — `cueOffsets` splices
 * out cues whose lifetime elapsed during a long frame, the text stream is
 * explicitly "current, not complete", and the announcer collapses a burst of
 * changes into one utterance. The catch-up loop was the one place it did not.
 *
 * 150 ms sits below the shortest cue's own lifetime (`blink`, 180 ms) so nothing
 * is fired that would already have finished, and roughly nine frames above
 * normal scheduling jitter so ordinary playback is untouched. A hitch bad enough
 * to exceed it is a hitch bad enough that dropping one gesture is the right call.
 */
export const STALE_CUE_MS = 150;

export function createPerformer({ rig, director, now = () => performance.now() }) {
  let active = null;
  let raf = null;

  function cancel() {
    if (raf !== null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
    if (active?.reject) active.settle();
    active = null;
  }

  function tick() {
    if (!active) return;
    const t = now() - active.t0;
    while (active.i < active.keys.length && active.keys[active.i].at <= t) {
      const key = active.keys[active.i++];
      const lateBy = t - key.at;
      if (key.emotion) director.emotion(key.emotion, { intensity: key.intensity });
      if (key.presence) director.presence(key.presence);
      // State catches up; momentary gestures do NOT. See STALE_CUE_MS.
      if (key.cue && lateBy <= STALE_CUE_MS) director.cue(key.cue, { intensity: key.intensity });
      // `hard` sets the channel instantly — frame-accurate, no spring lag.
      if (key.channels) {
        if (key.hard) rig.snapChannels(key.channels);
        else rig.setChannels(key.channels);
      }
    }
    if (active.i >= active.keys.length && t >= active.durationMs) {
      const done = active;
      active = null;
      raf = null;
      done.settle();
      return;
    }
    raf = requestAnimationFrame(tick);
  }

  /**
   * Run a keyframe timeline. Resolves when the last key has fired.
   * Cancels any timeline already running.
   */
  function perform(script = {}) {
    cancel();
    const keys = [...(script.keys ?? [])]
      .filter((k) => k && typeof k.at === 'number' && Number.isFinite(k.at))
      .sort((a, b) => a.at - b.at);
    if (!keys.length) return Promise.resolve({ ok: false, error: 'no keys' });

    const durationMs = script.durationMs ?? keys[keys.length - 1].at;
    return new Promise((resolve) => {
      active = {
        id: script.id ?? null,
        keys,
        i: 0,
        durationMs,
        t0: now(),
        settle: () => resolve({ ok: true, id: script.id ?? null, keys: keys.length }),
      };
      raf = requestAnimationFrame(tick);
    });
  }

  /**
   * Drive the mouth from text over a duration. Layered on `perform`, so it is
   * cancellable and composes with whatever emotion is held.
   */
  function speak({ text, durationMs, emotion, intensity, punctuationCues = true } = {}) {
    const ms = durationMs ?? Math.max(600, String(text ?? '').length * 55);
    const keys = mouthTrack(text, ms);
    if (punctuationCues) keys.push(...cuesFromText(text, ms));
    // Close the mouth at the end rather than leaving it hanging open.
    keys.push({ at: ms, channels: { mouthOpen: 0.1, mouthWidth: 1 } });
    if (emotion) keys.unshift({ at: 0, emotion, intensity });
    return perform({ id: 'speak', keys, durationMs: ms }).then((r) => ({
      ...r,
      // Live audio is sampled after the timeline each frame, so it wins. That is
      // the right precedence — measured amplitude beats a spelling guess — but
      // without saying so, a caller cannot tell its track was ignored.
      audioOverride: rig.hasAnalyser?.() === true,
    }));
  }

  // --- streaming dictation -------------------------------------------------
  //
  // `speak` needs the whole utterance up front, which is wrong for dictation and
  // for token-streamed replies: the words arrive a few at a time and the mouth
  // has to start moving before the sentence is known. A stream drains a queue
  // instead of running a fixed-length timeline, so text can be appended at any
  // moment without restarting anything.

  let stream = null;
  let streamRaf = null;

  const SILENCE_MS = 550; // matches VoiceDirector: how long before "silent"

  // The mouth may never fall more than this far behind the incoming text.
  //
  // Syllables drain at a fixed rate, so if text arrives faster than it is spoken
  // the queue grows without limit and the mouth lags further and further behind
  // the audio — push a long paragraph and you have queued minutes of movement.
  // For lip-sync the right policy is STAY CURRENT, NOT STAY COMPLETE: drop the
  // oldest unspoken syllables so the mouth tracks what is being said now.
  const MAX_LAG_MS = 1500;

  function streamTick() {
    if (!stream) return;
    const t = now();

    if (stream.queue.length) {
      if (t >= stream.nextAt) {
        const unit = stream.queue.shift();
        rig.setChannels(unit.channels);
        stream.nextAt = t + stream.rateMs;
        stream.lastSpokeAt = t;
        stream.spoken++;
        if (stream.autoPresence && !stream.speaking) {
          stream.speaking = true;
          director.presence('speaking');
        }
      }
    } else if (stream.speaking && t - stream.lastSpokeAt > SILENCE_MS) {
      // Ran dry: close the mouth and fall back to silence. Automatic presence
      // only ever moves between speaking and silent — it must not overwrite an
      // intentional `thinking` or `confused`.
      stream.speaking = false;
      rig.setChannels({ mouthOpen: 0.08, mouthWidth: 1 });
      if (stream.autoPresence) director.presence('silent');
    }
    streamRaf = requestAnimationFrame(streamTick);
  }

  function openStream({ id = null, emotion, intensity, rateMs = 105, autoPresence = true } = {}) {
    closeStream();
    cancel(); // a timeline and a stream would fight over the mouth
    stream = {
      id,
      queue: [],
      rateMs: Math.max(40, rateMs),
      nextAt: now(),
      lastSpokeAt: now(),
      speaking: false,
      autoPresence,
      spoken: 0,
      pushed: 0,
      dropped: 0,
    };
    if (emotion) director.emotion(emotion, { intensity });
    streamRaf = requestAnimationFrame(streamTick);
    return { ok: true, id };
  }

  /** Append text to an open stream. Starts one implicitly if none is open. */
  function pushText({ text, emotion, intensity, emphasis } = {}) {
    if (!stream) openStream({});
    const clean = String(text ?? '').trim();
    if (emotion) director.emotion(emotion, { intensity });
    if (emphasis) director.cue(emphasis, { intensity });
    if (!clean) return { ok: true, queued: stream.queue.length };

    // Reuse the same tokenizer as `speak`; the durations are re-derived from the
    // stream's rate rather than from a total length.
    const units = mouthTrack(clean, 1000);
    for (const u of units) stream.queue.push({ channels: u.channels });
    stream.pushed += units.length;

    // Trim from the FRONT: the newest syllables are the ones that match the
    // audio playing now, so the stale ones at the head are what to discard.
    const maxUnits = Math.max(4, Math.round(MAX_LAG_MS / stream.rateMs));
    if (stream.queue.length > maxUnits) {
      stream.dropped += stream.queue.length - maxUnits;
      stream.queue.splice(0, stream.queue.length - maxUnits);
    }

    // Punctuation still implies emphasis, positioned at the end of this chunk.
    for (const c of cuesFromText(clean, 0)) {
      stream.queue.push({ channels: {}, cue: c.cue });
    }
    return {
      ok: true,
      queued: stream.queue.length,
      dropped: stream.dropped,
      // As with speak(): a connected analyser overrides the queued mouth track.
      audioOverride: rig.hasAnalyser?.() === true,
    };
  }

  function closeStream() {
    if (streamRaf !== null) {
      cancelAnimationFrame(streamRaf);
      streamRaf = null;
    }
    if (stream) {
      rig.setChannels({ mouthOpen: 0.1, mouthWidth: 1 });
      if (stream.autoPresence && stream.speaking) director.presence('silent');
    }
    const closed = stream;
    stream = null;
    return { ok: true, spoken: closed?.spoken ?? 0 };
  }

  return {
    perform,
    speak,
    cancel,
    openStream,
    pushText,
    closeStream,
    isStreaming: () => stream !== null,
    isRunning: () => active !== null,
    getState: () => {
      if (active) {
        return {
          kind: 'perform',
          id: active.id,
          keys: active.keys.length,
          index: active.i,
          // What a concurrent frame() will lose, computed from the keyframes
          // that have NOT fired yet — a timeline owns only what it still has
          // left to write, so this narrows as it runs.
          owns: pendingOwnership(active),
        };
      }
      if (stream) {
        return {
          kind: 'stream',
          audioOverride: rig.hasAnalyser?.() === true,
          owns: SPOKEN_CHANNELS,
          id: stream.id,
          queued: stream.queue.length,
          spoken: stream.spoken,
          dropped: stream.dropped,
          lagMs: Math.round(stream.queue.length * stream.rateMs),
        };
      }
      return null;
    },
  };
}
