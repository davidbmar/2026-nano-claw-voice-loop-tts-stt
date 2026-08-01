const c = (min, max, neutral) => ({ min, max, neutral });

export const CHANNELS = {
  // Brows. Y: negative lowers, positive raises.
  // Angle: positive raises the INNER end (worried); negative lowers it (angry).
  // Left and right are independent because asymmetry is the entire vocabulary
  // of confusion — a symmetric face cannot read as confused.
  browLY: c(-1, 1, 0),
  browRY: c(-1, 1, 0),
  browLAngle: c(-1, 1, 0),
  browRAngle: c(-1, 1, 0),

  // Eyes. Open is lid aperture: 1 fully open, 0 closed.
  eyeLOpen: c(0, 1, 1),
  eyeROpen: c(0, 1, 1),
  eyeLSquint: c(0, 1, 0),
  eyeRSquint: c(0, 1, 0),
  eyeScaleY: c(0.6, 1.3, 1),
  // Lid shape, per eye. These carry more expression than aperture does.
  //
  // Tilt: positive drops the INNER corner (angry, determined); negative raises
  // it (sad, worried). This single asymmetry does more work than any other eye
  // parameter, and it is unreachable by scaling an oval.
  lidLTilt: c(-1, 1, 0),
  lidRTilt: c(-1, 1, 0),
  // Arch: positive is a high, rounded upper lid (innocent, surprised); negative
  // is flat and straight (focused, hard).
  lidLArch: c(-1, 1, 0),
  lidRArch: c(-1, 1, 0),
  // A drawn line under the eye. The reference carries one beneath each eye and
  // it supplies much of the expression in worried and thinking.
  lowerLid: c(0, 1, 0),
  pupilX: c(-1, 1, 0),
  pupilY: c(-1, 1, 0),
  highlightAngle: c(-Math.PI, Math.PI, -0.7),
  // Iris size carries a surprising amount: a small iris in a wide eye reads as
  // shock, a large one as warmth. Independent of eye aperture.
  irisScale: c(0.5, 1.35, 1),

  // Mouth.
  mouthOpen: c(0, 1, 0.45),
  mouthWidth: c(0.6, 1.4, 1),
  mouthCorner: c(-1, 1, 0.8),
  mouthCurve: c(-1, 1, 0.3),
  mouthShift: c(-1, 1, 0),
  // Morphs the mouth from a lens toward a circle. Without it every emotion is a
  // variation on one shape, which is the fastest way to make a face look canned.
  mouthRound: c(0, 1, 0),
  // A wavy, uncertain lip line — for worry and unease.
  mouthWaver: c(0, 1, 0),
  tongue: c(0, 1, 0.5),
  // Gritted teeth — the difference between "working" and "straining".
  teeth: c(0, 1, 0),

  blush: c(0, 1, 0.5),

  // Effort and labour. These are what let the character read as *working*
  // rather than merely emoting, which no amount of brow and mouth can convey.
  sweat: c(0, 1, 0),
  effort: c(0, 1, 0),
  progress: c(0, 1, 0),

  // Whole-body, applied to the stage element.
  headTilt: c(-1, 1, 0),
  bodyLean: c(-1, 1, 0),
  bodyBob: c(-1, 1, 0),
  squash: c(-1, 1, 0),

  // Screen effects.
  scanlines: c(0, 1, 0.12),
  binaryRain: c(0, 2, 0.5),
  flicker: c(0, 1, 0.05),
  glitch: c(0, 1, 0),
  glyphOpacity: c(0, 1, 0),
};

export function neutralChannels() {
  const out = {};
  for (const [k, spec] of Object.entries(CHANNELS)) out[k] = spec.neutral;
  return out;
}

/** Clamp to each channel's range; silently drop unknown keys and non-finite values. */
export function clampChannels(partial) {
  const out = {};
  if (!partial) return out;
  for (const [k, v] of Object.entries(partial)) {
    const spec = CHANNELS[k];
    if (!spec) continue;
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    out[k] = Math.min(spec.max, Math.max(spec.min, v));
  }
  return out;
}

export function mergeChannels(base, ...overlays) {
  const out = { ...base };
  for (const o of overlays) Object.assign(out, clampChannels(o));
  return out;
}
