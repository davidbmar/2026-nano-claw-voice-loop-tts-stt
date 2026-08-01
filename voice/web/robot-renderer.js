// Mounts one of the photoreal robot characters as an alternative to the talking
// cube. The vendored rig owns its portrait and extracted hardware layers; this
// adapter supplies nano-claw's renderer contract and the live analyser bridge
// the rig deliberately does not carry itself.

import { createRobotRig } from './vendor/robot/robot-rig.js?v=0.4.20';

const VENDOR_BASE = new URL('./vendor/robot/', import.meta.url);
const CHARACTER_IDS = new Set(['orange', 'pale', 'rust']);

function clamp(value) {
  const number = Number(value);
  if (Number.isNaN(number)) return 0;
  return Math.max(0, Math.min(1, number));
}

function resolveAsset(path) {
  return VENDOR_BASE + String(path || '').replace(/^\.?\//, '');
}

/** A small text equivalent for state that the robot otherwise conveys only in
 *  pixels. This is kept local because the robot vendor has no announcer module;
 *  accessibility parity with the mascot is non-negotiable. */
function createLiveRegion(container) {
  const region = document.createElement('div');
  region.setAttribute('role', 'status');
  region.setAttribute('aria-live', 'polite');
  region.setAttribute('aria-atomic', 'true');
  region.style.cssText =
    'position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;' +
    'clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0';
  container.appendChild(region);
  return region;
}

/**
 * The stage treatment: meet the robot close, then the camera pulls back.
 *
 * The portrait at natural size fills the whole voice field — striking for a
 * beat, oppressive as a resting state. So selection plays a camera move: the
 * face arrives near-full-bleed, holds a moment, and pans/zooms out into a
 * framed composition — a warm halo behind the head, the bust dissolving into
 * the console's dark floor. The end state is the poster; the intro is why the
 * poster makes sense.
 *
 * All of this is presentation, so it lives here in the adapter. The rig stays
 * neutral and paints the same pixels regardless of how the camera holds them.
 */
function buildCinematicFrame(container) {
  const frame = document.createElement('div');
  frame.style.cssText =
    'position:absolute;inset:0;overflow:hidden;display:flex;' +
    'align-items:center;justify-content:center;pointer-events:none;';

  const halo = document.createElement('div');
  halo.style.cssText =
    'position:absolute;left:50%;top:42%;width:74%;aspect-ratio:1;' +
    'transform:translate(-50%,-50%);border-radius:50%;opacity:0;' +
    'transition:opacity 1800ms ease 900ms;' +
    'background:radial-gradient(circle, rgba(255,158,32,0.11) 0%, ' +
    'rgba(255,158,32,0.05) 42%, rgba(0,0,0,0) 70%);';

  const camera = document.createElement('div');
  camera.style.cssText =
    'position:relative;width:min(74%, 560px);will-change:transform;' +
    '-webkit-mask-image:linear-gradient(to bottom, black 80%, transparent 99%);' +
    'mask-image:linear-gradient(to bottom, black 80%, transparent 99%);';

  frame.append(halo, camera);
  container.appendChild(frame);
  return { frame, halo, camera };
}

/** Close-up on the face, drifted slightly off-axis so the pull-back reads as a
 *  pan rather than a plain zoom. */
const CAMERA_FROM = 'translateX(-4%) translateY(16%) scale(1.85)';
/** The resting composition: pulled back, a touch high in the frame. */
const CAMERA_TO = 'translateX(0) translateY(-2%) scale(0.78)';

function playIntro({ halo, camera }) {
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (reduced) {
    // No camera move: land directly in the resting composition.
    camera.style.transform = CAMERA_TO;
    halo.style.transition = 'none';
    halo.style.opacity = '1';
    return;
  }
  camera.style.transform = CAMERA_FROM;
  // Double rAF so the close-up actually paints before the transition arms —
  // otherwise the browser coalesces both writes and the move never happens.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      camera.style.transition =
        'transform 3400ms cubic-bezier(0.19, 0.6, 0.22, 1) 650ms';
      camera.style.transform = CAMERA_TO;
      halo.style.opacity = '1';
    });
  });
}

