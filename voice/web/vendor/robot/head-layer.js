// Tilting the head instead of the whole portrait.
//
// Whole-portrait roll reads acceptably on a bust crop, but it is a photograph
// being rotated: the shoulders lean with the head, which is not what a head tilt
// looks like. Moving only the head needs three things.
//
//   PLATE   the portrait with the head ERASED, so the original does not show
//           through from underneath when the layer above it moves
//   HEAD    everything above the cut, rotating about a pivot in the neck
//   COLLAR  everything below the collar line, static, drawn back ON TOP
//
// The collar is what makes the whole thing possible. The head/neck boundary is
// metal on metal with no colour separation — genuinely hard to cut. It never has
// to be: cut generously LOW, into the region the collar covers, and composite
// the collar over the seam. That is how a ball joint works and how every 2D
// puppet rig is built. All three robots have a deep collar; measured on the
// orange one, the silhouette necks to its minimum at y=800 and the collar
// widens again from y=820.
//
// Erasing the head is the part with no obvious answer, and the geometry hands
// one over: above the collar the head is surrounded by backdrop on both sides,
// so each row can be filled by interpolating between the backdrop pixels just
// outside the silhouette. The backdrop is a smooth vignette, so a horizontal
// lerp across it is not an approximation of the right answer — it is the right
// answer, to within a couple of levels.

/** Rows are inpainted from this far outside the silhouette, to dodge its fringe. */
const MARGIN = 6;

function canvasOf(w, h) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return c;
}

/**
 * The portrait with the head erased.
 *
 * @param image  loaded portrait
 * @param part   head manifest entry
 * @param thr    luminance above which a pixel is "not backdrop"
 */
export function buildPlate(image, part, thr, contour = null) {
  const w = image.naturalWidth;
  const h = image.naturalHeight;
  const c = canvasOf(w, h);
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(image, 0, 0);

  const img = ctx.getImageData(0, 0, w, Math.min(h, part.collarY));
  const d = img.data;
  const rowW = w;
  // Silhouette bounds per row, kept for the neck extension below. It has to
  // know how wide the HEAD is at each destination row, not just how wide the
  // neck is where it was sampled.
  const bounds = [];

  for (let y = 0; y < Math.min(h, part.collarY); y++) {
    // Silhouette bounds for this row.
    let lo = -1;
    let hi = -1;
    for (let x = 0; x < rowW; x++) {
      const i = (y * rowW + x) * 4;
      const lum = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      if (lum > thr) { if (lo < 0) lo = x; hi = x; }
    }
    bounds[y] = lo < 0 ? null : { lo, hi };
    if (lo < 0) continue;

    const a = Math.max(0, lo - MARGIN);
    const b = Math.min(rowW - 1, hi + MARGIN);
    // No backdrop on one side means this row runs to the frame edge — there is
    // nothing to interpolate from, so leave it. In practice that only happens
    // below the collar, which this loop does not reach.
    if (a === 0 && b === rowW - 1) continue;

    const ai = (y * rowW + a) * 4;
    const bi = (y * rowW + b) * 4;
    const span = b - a;
    for (let x = a; x <= b; x++) {
      const t = span === 0 ? 0 : (x - a) / span;
      const i = (y * rowW + x) * 4;
      for (let k = 0; k < 3; k++) d[i + k] = Math.round(d[ai + k] + (d[bi + k] - d[ai + k]) * t);
    }
  }
  // Smooth vertically over the filled band.
  //
  // Each row was interpolated from its OWN endpoints, and those endpoints jump
  // where the silhouette does — an ear pod entering the row moves the sample
  // point 60px. Independently-correct rows stacked up produce horizontal
  // streaking, which is exactly what the exposed area looked like: plausible
  // backdrop, banded like a venetian blind.
  //
  // The real backdrop varies slowly in both directions, so averaging down the
  // column restores that and costs one pass.
  const K = 6;
  const src = new Uint8ClampedArray(d);
  for (let y = 0; y < Math.min(h, part.collarY); y++) {
    for (let x = 0; x < rowW; x++) {
      const i = (y * rowW + x) * 4;
      for (let k = 0; k < 3; k++) {
        let sum = 0;
        let n = 0;
        for (let dy = -K; dy <= K; dy++) {
          const yy = y + dy;
          if (yy < 0 || yy >= Math.min(h, part.collarY)) continue;
          sum += src[(yy * rowW + x) * 4 + k];
          n++;
        }
        d[i + k] = Math.round(sum / n);
      }
    }
  }
  ctx.putImageData(img, 0, 0);

  // The neck extension is GONE, deliberately.
  //
  // It tiled the band below the collar upward to keep wires continuing behind
  // the head, and it existed because the old model cut the head deep into the
  // collar and opened a gap that needed filling.
  //
  // Masking the head to its own outline closed that gap: the neck, collar and
  // wires are all static now, entirely inside the layers that never move, so
  // there is nothing to continue. What the extension left behind instead was
  // damage — it tiled across rows 544-800 clipped to a "neck span" that
  // measures orange's JAW (its real neck is below the luminance threshold), so
  // it painted near-full-width bars into the backdrop beside the ear pods.
  //
  // A mechanism that only existed to patch a different mechanism's flaw should
// go when that flaw does, rather than staying on as scenery.

// Erase the chin from the plate below the collar line.
//
// The plate below collarY is untouched artwork, which includes the original
// chin — a static copy behind everything. With the head masked to its outline
// and free to move, tilting away from that copy revealed it in place: a ghost
// chin behind the real one, reading as a hard line across the jaw that grew
// with the angle. Same per-row fill as the region above the collar, but here
// the span endpoints are the dark collar interior either side of the chin, so
// the fill is exactly what a lifted chin should reveal.
if (contour) {
  let deepest = part.collarY;
  for (let x = 0; x < w; x++) if (contour[x] > deepest) deepest = contour[x];
  if (deepest > part.collarY) {
    const bandH = Math.min(h, deepest + 2) - part.collarY;
    const band = ctx.getImageData(0, part.collarY, w, bandH);
    const bd = band.data;
    for (let by = 0; by < bandH; by++) {
      const yAbs = part.collarY + by;
      let x = 0;
      while (x < w) {
        if (contour[x] <= yAbs) { x++; continue; }
        let end = x;
        while (end < w && contour[end] > yAbs) end++;
        const a = Math.max(0, x - MARGIN);
        const b = Math.min(w - 1, end - 1 + MARGIN);
        const ai = (by * w + a) * 4;
        const bi = (by * w + b) * 4;
        const span = b - a;
        for (let xx = a; xx <= b; xx++) {
          const t = span === 0 ? 0 : (xx - a) / span;
          const i = (by * w + xx) * 4;
          for (let k = 0; k < 3; k++) {
            bd[i + k] = Math.round(bd[ai + k] + (bd[bi + k] - bd[ai + k]) * t);
          }
        }
        x = end;
      }
    }
    ctx.putImageData(band, 0, part.collarY);
  }
}
return c;
}

