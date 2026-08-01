import { faceGeometry, sweatGeometry, blushGeometry } from './face-geometry.js';
import { PIXEL_ELLIPSIS } from './expressions.js';

const INK = '#141014';

function fillPoly(ctx, pts) {
  if (!pts || pts.length < 3) return;
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.closePath();
  ctx.fill();
}

/**
 * Rectangle (0,0)-(w,h) with rounded corners as a point list. The corner radius
 * matches the bezel's inner radius so the cream fill never squares off the
 * screen's rounded corners.
 */
export function roundedQuadPath(w, h, radius, steps = 10) {
  const r = Math.max(0, Math.min(radius, Math.min(w, h) / 2));
  const corners = [
    [0, 0],
    [w, 0],
    [w, h],
    [0, h],
  ];
  if (r === 0) return corners;

  const dir = (from, to) => {
    const dx = to[0] - from[0];
    const dy = to[1] - from[1];
    const L = Math.hypot(dx, dy) || 1;
    return [dx / L, dy / L];
  };

  const out = [];
  for (let i = 0; i < 4; i++) {
    const prev = corners[(i + 3) % 4];
    const cur = corners[i];
    const next = corners[(i + 1) % 4];
    const [ax, ay] = dir(cur, prev);
    const [bx, by] = dir(cur, next);
    const a = [cur[0] + ax * r, cur[1] + ay * r];
    const b = [cur[0] + bx * r, cur[1] + by * r];
    out.push(a);
    for (let s = 1; s < steps; s++) {
      const t = s / steps;
      const mt = 1 - t;
      out.push([
        mt * mt * a[0] + 2 * mt * t * cur[0] + t * t * b[0],
        mt * mt * a[1] + 2 * mt * t * cur[1] + t * t * b[1],
      ]);
    }
    out.push(b);
  }
  return out;
}

/**
 * Filling the display interior with the screen's cream both erases the baked-in
 * face and binary border, and provides the drawing surface. No pre-baked
 * "blank screen" asset is needed.
 */
export function fillScreen(ctx, w, h, radius, cream) {
  ctx.clearRect(0, 0, w, h);
  const pts = roundedQuadPath(w, h, radius);
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.closePath();
  ctx.fillStyle = cream;
  ctx.fill();
}

/** Deterministic integer hash, so the border is stable across frames. */
function hash2(a, b) {
  let x = (a * 374761393 + b * 668265263) >>> 0;
  x = (x ^ (x >>> 13)) >>> 0;
  x = Math.imul(x, 1274126177) >>> 0;
  return (x ^ (x >>> 16)) >>> 0;
}

/**
 * Deterministic ring of border cells, two deep on each edge, with the centre
 * clear for the face.
 *
 * Roughly 45% of cells are left empty and some render as solid blocks rather
 * than digits: the original's frame is a sparse, irregular pixel-stepped edge,
 * and filling every cell with a digit reads as a wall of noise instead.
 */
export function binaryBorderCells(w, h, cell) {
  const cols = Math.floor(w / cell);
  const rows = Math.floor(h / cell);
  const cells = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const depth = Math.min(c, cols - 1 - c, r, rows - 1 - r);
      if (depth > 1) continue;
      const hv = hash2(c + 1, r + 1);
      // The outer ring is denser than the inner one, which gives the frame a
      // defined edge that fades inward rather than reading as scatter.
      if (hv % 100 < (depth === 0 ? 18 : 42)) continue;
      // Sub-cell jitter and per-cell scale break up the grid. Without them the
      // border reads as a table of text rather than a pixel-stepped frame.
      const jx = (((hv >>> 7) & 0xff) / 255 - 0.5) * cell * 0.34;
      const jy = (((hv >>> 15) & 0xff) / 255 - 0.5) * cell * 0.34;
      cells.push({
        x: c * cell + jx,
        y: r * cell + jy,
        w: cell,
        h: cell,
        i: r * cols + c,
        scale: 0.8 + (((hv >>> 3) & 0x7) / 7) * 0.45,
        // Occasional solid pixel blocks, mostly on the outermost ring.
        block: hv % 100 >= 86 && depth === 0,
      });
    }
  }
  return cells;
}

