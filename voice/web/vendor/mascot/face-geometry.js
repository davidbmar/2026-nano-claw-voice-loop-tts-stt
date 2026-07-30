// Normalised face space: (0,0) is the screen centre, 1 unit = half screen width.
// Landmarks match the original illustration's proportions.
// The original's eyes are large and dominate the screen, so these are sized
// generously rather than to a conventional face proportion.
const EYE_DX = 0.37;   // horizontal offset of each eye from centre
const EYE_CY = -0.14;  // eye centre height
const EYE_RX = 0.26;   // eye half width
const EYE_RY = 0.35;   // eye half height when fully open
const BROW_DY = -0.44; // brow height relative to the eye centre
// Measured against the reference: vertical EXTENT already matches within 3-5%.
// What differs is ink CONTINUITY — on a squinted expression the reference has ink
// in 83% of rows against 47% here, because short eyes leave a void above the
// mouth.
//
// Raising this to 0.36 to close that void was tried and reverted: it relocated
// the gap to BELOW the mouth rather than closing it (ink rows 0.838 -> 0.821 on
// concerned, 0.470 -> 0.466 on thinking) while shrinking total extent against a
// reference the face already matched. A gap cannot be filled by moving one of its
// edges inward; it needs more ink in the band, which is what the reference does
// with mid-face shading — and that risks colliding with the cheek blush.
const MOUTH_CY = 0.40;

// A quadratic bezier only reaches HALFWAY to its control point, so every
// control-point offset below is roughly double its intended visual displacement.
const BEZIER_REACH = 2;

const sign = (side) => (side === 'L' ? -1 : 1);

function ellipse(cx, cy, rx, ry, steps = 40, tilt = 0) {
  const ct = Math.cos(tilt);
  const st = Math.sin(tilt);
  const pts = [];
  for (let i = 0; i < steps; i++) {
    const t = (i / steps) * Math.PI * 2;
    const x = Math.cos(t) * rx;
    const y = Math.sin(t) * ry;
    pts.push([cx + x * ct - y * st, cy + x * st + y * ct]);
  }
  return pts;
}

function bezier(p0, p1, p2, steps) {
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const mt = 1 - t;
    pts.push([
      mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
      mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
    ]);
  }
  return pts;
}

/**
 * Concatenate arcs into a closed loop, dropping each arc's final point.
 *
 * `bezier` is endpoint-inclusive, so chaining arcs naively duplicates every
 * shared endpoint. Duplicates are zero-length edges, which make atan2(0,0)
 * return 0 and fabricate garbage tangents for any curvature analysis — and they
 * confuse smoothing passes.
 */
function closedLoop(...arcs) {
  const out = [];
  for (const arc of arcs) out.push(...arc.slice(0, -1));
  return out;
}

/**
 * Offset a centreline by a width profile that tapers to zero at both ends,
 * producing a closed brush-stroke outline. The taper is what makes the ink read
 * as a drawn stroke rather than a vector line — the single most important
 * property for sitting convincingly inside hand-drawn art.
 */
export function taperedStroke(centreline, maxWidth, steps = 24, profile = null) {
  const line =
    centreline.length > 3
      ? centreline
      : bezier(
          centreline[0],
          centreline[1] ?? centreline[0],
          centreline[2] ?? centreline[centreline.length - 1],
          steps,
        );
  const n = line.length;
  const left = [];
  const right = [];
  for (let i = 0; i < n; i++) {
    const prev = line[Math.max(0, i - 1)];
    const next = line[Math.min(n - 1, i + 1)];
    let dx = next[0] - prev[0];
    let dy = next[1] - prev[1];
    const L = Math.hypot(dx, dy) || 1;
    dx /= L;
    dy /= L;
    const t = i / (n - 1);
    // Default is symmetric: zero at both ends, thickest in the middle. A custom
    // profile lets a stroke be thick at one end and taper to a point at the
    // other, which is how a brow is actually drawn.
    const w = (maxWidth / 2) * (profile ? profile(t) : Math.sin(Math.PI * t));
    left.push([line[i][0] - dy * w, line[i][1] + dx * w]);
    right.push([line[i][0] + dy * w, line[i][1] - dx * w]);
  }
  return [...left, ...right.reverse()];
}