/**
 * Head and collar layers.
 *
 * `patches` are the antenna erasers, drawn INTO the head so the antennas do not
 * appear twice — once baked into the head layer and once as their own rotating
 * layer. They ride on top with `follows: 'head'`, which is correct: an antenna
 * is bolted to the head and tilts with it.
 */
export function extractHead(image, part, patches = []) {
  const w = image.naturalWidth;
  const h = image.naturalHeight;
  const scale = part.canvasScale ?? 1;
  const thr = part.threshold ?? 34;

  const cutH = Math.min(h, part.cutY);
  const head = canvasOf(Math.round(w * scale), Math.round(cutH * scale));
  const hctx = head.getContext('2d', { willReadFrequently: true });
  hctx.drawImage(image, 0, 0, w, cutH, 0, 0, head.width, head.height);

  // Antenna erasers go on BEFORE the alpha pass, not after.
  //
  // After, they stayed as opaque backdrop-coloured pixels riding along with the
  // head — a dark antenna-shaped smear sweeping across the real backdrop as the
  // head tilted. Applied first, the alpha pass sees them as the backdrop they
  // are and makes them transparent, so the plate behind simply shows through.
  for (const patch of patches) {
    // Destination size comes from the region's SOURCE-space extent, never from
    // the patch canvas's pixel dimensions — the patch is supersampled, so those
    // are `canvasScale` times larger. Using them painted each eraser at double
    // size, smearing a dark rectangle across the ear pod beside it.
    hctx.drawImage(patch.canvas, patch.x * scale, patch.y * scale,
      patch.w * scale, patch.h * scale);
  }

  // Alpha out the backdrop, so rotating the head reveals the plate behind it
  // rather than a rotating rectangle of black.
  //
  // Per-pixel thresholding does NOT work here, and the artwork says why:
  // measured on the orange robot, the ear pods' dark metal reads luminance
  // 21-26 while the backdrop reads 10. There is no threshold that keeps the
  // metal and drops the backdrop — 34 punched holes through both ear pods, and
  // the plate showing through them made the robot look damaged.
  //
  // A head is a solid connected object, so the silhouette is what matters, not
  // each pixel: everything BETWEEN the outermost above-threshold pixels in a
  // row is head, however dark it happens to be. The luminance ramp still runs,
  // but only to antialias the outer edge.
  const hd = hctx.getImageData(0, 0, head.width, head.height);
  const p = hd.data;
  const W = head.width;

  // The head layer is the HEAD SHAPE and nothing else.
  //
  // Earlier versions carried content well below the collar — first a neck stub,
  // then a taper — on the theory that the head needed material down there to
  // fill the sliver a tilt opens up. Every version leaked: whatever sits just
  // under the collar lifts to just ABOVE it when the head rotates, and above the
  // collar there is nothing to cover it. Collar ring, shoulder, stub edge — each
  // escaped in turn, and each fix moved the problem rather than removing it.
  //
  // The taper failed for a second reason worth recording, because it LOOKED
  // correct. It eased toward a "neck span" measured as the narrowest luminance
  // silhouette above the collar, and for orange that returns 525px — the JAW.
  // Its actual neck is a black concertina that falls below the threshold and is
  // invisible to the scan. So it eased from chin-width to chin-width and did
  // nothing whatsoever, while reading as a real fix in the diff.
  //
  // Masking to the head outline alone removes the whole category. Collar, neck
  // and wires stay in the static layers where they belong; the only thing that
  // moves is the only thing that should. The chin's bottom edge is then genuine
  // artwork rather than a manufactured boundary, which is exactly why it
  // survives rotation — a tilting head really does show its chin edge.
// Which bright span in a row is HEAD depends on where the row is.
//
// Above the collar, the hull of every bright run is head: the ear pods are
// separate bright islands beside the face, and the hull is what keeps them
// attached (their dark metal reads 21-26 against a backdrop of 10 — no
// per-pixel threshold can keep them).
//
// Near and below the collar line, that same hull — and the luminance rule
// that replaced it — kept dragging COLLAR along with the head. At y=806 the
// ring's left edge is lit at x=190 and the chin starts at 242; worse, the
// ring stays bright all the way down to the cut, so whole vertical strips of
// collar ring rode with the head and sheared sideways on every tilt. Down
// here the head is only the CENTRAL bright run — the chin — so that is the
// one kept: runs split on gaps wider than a rivet shadow, nearest-to-pivot
// wins.
//
// The boundary between the two regimes is centralFromY when the manifest
// sets it (orange's ring tops peek above its collar line), collarY otherwise.
const collarRowF = part.collarY * scale;
const centralFrom = (part.centralFromY ?? part.collarY) * scale;
const centreX = Math.round((part.pivot?.[0] ?? w / 2) * scale);
const BRIDGE = Math.round(8 * scale);
// The chin is CONNECTED to the face. Below centralFrom a run only counts as
// head if it overlaps the span kept in the row above; the chain of overlaps
// walks down the chin and dies at the first fully-dark band beneath it. That
// is what keeps glinting coils inside the collar bowl from being adopted as
// chin — they are bright, but nothing connects them to the face.
let prevLo = -1;
let prevHi = -1;
let chinMiss = 0;
let chinDead = false;
for (let y = 0; y < head.height; y++) {
  const runs = [];
  let runLo = -1;
  let runLast = -1;
  for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4;
    const lum = 0.299 * p[i] + 0.587 * p[i + 1] + 0.114 * p[i + 2];
    if (lum > thr) {
      if (runLo < 0) runLo = x;
      else if (x - runLast > BRIDGE) { runs.push([runLo, runLast]); runLo = x; }
      runLast = x;
    }
  }
  if (runLo >= 0) runs.push([runLo, runLast]);

let lo = -1;
let hi = -1;
if (y < centralFrom) {
  // Hull rows: no chain state at all. The first version let empty rows up
  // here fall through to the miss counter, and the canvas STARTS with empty
  // rows — backdrop above the crown — so the chain was declared dead before
  // the face existed and the head came out sliced flat at centralFrom.
  if (runs.length) {
    lo = runs[0][0];
    hi = runs[runs.length - 1][1];
    prevLo = lo;
    prevHi = hi;
  }
} else if (!chinDead) {
  const cands = prevLo >= 0
    ? runs.filter((r) => r[0] <= prevHi && r[1] >= prevLo)
    : runs;
  if (cands.length) {
    let best = cands[0];
    let bestD = Infinity;
    for (const r of cands) {
      const d = centreX < r[0] ? r[0] - centreX : centreX > r[1] ? centreX - r[1] : 0;
      if (d < bestD) { bestD = d; best = r; }
    }
    [lo, hi] = best;
    prevLo = lo;
    prevHi = hi;
    chinMiss = 0;
  } else {
    chinMiss += 1;
    if (chinMiss > 2) chinDead = true;
  }
}

  for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4;
    // Alpha from POSITION in the span, not from luminance: the boundary pixel
    // is a blend of metal and backdrop, and a luminance ramp renders it as a
    // bright halo tracing the jaw once the head moves.
    let a = 0;
    if (lo >= 0 && hi > lo && x >= lo && x <= hi) {
      a = Math.min(1, Math.min(x - lo, hi - x) / 2);
    }
    p[i + 3] = Math.round(255 * a);
  }
}
// Trace the head's REAL bottom edge, per column.
//
// `cutY` is a horizontal line, and every version of this so far ended up
// showing it: the head got sliced straight across where the artwork has a
// curve that dips through the middle of the chin. `cutY` now only bounds the
// search; the edge comes from the artwork.
//
// The edge is the bottom of what the head KEPT — not a second brightness
// test. The first version re-scanned for luminance >= 54 after the alpha
// pass had admitted content at > 34, and everything in between was erased:
// pale's shadowed lower chin, rust's dark blue chin panel, rust's dark ear
// cylinders, the shadowed undersides of both jaws. Each showed up as a black
// chunk missing from the face, with the lost band left behind as a static
// piece stuck to the neck. One definition of "head", used everywhere.
const bottomAt = new Int32Array(W).fill(-1);
const found = new Uint8Array(W);
for (let x = 0; x < W; x++) {
  for (let y = head.height - 1; y >= 0; y--) {
    if (p[(y * W + x) * 4 + 3] >= 128) { bottomAt[x] = y; found[x] = 1; break; }
  }
}

  // Smooth the traced edge. Per-column detection is locally noisy — a dark rivet
  // or a seam pulls one column up by several pixels — and an unsmoothed contour
  // reads as a torn edge rather than a moulded one.
