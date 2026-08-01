const CORNERS = ['tl', 'tr', 'br', 'bl'];
const HEX = /^#[0-9a-fA-F]{6}$/;

function num(v, path) {
  if (typeof v !== 'number' || !Number.isFinite(v)) {
    throw new Error(`rig.json: ${path} must be a finite number`);
  }
  return v;
}

function point(v, path) {
  if (!Array.isArray(v) || v.length !== 2) {
    throw new Error(`rig.json: ${path} must be [x, y]`);
  }
  return [num(v[0], `${path}[0]`), num(v[1], `${path}[1]`)];
}

export function parseRigConfig(raw) {
  if (!raw || typeof raw !== 'object') throw new Error('rig.json: not an object');
  const s = raw.screen;
  if (!s || typeof s !== 'object') throw new Error('rig.json: screen missing');
  if (!s.quad || typeof s.quad !== 'object') throw new Error('rig.json: screen.quad missing');

  const quad = {};
  for (const k of CORNERS) {
    if (!(k in s.quad)) throw new Error(`rig.json: screen.quad.${k} missing`);
    quad[k] = point(s.quad[k], `screen.quad.${k}`);
  }
  if (!HEX.test(s.cream ?? '')) {
    throw new Error('rig.json: screen.cream must be a #rrggbb hex colour');
  }
  const b = raw.body ?? {};
  if (typeof b.src !== 'string' || !b.src) throw new Error('rig.json: body.src missing');

  return {
    version: raw.version ?? 1,
    seed: raw.seed ?? 1,
    body: {
      src: b.src,
      // Optional responsive sources. The intrinsic size of whatever is served is
      // irrelevant to the quad maths: `width`/`height` define the COORDINATE
      // SPACE the quad is expressed in, and the rig scales by
      // clientWidth / body.width. So a smaller file needs no recalibration.
      srcset: typeof b.srcset === 'string' ? b.srcset : null,
      sizes: typeof b.sizes === 'string' ? b.sizes : null,
      width: num(b.width, 'body.width'),
      height: num(b.height, 'body.height'),
    },
    screen: {
      quad,
      outsetPx: s.outsetPx ?? 0,
      cornerRadiusPx: s.cornerRadiusPx ?? 0,
      cream: s.cream,
      canvasScale: s.canvasScale ?? 1,
    },
  };
}

/** Push every corner radially away from the quad's centroid. */
export function outsetQuad(quad, outsetPx) {
  if (!outsetPx) return quad;
  const pts = CORNERS.map((k) => quad[k]);
  const cx = pts.reduce((a, p) => a + p[0], 0) / 4;
  const cy = pts.reduce((a, p) => a + p[1], 0) / 4;
  const out = {};
  for (const k of CORNERS) {
    const [x, y] = quad[k];
    const d = Math.hypot(x - cx, y - cy) || 1;
    out[k] = [x + ((x - cx) / d) * outsetPx, y + ((y - cy) / d) * outsetPx];
  }
  return out;
}