/**
 * One Chaikin subdivision pass over a closed polygon.
 *
 * Rounds sharp vertices while leaving already-smooth runs essentially
 * untouched, which is precisely the selectivity needed here: the two lid arcs
 * are smooth in the middle and meet at a hard point at each corner.
 */
function chaikin(pts) {
  const out = [];
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[(i + 1) % n];
    out.push([x0 * 0.75 + x1 * 0.25, y0 * 0.75 + y1 * 0.25]);
    out.push([x0 * 0.25 + x1 * 0.75, y0 * 0.25 + y1 * 0.75]);
  }
  return out;
}

/**
 * Two arcs meeting at two rounded corners — how an eye is actually drawn.
 *
 * Built in canonical space where -rx is the INNER corner (toward the screen
 * centre) and +rx the outer, then mirrored per side by the caller. An ellipse
 * cannot express this: scaling one vertically moves both lids together, which
 * only ever reads as "sleepy" or "wide".
 */
function lidOutline(rx, ry, tilt, arch, squint, steps = 18) {
  const innerY = tilt * ry * 0.38;
  const outerY = -tilt * ry * 0.22;
  const chordMid = (innerY + outerY) / 2;

  // Control points reach only halfway, so both offsets are doubled.
  //
  // Arch REDISTRIBUTES lid height rather than adding it: a high arch lifts the
  // upper lid and pulls the lower lid up with it, keeping enclosed area roughly
  // constant. Letting arch scale total height instead makes it compound with
  // eyeScaleY, and surprise balloons into an alien stare. Size belongs to
  // eyeScaleY; shape belongs to arch.
  const upperCtrl = chordMid - ry * (1 + arch * 0.34) * BEZIER_REACH;
  const lowerCtrl = chordMid + ry * (1 - arch * 0.3 - squint * 0.62) * BEZIER_REACH;

  // The two lid arcs must NOT meet at a single point — that produces a cusp
  // where the outline reverses direction (~2.4 rad turn), which reads as a
  // spike rather than a drawn eye corner, and no amount of smoothing fixes a
  // genuine reversal. Instead each arc stops short of the corner and a small
  // outward-bulging cap joins them.
  const cap = ry * 0.2; // vertical half-height of the corner cap
  const bulge = rx * 0.07; // how far the cap bows outward

  const innerTop = [-rx, innerY - cap];
  const innerBot = [-rx, innerY + cap];
  const outerTop = [rx, outerY - cap];
  const outerBot = [rx, outerY + cap];

  const raw = closedLoop(
    bezier(innerTop, [0, upperCtrl], outerTop, steps),
    bezier(outerTop, [rx + bulge, outerY], outerBot, 6),
    bezier(outerBot, [0, lowerCtrl], innerBot, steps),
    bezier(innerBot, [-rx - bulge, innerY], innerTop, 6),
  );
  // One smoothing pass to blunt the remaining junctions without rounding the
  // eye back into a plain oval and losing the lid shape.
  return chaikin(raw);
}

/** Move every point toward the shape's centroid — a simple inset for a convex blob. */
function insetShape(pts, amount) {
  const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
  const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  return pts.map(([x, y]) => {
    const dx = x - cx;
    const dy = y - cy;
    const d = Math.hypot(dx, dy) || 1;
    const k = Math.max(0, d - amount) / d;
    return [cx + dx * k, cy + dy * k];
  });
}