const smoothed = new Int32Array(W).fill(-1);
const SPAN = Math.round(9 * scale);
for (let x = 0; x < W; x++) {
  if (!found[x]) continue;
  let sum = 0;
  let n = 0;
  for (let d = -SPAN; d <= SPAN; d++) {
    const xx = x + d;
    if (xx < 0 || xx >= W || !found[xx]) continue;
    sum += bottomAt[xx];
    n++;
  }
  smoothed[x] = Math.round(sum / n);
}

  const FEATHER = Math.max(1, Math.round(2 * scale));
  for (let x = 0; x < W; x++) {
    if (!found[x]) continue;
    const edge = smoothed[x];
    for (let y = Math.max(0, edge - FEATHER); y < head.height; y++) {
      const i = (y * W + x) * 4;
      if (p[i + 3] === 0) continue;
      const over = y - (edge - FEATHER);
      p[i + 3] = over >= FEATHER * 2 ? 0
        : Math.round(p[i + 3] * (1 - over / (FEATHER * 2)));
    }
  }

  hctx.putImageData(hd, 0, 0);

  const collarH = h - part.collarY;
  const collar = canvasOf(Math.round(w * scale), Math.round(collarH * scale));
  const cctx = collar.getContext('2d');
  cctx.drawImage(image, 0, part.collarY, w, collarH, 0, 0, collar.width, collar.height);

