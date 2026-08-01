// Drawing status lamps over the photograph.
//
// Same rule as the lenses, and the reason it holds here too: a lamp's face is
// self-luminous, so an opaque fill inside its radius erases whatever the
// photograph baked there. Outside the radius is metal, and only additive bloom
// goes there — which is physically what a lit lamp does to the panel around it.
//
// This is what lets a lit amber LED in the artwork be re-rendered as a pulsing
// blue one, and a dark port be switched on. "Power on what's there, never paint
// on what isn't": the geometry comes from the photograph, only the value changes.

/** One cluster shares a surface. Its bounds, in body pixels. */
export function ledClusterRect(part) {
  const pad = part.bloomPx ?? 26;
  const xs = part.lamps.map((l) => l.center[0]);
  const ys = part.lamps.map((l) => l.center[1]);
  const r = Math.max(...part.lamps.map((l) => l.radius));
  const x = Math.min(...xs) - r - pad;
  const y = Math.min(...ys) - r - pad;
  return {
    x,
    y,
    width: Math.max(...xs) + r + pad - x,
    height: Math.max(...ys) + r + pad - y,
    originX: -x,
    originY: -y,
  };
}

/**
 * Draw the cluster. Context origin is the BODY origin, so lamp centres are used
 * as authored — a cluster is a handful of lamps scattered across a chest, and
 * re-basing each one to a local origin buys nothing but a chance to get it wrong.
 */
export function drawLeds(ctx, part, { color, levels }) {
  ctx.save();
  const [cr, cg, cb] = rgb(color);

  part.lamps.forEach((lamp, i) => {
    const level = levels[i] ?? 0;
    const [x, y] = lamp.center;
    const r = lamp.radius;

    // Bloom first and additive, so it spills onto the photographed panel
    // without covering it. A dark lamp still gets a trace, because a real
    // indicator at rest is not a hole.
    ctx.globalCompositeOperation = 'lighter';
    const bloom = ctx.createRadialGradient(x, y, r * 0.4, x, y, r + (part.bloomPx ?? 26));
    bloom.addColorStop(0, `rgba(${cr}, ${cg}, ${cb}, ${(0.5 * level).toFixed(3)})`);
    bloom.addColorStop(0.5, `rgba(${cr}, ${cg}, ${cb}, ${(0.16 * level).toFixed(3)})`);
    bloom.addColorStop(1, `rgba(${cr}, ${cg}, ${cb}, 0)`);
    ctx.fillStyle = bloom;
    ctx.fillRect(x - r * 4, y - r * 4, r * 8, r * 8);

    // The lens face: opaque, so it erases whatever colour the artwork baked in.
    ctx.globalCompositeOperation = 'source-over';
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    const face = ctx.createRadialGradient(x - r * 0.25, y - r * 0.3, 0, x, y, r);
    const k = 0.22 + 0.78 * level;
    face.addColorStop(0, scale(color, Math.min(1.7, k * 1.6)));
    face.addColorStop(0.55, scale(color, k));
    face.addColorStop(1, scale(color, k * 0.34));
    ctx.fillStyle = face;
    ctx.fill();

    // A specular pip. Tiny, but it is what makes the lamp read as a domed lens
    // rather than as a flat coloured disc — the same reason the mesh matters on
    // the big lenses.
    ctx.globalCompositeOperation = 'lighter';
    ctx.beginPath();
    ctx.arc(x - r * 0.3, y - r * 0.34, r * 0.26, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(255, 255, 255, ${(0.25 + 0.5 * level).toFixed(3)})`;
    ctx.fill();
  });

  ctx.restore();
}

function rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

function scale(h, k) {
  const [r, g, b] = rgb(h).map((v) => Math.round(Math.min(255, v * Math.max(0, k))));
  return `rgb(${r}, ${g}, ${b})`;
}
