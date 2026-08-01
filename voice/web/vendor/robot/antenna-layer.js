// Antennas that raise and lower.
//
// This is the first part that MOVES rather than being repainted, and it needs
// something the lenses did not: the pixels have to leave the photograph.
//
// The spec assumed a build step — a script that cuts layer PNGs and writes a
// patched base. That turns out to be unnecessary here, and the reason is the
// artwork. All three robots stand against a near-black backdrop (measured
// luminance 10 on the orange robot), so an antenna standing in that backdrop can
// be separated by luminance alone, at load, in a canvas. No asset pipeline, no
// second copy of the image to keep in sync, and re-cutting is free when a bbox
// is adjusted.
//
// Two canvases come out of one region:
//
//   PATCH   the antenna's silhouette filled with backdrop colour, painted over
//           the original so the photographed antenna is gone
//   LAYER   the antenna's pixels with everything else transparent, rotated
//           about a pivot at its base
//
// The patch is what makes rotation possible at all. Without it the original
// stays put and the robot grows a second antenna every time the first one moves.

/** Feather width, in source pixels, for the extracted silhouette's edge. */
const FEATHER = 1.5;

/** Untouched pixels of a region, for asking what a location looked like originally. */
function ctxOf(image, x, y, w, h) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true });
  g.drawImage(image, x, y, w, h, 0, 0, w, h);
  return g.getImageData(0, 0, w, h).data;
}

/**
 * Separate an antenna from its backdrop.
 *
 * @param image the loaded base portrait
 * @param part  manifest entry: bbox [x,y,w,h], pivot, sign, threshold
 */