/**
 * Level of detail, from the screen's ACTUAL on-screen width in CSS pixels.
 *
 * The canvas is a fixed ~982px wide however small the character is displayed,
 * so at a 200px avatar the browser downscales it 20:1. Fine marks do not merely
 * shrink there — they alias into grey mush that muddies the few features which
 * would otherwise carry. Measured legibility floors:
 *
 *   96 px screen (420 px character)  face reads; the border is ALREADY mush
 *   64 px  (280 px)                   only mouth and eye mass survive; confused,
 *                                    working and sweating are indistinguishable
 *                                    without the glyph and meter
 *   46 px  (200 px)                   a few dark marks, no emotion legible
 *   32 px  (140 px)                   a blob
 *
 * The response is to draw FEWER, BOLDER marks — not the same marks smaller. For
 * the two overlays that carry information rather than mood, "bolder" means
 * literally bigger: see the glyph and progress notes below.
 */
export function levelOfDetail(screenPx) {
  if (!Number.isFinite(screenPx) || screenPx <= 0) return FULL_DETAIL;
  return {
    screenPx,
    // Thresholds come from the observations above, not from taste.
    border: screenPx >= 120,

    // The glyph and the progress meter are the only INFORMATION on this screen —
    // '?' means "I did not understand you" and the meter means "I am still
    // working". Everything else is affect. They used to be dropped at 110px
    // while blush and stipple, which are pure decoration, survived down to 85.
    // That is exactly backwards: under size pressure you shed decoration and
    // keep information.
    //
    // The measurement behind the old floor was right — a glyph at w*0.16 IS
    // mush on a 96px screen. The inference from it was wrong. "This mark is too
    // small to read" argues for drawing it bigger, not for deleting it, and
    // there is room: below 120px the border and scanlines have already gone, so
    // the screen is emptier than at full size, not busier.
    //
    // So they scale up as the screen shrinks and keep a floor far below, at the
    // point where the face itself stops reading at all.
    glyph: screenPx >= 60,
    glyphScale: screenPx >= 140 ? 1 : Math.min(2.1, 1 + (140 - screenPx) / 70),
    progress: screenPx >= 70,
    // Fewer, fatter blocks rather than nine that alias into a grey smear.
    progressCells: screenPx >= 140 ? 9 : 5,

    scanlines: screenPx >= 100,
    sweat: screenPx >= 90,
    effort: screenPx >= 90,
    blush: screenPx >= 85,
    stipple: screenPx >= 85,
    // Strokes thicken as the character shrinks, so the face keeps its weight.
    bold: screenPx >= 140 ? 1 : Math.min(2.1, 1 + (140 - screenPx) / 75),
  };
}

const FULL_DETAIL = {
  screenPx: Infinity,
  border: true, glyph: true, progress: true, blush: true,
  stipple: true, sweat: true, effort: true, scanlines: true, bold: 1,
  glyphScale: 1, progressCells: 9,
};

