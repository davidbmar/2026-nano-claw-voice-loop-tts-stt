// A character is a raster portrait plus a list of PARTS drawn over it.
//
// This generalises `rig-config.js`, which describes exactly one part — the
// mascot's CRT screen — and hard-codes that description into the top level of
// the object. Robots have seven or eight parts each, so the shape has to be a
// list. The mascot's screen becomes an ordinary entry in that list, which is
// what stops it being a special case forever.
//
// v1 documents still parse: `migrateV1` lifts a rig.json into this shape, so
// `public/rig.json` stays the source of truth for the mascot and its tests keep
// asserting against the file they already assert against.

const CORNERS = ['tl', 'tr', 'br', 'bl'];
const HEX = /^#[0-9a-fA-F]{6}$/;
const PART_TYPES = new Set(['screen', 'lens', 'grille', 'leds', 'antenna', 'head']);
/** Types that mount their own layers rather than a plain part-surface canvas. */
export const CUSTOM_SURFACE = new Set(['antenna', 'head']);

// A part's transform can compose with one other layer's. `body` means "no
// composition" and is the default; `head` means the part rides the head.
//
// This is a flat field rather than DOM nesting on purpose. A transformed
// element establishes a stacking context, so a lens nested inside the head
// element could never paint above a SIBLING collar — and the collar painting
// above the head is the whole mechanism that hides the neck seam. Flat siblings
// with explicit z and an explicit `follows` keep both properties.
const FOLLOWS = new Set(['body', 'head']);

function num(v, path) {
  if (typeof v !== 'number' || !Number.isFinite(v)) {
    throw new Error(`manifest: ${path} must be a finite number`);
  }
  return v;
}

function positive(v, path) {
  const n = num(v, path);
  if (n <= 0) throw new Error(`manifest: ${path} must be greater than zero`);
  return n;
}

function point(v, path) {
  if (!Array.isArray(v) || v.length !== 2) {
    throw new Error(`manifest: ${path} must be [x, y]`);
  }
  return [num(v[0], `${path}[0]`), num(v[1], `${path}[1]`)];
}

function hex(v, path) {
  if (!HEX.test(v ?? '')) throw new Error(`manifest: ${path} must be a #rrggbb hex colour`);
  return v;
}

function quad(v, path) {
  if (!v || typeof v !== 'object') throw new Error(`manifest: ${path} missing`);
  const out = {};
  for (const k of CORNERS) {
    if (!(k in v)) throw new Error(`manifest: ${path}.${k} missing`);
    out[k] = point(v[k], `${path}.${k}`);
  }
  return out;
}

/**
 * A lens is a circle, not a quad.
 *
 * The mascot needed a projective map because its monitor is drawn at an angle —
 * `TL->TR` is (490,-22) against `BL->BR` of (479,-29), and an affine fit drifts
 * ten pixels at the far corner. These portraits are shot dead-on, so a lens is a
 * near-circle and a centre plus a radius describes it exactly. `quad` stays
 * available per part for anything that genuinely is skewed.
 */
function parseLens(raw, path) {
  return {
    center: point(raw.center, `${path}.center`),
    radius: positive(raw.radius, `${path}.radius`),
    // How far past the glowing dome the canvas extends. The bezel is
    // photographic and must never be drawn on, but the glow BLOOMS past the
    // dome edge, and a canvas clipped exactly at `radius` would cut the bloom
    // into a hard circle — which reads as a sticker, the exact failure this
    // design is trying to avoid.
    bloomPx: raw.bloomPx ?? 18,
    glow: {
      core: hex(raw.glow?.core, `${path}.glow.core`),
      mid: hex(raw.glow?.mid, `${path}.glow.mid`),
      rim: hex(raw.glow?.rim, `${path}.glow.rim`),
    },
    mesh: raw.mesh
      ? {
          bars: num(raw.mesh.bars ?? 7, `${path}.mesh.bars`),
          meridians: num(raw.mesh.meridians ?? 2, `${path}.mesh.meridians`),
          color: hex(raw.mesh.color, `${path}.mesh.color`),
          widthPx: raw.mesh.widthPx ?? 3,
        }
      : null,
    // Which side this lens is, so per-side channels (browLY vs browRY,
    // eyeLSquint vs eyeRSquint) reach the right hardware. Getting this wrong is
    // invisible on a symmetric expression and obvious on `skeptical`.
    side: raw.side === 'right' ? 'right' : 'left',
  };
}

