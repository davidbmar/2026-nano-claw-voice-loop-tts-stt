// Mounts the computer-mascot character as an alternative to the talking cube.
//
// The mascot lives in vendor/mascot/ (see vendor/mascot/README.md for the sync
// record). It already ships `createRendererShim`, which answers nano-claw's
// renderer contract verbatim — this module exists only to do the three things
// the shim cannot do for us: build the DOM subtree the rig needs, resolve the
// vendored asset paths, and hide the fact that mounting is asynchronous.
//
// Asynchrony is the one real difference from the cube. `new TalkingCubeRenderer`
// is synchronous and runs at module scope; the mascot must fetch rig.json and
// await the character art before the rig can lay anything out (it scales by
// bodyEl.clientWidth, so an image without intrinsic dimensions gives it nothing
// to measure). The cube therefore stays the boot-time default and the mascot is
// only ever mounted by an explicit, awaited swap.

import { createRig } from './vendor/mascot/character-rig.js';
import { createDirector } from './vendor/mascot/character-director.js';
import { createAnnouncer } from './vendor/mascot/announcer.js';
import { parseRigConfig } from './vendor/mascot/rig-config.js';
import { loadImage } from './vendor/mascot/load-image.js';
import { createRendererShim } from './vendor/mascot/nano-claw-adapter.js';

const VENDOR_BASE = 'vendor/mascot/public/';

/** Resolve an asset path recorded in rig.json against the vendored public dir.
 *  rig.json stores paths relative to ITSELF ("layers/body.webp"), not to the
 *  page, so serving it from a subdirectory would otherwise 404. */
function resolveAsset(path) {
  return VENDOR_BASE + String(path || '').replace(/^\.?\//, '');
}

/** srcset is a comma-separated list of "<url> <descriptor>" pairs; only the
 *  URL half is rewritten. */
function resolveSrcset(srcset) {
  if (!srcset) return undefined;
  return String(srcset)
    .split(',')
    .map((entry) => {
      const parts = entry.trim().split(/\s+/);
      if (!parts[0]) return '';
      parts[0] = resolveAsset(parts[0]);
      return parts.join(' ');
    })
    .filter(Boolean)
    .join(', ');
}

/** Build the DOM the rig expects, inside the host's stage element.
 *  Mirrors index.html in the mascot repo: stage > inner > (img, canvas, svg). */
function buildMount(container) {
  const inner = document.createElement('div');
  inner.className = 'mascot-inner';

  const body = document.createElement('img');
  body.className = 'mascot-body';
  body.alt = '';

  const screen = document.createElement('canvas');
  screen.className = 'mascot-screen';
  screen.setAttribute('aria-hidden', 'true');

  const calibration = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  calibration.setAttribute('class', 'mascot-calibration');
  calibration.setAttribute('aria-hidden', 'true');
  calibration.setAttribute('hidden', '');

  inner.append(body, screen, calibration);
  container.appendChild(inner);
  return { inner, body, screen, calibration };
}

/**
 * Mount the mascot and return an object answering nano-claw's renderer contract.
 *
 * @param {HTMLElement} container element the character is mounted into
 * @returns {Promise<object>} the renderer shim, plus `unmount()` which tears
 *          down the rig AND removes the DOM this module created.
 */
export async function createMascotRenderer(container) {
  if (!container) throw new Error('createMascotRenderer: container is required');

  const response = await fetch(VENDOR_BASE + 'rig.json');
  if (!response.ok) throw new Error(`mascot rig.json: HTTP ${response.status}`);
  const config = parseRigConfig(await response.json());

  const els = buildMount(container);

  await loadImage(els.body, resolveAsset(config.body?.src), {
    srcset: resolveSrcset(config.body?.srcset),
    sizes: config.body?.sizes,
  });

  const rig = createRig({
    config,
    bodyEl: els.body,
    canvasEl: els.screen,
    stageEl: els.inner,
    calibrationEl: els.calibration,
  });

  // The character conveys its status only in pixels; the announcer gives that a
  // text equivalent for assistive technology. Cheap, and the cube has no
  // equivalent — dropping it would make the mascot strictly less accessible
  // than the thing it replaces.
  const announcer = createAnnouncer({ container });
  const director = createDirector(rig, { onState: (s) => announcer.update(s) });
  rig.start();
  // Presence is the host's business (nano-claw drives it from pipeline state),
  // but the character must not sit in its boot pose waiting for the first
  // event — the console can be idle for a long time before anyone speaks.
  director.presence('idle');

  const shim = createRendererShim(rig, director);

  return Object.assign(shim, {
    /** Tear down the rig and remove the DOM this module added.
     *  The shim's own destroy() stops the rig and cancels the director's
     *  rAF loops (it documents a measured 14-write leak without that); this
     *  wrapper additionally reclaims the elements, since the host did not
     *  create them and cannot be expected to know about them. */
    unmount() {
      try {
        shim.destroy();
      } finally {
        els.inner.remove();
        // The announcer hands back its own region element. Querying for it by
        // class would silently fail — it is created with role/aria attributes
        // and inline styles, and carries no class at all.
        announcer.region?.remove();
      }
      return true;
    },
    /** Emotion and presence, in the vocabulary nano-claw already emits. The
     *  director's methods are `emotion`/`presence` — both return a boolean,
     *  false for a name this build cannot resolve, matching the contract
     *  `window.VoiceEmotion` already exposes on the cube. */
    applyEmotion(name, intensity) {
      return director.emotion(name, { intensity }) === true;
    },
    applyPresence(name) {
      return director.presence(name) === true;
    },
  });
}
