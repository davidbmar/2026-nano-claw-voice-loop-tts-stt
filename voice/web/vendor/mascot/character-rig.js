import { outsetQuad } from './rig-config.js';
import { solveHomography, cssMatrix3d } from './homography.js';
import { drawFace, levelOfDetail } from './face-render.js';
import { CHANNELS, neutralChannels, clampChannels } from './face-channels.js';
import { createSpringSet } from './springs.js';
import { createAutonomic, mulberry32 } from './autonomic.js';

/**
 * How far each body channel actually moves the character.
 *
 * These were originally an order of magnitude smaller — a full head tilt gave
 * 2.2 degrees and a full squash gave a 3.5% scale, both far below the threshold
 * of visibility. The channels were wired, driven and tested, and the body
 * language was completely inert: the test asserted the transform CHANGED, never
 * that it changed enough to see.
 *
 * Cartoon squash and stretch wants 8-12%; a readable tilt wants 10-15 degrees.
 * Breathing is deliberately far smaller than a gesture.
 */
export const BODY = {
  tiltDeg: 10,
  leanDeg: 6.5,
  squashPct: 0.095,
  bobPx: 13,
};

/**
 * How much body motion survives under prefers-reduced-motion.
 *
 * The preference is about MOTION, and its primary concern is large-element and
 * continuously looping movement — which is exactly what this transform drives. An
 * earlier version suppressed only the screen effects and left the whole character
 * bobbing, squashing and leaning, which is backwards: it damped the small thing
 * and left the loud thing running.
 *
 * Expressive tilt and lean are damped rather than removed, because they carry
 * information — a confused head-tilt is part of the message. The continuous idle
 * bob is removed outright, since a permanent loop has no informational content.
 */
export const REDUCED = { tilt: 0.35, lean: 0.35, squash: 0.3, bob: 0 };

/** Pure: body channels -> a CSS transform. Extracted so magnitude is testable. */
export function bodyTransform(ch, reduced = false) {
  const k = reduced ? REDUCED : { tilt: 1, lean: 1, squash: 1, bob: 1 };
  const rot = (ch.headTilt ?? 0) * BODY.tiltDeg * k.tilt
            + (ch.bodyLean ?? 0) * BODY.leanDeg * k.lean;
  const bob = (ch.bodyBob ?? 0) * BODY.bobPx * k.bob;
  // Squash preserves volume: narrowing vertically widens horizontally.
  const sq = 1 - (ch.squash ?? 0) * BODY.squashPct * k.squash;
  return `translateY(${bob.toFixed(2)}px) rotate(${rot.toFixed(3)}deg) scale(${(1 / sq).toFixed(4)}, ${sq.toFixed(4)})`;
}

const ORDER = ['tl', 'tr', 'br', 'bl'];

/**
 * The styles this rig's GEOMETRY depends on, applied to the elements a host
 * hands in. Not presentation — those are inputs to the maths below.
 *
 * The homography maps canvas space to the body's displayed space assuming the
 * canvas is absolutely positioned at the body's top-left corner and transforms
 * about `0 0`. Body motion pivots about `stageEl`, and it has to pivot about the
 * character's FEET or the whole thing rotates around a point in mid-air.
 *
 * These used to live only in the harness's own `src/styles.css`, which is not a
 * JS module and so was never part of what a host vendors. nano-claw copied the
 * fourteen modules, built its own DOM, and got none of them. Measured in their
 * running console: the canvas was `position: static`, so instead of overlaying
 * the character it stacked 908 px BELOW it and made the container 1734 px tall
 * against an 819 px character; and `transform-origin` was the browser default
 * 50% 50%, putting the pivot 48 px below the character's feet. The face still
 * landed correctly, which is exactly why nobody would have caught it — body
 * language was rotating about empty space and the layout was twice its true
 * height, both silently.
 *
 * A component that documents its CSS requirements is relying on someone reading
 * a file that is not in the payload. Asserting them costs four lines.
 * Presentation — how large the character is, whether it is centred, what happens
 * when the viewport is short — stays entirely the host's business.
 */
