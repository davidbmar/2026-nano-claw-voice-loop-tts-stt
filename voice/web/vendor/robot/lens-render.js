// Drawing a glowing lens over a photograph of a glowing lens.
//
// The mascot's founding trick is that filling the CRT with its own cream both
// ERASES the face baked into the artwork and provides a surface to draw on. That
// transfers here, with one boundary that has to be respected absolutely:
//
//   Inside the dome radius, an opaque radial fill erases the photographed glow
//   completely, so the filament can be drawn anywhere within it.
//
//   Outside the dome radius there is photographed METAL, and no flat colour
//   erases that. Nothing opaque may be drawn there — only additive bloom, which
//   reads as light spilling onto the bezel because that is what it is.
//
// An earlier design moved the whole lens canvas to fake head parallax. That
// crosses the boundary: it slides the painted dome off the photographed one and
// puts two eyes on the robot. The bezel is a hard edge, not a soft guideline.

/** Geometry only, so the maths is testable without a canvas. */
export function lensGeometry(part, ch = {}) {
  const r = part.radius;

  // The lid closes from the top, as a mechanical shutter does. `eyeOpen` is the
  // autonomic blink channel (1 = open); squint closes from BOTH edges and is
  // driven by emotion, so the two compose rather than fight — a blink during a
  // held squint shuts the aperture while the squint stays put.
  const open = clamp01(ch.eyeOpen ?? 1);
  const squint = clamp01(ch.squint ?? 0);

  // Blade travel, as a fraction of the dome's diameter, per edge.
  //
  // The blink coefficient is exactly 1, not 0.92: at 0.92 a full blink left an
  // 8% slit of lit dome showing, so the eye never actually shut. It looked like
  // a blink in motion and read as a stuck shutter when held. The test asserts
  // closure rather than movement, which is the only way that shows up.
  const topTravel = (1 - open) + squint * 0.28;
  const bottomTravel = squint * 0.22;

  // The filament rides `pupilX`/`pupilY`, but only across the inner two-fifths
  // of the dome. Past that the hot core touches the rim and the lens reads as
  // broken rather than as looking somewhere.
  const travel = r * 0.4;
  const hotX = clampSigned(ch.gazeX ?? 0) * travel;
  const hotY = clampSigned(ch.gazeY ?? 0) * travel;

  return {
    radius: r,
    hot: [hotX, hotY],
    // Core size tracks `glow`. A dim lamp is not a small lamp, so the floor is
    // well above zero — the core shrinks and cools, it does not vanish.
    coreRadius: r * (0.16 + 0.1 * clamp01(ch.glow ?? 0.7)),
    topLidY: -r + topTravel * 2 * r,
    bottomLidY: r - bottomTravel * 2 * r,
    // Aperture fully shut. Below this the blades overlap and drawing both
    // produces a seam artifact where they cross.
    shut: topTravel + bottomTravel >= 1,
  };
}

const clamp01 = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
const clampSigned = (v) => (Number.isFinite(v) ? Math.max(-1, Math.min(1, v)) : 0);

/**
 * How bright the lamp is right now, including its instabilities.
 *
 * `sweat` maps to flicker because an unstable supply is what a machine's version
 * of stress looks like. Frequency is held below the WCAG three-flashes-per-second
 * bound, matching the rule `face-render.js` already applies to its own flicker —
 * this is a photosensitivity limit, not a style choice.
 */
export function lensBrightness(ch = {}, time = 0) {
  // Floor at 0.62, not 0.35. The first version put neutral at 0.72 and then
  // composited 28% black across the entire dome to get there, which read as a
  // lamp seen through smoked glass rather than as a lit one. A filament at rest
  // is still a filament; the range is "warm" to "running hot", not "off" to
  // "on". Being off is a separate state with its own shape.
  const base = 0.62 + 0.38 * clamp01(ch.glow ?? 0.7);
  const unstable = clamp01(ch.sweat ?? 0);
  if (unstable < 0.01) return base;
  const wobble = Math.sin(time * 2 * Math.PI * 2.4) * 0.5 + Math.sin(time * 2 * Math.PI * 1.1) * 0.5;
  return Math.max(0.08, base * (1 - unstable * 0.4 * (0.5 + 0.5 * wobble)));
}