// Clip the collar's top to the SAME traced contour, where the chin dips
// below the collar line.
//
// The collar is a rectangular slice of the artwork, and on all three robots
// the chin hangs below the collar line — so the slice carried a static copy
// of the lower chin, drawn on top of everything. At rest the copy sat exactly
// over the head's own chin and was invisible. The moment the head tilted the
// two came apart: the head's chin moved, the copy stayed, and the pair read
// as a hard line across the jaw that grew with the angle.
//
// The head fades out across the same feather the collar fades in across, so
// at rest the cross-fade reassembles the original pixels exactly.
const cd = cctx.getImageData(0, 0, collar.width, collar.height);
const cdd = cd.data;
for (let x = 0; x < W; x++) {
  if (!found[x]) continue;
  const edge = smoothed[x] - collarRowF;   // contour in collar-row coords
  if (edge <= -FEATHER) continue;          // head ends above the collar
  const to = Math.min(collar.height, Math.ceil(edge + FEATHER));
  for (let cy = 0; cy < to; cy++) {
    const j = (cy * collar.width + x) * 4;
    const t = (cy - (edge - FEATHER)) / (FEATHER * 2);
    const a2 = t <= 0 ? 0 : t >= 1 ? 1 : t;
    cdd[j + 3] = Math.round(cdd[j + 3] * a2);
  }
}
cctx.putImageData(cd, 0, 0);