/**
 * An antenna is a region of the photograph that gets lifted out and rotated.
 *
 * `bbox` must contain the whole antenna AND nothing else — any head pixel
 * inside it gets extracted too and swings along, which looks like the robot
 * shedding a piece of its face.
 *
 * `pivot` is the joint, normally on the bbox's bottom edge. Rotating there keeps
 * the base almost stationary, so the patched hole underneath stays covered and
 * no seam opens up.
 */
function parseAntenna(raw, path) {
  const b = raw.bbox;
  if (!Array.isArray(b) || b.length !== 4) {
    throw new Error(`manifest: ${path}.bbox must be [x, y, w, h]`);
  }
  const bbox = [num(b[0], `${path}.bbox[0]`), num(b[1], `${path}.bbox[1]`),
    positive(b[2], `${path}.bbox[2]`), positive(b[3], `${path}.bbox[3]`)];
  const pivot = point(raw.pivot, `${path}.pivot`);
  // A pivot outside its own bbox rotates the layer about empty space and the
  // antenna swings away from the head entirely.
  if (pivot[0] < bbox[0] || pivot[0] > bbox[0] + bbox[2]
    || pivot[1] < bbox[1] || pivot[1] > bbox[1] + bbox[3]) {
    throw new Error(`manifest: ${path}.pivot must lie inside ${path}.bbox`);
  }
  const sign = raw.sign ?? 1;
  if (sign !== 1 && sign !== -1) throw new Error(`manifest: ${path}.sign must be 1 or -1`);
  return {
    bbox,
    pivot,
    sign,
    maxDeg: num(raw.maxDeg ?? 9, `${path}.maxDeg`),
    // A dish sweeps on a clock instead of tracking a channel. Same extraction
    // and same pivot rotation; only what drives the angle differs.
    dish: raw.dish === true,
    threshold: num(raw.threshold ?? 34, `${path}.threshold`),
    side: raw.side === 'right' ? 'right' : 'left',
  };
}

function parsePart(raw, i) {
  const path = `parts[${i}]`;
  if (!raw || typeof raw !== 'object') throw new Error(`manifest: ${path} must be an object`);
  if (typeof raw.id !== 'string' || !raw.id) throw new Error(`manifest: ${path}.id missing`);
  if (!PART_TYPES.has(raw.type)) {
    throw new Error(
      `manifest: ${path}.type "${raw.type}" unknown — expected one of ${[...PART_TYPES].join(', ')}`,
    );
  }
  const follows = raw.follows ?? 'body';
  if (!FOLLOWS.has(follows)) {
    throw new Error(`manifest: ${path}.follows must be one of ${[...FOLLOWS].join(', ')}`);
  }

  const part = {
    id: raw.id,
    type: raw.type,
    z: num(raw.z ?? 0, `${path}.z`),
    follows,
    canvasScale: raw.canvasScale ?? 2,
  };

  if (raw.type === 'lens') Object.assign(part, parseLens(raw, path));
  if (raw.type === 'antenna') Object.assign(part, parseAntenna(raw, path));
  if (raw.type === 'head') {
    part.cutY = positive(raw.cutY, `${path}.cutY`);
    part.collarY = positive(raw.collarY, `${path}.collarY`);
    part.pivot = point(raw.pivot, `${path}.pivot`);
    part.maxRollDeg = num(raw.maxRollDeg ?? 11, `${path}.maxRollDeg`);
    // Where the head's silhouette rule switches from hull (keeps the detached
    // ear pods) to central-run-only (drops the collar ring). Defaults to
    // collarY; a character whose ring tops peek above its collar line sets it
    // a little higher.
    if (raw.centralFromY !== undefined) {
      part.centralFromY = num(raw.centralFromY, `${path}.centralFromY`);
    }
    part.threshold = num(raw.threshold ?? 34, `${path}.threshold`);
    // The collar must start ABOVE the cut, or there is a band where the head
    // layer has ended and the collar has not begun — a horizontal gap straight
    // through the neck, showing the erased plate behind.
    if (part.collarY >= part.cutY) {
      throw new Error(
        `manifest: ${path}.collarY (${part.collarY}) must be above ${path}.cutY (${part.cutY}) `
        + 'so the collar covers the seam',
      );
    }
    // The pivot belongs in the neck, below the collar line. Above it the head
    // swings about a point on its own face.
    if (part.pivot[1] < part.collarY) {
      throw new Error(`manifest: ${path}.pivot must sit at or below collarY`);
    }
  }
  if (raw.type === 'leds') {
    if (!Array.isArray(raw.lamps) || raw.lamps.length === 0) {
      throw new Error(`manifest: ${path}.lamps must be a non-empty array`);
    }
    part.lamps = raw.lamps.map((l, k) => ({
      center: point(l.center, `${path}.lamps[${k}].center`),
      radius: positive(l.radius, `${path}.lamps[${k}].radius`),
    }));
    part.bloomPx = raw.bloomPx ?? 26;
  }
  if (raw.type === 'screen') {
    part.quad = quad(raw.quad, `${path}.quad`);
    part.outsetPx = raw.outsetPx ?? 0;
    part.cornerRadiusPx = raw.cornerRadiusPx ?? 0;
    part.cream = hex(raw.cream, `${path}.cream`);
  }
  return part;
}