export function applyMountStyles({ bodyEl, canvasEl, stageEl, calibrationEl = null }) {
  // Inline so a host stylesheet cannot quietly override a geometric requirement.
  if (stageEl) {
    stageEl.style.position = 'relative';   // anchors the absolutely-placed canvas
    stageEl.style.transformOrigin = '50% 92%';
  }
  // `inline` puts the image on a text baseline, adding descender space beneath it
  // and shifting every measurement taken from its box.
  if (bodyEl) bodyEl.style.display = 'block';
  for (const el of [canvasEl, calibrationEl]) {
    if (!el) continue;
    el.style.position = 'absolute';
    el.style.top = '0';
    el.style.left = '0';
    el.style.transformOrigin = '0 0';
  }
}

export function createRig({ config, bodyEl, canvasEl, stageEl, calibrationEl = null }) {
  const { screen, body } = config;
  let quad = screen.quad;
  let raf = null;

  applyMountStyles({ bodyEl, canvasEl, stageEl, calibrationEl });

  // Canvas resolution: the quad's average edge lengths, supersampled so the
  // tapered ink stays crisp after the transform downsamples it.
  const q0 = outsetQuad(quad, screen.outsetPx);
  const edge = (a, b) => Math.hypot(q0[b][0] - q0[a][0], q0[b][1] - q0[a][1]);
  const canvasW = Math.round(((edge('tl', 'tr') + edge('bl', 'br')) / 2) * screen.canvasScale);
  const canvasH = Math.round(((edge('tl', 'bl') + edge('tr', 'br')) / 2) * screen.canvasScale);
  canvasEl.width = canvasW;
  canvasEl.height = canvasH;
  canvasEl.style.width = `${canvasW}px`;
  canvasEl.style.height = `${canvasH}px`;
  const ctx = canvasEl.getContext('2d');

  // Radius is authored in body-image pixels; convert to canvas pixels.
  const radiusCanvas = screen.cornerRadiusPx * screen.canvasScale;

  const springs = createSpringSet(neutralChannels());
  const autonomic = createAutonomic({ rng: mulberry32(config.seed ?? 1) });
  let autonomicOn = true;
  let glyph = null;
  const cues = [];
  let last = null;
  let elapsed = 0;
  let showCalibration = false;
  // The composed output of the last frame: baseline + cues + autonomic. Kept
  // separate from the spring baseline because a saved profile wants the held
  // expression, not whatever momentary blink happened to be in flight.
  let rendered = neutralChannels();
  // Recomputed on layout: how many CSS pixels the screen actually occupies.
  // Detail is dropped below the measured legibility floors rather than rendered
  // and thrown away by the browser's downscale.
  let lod = levelOfDetail(Infinity);

  // Live audio, when a host hands over a Web Audio AnalyserNode. nano-claw's
  // Pcm16AudioPlayer exposes one off its TTS output, so this replaces the
  // spelling-derived mouth track with actual amplitude and spectral content.
  let analyser = null;
  let timeData = null;
  let freqData = null;
  let audioLevel = 0;
  let audioWidth = 1;
  // Running peak, for adaptive normalisation. A fixed gain cannot work when the
  // host's output loudness is unknown: too high and the mouth pins open through
  // loud speech, too low and it barely moves on a quiet source.
  let audioPeak = 0.06;

  // Tracked live rather than sampled once: someone who enables the setting while
  // the page is open should be honoured, not ignored until reload.
  const motionQuery = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null;
  let reducedMotion = motionQuery?.matches ?? false;
  let reducedForced = null; // an explicit override wins over the media query
  const effectiveReduced = () => reducedForced ?? reducedMotion;
  // Named rather than inline so `stop()` can remove it. An anonymous handler here
  // leaked: it closes over the rig, so every destroyed instance stayed reachable
  // from the media query list — including its 982x908 canvas context — and a host
  // that mounts and unmounts the avatar accumulated one per cycle.
  const onMotionPreference = (e) => { reducedMotion = e.matches; };
  motionQuery?.addEventListener?.('change', onMotionPreference);

  function layout() {
    // The body <img> is displayed scaled; map canvas space -> displayed space.
    const scale = bodyEl.clientWidth / body.width;
    if (!scale) return;
    const out = outsetQuad(quad, screen.outsetPx);
    const src = [
      [0, 0],
      [canvasW, 0],
      [canvasW, canvasH],
      [0, canvasH],
    ];
    const dst = ORDER.map((k) => [out[k][0] * scale, out[k][1] * scale]);
    lod = levelOfDetail(Math.hypot(dst[1][0] - dst[0][0], dst[1][1] - dst[0][1]));
    try {
      canvasEl.style.transform = cssMatrix3d(solveHomography(src, dst));
    } catch (err) {
      console.warn('[mascot] could not solve screen transform', err);
    }
    if (showCalibration) drawCalibration(scale, out);
  }

  function drawCalibration(scale, out) {
    if (!calibrationEl) return;
    calibrationEl.setAttribute('width', String(bodyEl.clientWidth));
    calibrationEl.setAttribute('height', String(bodyEl.clientHeight));
    const pts = ORDER.map((k) => `${out[k][0] * scale},${out[k][1] * scale}`).join(' ');
    calibrationEl.innerHTML =
      `<polygon points="${pts}" fill="none" stroke="#ff2d55" stroke-width="2"/>` +
      ORDER.map((k) => {
        const x = out[k][0] * scale;
        const y = out[k][1] * scale;
        return (
          `<circle cx="${x}" cy="${y}" r="5" fill="#0a84ff"/>` +
          `<text x="${x + 8}" y="${y - 8}" fill="#ff2d55" font-size="12">${k}</text>`
        );
      }).join('');
  }

  // Every active cue is iterated each frame, so the list needs a ceiling: an
  // agent emitting cues in a loop would otherwise grow it without bound. Cues
  // are additive impulses, so the newest carry the current intent — drop the
  // oldest.
  const MAX_CUES = 24;

  function addCue(c) {
    cues.push({ channels: c.channels, durationMs: c.durationMs, t: 0 });
    if (cues.length > MAX_CUES) cues.splice(0, cues.length - MAX_CUES);
  }

  /** Sum every active cue's decaying offset. */
  function cueOffsets(dt) {
    const out = {};
    for (let i = cues.length - 1; i >= 0; i--) {
      const cue = cues[i];
      cue.t += dt * 1000;
      const k = cue.t / cue.durationMs;
      if (k >= 1) {
        cues.splice(i, 1);
        continue;
      }
      // Rise fast, then decay smoothly back to the emotion baseline.
      const env = k < 0.2 ? k / 0.2 : 1 - (k - 0.2) / 0.8;
      for (const [name, v] of Object.entries(cue.channels)) {
        out[name] = (out[name] ?? 0) + v * env;
      }
    }
    return out;
  }

  const clamp1 = (name, v) =>
    Math.min(CHANNELS[name].max, Math.max(CHANNELS[name].min, v));

  function applyBodyTransform(ch) {
    stageEl.style.transform = bodyTransform(ch, effectiveReduced());
  }

  /**
   * Sample the analyser once per frame.
   *
   * Openness comes from RMS on the TIME-domain data, which is true loudness.
   * Width comes from the balance of low against high frequency energy: an "ee"
   * is bright and wide, an "oo" is dark and rounded, so the spectral tilt gives
   * a crude but real formant cue that spelling cannot.
   */
  function sampleAudio() {
    if (!analyser) return false;
    analyser.getByteTimeDomainData(timeData);
    let sum = 0;
    for (let i = 0; i < timeData.length; i++) {
      const v = (timeData[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / timeData.length);

    // Track the peak and normalise against it, with a slow decay so the range
    // recovers after a loud passage. The floor stops room noise being amplified
    // into a chattering mouth during silence.
    const PEAK_FLOOR = 0.045;
    audioPeak = Math.max(rms, audioPeak * 0.9985, PEAK_FLOOR);
    const level = Math.min(1, (rms / audioPeak) * 0.92);
    // Attack fast, release slow — a mouth closes more slowly than it opens.
    audioLevel += (level - audioLevel) * (level > audioLevel ? 0.55 : 0.18);

    analyser.getByteFrequencyData(freqData);
    const n = freqData.length;
    let low = 0;
    let high = 0;
    // Split near 1 kHz, not 4 kHz. Speech energy lives at 100-3000 Hz, so a
    // split at 18% of bins (~4.3 kHz at fftSize 512) puts nearly everything in
    // the low bucket and pins the tilt permanently negative.
    const split = Math.max(1, Math.floor(n * 0.05));
    for (let i = 0; i < split; i++) low += freqData[i];
    for (let i = split; i < n; i++) high += freqData[i];
    low /= split;
    high /= Math.max(1, n - split);
    const tilt = (high - low) / 255; // bright positive, dark negative
    const targetWidth = 1 + Math.max(-0.35, Math.min(0.35, tilt * 1.6));
    audioWidth += (targetWidth - audioWidth) * 0.2;
    return true;
  }

  function frame(now) {
    const t = now / 1000;
    const dt = last === null ? 1 / 60 : Math.max(0, t - last);
    last = t;
    elapsed += Math.min(dt, 0.05);

    // Live audio overrides the mouth when connected, because measured amplitude
    // beats any text-derived approximation.
    if (sampleAudio() && audioLevel > 0.012) {
      springs.setTarget({
        mouthOpen: 0.08 + audioLevel * 0.78,
        mouthWidth: audioWidth,
      });
    }

    // Three additive layers: spring baseline + decaying cues + autonomic noise.
    const base = springs.step(dt);
    const offs = cueOffsets(dt);
    const shown = { ...base };
    for (const [name, v] of Object.entries(offs)) {
      if (name in shown) shown[name] = clamp1(name, shown[name] + v);
    }
    if (autonomicOn) {
      const au = autonomic.step(dt);
      // Blink MULTIPLIES aperture so it composes with a held squint.
      shown.eyeLOpen = clamp1('eyeLOpen', shown.eyeLOpen * au.eyeLOpen);
      shown.eyeROpen = clamp1('eyeROpen', shown.eyeROpen * au.eyeROpen);
      shown.pupilX = clamp1('pupilX', shown.pupilX + au.pupilX);
      shown.pupilY = clamp1('pupilY', shown.pupilY + au.pupilY);
      shown.bodyBob = clamp1('bodyBob', shown.bodyBob + au.bodyBob * 0.5);
    }

    rendered = shown;
    drawFace(
      ctx,
      { w: canvasW, h: canvasH, radius: radiusCanvas, cream: screen.cream },
      shown,
      { time: elapsed, reducedMotion: effectiveReduced(), glyph, lod },
    );
    applyBodyTransform(shown);
    raf = requestAnimationFrame(frame);
  }

  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => layout()) : null;

  return {
    start() {
      layout();
      ro?.observe(bodyEl);
      // Re-arm after a stop(). addEventListener with the same reference is a
      // no-op if it is already registered, so this stays safe on a fresh rig.
      motionQuery?.addEventListener?.('change', onMotionPreference);
      if (raf === null) raf = requestAnimationFrame(frame);
    },
    /**
     * Release everything this rig registered. Safe to call twice.
     *
     * `start()` re-arms the loop afterwards, so this is a pause as well as a
     * teardown — but the media-query listener has to come off either way, since
     * it is the one registration that outlives the render loop.
     */
    stop() {
      ro?.disconnect();
      motionQuery?.removeEventListener?.('change', onMotionPreference);
      if (raf !== null) cancelAnimationFrame(raf);
      raf = null;
    },
    setChannels(partial) {
      springs.setTarget(clampChannels(partial));
    },
    /** Swap transition dynamics — a DYNAMICS name or explicit stiffness/damping. */
    setDynamics(spec) {
      return springs.setDynamics(spec);
    },
    getDynamics() {
      return springs.getDynamics();
    },
    /** The detail level in force, derived from the screen's on-screen size. */
    getLod() {
      return { ...lod };
    },
    snapChannels(partial) {
      springs.snap(clampChannels(partial));
    },
    /**
     * Return to a known baseline. Needed because EMOTIONS.neutral is {}, so
     * emotion('neutral') is a no-op and cannot clear a held expression.
     */
    resetChannels() {
      springs.snap(neutralChannels());
      cues.length = 0;
      glyph = null;
    },
    /**
     * The held expression — where the springs are HEADING. Use this for profiles.
     *
     * This used to return `springs.values`, the springs' current integrated
     * position, which is a different thing and only looks the same at rest. The
     * stated rationale for the method ("a saved profile should capture the held
     * expression, not whatever momentary blink was in flight") was half-served:
     * blinks are autonomic and applied on top of the springs, so those were
     * correctly excluded, and that is why this went unnoticed. But a profile
     * exported 100 ms into a 617 ms `somber` transition captured roughly 40% of
     * somber — a pose nobody chose and no emotion in the table describes.
     *
     * It also made the obvious probe lie: `emotion('somber')` followed by
     * `getChannels()` returned the PREVIOUS expression, because the springs had
     * not stepped yet.
     *
     * For what is actually on screen, including cues and autonomic motion, use
     * `getRenderedChannels()`.
     */
    getChannels() {
      return { ...springs.targets };
    },
    /** What was actually drawn last frame, including cues and autonomic noise. */
    getRenderedChannels() {
      return { ...rendered };
    },
    addCue,
    blink() {
      autonomic.forceBlink();
    },
    setAutonomic(on) {
      autonomicOn = !!on;
    },
    /**
     * Force reduced motion on or off, or pass null to follow the OS preference.
     * A host with its own accessibility switch needs this.
     */
    setReducedMotion(on) {
      reducedForced = on === null || on === undefined ? null : !!on;
      return effectiveReduced();
    },
    isReducedMotion() {
      return effectiveReduced();
    },
    setGlyph(g) {
      glyph = g || null;
    },
    setCalibration(on) {
      showCalibration = !!on;
      if (calibrationEl) calibrationEl.hidden = !showCalibration;
      layout();
    },
    recalibrate(nextQuad) {
      quad = nextQuad;
      layout();
    },
    /**
     * Sample a live Web Audio AnalyserNode every frame.
     *
     * This is the renderer contract nano-claw calls — its Pcm16AudioPlayer
     * exposes `.analyser` off the TTS output. Real amplitude beats the
     * spelling-derived mouth track, so while an analyser is connected it wins.
     */
    connectAnalyser(node) {
      if (!node || typeof node.getByteTimeDomainData !== 'function') {
        throw new TypeError('connectAnalyser expects a Web Audio AnalyserNode.');
      }
      analyser = node;
      timeData = new Uint8Array(node.fftSize);
      freqData = new Uint8Array(node.frequencyBinCount);
      return true;
    },
    disconnectAnalyser() {
      analyser = null;
      timeData = null;
      freqData = null;
      audioLevel = 0;
      audioWidth = 1;
      audioPeak = 0.06;
      springs.setTarget({ mouthOpen: 0.1, mouthWidth: 1 });
      return true;
    },
    hasAnalyser() {
      return analyser !== null;
    },
    getAudio() {
      return { level: audioLevel, width: audioWidth, connected: analyser !== null };
    },

    /** Drive the mouth from audio amplitude, 0..1. */
    pushAudioFrame({ level = 0, speaking = true } = {}) {
      const l = Math.min(1, Math.max(0, Number(level) || 0));
      springs.setTarget({ mouthOpen: speaking ? 0.1 + l * 0.75 : 0.05 });
    },
    setAudioLevel(level) {
      this.pushAudioFrame({ level, speaking: true });
    },
    get canvasSize() {
      return { w: canvasW, h: canvasH };
    },
  };
}