export function eyeGeometry(ch, side) {
  const s = sign(side);
  const cx = s * EYE_DX;
  const open = side === 'L' ? ch.eyeLOpen : ch.eyeROpen;
  const squint = side === 'L' ? ch.eyeLSquint : ch.eyeRSquint;
  const tilt = (side === 'L' ? ch.lidLTilt : ch.lidRTilt) ?? 0;
  const arch = (side === 'L' ? ch.lidLArch : ch.lidRArch) ?? 0;
  const ry = EYE_RY * ch.eyeScaleY * Math.max(0.03, open) * (1 - 0.4 * squint);

  if (open < 0.12) {
    // Closed: a downward-curving arc drawn as a tapered stroke, matching the
    // reference blink expression.
    const arc = [
      [cx - EYE_RX, EYE_CY],
      [cx, EYE_CY + 0.14 * BEZIER_REACH],
      [cx + EYE_RX, EYE_CY],
    ];
    return { outline: taperedStroke(arc, 0.07, 20), pupil: null, highlight: null, closed: true };
  }

  // The original eye is a thick ink outline around a white sclera, with a large
  // dark iris inside it. Three nested shapes, not one filled blob.
  //
  // The outline weight must be PROPORTIONAL to the eye. A fixed inset eats most
  // of a squinted eye, leaving it almost entirely ink with a sliver of white.
  const LINE = Math.min(0.032, ry * 0.15);
  const lean = s * 0.13; // eyes lean outward, as in the reference

  // Build in canonical space (inner corner at -rx), mirror for the left eye,
  // then apply the outward lean.
  const cl = Math.cos(lean);
  const sl = Math.sin(lean);
  // Canonical -rx is the inner corner, which must land on the side facing the
  // screen centre: identity for the right eye, mirrored for the left. Using -s
  // here instead flips which corner the tilt acts on, silently inverting every
  // hard/soft expression.
  const place = (pts) =>
    pts.map(([x, y]) => {
      const mx = x * s;
      return [cx + mx * cl - y * sl, EYE_CY + mx * sl + y * cl];
    });

  const local = lidOutline(EYE_RX, ry, tilt, arch, squint);
  const localSclera = insetShape(local, LINE);
  const outline = place(local);
  const sclera = place(localSclera);

  // Iris placement is derived from the SCLERA'S ACTUAL BOUNDS, not from fixed
  // landmarks. Tilt and arch move the lid shape around, so anchoring the iris to
  // a constant leaves it sitting low in the eye — and any future lid channel
  // would break it again.
  const sxs = localSclera.map((p) => p[0]);
  const sys = localSclera.map((p) => p[1]);
  const sMinX = Math.min(...sxs);
  const sMaxX = Math.max(...sxs);
  const sMinY = Math.min(...sys);
  const sMaxY = Math.max(...sys);
  const halfW = (sMaxX - sMinX) / 2;
  const halfH = (sMaxY - sMinY) / 2;
  const localCX = (sMinX + sMaxX) / 2;
  const localCY = (sMinY + sMaxY) / 2;

  const irisR = Math.min(halfW, halfH) * 0.94 * (ch.irisScale ?? 1);
  const travelX = Math.max(0, halfW - irisR) * 0.95;
  const travelY = Math.max(0, halfH - irisR) * 0.95;

  // The eye's CENTRE is geometry, so it goes through `place` and gets mirrored.
  // The gaze OFFSET is a screen direction and must not be mirrored, or pupilX
  // would send the two irises opposite ways and cross the eyes.
  const [ecx, ecy] = place([[localCX, localCY]])[0];
  const px = ecx + ch.pupilX * travelX;
  const py = ecy + ch.pupilY * travelY;
  const pupil = ellipse(px, py, irisR * 0.94, irisR, 44, lean);

  // An oval highlight rather than a round dot, tilted like the reference's.
  const hx = px + Math.cos(ch.highlightAngle) * irisR * 0.42;
  const hy = py + Math.sin(ch.highlightAngle) * irisR * 0.42;
  const highlight = ellipse(hx, hy, irisR * 0.42, irisR * 0.3, 24, ch.highlightAngle + 1.2);

  // NOTE: interior iris shading was tried three ways — a halftone dot lattice,
  // a thick crescent, and a thin crescent — and all three failed. At the ~45px
  // display size the iris has no pixels to carry shading AND a highlight; every
  // version read as a two-tone disc rather than light pooling. The highlight
  // alone supplies the dimensional cue. Deliberately omitted, not forgotten.

  return { outline, sclera, pupil, highlight, closed: false };
}

/**
 * A line under the eye, suggesting the lower lid.
 *
 * The reference draws one beneath each eye, heaviest in `concerned` and
 * `thinking`, and its absence is a large part of why the procedural eyes read
 * as simpler than the drawn ones. Derived from the eye's own sclera bounds so it
 * tracks whatever lid shape is active.
 */