export function extractAntenna(image, part) {
  const [bx, by, bw, bh] = part.bbox;
  const scale = part.canvasScale ?? 2;

  const src = document.createElement('canvas');
  src.width = bw;
  src.height = bh;
  const sctx = src.getContext('2d', { willReadFrequently: true });
  sctx.drawImage(image, bx, by, bw, bh, 0, 0, bw, bh);

  const img = sctx.getImageData(0, 0, bw, bh);
  const d = img.data;
  const thr = part.threshold ?? 34;

  // Alpha from luminance, ramped rather than stepped. A hard threshold leaves a
  // hard-edged cutout that crawls visibly against the backdrop as it rotates —
  // the giveaway that a piece of the photo has been lifted out of it.
  for (let i = 0; i < d.length; i += 4) {
    const lum = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    const a = (lum - thr) / (thr * FEATHER);
    d[i + 3] = Math.round(255 * Math.max(0, Math.min(1, a)));
  }
  sctx.putImageData(img, 0, 0);

  // The layer, supersampled to match the lens surfaces.
  const layer = document.createElement('canvas');
  layer.width = Math.round(bw * scale);
  layer.height = Math.round(bh * scale);
  const lctx = layer.getContext('2d');
  lctx.imageSmoothingQuality = 'high';
  lctx.drawImage(src, 0, 0, layer.width, layer.height);

  // The patch: the silhouette in backdrop colour, but ONLY where the antenna
  // stands in the backdrop.
  //
  // Filling the whole silhouette is the obvious version and it puts a black
  // antenna-shaped smear across the ear pod, because the lower third of the rod
  // is photographed against the head, not against black. There is no backdrop
  // there to restore — that is the "clean plate" problem, unsolved and
  // unnecessary to solve.
  //
  // Unnecessary because the pivot is at the base: the part of the antenna that
  // overlaps the head barely moves, so leaving it un-erased means the rotating
  // layer covers its own original. Only the free-standing part, out in the
  // black, needs erasing — and there the backdrop colour IS the correct answer.
  //
  // "In the backdrop" is decided by looking around each pixel rather than by a
  // hand-drawn boundary: sample a ring outside the silhouette and ask whether
  // it is dark. Over the head it is bright and the pixel is left alone.
  const src2 = sctx.getImageData(0, 0, bw, bh);
  const a2 = src2.data;
  const base = ctxOf(image, bx, by, bw, bh);
  // 12, not 7. The rod thickens toward its base, and at R=7 an interior pixel
  // there saw no non-antenna context at all — which the rule below counted as
  // backdrop, so the patch bit a black notch out of the ear pod. A radius wider
  // than the rod guarantees every pixel gets a real answer.
  const R = 12;
  for (let y = 0; y < bh; y++) {
    for (let x = 0; x < bw; x++) {
      const i = (y * bw + x) * 4;
      if (a2[i + 3] === 0) continue;
      let bright = 0;
      let seen = 0;
      for (const [dx, dy] of [[-R, 0], [R, 0], [0, -R], [0, R], [-R, -R], [R, -R]]) {
        const sx = x + dx;
        const sy = y + dy;
        if (sx < 0 || sy < 0 || sx >= bw || sy >= bh) continue;
        const j = (sy * bw + sx) * 4;
        if (a2[j + 3] > 40) continue;                 // still antenna, not context
        seen++;
        const l = 0.299 * base[j] + 0.587 * base[j + 1] + 0.114 * base[j + 2];
        if (l > thr * 2.2) bright++;
      }
      // No usable context means DON'T patch. Leaving a pixel un-erased is
      // invisible — the rotating layer covers it — while erasing one that
      // should not be erased punches a hole in the head.
      if (seen === 0 || bright / seen > 0.34) a2[i + 3] = 0;
    }
  }
  const maskCanvas = document.createElement('canvas');
  maskCanvas.width = bw;
  maskCanvas.height = bh;
  maskCanvas.getContext('2d').putImageData(src2, 0, 0);

  const patch = document.createElement('canvas');
  patch.width = layer.width;
  patch.height = layer.height;
  const pctx = patch.getContext('2d');

  // Fill from the backdrop BESIDE the antenna, not from a flat colour.
  //
  // The manifest's `backdrop` is one hex value, and the photograph's backdrop is
  // a vignette — brighter near the head, falling off outward. Painting the flat
  // value left a visible ghost in exactly the antenna's shape: not black on
  // bright, but slightly-too-dark on almost-right, which is arguably worse
  // because it reads as a smudge rather than as a mistake.
  //
  // Sampling ~48px further out lands in clean backdrop at nearly the same
  // vignette level, so the fill carries the gradient for free. A clone stamp,
  // and the reason it works here is the same reason the whole approach works:
  // the neighbouring region is featureless.
  const shift = (part.sign ?? 1) * 48;
  pctx.drawImage(image, bx + shift, by, bw, bh, 0, 0, patch.width, patch.height);
  pctx.globalCompositeOperation = 'destination-in';
  pctx.drawImage(maskCanvas, 0, 0, patch.width, patch.height);
  // Dilate slightly. An antialiased edge leaves a faint bright fringe of the
  // original behind, and a one-pixel halo of the old antenna is more noticeable
  // than the antenna itself once it starts moving.
  pctx.globalCompositeOperation = 'source-over';
  for (const [dx, dy] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {
    pctx.drawImage(patch, dx, dy);
  }

  return { layer, patch, rect: { x: bx, y: by, width: bw, height: bh }, scale };
}

/**
 * How far this antenna is rotated, in degrees, for the current channels.
 *
 * A rod on one pivot has ONE degree of freedom, so elevation and cant both
 * resolve to the same rotation and simply sum. Keeping both wired matters
 * because emotions drive them independently — `confused` sets browLY 0.7 against
 * browRY -0.45, and `skeptical` sets browLAngle -0.15 against browRAngle -0.35.
 * Reading only one channel would throw away half the asymmetry that makes those
 * expressions read.
 *
 * `sign` points "up and outward" for the side this antenna is on: raising the
 * left antenna is a counter-clockwise rotation and the right one clockwise, and
 * without the flip a raised brow lifts one antenna while dropping the other.
 */
export function antennaAngle(part, ch = {}, reduced = false) {
  const elevation = clampSigned(ch.elevation ?? 0);
  const cant = clampSigned(ch.cant ?? 0);
  const max = part.maxDeg ?? 9;
  const deg = (elevation * 0.68 + cant * 0.42) * max;
  // Expressive rotation carries information, so it is damped rather than
  // removed under reduced motion — the same call `character-rig.js` makes for
  // head tilt, and for the same reason.
  const out = (part.sign ?? 1) * deg * (reduced ? 0.35 : 1);
  // Normalise -0. A left antenna at rest computes `-1 * 0`, which is negative
  // zero: harmless in the transform string, but `Object.is(-0, 0)` is false, so
  // it fails any strict equality check against rest — including the one that
  // guards the antennas against drifting when nothing is driving them.
  return out === 0 ? 0 : out;
}

const clampSigned = (v) => (Number.isFinite(v) ? Math.max(-1, Math.min(1, v)) : 0);

/**
 * A dish sweeping, as an angle in degrees.
 *
 * A saucer on a vertical stalk is rotationally symmetric about that stalk, so
 * "spinning" it is invisible — there is nothing on it to track. What does read
 * is TIPPING: rotating the ellipse in plane lifts one edge and drops the other,
 * which is what a scanning dish looks like from the side.
 *
 * The sweep is a triangle wave, not a sine. A sine spends most of its time near
 * the ends and eases through the middle, which reads as a pendulum — something
 * swinging under gravity. A scanning mechanism does the opposite: constant rate
 * across the arc, then a turnaround. That is the same distinction `servo.js`
 * exists to make for the head, arrived at from the other direction.
 *
 * Rate scales with `intensity`, so a busy agent scans faster. Held below the
 * flash bound is not a concern here — this is motion, not luminance — but it is
 * kept slow because a fast sweep reads as agitation rather than attention.
 */
export function dishAngle(part, time, { rate = 0.5, reduced = false } = {}) {
  if (reduced) return 0;
  const hz = 0.06 + 0.16 * clamp01Local(rate);
  const phase = (time * hz) % 1;
  // Triangle: 0 -> 1 -> 0 across the cycle, remapped to -1..1.
  const tri = phase < 0.5 ? phase * 4 - 1 : 3 - phase * 4;
  const deg = tri * (part.maxDeg ?? 8);
  return deg === 0 ? 0 : deg;
}

const clamp01Local = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);