/**
 * Blend the lamp's colour toward cool white as `mouthCorner` rises.
 *
 * Warmth is the robot's version of a smile: an amber filament is a comfortable
 * one, a white-hot one is running hard. `mouthCorner` is the channel every
 * emotion in `expressions.js` actually drives (`mouthCurve` exists but nothing
 * writes to it), so this reads real expression data rather than a dead channel.
 */
export function glowStops(part, ch = {}) {
  const warmth = clampSigned(ch.hue ?? 0);
  return {
    core: mix(part.glow.core, warmth > 0 ? '#e8f4ff' : '#ffd9a0', Math.abs(warmth) * 0.55),
    mid: mix(part.glow.mid, warmth > 0 ? '#8fc8ff' : '#ff7a18', Math.abs(warmth) * 0.5),
    rim: part.glow.rim,
  };
}

function mix(a, b, t) {
  const k = Math.max(0, Math.min(1, t));
  const [ar, ag, ab] = rgb(a);
  const [br, bg, bb] = rgb(b);
  const to = (x, y) => Math.round(x + (y - x) * k);
  return `#${[to(ar, br), to(ag, bg), to(ab, bb)].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

function rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

/**
 * Draw one lens. The context is pre-translated so (0,0) is the dome centre and
 * one unit is one body-space pixel.
 */
export function drawLens(ctx, part, ch = {}, time = 0) {
  const g = lensGeometry(part, ch);
  const stops = glowStops(part, ch);
  const bright = lensBrightness(ch, time);
  const r = g.radius;

  ctx.save();

  // 1. Bloom. Drawn FIRST and additively so it lands under everything and
  //    spills past the dome onto the photographed bezel without covering it.
  ctx.globalCompositeOperation = 'lighter';
  const bloom = ctx.createRadialGradient(g.hot[0], g.hot[1], r * 0.2, 0, 0, r + part.bloomPx);
  bloom.addColorStop(0, rgba(stops.mid, 0.28 * bright));
  bloom.addColorStop(0.65, rgba(stops.mid, 0.11 * bright));
  bloom.addColorStop(1, rgba(stops.mid, 0));
  ctx.fillStyle = bloom;
  ctx.fillRect(-r - part.bloomPx, -r - part.bloomPx, (r + part.bloomPx) * 2, (r + part.bloomPx) * 2);

  // 2. Everything below is opaque and must stay inside the dome.
  ctx.globalCompositeOperation = 'source-over';
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.clip();

  // The dome itself, which erases the photographed glow. Brightness is baked
  // into the STOPS rather than applied afterward as a black wash: a wash dims
  // the hot core and the dark rim by the same amount, which is not what dimming
  // a lamp does. Scaling the stops keeps the rim dark and lets the core carry
  // the change, so a dim lamp reads as a cooler filament rather than as the same
  // filament behind smoked glass.
  const dome = ctx.createRadialGradient(g.hot[0], g.hot[1], 0, g.hot[0], g.hot[1], r);
  // Amber from edge to edge. The dome carries NO white at all — every stop is
  // the mid colour, falling off toward the rim.
  //
  // The first version put `core` (near-white) at stop 0, which made the inner
  // fifth of the lens grey before the gradient ever reached amber, and the
  // additive hotspot then piled white on top of grey. The result read as a
  // beige panel with a lamp behind it. White belongs in exactly one place: the
  // additive hotspot below, which is small and sits on top of full-strength
  // amber. That is what a filament looks like.
  dome.addColorStop(0, scaleColor(stops.mid, bright * 1.18));
  dome.addColorStop(0.45, scaleColor(stops.mid, bright * 0.95));
  dome.addColorStop(0.82, scaleColor(stops.mid, bright * 0.52));
  dome.addColorStop(1, scaleColor(stops.rim, Math.min(1, bright)));
  ctx.fillStyle = dome;
  ctx.globalAlpha = 1;
  ctx.fillRect(-r, -r, r * 2, r * 2);

  if (part.mesh) drawMesh(ctx, part, r);

  // The hot core sits ON TOP of the mesh, additively. A filament is brighter
  // than the wires in front of it, so at the centre the wires wash out — that
  // blow-out is what makes it read as genuinely bright rather than as a
  // light-coloured circle. Drawn after the mesh for exactly that reason.
  ctx.globalCompositeOperation = 'lighter';
  // Kept deliberately SMALL — 1.5x the core, not 2.6x. At 2.6 the blow-out
  // reached past half the dome and the lens went uniformly pale: the amber that
  // makes it read as a warm filament was being erased by the very highlight
  // meant to sit on top of it. A hotspot has to be small to read as hot.
  const hot = ctx.createRadialGradient(
    g.hot[0], g.hot[1], 0, g.hot[0], g.hot[1], g.coreRadius * 1.5,
  );
  hot.addColorStop(0, rgba(stops.core, 0.9 * bright));
  hot.addColorStop(0.45, rgba(stops.core, 0.3 * bright));
  hot.addColorStop(1, rgba(stops.core, 0));
  ctx.fillStyle = hot;
  ctx.fillRect(-r, -r, r * 2, r * 2);
  ctx.globalCompositeOperation = 'source-over';

  drawBlades(ctx, g, r);

  ctx.restore();
}

/**
 * Latitude arcs plus meridians — the grille over the dome.
 *
 * This is what makes the lens read as HARDWARE rather than as a glowing circle.
 *
 * Drawn as single curved ARCS, not closed ellipses. The first version stroked
 * `ellipse(...)` for each wire, which outlines a shape: every bar came out as
 * two parallel lines and every meridian as a full oval, and the lens read as a
 * wire basket. A wire wrapped round a sphere projects to ONE curve, bowing away
 * from the equator — so each is a quadratic through three points.
 *
 * The bars also carry a highlight below them. A real wire in front of a lamp is
 * lit along its lower edge by the light behind it, and that single detail is
 * most of what separates "drawn on" from "in there".
 */
function drawMesh(ctx, part, r) {
  const { bars, meridians, color, widthPx } = part.mesh;
  ctx.lineCap = 'round';

  for (let i = 1; i < bars; i++) {
    const t = (i / bars) * 2 - 1;
    const y = t * r * 0.9;
    const halfWidth = Math.sqrt(Math.max(0, 1 - t * t)) * r * 0.93;
    if (halfWidth < r * 0.12) continue;
    // Wires bow away from the equator, so sag follows the latitude's sign.
    const sag = t * r * 0.08;

    // 0.14, not 0.30. At 0.30 the seven highlights together laid enough cream
    // across the dome to desaturate the whole lens — the detail meant to make
    // one wire read as lit was washing out the lamp it sat in front of.
    ctx.strokeStyle = 'rgba(255, 232, 186, 0.14)';
    ctx.lineWidth = widthPx * 0.7;
    arc(ctx, -halfWidth, y + widthPx * 0.9, halfWidth, y + sag + widthPx * 0.9);

    ctx.strokeStyle = color;
    ctx.lineWidth = widthPx;
    arc(ctx, -halfWidth, y, halfWidth, y + sag);
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = widthPx * 0.9;
  for (let i = 0; i < meridians; i++) {
    const t = meridians === 1 ? 0 : (i / (meridians - 1)) * 2 - 1;
    // Bow gently. At 1.75x the meridians ballooned outward and read as the
    // staves of a barrel rather than as longitude on a sphere.
    const x = t * r * 0.42;
    const h = r * 0.9;
    ctx.beginPath();
    ctx.moveTo(x * 0.7, -h);
    ctx.quadraticCurveTo(x * 1.22, 0, x * 0.7, h);
    ctx.stroke();
  }
}

/** One wire: a quadratic bowing to `midY` between two rim points. */
function arc(ctx, x0, y0, x1, midY) {
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.quadraticCurveTo(0, midY, x1, y0);
  ctx.stroke();
}

/** Mechanical shutter blades, closing from the rim. Never drawn past it. */
function drawBlades(ctx, g, r) {
  ctx.fillStyle = '#100b08';
  if (g.shut) {
    ctx.fillRect(-r, -r, r * 2, r * 2);
    return;
  }
  if (g.topLidY > -r) ctx.fillRect(-r, -r, r * 2, g.topLidY + r);
  if (g.bottomLidY < r) ctx.fillRect(-r, g.bottomLidY, r * 2, r - g.bottomLidY);
}

function rgba(hexColor, a) {
  const [r, g, b] = rgb(hexColor);
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, a))})`;
}

/** Multiply a colour's channels, for baking brightness into a gradient stop. */
function scaleColor(hexColor, k) {
  const f = Math.max(0, k);
  const [r, g, b] = rgb(hexColor).map((v) => Math.round(Math.min(255, v * f)));
  return `rgb(${r}, ${g}, ${b})`;
}