export function lowerLidGeometry(ch, side, bold = 1) {
  const amount = ch.lowerLid ?? 0;
  if (amount <= 0.04) return null;
  const eye = eyeGeometry(ch, side);
  if (eye.closed || !eye.sclera) return null;

  const xs = eye.sclera.map((p) => p[0]);
  const ys = eye.sclera.map((p) => p[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys);
  const inset = (maxX - minX) * 0.14;
  const drop = (maxY - Math.min(...ys)) * 0.16 * amount;

  return taperedStroke(
    [
      [minX + inset, maxY + drop * 0.4],
      [(minX + maxX) / 2, maxY + drop * BEZIER_REACH],
      [maxX - inset, maxY + drop * 0.4],
    ],
    0.03 * bold * (0.6 + 0.4 * amount),
    18,
  );
}

/**
 * Halftone shading on the upper lid and under the eye.
 *
 * The reference shades both, and their absence is why the procedural eyes read
 * flatter than the drawn ones. Scale permits it here: these regions span roughly
 * 250 canvas px, wider than the cheek patch where halftone already works, and
 * far wider than the iris where it failed.
 *
 * Only the upper lid is shaded. It is part of the eye's FORM and so is nearly
 * always present. An under-eye version was tried and cut — see the note below.
 */
export function eyeShadeGeometry(ch, side) {
  const eye = eyeGeometry(ch, side);
  if (eye.closed || !eye.sclera) return [];

  const xs = eye.sclera.map((p) => p[0]);
  const ys = eye.sclera.map((p) => p[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const w = maxX - minX;
  const h = maxY - minY;
  const cx = (minX + maxX) / 2;

  const dots = [];
  // A band hugging a horizontal edge of the eye, denser toward the middle.
  const band = (edgeY, dir, rows, strength, spread) => {
    for (let r = 0; r < rows; r++) {
      const fr = rows === 1 ? 0 : r / (rows - 1);
      const y = edgeY + dir * spread * h * (0.12 + fr * 0.7);
      // Narrow as the band moves away from the eye, following its curve.
      const chord = Math.sqrt(Math.max(0, 1 - fr * fr * 0.75));
      const half = (w / 2) * 0.82 * chord;
      const count = Math.max(2, Math.round(9 * chord));
      for (let i = 0; i < count; i++) {
        const fx = count === 1 ? 0 : (i / (count - 1)) * 2 - 1;
        const stagger = r % 2 ? half / Math.max(1, count - 1) : 0;
        const x = cx + fx * half + stagger;
        if (Math.abs(x - cx) > half) continue;
        const fade = (1 - fr * 0.55) * (1 - Math.abs(fx) * 0.35);
        // Dot size is set from the region's WIDTH, which is what the halftone
        // rule is stated against — sizing off the eye's height instead couples
        // it to an unrelated dimension and lets tall eyes exceed the limit.
        dots.push([x, y, w * 0.026 * (0.55 + 0.45 * fade) * strength, fade * strength]);
      }
    }
  };

  // Upper lid: ABOVE the eye's top edge, on the lid itself. Not below it —
  // dots inside the eye are painted over by the sclera fill, which is drawn
  // afterwards, so they would be invisible as well as anatomically wrong.
  // Bands must HUG the eye. At a third of an eye-height away the dots read as a
  // decorative dotted line floating nearby rather than as shading on the lid.
  band(minY, -1, 3, 1, 0.17);
  // NOTE: an under-eye stipple bag was tried here and cut. It collided with the
  // cheek blush — the blush sits at y~0.2 and the bag at y~0.02-0.10, so on a
  // squinted eye the two dot fields merged into one speckled region instead of
  // reading as two features. It was also redundant: `lowerLidGeometry` already
  // states that anatomy as a clean stroke, which reads better and at more sizes.
  // Deliberately omitted.

  return dots;
}

export function browGeometry(ch, side, bold = 1) {
  const s = sign(side);
  const y = side === 'L' ? ch.browLY : ch.browRY;
  const angle = side === 'L' ? ch.browLAngle : ch.browRAngle;
  const cx = s * EYE_DX;
  const base = EYE_CY + BROW_DY - y * 0.14;

  // Inner end sits towards the screen centre, outer end away from it.
  const innerX = cx - s * EYE_RX * 1.05;
  const outerX = cx + s * EYE_RX * 1.05;
  // Positive angle raises the inner end (worried) and drops the outer end.
  const innerY = base - angle * 0.16;
  const outerY = base + angle * 0.09;
  const midY = Math.min(innerY, outerY) - 0.07 * BEZIER_REACH;

  // The centreline runs outer -> mid -> inner, so t=0 is the outer end. The
  // reference brows are heaviest just inboard of the outer end and taper to a
  // point at the inner tip; a symmetric taper reads as a generic arc.
  //
  // For sin(PI * t^k) the peak sits where t^k = 0.5, i.e. t = 0.5^(1/k). So k
  // BELOW 1 moves the peak earlier and k above 1 moves it later — an exponent
  // above 1 produces a back-weighted brow, the opposite of what is wanted.
  const browProfile = (t) => Math.sin(Math.PI * Math.pow(t, 0.72)) * (1 - 0.3 * t);
  return taperedStroke(
    [
      [outerX, outerY],
      [(outerX + innerX) / 2, midY],
      [innerX, innerY],
    ],
    0.105 * bold,
    28,
    browProfile,
  );
}

/**
 * A thin arc beneath the mouth, suggesting the lower lip.
 *
 * The reference draws one under every expression, and its absence is much of why
 * a filled shape reads as a flat squiggle rather than as a drawn mouth.
 */
function lowerLipArc(lx, rx, shift, cornerY, bold, drop = 0.075) {
  const inset = (rx - lx) * 0.17;
  return taperedStroke(
    [
      [lx + inset, cornerY + drop * 0.55],
      [shift, cornerY + drop * BEZIER_REACH],
      [rx - inset, cornerY + drop * 0.55],
    ],
    0.028 * bold,
    16,
  );
}

export function mouthGeometry(ch, bold = 1) {
  const halfW = 0.40 * ch.mouthWidth;
  const shift = ch.mouthShift * 0.1;
  const cy = MOUTH_CY;
  const cornerLift = ch.mouthCorner * 0.19;
  const openness = ch.mouthOpen;

  const lx = -halfW + shift;
  const rx = halfW + shift;
  const cornerY = cy - cornerLift;
  const upperMidY = cy - ch.mouthCurve * 0.06 * BEZIER_REACH;
  const lowerMidY = cy + (0.05 + openness * 0.5) * BEZIER_REACH;

  if (openness < 0.1) {
    // A closed lip line, optionally wavering for unease.
    const waver = ch.mouthWaver ?? 0;
    const mid = [shift, upperMidY + 0.03 * BEZIER_REACH];
    if (waver > 0.05) {
      const line = [];
      const N = 22;
      for (let i = 0; i <= N; i++) {
        const t = i / N;
        const x = lx + (rx - lx) * t;
        const base = cornerY + Math.sin(Math.PI * t) * (mid[1] - cornerY);
        line.push([x, base + Math.sin(t * Math.PI * 5) * 0.045 * waver]);
      }
      // Note the `bold` factor: this branch previously omitted it, so a
      // wavering mouth stayed hairline-thin at small sizes while every other
      // feature thickened.
      return {
        outline: taperedStroke(line, 0.082 * bold, 22),
        tongue: null,
        lowerLip: lowerLipArc(lx, rx, shift, cornerY, bold, 0.09),
      };
    }
    return {
      outline: taperedStroke([[lx, cornerY], mid, [rx, cornerY]], 0.086 * bold, 20),
      tongue: null,
      lowerLip: lowerLipArc(lx, rx, shift, cornerY, bold),
    };
  }

  const lens = closedLoop(
    bezier([lx, cornerY], [shift, upperMidY], [rx, cornerY], 18),
    bezier([rx, cornerY], [shift, lowerMidY], [lx, cornerY], 18),
  );

  const round = ch.mouthRound ?? 0;
  let outline = lens;
  if (round > 0.02) {
    // Blend the lens toward an ellipse so a rounded "o" is reachable without a
    // separate mouth model.
    //
    // Both shapes MUST be walked in the same direction from the same landmark.
    // The lens runs left -> top -> right -> bottom -> left, so the ellipse is
    // parameterised from theta = PI upward to match. Index-matching against an
    // ellipse that starts elsewhere yields a self-intersecting polygon, and with
    // the nonzero fill rule that cancels its own interior — the mouth vanishes.
    const midY = (cornerY + lowerMidY) / 2;
    const ry = (lowerMidY - cornerY) / 2;
    const rxr = halfW * (1 - 0.42 * round);
    const n = lens.length;
    outline = lens.map(([x, y], i) => {
      const theta = Math.PI + (i / n) * Math.PI * 2;
      const cxr = shift + Math.cos(theta) * rxr;
      const cyr = midY + Math.sin(theta) * ry;
      return [x + (cxr - x) * round, y + (cyr - y) * round];
    });
  }

  let tongue = null;
  if (ch.tongue > 0.05 && openness > 0.3) {
    // The tongue sits inside the lower lip, so it hangs off the mouth's actual
    // lower edge rather than off the control point.
    const lowerEdge = 0.25 * cornerY + 0.5 * lowerMidY + 0.25 * cornerY;
    const tw = halfW * 0.46;
    const ty = lowerEdge - 0.1 * ch.tongue;
    tongue = bezier(
      [shift - tw, ty],
      [shift, ty + 0.11 * ch.tongue * BEZIER_REACH],
      [shift + tw, ty],
      16,
    ).concat(bezier([shift + tw, ty], [shift, ty - 0.03], [shift - tw, ty], 8));
  }
  return {
    outline,
    tongue,
    lowerLip: openness < 0.55 ? lowerLipArc(lx, rx, shift, cornerY + (lowerMidY - cornerY) * 0.5, bold, 0.06) : null,
  };
}

/**
 * A row of teeth across the top of an open mouth. Gritted teeth are the
 * difference between reading as "working" and reading as "straining", and no
 * amount of brow or mouth shape substitutes for them.
 */
export function teethGeometry(ch) {
  if (ch.teeth <= 0.05 || ch.mouthOpen < 0.12) return null;
  const halfW = 0.40 * ch.mouthWidth;
  const shift = ch.mouthShift * 0.1;
  const cornerY = MOUTH_CY - ch.mouthCorner * 0.17;
  const lowerMidY = MOUTH_CY + (0.05 + ch.mouthOpen * 0.5) * BEZIER_REACH;
  const lowerEdge = 0.5 * cornerY + 0.5 * lowerMidY;

  // Shallow, and inset from the corners, so dark mouth still reads around them.
  // Filling much of the mouth height turns the row into a zipper.
  const depth = (lowerEdge - cornerY) * 0.24 * ch.teeth;
  const inset = halfW * 0.24;
  const left = -halfW + shift + inset;
  const right = halfW + shift - inset;
  const count = Math.max(4, Math.round(6 * ch.mouthWidth));
  const gap = (right - left) / count;
  const rects = [];
  for (let i = 0; i < count; i++) {
    const x = left + i * gap;
    // Follow the upper lip's curve so the row doesn't float flat.
    const t = (x - (-halfW + shift)) / (2 * halfW || 1);
    const lip = cornerY + Math.sin(Math.PI * Math.min(1, Math.max(0, t))) * (MOUTH_CY - cornerY) * 0.5;
    rects.push([x + gap * 0.08, lip, gap * 0.84, depth]);
  }
  return rects;
}

/**
 * Cartoon sweat beads: a teardrop, point up, flung from the upper corners of
 * the screen. `phase` (0..1) drives the fall so the renderer can animate them
 * without holding state.
 */
export function sweatGeometry(ch, phase = 0) {
  if (ch.sweat <= 0.04) return [];
  const beads = [];
  // Kept inboard of the binary border so beads never collide with it.
  const SLOTS = [
    [-0.6, -0.62, 0.0],
    [0.64, -0.58, 0.45],
    [-0.68, -0.3, 0.72],
  ];
  const active = ch.sweat > 0.6 ? 3 : ch.sweat > 0.3 ? 2 : 1;
  for (let i = 0; i < active; i++) {
    const [bx, by, off] = SLOTS[i];
    const p = (phase + off) % 1;
    const fall = p * 0.5;
    // Large enough to read as a bead at display size rather than as a speck.
    const scale = (0.085 + 0.03 * ch.sweat) * (1 - p * 0.3);
    // Fade in fast, out slow, so beads appear flung and then vanish.
    const alpha = Math.min(1, p * 6) * (1 - p * 0.85) * ch.sweat;
    if (alpha <= 0.02) continue;
    const cx = bx + p * 0.06 * Math.sign(bx);
    const cy = by + fall;
    // Teardrop: a circle with a point pulled upward.
    const pts = [];
    const STEPS = 22;
    for (let s = 0; s < STEPS; s++) {
      const t = (s / STEPS) * Math.PI * 2 - Math.PI / 2;
      // Narrow the top into a tip.
      const taper = t > Math.PI / 2 && t < (3 * Math.PI) / 2 ? 1 : 0.55;
      pts.push([cx + Math.cos(t) * scale * taper, cy + Math.sin(t) * scale]);
    }
    pts.push([cx, cy - scale * 2.1]); // the tip
    beads.push({
      pts,
      alpha,
      // A glint low-right in the bead, opposite the tip.
      glint: [cx + scale * 0.3, cy + scale * 0.32, scale * 0.24],
    });
  }
  return beads;
}

/**
 * Stippled cheek blush.
 *
 * Halftone dots, which is how the reference shades — and unlike the iris, the
 * scale supports it here. The cheek patch is roughly 3x the iris width, so dots
 * that fall below a device pixel inside an iris read comfortably on a cheek.
 * Same technique, different scale, opposite verdict.
 */
export function blushGeometry(ch) {
  if ((ch.blush ?? 0) <= 0.02) return [];
  const dots = [];
  const CX = 0.5;
  const CY = 0.2;
  const RX = 0.145;
  const RY = 0.078;
  const ROWS = 5;
  for (const side of [-1, 1]) {
    for (let r = 0; r < ROWS; r++) {
      const fy = (r / (ROWS - 1)) * 2 - 1; // -1 .. 1 down the patch
      const chord = Math.sqrt(Math.max(0, 1 - fy * fy));
      const count = Math.max(2, Math.round(7 * chord));
      for (let i = 0; i < count; i++) {
        const fx = count === 1 ? 0 : (i / (count - 1)) * 2 - 1;
        const stagger = r % 2 ? (RX * chord) / Math.max(1, count - 1) : 0;
        const x = side * CX + fx * RX * chord + stagger;
        const y = CY + fy * RY;
        // Denser toward the middle, which is how a drawn blush fades at its edge.
        const falloff = 1 - Math.min(1, Math.hypot(fx, fy) * 0.55);
        dots.push([x, y, 0.010 + 0.008 * falloff]);
      }
    }
  }
  return dots;
}

/**
 * Cartoon strain marks at the temples: short PARALLEL strokes, offset
 * perpendicular to each other.
 *
 * They must be parallel, not fanned from a point — three lines radiating from a
 * single origin is the universal grammar for an arrow, so a fan reads as
 * direction rather than exertion.
 */
export function effortGeometry(ch) {
  if (ch.effort <= 0.05) return [];
  const lines = [];
  const ANCHORS = [
    [-0.72, -0.56],
    [0.72, -0.56],
  ];
  for (const [ax, ay] of ANCHORS) {
    const dir = Math.sign(ax);
    // A single shared angle, leaning up and outward.
    const a = -0.62 * dir;
    const ux = Math.cos(a) * dir;
    const uy = Math.sin(a);
    // Perpendicular, for spacing the parallel strokes.
    const nx = -uy * dir;
    const ny = ux * dir;
    const len = 0.085 + 0.06 * ch.effort;
    const spacing = 0.062;
    for (let i = -1; i <= 1; i++) {
      const ox = ax + nx * i * spacing;
      const oy = ay + ny * i * spacing;
      // The middle stroke is longest, as in hand-drawn strain marks.
      const l = len * (i === 0 ? 1 : 0.68);
      lines.push([
        [ox, oy],
        [ox + ux * l, oy + uy * l],
      ]);
    }
  }
  return lines;
}

export function faceGeometry(ch, { bold = 1 } = {}) {
  return {
    eyes: [eyeGeometry(ch, 'L'), eyeGeometry(ch, 'R')],
    brows: [browGeometry(ch, 'L', bold), browGeometry(ch, 'R', bold)],
    lowerLids: [lowerLidGeometry(ch, 'L', bold), lowerLidGeometry(ch, 'R', bold)],
    eyeShade: [eyeShadeGeometry(ch, 'L'), eyeShadeGeometry(ch, 'R')],
    mouth: mouthGeometry(ch, bold),
    teeth: teethGeometry(ch),
    effort: effortGeometry(ch),
  };
}