// The contour in SOURCE coordinates, for buildPlate: the plate below the
// collar still carries the original chin too — a third static copy — and
// erasing it needs to know where the chin is.
const contour = new Int32Array(w).fill(-1);
for (let sx = 0; sx < w; sx++) {
  const hx = Math.min(W - 1, Math.round(sx * scale));
  if (found[hx] && smoothed[hx] >= 0) contour[sx] = Math.round(smoothed[hx] / scale);
}

return { head, collar, scale, collarY: part.collarY, contour };
}

/**
 * How far the head is rolled, in degrees.
 *
 * Only roll for now. Yaw and pitch on a rigid layer are a 2D approximation that
 * needs a mesh warp to hold up; roll is a TRUE rotation of a flat image and
 * costs nothing in fidelity, which is why it ships first.
 */
export function headAngle(part, ch = {}, reduced = false) {
  const tilt = Number.isFinite(ch.headTilt) ? Math.max(-1, Math.min(1, ch.headTilt)) : 0;
  const deg = tilt * (part.maxRollDeg ?? 11) * (reduced ? 0.35 : 1);
  return deg === 0 ? 0 : deg;
}

export function createHeadSurface({ image, part, manifest, container, patches }) {
  const ex = extractHead(image, part, patches);
  const { head, collar, scale } = ex;
  const plate = buildPlate(image, part, part.threshold ?? 34, ex.contour);

  const mk = (canvas, z, id) => {
    canvas.className = 'part part-head';
    canvas.dataset.partId = id;
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.position = 'absolute';
    canvas.style.left = '0';
    canvas.style.top = '0';
    canvas.style.transformOrigin = '0 0';
    canvas.style.pointerEvents = 'none';
    canvas.style.zIndex = String(z);
    container.append(canvas);
    return canvas;
  };
  mk(plate, part.z - 2, `${part.id}-plate`);
  mk(head, part.z, part.id);
  mk(collar, part.z + 2, `${part.id}-collar`);

  let displayScale = 1;
  let angle = 0;

  /** The pivot in DISPLAY pixels — what dependent parts rotate about too. */
  function pivotPx() {
    return [part.pivot[0] * displayScale, part.pivot[1] * displayScale];
  }

  function place() {
    const sPlate = displayScale;
    plate.style.transform = `scale(${(sPlate / 1).toFixed(5)})`;

    const s = displayScale / scale;
    const [px, py] = [part.pivot[0] * scale, part.pivot[1] * scale];
    // Same composition as the antennas: pivot INSIDE the scale, so the origin
    // carries no (1 - s) offset.
    head.style.transform =
      `scale(${s.toFixed(5)}) translate(${px.toFixed(2)}px, ${py.toFixed(2)}px) ` +
      `rotate(${angle.toFixed(3)}deg) translate(${(-px).toFixed(2)}px, ${(-py).toFixed(2)}px)`;
    collar.style.transform =
      `translate(0px, ${(part.collarY * displayScale).toFixed(2)}px) scale(${s.toFixed(5)})`;
  }

  return {
    id: part.id,
    part,
    isHead: true,
    layout(displayWidth) {
      displayScale = displayWidth / manifest.body.width;
      place();
    },
    setAngle(deg) {
      if (Math.abs(deg - angle) < 0.01) return;
      angle = deg;
      place();
    },
    /** What a `follows: 'head'` part must compose into its own transform. */
    headTransform() {
      const [px, py] = pivotPx();
      return angle === 0
        ? ''
        : `translate(${px.toFixed(2)}px, ${py.toFixed(2)}px) rotate(${angle.toFixed(3)}deg) `
          + `translate(${(-px).toFixed(2)}px, ${(-py).toFixed(2)}px) `;
    },
    destroy() { head.remove(); collar.remove(); plate.remove(); },
  };
}
