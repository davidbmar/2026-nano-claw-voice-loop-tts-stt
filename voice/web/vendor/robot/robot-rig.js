// A host-mountable robot character: portrait, extracted hardware and motion.
//
// The harness in `robot-app.js` is deliberately only a host now. Everything
// that makes a robot render belongs here so another application can vendor the
// module, hand it one container, and get the same rig without recreating the
// harness's DOM or depending on its stylesheet.

import { parseManifest } from './character-manifest.js';
import { createPartSurface } from './part-surface.js';
import { createAntennaSurface, antennaAngle, dishAngle } from './antenna-layer.js';
import { createHeadSurface, headAngle } from './head-layer.js';
import { drawLens } from './lens-render.js';
import { drawLeds } from './led-render.js';
import { ledLevels } from './led-patterns.js';
import { lensChannels, antennaChannels, rigidBody } from './robot-channels.js';
import { bodyTransform } from './character-rig.js';
import { createAutonomic, mulberry32 } from './autonomic.js';
import { createSpringSet } from './springs.js';
import { createServo } from './servo.js';
import { EMOTIONS, PRESENCES } from './expressions.js';
import { CHANNELS, neutralChannels } from './face-channels.js';
import { loadImage } from './load-image.js';

const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const clamp01 = (v) => Math.max(0, Math.min(1, v));

async function loadCharacter(manifestUrl) {
  // `no-store`, because a cached manifest is indistinguishable from a bug.
  //
  // The JS modules hot-reload, so edits to them show up immediately — but these
  // manifests are plain JSON fetched at runtime, and the browser will happily
  // serve a stale copy across a normal refresh. Every coordinate lives in here,
  // so a cached one means the calibration you are looking at is not the
  // calibration on disk, and the render looks unchanged for reasons that have
  // nothing to do with the code. That cost a round of "did you actually fix it?"
  // and it should not be able to happen twice.
  const res = await fetch(manifestUrl, { cache: 'no-store' });
  if (!res.ok) throw new Error(`could not load character manifest: ${res.status}`);
  return parseManifest(await res.json());
}

/** Decode the portrait once, off-DOM, so parts can be cut out of its pixels. */
function loadPixels(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`could not decode ${src}`));
    img.src = src;
  });
}

/** Resolve every URL in a manifest srcset without changing its descriptors. */
function resolveSrcset(srcset, resolveAsset) {
  if (!srcset) return null;
  return srcset.split(',').map((candidate) => {
    const match = candidate.trim().match(/^(\S+)(\s+.+)?$/);
    return match ? `${resolveAsset(match[1])}${match[2] ?? ''}` : candidate;
  }).join(', ');
}

/** Geometry the component owns, independent of any host stylesheet. */
function createMount() {
  const wrapper = document.createElement('div');
  wrapper.className = 'robot-rig stage-inner';
  wrapper.style.position = 'relative';
  wrapper.style.transformOrigin = '50% 100%';
  wrapper.style.willChange = 'transform';

  const bodyEl = document.createElement('img');
  bodyEl.className = 'body';
  bodyEl.alt = '';
  bodyEl.setAttribute('aria-hidden', 'true');
  // `inline` puts the image on a text baseline, adding descender space beneath
  // it and shifting every measurement taken from its box.
  bodyEl.style.display = 'block';
  // Constrained to the CONTAINER, never the image's natural size. The harness
  // happened to get this from its stylesheet, so the factory looked correct
  // there and rendered the portrait at a full 1024px inside any host that
  // vendors these modules — the first host mounted it into a 560px camera frame
  // and got a robot the size of the viewport, cropped at both ends. Geometry
  // the layout depends on has to live here, not in CSS a host never loads.
  bodyEl.style.width = '100%';
  bodyEl.style.height = 'auto';

  // Parts live in a flat sibling layer, NOT nested under the head element.
  // A transformed element establishes a stacking context, so a lens nested
  // inside a transformed head could never paint above a sibling collar — and
  // the collar painting above the head is what hides the neck seam.
  const partsEl = document.createElement('div');
  partsEl.className = 'parts';
  partsEl.setAttribute('aria-hidden', 'true');
  partsEl.style.position = 'absolute';
  partsEl.style.inset = '0';
  partsEl.style.pointerEvents = 'none';

  wrapper.append(bodyEl, partsEl);
  return { wrapper, bodyEl, partsEl };
}

