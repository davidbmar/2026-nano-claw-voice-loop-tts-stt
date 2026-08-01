// Mounts one of the photoreal robot characters as an alternative to the talking
// cube. The vendored rig owns its portrait and extracted hardware layers; this
// adapter supplies nano-claw's renderer contract and the live analyser bridge
// the rig deliberately does not carry itself.

import { createRobotRig } from './vendor/robot/robot-rig.js';

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
  const rig = await createRobotRig({ container, manifestUrl, resolveAsset });
  rig.start();
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
      }
      return true;
    },
  });
}