export function drawFace(
  ctx,
  { w, h, radius, cream },
  ch,
  { time = 0, reducedMotion = false, glyph = null, lod = FULL_DETAIL } = {},
) {
  fillScreen(ctx, w, h, radius, cream);

  // Binary border, drawn under the face.
  const cell = Math.max(8, Math.round(w / 26));
  if (lod.border) {
  ctx.save();
  ctx.fillStyle = INK;
  ctx.textBaseline = 'top';
  const speed = reducedMotion ? 0 : ch.binaryRain;
  const phase = Math.floor(time * speed * 4);
  for (const c of binaryBorderCells(w, h, cell)) {
    const bit = ((c.i * 2654435761) ^ (phase * 40503)) >>> 0;
    if (c.block) {
      ctx.globalAlpha = 0.92;
      const bs = cell * 0.7 * c.scale;
      ctx.fillRect(c.x + (cell - bs) / 2, c.y + (cell - bs) / 2, bs, bs);
      continue;
    }
    ctx.globalAlpha = 0.45 + 0.5 * (((bit >>> 8) & 0xff) / 255);
    ctx.font = `${Math.round(cell * 0.92 * c.scale)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.fillText(bit & 1 ? '1' : '0', c.x + cell * 0.16, c.y);
  }
  ctx.restore();
  }

  // Face space -> canvas pixels: x in [-1,1] spans the width.
  const scale = w / 2;
  ctx.save();
  ctx.translate(w / 2, h / 2);
  ctx.scale(scale, scale);

  // Blush sits under the ink. Stippled, not a flat ellipse — a flat grey oval
  // reads as a smudge, and dialling its alpha down to hide that just makes it
  // invisible. At cheek scale the halftone dots resolve properly.
  const blushDots = lod.blush ? blushGeometry(ch) : [];
  if (blushDots.length) {
    ctx.save();
    ctx.globalAlpha = Math.min(1, 0.72 * ch.blush);
    ctx.fillStyle = INK;
    for (const [bx, by, br] of blushDots) {
      ctx.beginPath();
      ctx.arc(bx, by, br, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  const g = faceGeometry(ch, { bold: lod.bold });
  ctx.fillStyle = INK;

  // Halftone shading on the lids, drawn UNDER the eye ink so the eye's own
  // outline stays the crispest thing in the region. Gated by LOD along with the
  // other stipple, since below ~85px the dots fall under a device pixel.
  if (lod.stipple) {
    ctx.save();
    for (const dots of g.eyeShade ?? []) {
      for (const [sx, sy, sr, alpha] of dots) {
        ctx.globalAlpha = Math.min(1, 0.66 * alpha);
        ctx.beginPath();
        ctx.arc(sx, sy, sr, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.restore();
  }

  for (const brow of g.brows) fillPoly(ctx, brow);
  for (const eye of g.eyes) {
    // Nested shapes: ink outline, cream sclera, dark iris, cream highlight.
    ctx.fillStyle = INK;
    fillPoly(ctx, eye.outline);
    if (eye.sclera) {
      ctx.fillStyle = cream;
      fillPoly(ctx, eye.sclera);
      ctx.fillStyle = INK;
      fillPoly(ctx, eye.pupil);
      ctx.fillStyle = cream;
      fillPoly(ctx, eye.highlight);
      ctx.fillStyle = INK;
    }
  }
  // Lower-lid lines sit under the eyes, lighter than the eye's own ink.
  if (g.lowerLids) {
    ctx.save();
    ctx.globalAlpha = 0.7;
    for (const lid of g.lowerLids) if (lid) fillPoly(ctx, lid);
    ctx.restore();
  }

  fillPoly(ctx, g.mouth.outline);
  // The lower lip sits under the mouth and is lighter than the ink of the mouth
  // itself — it suggests form rather than drawing a second mouth.
  if (g.mouth.lowerLip) {
    ctx.save();
    ctx.globalAlpha = 0.62;
    fillPoly(ctx, g.mouth.lowerLip);
    ctx.restore();
  }
  if (g.mouth.tongue) {
    ctx.fillStyle = cream;
    fillPoly(ctx, g.mouth.tongue);
    ctx.fillStyle = INK;
  }
  // Teeth are drawn after the mouth so they sit inside it.
  if (g.teeth) {
    ctx.fillStyle = cream;
    for (const [tx, ty, tw, th] of g.teeth) ctx.fillRect(tx, ty, tw, th);
    ctx.fillStyle = INK;
  }

  // Strain lines radiating from the upper corners.
  if (lod.effort && g.effort.length) {
    ctx.save();
    ctx.globalAlpha = Math.min(1, 0.45 + 0.5 * ch.effort);
    ctx.strokeStyle = INK;
    ctx.lineWidth = 0.016;
    ctx.lineCap = 'round';
    for (const [[x0, y0], [x1, y1]] of g.effort) {
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Sweat beads. Phase is driven by time so no state is held here.
  //
  // Drawn as an ink OUTLINE with a light interior and a highlight — in a
  // pen-and-ink world that is how water reads. A solid filled shape reads as a
  // pebble, not a droplet.
  const beads = lod.sweat ? sweatGeometry(ch, reducedMotion ? 0.35 : (time * 0.75) % 1) : [];
  for (const bead of beads) {
    ctx.save();
    ctx.globalAlpha = bead.alpha;
    ctx.beginPath();
    ctx.moveTo(bead.pts[0][0], bead.pts[0][1]);
    for (let i = 1; i < bead.pts.length; i++) ctx.lineTo(bead.pts[i][0], bead.pts[i][1]);
    ctx.closePath();
    ctx.fillStyle = cream;
    ctx.fill();
    ctx.strokeStyle = INK;
    ctx.lineWidth = 0.014;
    ctx.lineJoin = 'round';
    ctx.stroke();
    // A highlight glint, offset up and left like the eye highlights.
    if (bead.glint) {
      ctx.fillStyle = INK;
      ctx.globalAlpha = bead.alpha * 0.55;
      ctx.beginPath();
      ctx.arc(bead.glint[0], bead.glint[1], bead.glint[2], 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  ctx.restore();

  // Status glyph: '?' when confused, a pixel ellipsis when thinking.
  if (lod.glyph && glyph && ch.glyphOpacity > 0.03) {
    ctx.save();
    ctx.globalAlpha = Math.min(1, ch.glyphOpacity);
    ctx.fillStyle = INK;
    // Sits between the brow and the border, clear of both. Grows as the screen
    // shrinks so it stays readable at avatar sizes (see levelOfDetail).
    const base = w * 0.16;
    const size = Math.round(base * (lod.glyphScale ?? 1));
    // Grow inward from a fixed right edge, or an enlarged glyph runs off the
    // screen. At full detail `size === base` and this is exactly w * 0.82.
    const gx = w * 0.82 - (size - base) * 0.5;
    const gy = h * 0.32;

    if (glyph === PIXEL_ELLIPSIS) {
      // Drawn as geometry, not text: as type, three dots are the finest marks on
      // the screen and the first thing a 10:1 downscale destroys. As blocks they
      // keep the shape that means "thinking" while carrying enough mass to
      // survive it — and they match the border and progress meter, which are
      // already chunky blocks rather than fine marks.
      const dot = size * 0.2;
      const gap = dot * 0.55;
      const span = dot * 3 + gap * 2;
      for (let i = 0; i < 3; i++) {
        ctx.fillRect(
          Math.round(gx - span / 2 + i * (dot + gap)),
          Math.round(gy - dot / 2),
          Math.round(dot),
          Math.round(dot),
        );
      }
    } else {
      ctx.font = `bold ${size}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'center';
      ctx.fillText(glyph, gx, gy);
    }
    ctx.restore();
  }

  // Progress meter: what makes "working" legible as a task rather than a mood.
  //
  // Drawn as chunky pixel blocks in the same language as the binary border, NOT
  // as a bordered bar. A thin-stroked rectangle with segments is 1990s UI chrome
  // and clashes badly with 1930s pen-and-ink — the character's whole premise is
  // that its face is a retro CRT, so its readouts must live in that world.
  if (lod.progress && ch.progress > 0.01) {
    ctx.save();
    const cells = lod.progressCells ?? 9;
    // Keep the meter's overall width roughly constant as the count drops, so
    // fewer cells means fatter ones rather than a shorter bar.
    const blk = cell * 0.92 * (cells < 9 ? 1.5 : 1);
    const gap = blk * 0.34;
    const totalW = cells * blk + (cells - 1) * gap;
    const bx = (w - totalW) / 2;
    const by = h * 0.8;
    const filled = Math.round(cells * Math.min(1, ch.progress));
    ctx.fillStyle = INK;
    for (let i = 0; i < cells; i++) {
      const x = bx + i * (blk + gap);
      if (i < filled) {
        ctx.globalAlpha = 0.9;
        ctx.fillRect(x, by, blk, blk);
      } else {
        // Remaining cells are hollow pixel outlines, matching the border's weight.
        ctx.globalAlpha = 0.4;
        ctx.fillRect(x, by, blk, blk * 0.18);
        ctx.fillRect(x, by + blk * 0.82, blk, blk * 0.18);
        ctx.fillRect(x, by, blk * 0.18, blk);
        ctx.fillRect(x + blk * 0.82, by, blk * 0.18, blk);
      }
    }
    ctx.restore();
  }

  // Scanlines.
  if (lod.scanlines && ch.scanlines > 0.01) {
    ctx.save();
    ctx.globalAlpha = 0.1 * ch.scanlines;
    ctx.fillStyle = INK;
    for (let y = 0; y < h; y += 4) ctx.fillRect(0, y, w, 1);
    ctx.restore();
  }

  // Glitch: horizontal slice displacement, the classic CRT tear.
  if (!reducedMotion && ch.glitch > 0.02) {
    const slices = 4;
    for (let i = 0; i < slices; i++) {
      const sy = Math.floor(((Math.sin(time * 53 + i * 2.1) * 0.5 + 0.5) * (h - 20)));
      const sh = Math.max(3, Math.round(h * 0.03));
      const dx = Math.sin(time * 71 + i) * w * 0.06 * ch.glitch;
      ctx.drawImage(ctx.canvas, 0, sy, w, sh, dx, sy, w, sh);
    }
  }

  // Flicker. Frequency is held below the WCAG three-flashes-per-second bound.
  if (!reducedMotion && ch.flicker > 0.01) {
    const f = 0.07 * ch.flicker * (0.5 + 0.5 * Math.sin(time * 2 * Math.PI * 2.5));
    ctx.save();
    ctx.globalAlpha = Math.max(0, f);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, w, h);
    ctx.restore();
  }
}