function mount(manifest, pixels, bodyEl, partsEl) {
  // Antennas first, and the head last of the custom layers: the head is cut
  // from the original image, so it carries baked antennas unless their erasers
  // are drawn into it. Build order therefore matters, and it is the opposite of
  // paint order (which `z` decides independently).
  const hasHead = manifest.parts.some((p) => p.type === 'head');
  const antennas = manifest.parts
    .filter((p) => p.type === 'antenna')
    .map((part) => createAntennaSurface({
      image: pixels, part, manifest, container: partsEl, mountPatch: !hasHead,
    }));

  const surfaces = manifest.parts.map((part) => {
    if (part.type === 'antenna') return antennas.find((a) => a.id === part.id);
    if (part.type === 'head') {
      return createHeadSurface({
        image: pixels, part, manifest, container: partsEl,
        patches: antennas.map((a) => a.patchSource),
      });
    }
    return createPartSurface({ part, manifest, container: partsEl });
  });

  // The plate replaces the portrait entirely once a head layer exists — leaving
  // the <img> visible would show the original head straight through the hole
  // the plate erased.
  bodyEl.style.visibility = hasHead ? 'hidden' : 'visible';
  return surfaces;
}

/**
 * Mount a robot portrait and its animated hardware into a host-owned element.
 *
 * The factory settles only after the body and extraction copy have decoded and
 * every surface has received its first layout. Rendering remains stopped until
 * the host calls `start()`.
 */