export function parseManifest(raw) {
  if (!raw || typeof raw !== 'object') throw new Error('manifest: not an object');
  if (raw.version !== 2) throw new Error(`manifest: version must be 2, got ${raw.version}`);
  if (typeof raw.id !== 'string' || !raw.id) throw new Error('manifest: id missing');

  const b = raw.body ?? {};
  if (typeof b.src !== 'string' || !b.src) throw new Error('manifest: body.src missing');

  if (!Array.isArray(raw.parts) || raw.parts.length === 0) {
    throw new Error('manifest: parts must be a non-empty array');
  }

  const parts = raw.parts.map(parsePart);

  // A duplicate id silently shadows a part: both mount, one is updated, and the
  // other sits frozen on screen looking like a rendering bug rather than a
  // config one.
  const seen = new Set();
  for (const p of parts) {
    if (seen.has(p.id)) throw new Error(`manifest: duplicate part id "${p.id}"`);
    seen.add(p.id);
  }

  // `follows: 'head'` with no head part means the transform composes against
  // nothing, and every dependent part silently stops tracking the head.
  const hasHead = parts.some((p) => p.type === 'head');
  const orphan = parts.find((p) => p.follows === 'head' && !hasHead);
  if (orphan) {
    throw new Error(`manifest: part "${orphan.id}" follows "head", but no head part is declared`);
  }

  return {
    version: 2,
    id: raw.id,
    name: raw.name ?? raw.id,
    seed: raw.seed ?? 1,
    // Rigid characters get squash and biological bob zeroed. A steel head does
    // not deform, and volume-preserving squash on one reads as rubber.
    rigid: raw.rigid === true,
    backdrop: raw.backdrop ? hex(raw.backdrop, 'backdrop') : '#000000',
    body: {
      src: b.src,
      srcset: typeof b.srcset === 'string' ? b.srcset : null,
      sizes: typeof b.sizes === 'string' ? b.sizes : null,
      width: positive(b.width, 'body.width'),
      height: positive(b.height, 'body.height'),
    },
    // Sorted so mount order is paint order and callers never depend on the
    // order somebody happened to type the parts in.
    parts: parts.sort((x, y) => x.z - y.z),
  };
}

/** Lift a v1 rig.json into a v2 manifest, so the mascot is just another character. */
export function migrateV1(raw) {
  if (!raw || typeof raw !== 'object') throw new Error('manifest: not an object');
  if (raw.version !== undefined && raw.version !== 1) {
    throw new Error(`manifest: migrateV1 got version ${raw.version}`);
  }
  const s = raw.screen ?? {};
  return parseManifest({
    version: 2,
    id: 'computer',
    name: 'Computer Mascot',
    seed: raw.seed ?? 1,
    rigid: false,
    body: raw.body,
    parts: [
      {
        id: 'screen',
        type: 'screen',
        z: 1,
        follows: 'body',
        quad: s.quad,
        outsetPx: s.outsetPx,
        cornerRadiusPx: s.cornerRadiusPx,
        cream: s.cream,
        canvasScale: s.canvasScale,
      },
    ],
  });
}

/** Parse either shape. Version 1 documents route through the migration. */
export function loadManifest(raw) {
  if (raw && typeof raw === 'object' && raw.version === 2) return parseManifest(raw);
  return migrateV1(raw);
}
