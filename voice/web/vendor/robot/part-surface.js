import { ledClusterRect } from './led-render.js';

// Mount one canvas per part, positioned in body-image space.
//
// The mascot mounts a single canvas and maps it through a homography, because
// its monitor is drawn at an angle. These portraits are frontal, so a lens is a
// near-circle and needs only a translate and a scale. The projective path stays
// available for any part that genuinely is skewed — `screen` still uses it.
//
// Why one canvas PER PART rather than one over the whole portrait: a lens canvas
// is ~200px square, so clearing and redrawing it every frame is nothing. A
// body-sized canvas is 1024x1280, and clearing that 60 times a second to move
// two small discs is most of a frame's budget spent on empty pixels. The cost
// is one compositor layer per part, which is why parts are counted in single
// digits and LEDs share one surface rather than getting one each.

/**
 * Geometry for a part's canvas, in body-image pixels.
 *
 * Extracted from mounting so it can be unit-tested with no DOM. Returns the
 * canvas rect and the offset of the part's origin within it.
 */
export function surfaceRect(part) {
  // One surface for the whole LED cluster, not one per lamp. Three 20px lamps
  // would otherwise cost three compositor layers to say one thing, and they
  // always change together anyway.
  if (part.type === 'leds') return ledClusterRect(part);
  if (part.type === 'lens') {
    const pad = part.bloomPx ?? 0;
    const half = part.radius + pad;
    return {
      x: part.center[0] - half,
      y: part.center[1] - half,
      width: half * 2,
      height: half * 2,
      // Where the part's own (0,0) sits inside the canvas.
      originX: half,
      originY: half,
    };
  }
  throw new Error(`part-surface: no rect rule for type "${part.type}"`);
}

/**
 * Styles the geometry depends on. Applied here rather than left to a stylesheet
 * because a host that vendors these modules and builds its own DOM gets none of
 * a stylesheet — a failure this project has already had once, when nano-claw
 * copied the modules and the canvas laid out 908px below the character because
 * it was `position: static`.
 */
export function applySurfaceStyles(canvas) {
  canvas.style.position = 'absolute';
  canvas.style.left = '0';
  canvas.style.top = '0';
  canvas.style.transformOrigin = '0 0';
  canvas.style.pointerEvents = 'none';
}

export function createPartSurface({ part, manifest, container }) {
  const canvas = document.createElement('canvas');
  canvas.className = `part part-${part.type}`;
  canvas.dataset.partId = part.id;
  canvas.setAttribute('aria-hidden', 'true');
  applySurfaceStyles(canvas);
  canvas.style.zIndex = String(part.z);
  container.append(canvas);

  const rect = surfaceRect(part);
  const scale = part.canvasScale ?? 2;
  canvas.width = Math.round(rect.width * scale);
  canvas.height = Math.round(rect.height * scale);
  const ctx = canvas.getContext('2d');

  let displayScale = 1;
  // Rotation inherited from the head. A lens is part of the face, so it has to
  // tilt with it — without this the eyes hold level while the head leans, which
  // is the single most obvious way a head layer can look broken.
  let inherited = '';

  /**
   * Position the canvas for the current display size.
   *
   * `displayScale` is the ratio of rendered width to the manifest's coordinate
   * space, so the manifest's numbers never need recalibrating when a smaller
   * image is served — the same property `rig-config.js` documents for the
   * mascot's quad.
   */
  function place() {
    const s = displayScale / scale;
    canvas.style.transform =
      inherited +
      `translate(${(rect.x * displayScale).toFixed(2)}px, ${(rect.y * displayScale).toFixed(2)}px) ` +
      `scale(${s.toFixed(5)})`;
  }

  function layout(displayWidth) {
    displayScale = displayWidth / manifest.body.width;
    place();
  }

  /**
   * Hand a caller a context whose origin is the part's own origin, in body
   * pixels. Renderers then draw in the coordinate space the manifest describes
   * and never need to know about device pixels or display scaling.
   */
  function paint(draw) {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(scale, 0, 0, scale, rect.originX * scale, rect.originY * scale);
    draw(ctx);
  }

  return {
    id: part.id,
    part,
    canvas,
    layout,
    paint,
    setInherited(prefix) {
      if (prefix === inherited) return;
      inherited = prefix;
      place();
    },
    destroy: () => canvas.remove(),
  };
}