export async function createRobotRig({ container, manifestUrl, resolveAsset }) {
  if (!container || typeof container.append !== 'function') {
    throw new TypeError('createRobotRig: container is required');
  }
  if (typeof resolveAsset !== 'function') {
    throw new TypeError('createRobotRig: resolveAsset must be a function');
  }

  const manifest = await loadCharacter(manifestUrl);
  const { wrapper, bodyEl, partsEl } = createMount();
  const bodySrc = resolveAsset(manifest.body.src);
  const srcset = resolveSrcset(manifest.body.srcset, resolveAsset);

  let pixels;
  let surfaces;
  try {
    // Decoded separately from the <img> because parts are cut from its PIXELS.
    // Reading them back off the displayed element would mean waiting on layout
    // and reading a scaled copy; this reads the source at full resolution.
    [, pixels] = await Promise.all([
      loadImage(bodyEl, bodySrc, { srcset, sizes: manifest.body.sizes }),
      loadPixels(bodySrc),
    ]);
    surfaces = mount(manifest, pixels, bodyEl, partsEl);
  } catch (err) {
    wrapper.remove();
    throw err;
  }

  container.append(wrapper);

  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  const head = surfaces.find((s) => s.isHead) ?? null;
  const springs = createSpringSet(neutralChannels(), { stiffness: 120, damping: 22 });
  // The manifest carries a seed, not a generator. All three autonomic
  // subsystems draw from ONE stream, so the seed decides the whole idle
  // performance — two characters on the same seed would blink in lockstep,
  // which is very visible when they sit side by side.
  const autonomic = createAutonomic({ rng: mulberry32(manifest.seed) });
  const held = { emotion: 'neutral', presence: null, intensity: 1 };
  const overrides = {};
  let audioLevel = 0;

  // Swapping characters must STOP the previous loop, not just replace what it
  // draws. Without this each switch leaves another rAF chain running: they paint
  // detached canvases so nothing looks wrong, while the frame budget quietly
  // divides among however many characters have been selected this session.
  // Flipping between three robots a few times is enough to notice.
  let alive = false;
  let destroyed = false;
  let raf = 0;
  let last = performance.now();

  // Rigid characters drop the biological signals outright. A steel head does not
  // breathe, and volume-preserving squash on one reads as rubber. Suppressed
  // here rather than in the generator so the generator stays shared.
  const suppress = manifest.rigid ? new Set(['bodyBob']) : new Set();

  // A robot's head is servo-driven, not sprung. The spring eases out
  // asymptotically — an organic settle that never quite stops, right for a
  // face, wrong for a machine. The servo ramps, cruises at constant rate, and
  // locks dead on the target. Same channel, different integrator; the mascot
  // keeps its spring.
  const tiltServo = manifest.rigid ? createServo({ maxRate: 1.1, accel: 4 }) : null;

  function recompose() {
    const target = neutralChannels();
    const e = EMOTIONS[held.emotion] ?? {};
    for (const [k, v] of Object.entries(e)) {
      if (k in target) target[k] = (CHANNELS[k].neutral ?? 0)
        + (v - (CHANNELS[k].neutral ?? 0)) * held.intensity;
    }
    if (held.presence) {
      for (const [k, v] of Object.entries(PRESENCES[held.presence] ?? {})) target[k] = v;
    }
    springs.setTargets(target);
  }

  function layout() {
    if (destroyed) return;
    const w = bodyEl.clientWidth;
    if (w > 0) for (const surface of surfaces) surface.layout(w);
  }

  const onResize = () => layout();
  window.addEventListener('resize', onResize);

  // An emphasis beat belongs on the lamps, not on body geometry. Keep it small
  // enough to read as one brightening rather than a flash, and let it settle all
  // the way back in roughly the duration nano-claw gives an impact cue.
  const PULSE_MS = 600;
  const PULSE_GLOW = 0.28;
  let pulseAt = -Infinity;
  let pulseAmount = 0;

  function pulseGlow(now) {
    const k = (now - pulseAt) / PULSE_MS;
    if (!(k >= 0 && k < 1)) return 0;
    // Smooth decay: full strength immediately, zero slope at the landing.
    const rest = 1 - k;
    return pulseAmount * rest * rest * (1 + 2 * k);
  }

  function frame(now) {
    if (!alive || destroyed) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;

    const auto = autonomic.step(dt);
    const ch = { ...springs.step(dt) };
    for (const [k, v] of Object.entries(auto)) {
      if (suppress.has(k)) continue;
      // Blink and saccade are ADDITIVE over the held expression, which is what
      // lets a blink close the lid of an already-squinting eye without
      // discarding the squint.
      if (k === 'eyeLOpen' || k === 'eyeROpen') ch[k] = Math.min(ch[k] ?? 1, v);
      else ch[k] = (ch[k] ?? 0) + v;
    }
    Object.assign(ch, overrides);

    if (tiltServo) {
      // The servo chases the RAW target — the held expression's value, or the
      // slider's override — not the spring's smoothed output. Chasing the
      // spring would stack two integrators and ease the ease; the point is to
      // replace the spring's shape for this channel, not to filter it.
      tiltServo.setTarget(overrides.headTilt ?? springs.targets.headTilt ?? 0);
      ch.headTilt = tiltServo.step(dt);
    }

    // With a head layer, the HEAD tilts and the shoulders stay put — which is
    // what a head tilt actually looks like. Without one, the whole portrait
    // rolls, which is the cheaper approximation and still reads on a bust crop.
    const bodyCh = manifest.rigid ? rigidBody(ch) : ch;
    if (head) {
      head.setAngle(headAngle(head.part, ch, reducedMotion));
      const inherit = head.headTransform();
      for (const surface of surfaces) {
        if (surface !== head && surface.part.follows === 'head' && surface.setInherited) {
          surface.setInherited(inherit);
        }
      }
      // The body keeps lean and bob; only the roll moves up to the head, or it
      // would be applied twice.
      wrapper.style.transform = bodyTransform({ ...bodyCh, headTilt: 0 }, reducedMotion);
    } else {
      // Pivot at the bottom centre, because the character's neck is there. The
      // browser default of 50% 50% would swing the whole bust about a point in
      // its own chest, which reads as the robot being shaken rather than looking.
      wrapper.style.transform = bodyTransform(bodyCh, reducedMotion);
    }
    wrapper.style.transformOrigin = '50% 100%';

    const t = now / 1000;
    const glowBoost = pulseGlow(now);
    for (const surface of surfaces) {
      if (surface.part.type === 'lens') {
        const lens = lensChannels(ch, surface.part.side);
        // Pulse is additive over expression, but a lamp cannot be brighter than
        // its channel's ceiling no matter how many emphasis beats arrive.
        lens.glow = Math.min(1, lens.glow + glowBoost);
        surface.paint((ctx) => drawLens(ctx, surface.part, lens, reducedMotion ? 0 : t));
      } else if (surface.part.type === 'leds') {
        // Presence, not emotion. The LED cluster answers "what is it doing";
        // the lenses answer "how does it feel". Separate hardware for the two
        // is what stops them contending for the same surface.
        const lit = ledLevels(held.presence ?? 'idle', surface.part.lamps.length, t, {
          rate: held.intensity,
          level: audioLevel,
          reduced: reducedMotion,
        });
        surface.paint((ctx) => drawLeds(ctx, surface.part, lit));
      } else if (surface.part.type === 'antenna' && surface.part.dish) {
        // A dish sweeps on its own clock rather than tracking a channel — it is
        // hardware doing its job, not an expression. Rate follows intensity, so
        // a busier agent scans faster.
        surface.setAngle(dishAngle(surface.part, t, {
          rate: held.intensity, reduced: reducedMotion,
        }));
      } else if (surface.part.type === 'antenna') {
        // A CSS rotate, not a repaint. The extraction happened once at load;
        // per frame this is a transform the compositor handles, which is the
        // same reasoning `homography.js` gives for using matrix3d on the
        // mascot's screen instead of redrawing it.
        surface.setAngle(antennaAngle(
          surface.part,
          antennaChannels(ch, surface.part.side),
          reducedMotion,
        ));
      }
    }
    raf = requestAnimationFrame(frame);
  }

  function setIntensity(v) {
    const n = Number(v);
    // NaN has no defensible place in an ordered range. Refuse it rather than
    // destroying the held expression; infinities still clamp unambiguously.
    if (Number.isNaN(n)) return false;
    held.intensity = clamp01(n);
    recompose();
    return true;
  }

  function snapTo(state) {
    if (!state) return;
    if (hasOwn(EMOTIONS, state.emotion)) held.emotion = state.emotion;
    if (state.presence === null || hasOwn(PRESENCES, state.presence)) {
      held.presence = state.presence;
    }
    const intensity = Number(state.intensity);
    if (!Number.isNaN(intensity)) held.intensity = clamp01(intensity);
    recompose();
    springs.snap(springs.targets);
    // A costume change is not a head move — land in the held pose, no sweep.
    if (tiltServo) tiltServo.snap(overrides.headTilt ?? springs.targets.headTilt ?? 0);
  }

  recompose();
  layout();

  return {
    manifest,
    start() {
      if (destroyed || alive) return;
      alive = true;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      alive = false;
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      for (const surface of surfaces) surface.destroy?.();
      wrapper.remove();
    },
    setEmotion(name, { intensity } = {}) {
      if (!hasOwn(EMOTIONS, name)) return false;
      held.emotion = name;
      if (intensity !== undefined) {
        const n = Number(intensity);
        if (!Number.isNaN(n)) held.intensity = clamp01(n);
      }
      recompose();
      return true;
    },
    setPresence(name) {
      if (!hasOwn(PRESENCES, name)) return false;
      held.presence = name;
      recompose();
      return true;
    },
    setIntensity,
    setAudioLevel(v) {
      const n = Number(v);
      audioLevel = clamp01(Number.isNaN(n) ? 0 : n);
    },
    pulse(strength = 1) {
      const n = Number(strength);
      pulseAmount = PULSE_GLOW * clamp01(Number.isNaN(n) ? 0 : n);
      pulseAt = performance.now();
    },
    setOverride(name, v) {
      if (v === null) delete overrides[name];
      else overrides[name] = v;
    },
    forceBlink() {
      autonomic.forceBlink();
    },
    layout,
    getState() {
      return { ...held, audioLevel };
    },
    snapTo,
  };
}