export function createAntennaSurface({ image, part, manifest, container, mountPatch = true }) {
  const { layer, patch, rect, scale } = extractAntenna(image, part);

  const mk = (canvas, z) => {
    canvas.className = 'part part-antenna';
    canvas.dataset.partId = z === part.z ? part.id : `${part.id}-patch`;
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.position = 'absolute';
    canvas.style.left = '0';
    canvas.style.top = '0';
    canvas.style.pointerEvents = 'none';
    canvas.style.zIndex = String(z);
    container.append(canvas);
    return canvas;
  };
  // The patch is mounted only when there is no head layer. With a head, the
  // patch is drawn INTO it instead — the head is cut from the original image
  // and would otherwise carry a baked antenna that the rotating layer then
  // duplicates.
  if (!mountPatch) patch.style.display = 'none';
  mk(patch, part.z - 1);
  mk(layer, part.z);

  let displayScale = 1;
  let angle = 0;
  // Rotation inherited from the head, composed IN FRONT of this layer's own
  // transform. An antenna is bolted to the head: it tilts with it, and then
  // rotates about its own pivot on top of that.
  let inherited = '';

  function place() {
    const s = displayScale / scale;
    const tx = rect.x * displayScale;
    const ty = rect.y * displayScale;
    patch.style.transformOrigin = '0 0';
    patch.style.transform = `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${s.toFixed(5)})`;

    // Rotate about the pivot by composing it into the transform CHAIN, with
    // transform-origin left at 0 0.
    //
    // Using `transform-origin` for this is the obvious approach and it is wrong
    // whenever the same transform also scales. CSS applies the origin as
    // `translate(O) · M · translate(-O)`, so with `M = translate(T) · scale(s)`
    // a point lands at `O + T + s(p - O)` — an unwanted `O(1 - s)` offset. At
    // s ≈ 0.33 and a pivot 352px down the canvas, that threw each antenna about
    // 236px toward the shoulders. They came off the head entirely.
    //
    // Chaining translate(P) rotate translate(-P) inside the scale has no such
    // term: a point lands at `T + s(R(p - P) + P)`, which is the rotation about
    // P that was wanted.
    const px = (part.pivot[0] - rect.x) * scale;
    const py = (part.pivot[1] - rect.y) * scale;
    layer.style.transformOrigin = '0 0';
    layer.style.transform =
      inherited +
      `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${s.toFixed(5)}) ` +
      `translate(${px.toFixed(2)}px, ${py.toFixed(2)}px) ` +
      `rotate(${angle.toFixed(3)}deg) ` +
      `translate(${(-px).toFixed(2)}px, ${(-py).toFixed(2)}px)`;
  }

  return {
    id: part.id,
    part,
    /**
     * The eraser, for a head layer to bake in rather than mount separately.
     *
     * Carries the region's SOURCE-space size, not the canvas's pixel size. The
     * canvas is supersampled, so those differ by `canvasScale` — and a consumer
     * that reaches for `canvas.width` as a destination size paints the eraser at
     * double scale. That is not a crash; it renders fine and quietly destroys
     * whatever is 2x further along, which here was an ear pod.
     */
    patchSource: { canvas: patch, x: rect.x, y: rect.y, w: rect.width, h: rect.height },
    /** Rotation contributed by whatever this part follows, composed in front. */
    setInherited(prefix) {
      if (prefix === inherited) return;
      inherited = prefix;
      place();
    },
    layout(displayWidth) {
      displayScale = displayWidth / manifest.body.width;
      place();
    },
    setAngle(deg) {
      if (Math.abs(deg - angle) < 0.01) return;
      angle = deg;
      place();
    },
    destroy() { layer.remove(); patch.remove(); },
  };
}