/**
 * Mount a robot and return an object answering nano-claw's renderer contract.
 *
 * @param {HTMLElement} container element the character is mounted into
 * @param {'orange'|'pale'|'rust'} characterId vendored character manifest id
 * @returns {Promise<object>} renderer shim plus an `unmount()` DOM teardown
 */
export async function createRobotRenderer(container, characterId) {
  if (!container) throw new Error('createRobotRenderer: container is required');
  if (!CHARACTER_IDS.has(characterId)) {
    throw new Error(`createRobotRenderer: unknown character "${characterId}"`);
  }

  const manifestUrl = VENDOR_BASE + `characters/${characterId}/character.json`;
  const stage = buildCinematicFrame(container);
  const rig = await createRobotRig({ container: stage.camera, manifestUrl, resolveAsset });
  rig.start();
  playIntro(stage);
  // Presence is the host's business (nano-claw drives it from pipeline state),
  // but the character must not sit in its boot pose waiting for the first
  // event — the console can be idle for a long time before anyone speaks.
  rig.setPresence('idle');

  const liveRegion = createLiveRegion(container);
  const characterName = rig.manifest?.name || characterId;
  let emotion = 'neutral';
  let presence = 'idle';
  let analyser = null;
  let waveform = null;
  let analyserFrame = 0;
  let destroyed = false;

  function announce() {
    liveRegion.textContent = `${characterName}: ${emotion}, ${presence}`;
  }

  function setAudioLevel(level) {
    rig.setAudioLevel(clamp(level));
    return true;
  }

  function disconnectAnalyser() {
    analyser = null;
    waveform = null;
    if (analyserFrame) cancelAnimationFrame(analyserFrame);
    analyserFrame = 0;
    rig.setAudioLevel(0);
    return true;
  }

  function readAnalyser() {
    if (!analyser || !waveform || destroyed) return;
    analyser.getByteTimeDomainData(waveform);
    let sum = 0;
    for (const value of waveform) {
      const sample = (value - 128) / 128;
      sum += sample * sample;
    }
    // Match talking-cube.js's 4.2x RMS gain so renderer swaps do not change the
    // apparent sensitivity to the same TTS analyser.
    rig.setAudioLevel(clamp(Math.sqrt(sum / waveform.length) * 4.2));
    analyserFrame = requestAnimationFrame(readAnalyser);
  }

  // These host-owned profile and presentation settings have no robot mapping;
  // a host entitled to call them must not throw.
  const noop = () => true;
  const shim = {
    connectAnalyser(node) {
      disconnectAnalyser();
      if (destroyed || !node || typeof node.getByteTimeDomainData !== 'function') return false;
      analyser = node;
      waveform = new Uint8Array(node.fftSize);
      analyserFrame = requestAnimationFrame(readAnalyser);
      return true;
    },
    disconnectAnalyser,
    pushAudioFrame(frame = {}) {
      return setAudioLevel(frame?.level);
    },
    setAudioLevel,
    setSpeaking(on) {
      rig.setAudioLevel(on ? 0.4 : 0);
      return true;
    },
    pulse(opts) {
      rig.pulse(clamp(opts?.strength ?? 1));
      return true;
    },
    getProfile() {
      return {};
    },
    importProfile: noop,
    configure: noop,
    setColors: noop,
    setPattern: noop,
    setPanelOpen: noop,
    destroy() {
      if (destroyed) return true;
      disconnectAnalyser();
      destroyed = true;
      rig.destroy();
      return true;
    },
    applyEmotion(name, intensity) {
      const applied = rig.setEmotion(name, { intensity }) === true;
      if (applied) {
        emotion = name;
        announce();
      }
      return applied;
    },
    applyPresence(name) {
      const applied = rig.setPresence(name) === true;
      if (applied) {
        presence = name;
        announce();
      }
      return applied;
    },
  };

  announce();
  return Object.assign(shim, {
    /** Stop both animation loops and remove the live region this module added.
     *  The rig removes its own portrait subtree in destroy(). */
    unmount() {
      try {
        shim.destroy();
      } finally {
        liveRegion.remove();
        stage.frame.remove();
      }
      return true;
    },
  });
}
