// Synced from /Users/davidmar/src/talking_visualization/src/talking-cube.js on 2026-07-18.
// Renderer capabilities were ported wholesale; the upstream demo/director shell was not
// ported because nano-claw owns the audio pipeline, console layout, theme, and lifecycle.

const TAU = Math.PI * 2;

const clamp = (value, min = 0, max = 1) => Math.max(min, Math.min(max, value));
const lerp = (a, b, amount) => a + (b - a) * amount;
const normalizeAngle = (value) => (
  value >= -Math.PI && value <= Math.PI
    ? value
    : Math.atan2(Math.sin(value), Math.cos(value))
);
const smoothstep = (edge0, edge1, value) => {
  const t = clamp((value - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
};

function hexToRgb(hex) {
  const clean = String(hex).replace('#', '').trim();
  const value = clean.length === 3
    ? clean.split('').map((char) => char + char).join('')
    : clean.padEnd(6, '0').slice(0, 6);
  const parsed = Number.parseInt(value, 16);
  return {
    r: (parsed >> 16) & 255,
    g: (parsed >> 8) & 255,
    b: parsed & 255,
  };
}

function rgbToHex({ r, g, b }) {
  return `#${[r, g, b]
    .map((value) => Math.round(clamp(value, 0, 255)).toString(16).padStart(2, '0'))
    .join('')}`;
}

function mixColor(a, b, amount) {
  const ca = hexToRgb(a);
  const cb = hexToRgb(b);
  return rgbToHex({
    r: lerp(ca.r, cb.r, amount),
    g: lerp(ca.g, cb.g, amount),
    b: lerp(ca.b, cb.b, amount),
  });
}

function rgba(hex, alpha) {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${clamp(alpha)})`;
}

function rotatePoint(point, yaw, pitch, roll = 0) {
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  const cr = Math.cos(roll);
  const sr = Math.sin(roll);
  const x1 = point.x * cy - point.z * sy;
  const z1 = point.x * sy + point.z * cy;
  const y1 = point.y * cp - z1 * sp;
  const z2 = point.y * sp + z1 * cp;
  return {
    x: x1 * cr - y1 * sr,
    y: x1 * sr + y1 * cr,
    z: z2,
  };
}

function seededNoise(x, y, z, seed = 0) {
  const value = Math.sin(x * 91.17 + y * 157.31 + z * 73.73 + seed * 37.11) * 43758.5453;
  return value - Math.floor(value);
}

function onRegularPolygonBoundary(u, v, sides, thickness = 0.095) {
  const radius = Math.hypot(u, v);
  const sector = TAU / sides;
  const rotation = -Math.PI / 2;
  const angle = Math.atan2(v, u) - rotation;
  const localAngle = ((angle + sector / 2) % sector + sector) % sector - sector / 2;
  const boundaryRadius = Math.cos(Math.PI / sides) / Math.cos(localAngle);
  return Math.abs(radius - boundaryRadius) <= thickness;
}

const PROFILE_SCHEMA = 'voxels-spatial-voice/profile';
const PROFILE_VERSION = 2;
const POLYGON_SIDES = Object.freeze([3, 5, 8]);
const ROTATION_AXIS_NAMES = Object.freeze({
  x: 'X axis',
  y: 'Y axis',
  z: 'Z axis',
  voice: 'Voice shift',
});

const DEFAULT_PROFILE = Object.freeze({
  schema: PROFILE_SCHEMA,
  version: PROFILE_VERSION,
  settings: Object.freeze({
    gridSize: 15,
    pattern: 'nebula',
    formation: 'octahedron',
    polygonSides: 8,
    elementShape: 'octagon',
    primary: '#ff775f',
    secondary: '#ffca5c',
    energy: 0.68,
    response: 0.78,
    speed: 0.5,
    bloom: 0.48,
    opacity: 0.7,
    density: 0.9,
    spread: 1.04,
    rotationSpeed: 0.22,
    rotationAxis: 'voice',
    autoRotate: true,
    showLinks: false,
    showFrame: false,
    idleLevel: 0.06,
  }),
  camera: Object.freeze({
    yaw: -0.6266189174624561,
    pitch: 1.0165614303738644,
    roll: 0.1112071951618051,
    distance: 590,
  }),
});
const DEFAULT_SETTINGS = DEFAULT_PROFILE.settings;

const PATTERN_NAMES = Object.freeze({
  focus: 'Lens drift',
  wave: 'Voice wave',
  spectrum: 'Spectrum',
  helix: 'Double helix',
  scan: 'Signal scan',
  nebula: 'Nebula',
});

const FORMATION_NAMES = Object.freeze({
  focus: 'Lens field',
  polygon: 'Polygon axes',
  octahedron: 'Octahedron',
  cube: 'Cube',
});

const ELEMENT_NAMES = Object.freeze({
  orb: 'Soft orb',
  octagon: 'Octagon',
  diamond: 'Diamond',
  voxel: '3D voxel',
});

/**
 * Voice-reactive 3D cube renderer.
 *
 * Input values are normalized to 0..1. Call `pushAudioFrame()` at the cadence
 * of your audio pipeline; the renderer handles smoothing and falloff.
 */
export class TalkingCubeRenderer extends EventTarget {
  constructor(canvas, options = {}) {
    super();
    if (!(canvas instanceof HTMLCanvasElement)) {
      throw new TypeError('TalkingCubeRenderer requires a canvas element.');
    }

    this.canvas = canvas;
    this.context = canvas.getContext('2d', { alpha: false });
    this.settings = { ...DEFAULT_SETTINGS, ...options };
    this.camera = {
      yaw: DEFAULT_PROFILE.camera.yaw,
      pitch: DEFAULT_PROFILE.camera.pitch,
      roll: DEFAULT_PROFILE.camera.roll,
      distance: DEFAULT_PROFILE.camera.distance,
      targetDistance: DEFAULT_PROFILE.camera.distance,
      dragVelocityX: 0,
      dragVelocityY: 0,
    };
    this.viewport = { width: 1, height: 1, dpr: 1, centerX: 0, centerY: 0, fov: 820 };
    this.signal = {
      targetLevel: 0,
      level: 0,
      speaking: false,
      bands: Array(9).fill(0),
      targetBands: Array(9).fill(0),
      lastFrameAt: 0,
      source: 'idle',
    };
    this.drag = null;
    this.pulses = [];
    this.cubes = [];
    this.peakLevel = 0;
    this.breath = 1;
    this.hueShift = 0;
    this.rotationVector = { x: 0, y: 1, z: 0 };
    this.frameRequest = 0;
    this.lastFrame = performance.now();
    this.time = 0;
    this.panelOpen = true;
    this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.destroyed = false;
    this.analyser = null;
    this.analyserFrequency = null;
    this.analyserWaveform = null;

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.canvas);
    this.boundHandlers = {};
    this.bindInteraction();
    this.buildGrid();
    this.resize();
    this.frameRequest = requestAnimationFrame((now) => this.frame(now));
  }

  buildGrid() {
    const size = clamp(Math.round(this.settings.gridSize), 3, 15);
    this.settings.gridSize = size;
    const half = (size - 1) / 2;
    this.cubes = [];

    for (let x = 0; x < size; x += 1) {
      for (let y = 0; y < size; y += 1) {
        for (let z = 0; z < size; z += 1) {
          const nx = (x - half) / Math.max(half, 1);
          const ny = (y - half) / Math.max(half, 1);
          const nz = (z - half) / Math.max(half, 1);
          this.cubes.push({
            x: x - half,
            y: y - half,
            z: z - half,
            nx,
            ny,
            nz,
            radius: Math.hypot(nx, ny, nz),
            phase: seededNoise(x, y, z, size) * TAU,
            noise: seededNoise(z, x, y, size + 4),
            activity: 0,
            target: 0,
            depth: 0,
            screen: null,
            index: this.cubes.length,
          });
        }
      }
    }

    this.dispatchState('grid');
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);

    const panelAllowance = this.panelOpen && width > 900 ? 350 : 0;
    const availableWidth = width - panelAllowance;
    const visualScale = clamp(Math.min(availableWidth / 760, height / 760), 0.64, 1.12);
    this.viewport = {
      width,
      height,
      dpr,
      centerX: availableWidth / 2 + (width > 900 ? 12 : 0),
      centerY: height / 2 - (height < 700 ? 4 : 10),
      fov: 840 * visualScale,
    };
  }

  setPanelOpen(open) {
    this.panelOpen = Boolean(open);
    this.resize();
  }

  configure(next = {}, options = {}) {
    const previousSize = this.settings.gridSize;
    const allowed = Object.keys(DEFAULT_SETTINGS);
    for (const [key, value] of Object.entries(next)) {
      if (!allowed.includes(key)) continue;
      if (['primary', 'secondary', 'pattern', 'formation', 'elementShape', 'rotationAxis'].includes(key)) {
        this.settings[key] = String(value);
      } else if (['autoRotate', 'showLinks', 'showFrame'].includes(key)) {
        this.settings[key] = Boolean(value);
      } else {
        this.settings[key] = Number(value);
      }
    }

    if (!PATTERN_NAMES[this.settings.pattern]) this.settings.pattern = 'focus';
    if (!FORMATION_NAMES[this.settings.formation]) this.settings.formation = 'focus';
    if (!ELEMENT_NAMES[this.settings.elementShape]) this.settings.elementShape = 'octagon';
    if (!POLYGON_SIDES.includes(this.settings.polygonSides)) this.settings.polygonSides = 8;
    if (!ROTATION_AXIS_NAMES[this.settings.rotationAxis]) this.settings.rotationAxis = 'y';
    if (previousSize !== this.settings.gridSize) this.buildGrid();
    if (!options.silent) this.dispatchState('configure');
    return options.silent ? this.settings : this.getState();
  }

  setPattern(pattern, options = {}) {
    if (!PATTERN_NAMES[pattern]) {
      throw new RangeError(`Unknown pattern “${pattern}”. Try ${Object.keys(PATTERN_NAMES).join(', ')}.`);
    }
    this.configure({ ...options, pattern });
    this.pulse({ strength: 0.72, duration: 760 });
    return this;
  }

  setColors(primary, secondary = primary) {
    this.configure({ primary, secondary });
    return this;
  }

  setAudioLevel(level, bands) {
    return this.pushAudioFrame({ level, bands });
  }

  pushAudioFrame(frame = {}) {
    const level = clamp(Number(frame.level) || 0);
    const incomingBands = frame.bands || frame.frequencyData;
    this.signal.targetLevel = level;
    this.signal.speaking = frame.speaking ?? level > 0.045;
    this.signal.lastFrameAt = performance.now();
    this.signal.source = frame.source || 'external';

    if (incomingBands?.length) {
      this.signal.targetBands = this.normalizeBands(incomingBands);
    } else {
      this.signal.targetBands = this.signal.targetBands.map((_, index) => {
        const contour = 0.45 + Math.sin(index * 1.7 + this.time * 2.1) * 0.18;
        return clamp(level * contour + level * 0.36);
      });
    }

    this.dispatchEvent(new CustomEvent('audioframe', {
      detail: {
        level,
        bands: [...this.signal.targetBands],
        speaking: this.signal.speaking,
        source: this.signal.source,
      },
    }));
    return this;
  }

  pushPCM16(samples, options = {}) {
    const input = samples instanceof Int16Array
      ? samples
      : new Int16Array(samples.buffer || samples);
    if (!input.length) return this;

    let sum = 0;
    const segments = Array(9).fill(0);
    const counts = Array(9).fill(0);
    for (let index = 0; index < input.length; index += 1) {
      const sample = input[index] / 32768;
      sum += sample * sample;
      const band = Math.min(8, Math.floor(index / input.length * 9));
      segments[band] += Math.abs(sample);
      counts[band] += 1;
    }
    const rms = Math.sqrt(sum / input.length);
    const gain = options.gain ?? 3.2;
    return this.pushAudioFrame({
      level: clamp(rms * gain),
      bands: segments.map((value, index) => clamp((value / Math.max(1, counts[index])) * gain * 1.7)),
      source: options.source || 'pcm16',
    });
  }

  normalizeBands(values) {
    const source = Array.from(values, Number);
    const maxValue = Math.max(...source, 1);
    const scale = maxValue > 1 ? 1 / 255 : 1;
    return Array.from({ length: 9 }, (_, index) => {
      const from = Math.floor(index / 9 * source.length);
      const to = Math.max(from + 1, Math.floor((index + 1) / 9 * source.length));
      let total = 0;
      for (let cursor = from; cursor < to; cursor += 1) total += source[cursor] || 0;
      return clamp(total / (to - from) * scale);
    });
  }

  setSpeaking(speaking) {
    this.signal.speaking = Boolean(speaking);
    if (!speaking) this.signal.targetLevel = 0;
    this.dispatchState('speaking');
    return this;
  }

  connectAnalyser(analyser) {
    if (!analyser || typeof analyser.getByteFrequencyData !== 'function') {
      throw new TypeError('connectAnalyser expects a Web Audio AnalyserNode.');
    }
    this.analyser = analyser;
    this.analyserFrequency = new Uint8Array(analyser.frequencyBinCount);
    this.analyserWaveform = new Uint8Array(analyser.fftSize);
    return this;
  }

  disconnectAnalyser() {
    this.analyser = null;
    this.analyserFrequency = null;
    this.analyserWaveform = null;
    return this;
  }

  readAnalyser() {
    if (!this.analyser) return;
    this.analyser.getByteFrequencyData(this.analyserFrequency);
    this.analyser.getByteTimeDomainData(this.analyserWaveform);
    let sum = 0;
    for (const value of this.analyserWaveform) {
      const sample = (value - 128) / 128;
      sum += sample * sample;
    }
    const rms = Math.sqrt(sum / this.analyserWaveform.length);
    this.pushAudioFrame({
      level: clamp(rms * 4.2),
      bands: this.analyserFrequency,
      source: 'analyser',
    });
  }

  pulse(options = {}) {
    const now = performance.now();
    this.pulses.push({
      start: now,
      duration: clamp(Number(options.duration) || 1100, 180, 5000),
      strength: clamp(Number(options.strength) || 1, 0, 2),
      color: options.color || this.settings.primary,
      origin: options.origin || { x: 0, y: 0, z: 0 },
    });
    this.dispatchEvent(new CustomEvent('pulse', { detail: this.pulses.at(-1) }));
    return this;
  }

  resetCamera() {
    this.camera.yaw = DEFAULT_PROFILE.camera.yaw;
    this.camera.pitch = DEFAULT_PROFILE.camera.pitch;
    this.camera.roll = DEFAULT_PROFILE.camera.roll;
    this.camera.distance = DEFAULT_PROFILE.camera.distance;
    this.camera.targetDistance = DEFAULT_PROFILE.camera.distance;
    this.camera.dragVelocityX = 0;
    this.camera.dragVelocityY = 0;
    return this;
  }

  getState() {
    return {
      settings: { ...this.settings },
      signal: {
        level: this.signal.level,
        speaking: this.signal.speaking,
        bands: [...this.signal.bands],
        source: this.signal.source,
      },
      camera: {
        yaw: this.camera.yaw,
        pitch: this.camera.pitch,
        roll: this.camera.roll,
        distance: this.camera.distance,
      },
      cubeCount: this.cubes.length,
      patterns: { ...PATTERN_NAMES },
      formations: { ...FORMATION_NAMES },
      elementShapes: { ...ELEMENT_NAMES },
      rotationAxes: { ...ROTATION_AXIS_NAMES },
    };
  }

  getProfile() {
    return {
      schema: PROFILE_SCHEMA,
      version: PROFILE_VERSION,
      settings: { ...this.settings },
      camera: {
        yaw: this.camera.yaw,
        pitch: this.camera.pitch,
        roll: this.camera.roll,
        distance: this.camera.targetDistance,
      },
    };
  }

  exportProfile(space = 2) {
    return JSON.stringify(this.getProfile(), null, clamp(Number(space) || 0, 0, 10));
  }

  importProfile(profile) {
    let parsed = profile;
    if (typeof profile === 'string') {
      try {
        parsed = JSON.parse(profile);
      } catch (error) {
        throw new TypeError(`Could not parse visualization profile: ${error.message}`);
      }
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new TypeError('Visualization profile must be a JSON object or JSON string.');
    }
    if (parsed.schema && parsed.schema !== PROFILE_SCHEMA) {
      throw new TypeError(`Unsupported visualization profile schema: ${parsed.schema}`);
    }
    if (Number(parsed.version || 1) > PROFILE_VERSION) {
      throw new RangeError(`Visualization profile version ${parsed.version} is newer than supported version ${PROFILE_VERSION}.`);
    }

    const sourceSettings = parsed.settings && typeof parsed.settings === 'object'
      ? parsed.settings
      : parsed;
    const settings = { ...sourceSettings };
    const profileVersion = Number(parsed.version || 1);
    if (profileVersion < 2 && settings.rotationAxis === undefined) settings.rotationAxis = 'y';
    this.configure(settings);

    if (parsed.camera && typeof parsed.camera === 'object') {
      const yaw = Number(parsed.camera.yaw);
      const pitch = Number(parsed.camera.pitch);
      const roll = Number(parsed.camera.roll);
      const distance = Number(parsed.camera.distance);
      if (Number.isFinite(yaw)) this.camera.yaw = normalizeAngle(yaw);
      if (Number.isFinite(pitch)) this.camera.pitch = normalizeAngle(pitch);
      if (Number.isFinite(roll)) this.camera.roll = normalizeAngle(roll);
      else if (profileVersion < 2) this.camera.roll = 0;
      if (Number.isFinite(distance)) {
        this.camera.distance = clamp(distance, 350, 980);
        this.camera.targetDistance = this.camera.distance;
      }
      this.camera.dragVelocityX = 0;
      this.camera.dragVelocityY = 0;
    }

    this.dispatchState('profile');
    return this.getState();
  }

  dispatchState(reason) {
    this.dispatchEvent(new CustomEvent('statechange', {
      detail: { reason, ...this.getState() },
    }));
  }

  bindInteraction() {
    const onPointerDown = (event) => {
      if (event.button !== 0) return;
      this.canvas.setPointerCapture?.(event.pointerId);
      this.drag = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
      this.camera.dragVelocityX = 0;
      this.camera.dragVelocityY = 0;
      this.canvas.classList.add('dragging');
    };
    const onPointerMove = (event) => {
      if (!this.drag || event.pointerId !== this.drag.pointerId) return;
      const dx = event.clientX - this.drag.x;
      const dy = event.clientY - this.drag.y;
      this.camera.yaw = normalizeAngle(this.camera.yaw + dx * 0.006);
      this.camera.pitch = normalizeAngle(this.camera.pitch + dy * 0.005);
      this.camera.dragVelocityX = dx * 0.0009;
      this.camera.dragVelocityY = dy * 0.00075;
      this.drag.x = event.clientX;
      this.drag.y = event.clientY;
    };
    const onPointerUp = (event) => {
      if (!this.drag || event.pointerId !== this.drag.pointerId) return;
      this.drag = null;
      this.canvas.classList.remove('dragging');
    };
    const onWheel = (event) => {
      event.preventDefault();
      this.camera.targetDistance = clamp(
        this.camera.targetDistance * Math.exp(event.deltaY * 0.0011),
        350,
        980,
      );
    };
    const onDoubleClick = () => this.pulse({ strength: 1.25 });

    this.boundHandlers = { onPointerDown, onPointerMove, onPointerUp, onWheel, onDoubleClick };
    this.canvas.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    this.canvas.addEventListener('wheel', onWheel, { passive: false });
    this.canvas.addEventListener('dblclick', onDoubleClick);
  }

  updateSignal(delta) {
    if (this.analyser) this.readAnalyser();

    const now = performance.now();
    if (now - this.signal.lastFrameAt > 220 && !this.analyser) {
      this.signal.targetLevel *= Math.pow(0.72, delta * 60);
      this.signal.targetBands = this.signal.targetBands.map((band) => band * Math.pow(0.76, delta * 60));
      if (this.signal.targetLevel < 0.02) this.signal.speaking = false;
    }

    const attack = 1 - Math.pow(1 - clamp(this.settings.response * 0.78 + 0.15), delta * 60);
    const release = 1 - Math.pow(0.9, delta * 60);
    const levelRate = this.signal.targetLevel > this.signal.level ? attack : release;
    this.signal.level = lerp(this.signal.level, this.signal.targetLevel, levelRate);
    this.signal.bands = this.signal.bands.map((band, index) => {
      const target = this.signal.targetBands[index] || 0;
      return lerp(band, target, target > band ? attack : release * 0.75);
    });
  }

  patternValue(cube, time) {
    const { pattern, speed } = this.settings;
    const level = this.signal.level;
    const bands = this.signal.bands;
    const bandX = bands[Math.round((cube.nx + 1) * 4)] || 0;
    const bandZ = bands[Math.round((cube.nz + 1) * 4)] || 0;
    const bandY = bands[Math.round((cube.ny + 1) * 4)] || 0;
    const t = time * speed;

    if (pattern === 'focus') {
      // A broad optical focus plane glides across an ordered halftone field.
      // Voice bands modulate whole rows, avoiding isolated or random flashes.
      const focalX = Math.sin(t * 0.72) * 0.48;
      const lens = Math.exp(-((cube.nx - focalX) ** 2) * 5.2);
      const row = 0.72 + bandY * 0.82 + level * 0.38;
      const edgeFeather = smoothstep(1.18, 0.18, Math.hypot(cube.nx * 0.82, cube.ny));
      const softWave = 0.88 + Math.sin(cube.ny * 2.4 - t * 0.8) * 0.12;
      return (0.28 + lens * 0.72 * row) * edgeFeather * softWave;
    }

    if (pattern === 'spectrum') {
      const column = clamp(bandX * 0.72 + bandZ * 0.46 + level * 0.28);
      const floor = 1 - (cube.ny + 1) / 2;
      const height = smoothstep(floor - 0.18, floor + 0.06, column);
      const cap = 1 - smoothstep(column + 0.02, column + 0.26, floor);
      return height * cap * (0.62 + Math.sin(t * 7 + cube.phase) * 0.18 + level * 0.3);
    }

    if (pattern === 'helix') {
      const angle = Math.atan2(cube.nz, cube.nx);
      const desiredY = Math.sin(angle * 1.2 - t * 2.4) * 0.72;
      const desiredY2 = Math.sin(angle * 1.2 - t * 2.4 + Math.PI) * 0.72;
      const strand = Math.exp(-Math.min(
        Math.abs(cube.ny - desiredY),
        Math.abs(cube.ny - desiredY2),
      ) * 7.5);
      const radiusBand = Math.exp(-Math.abs(Math.hypot(cube.nx, cube.nz) - 0.72) * 6.5);
      return strand * radiusBand * (0.68 + level * 0.62);
    }

    if (pattern === 'scan') {
      const plane = ((t * 0.58 + 1.4) % 2.8) - 1.4;
      const primary = Math.exp(-Math.abs(cube.nx - plane) * 8.5);
      const afterglow = Math.exp(-Math.abs(cube.nx - plane + 0.3) * 3.5) * 0.22;
      const voiceGrain = 0.62 + bandZ * 0.52 + Math.sin(cube.ny * 4 + t * 3) * 0.12;
      return (primary + afterglow) * voiceGrain;
    }

    if (pattern === 'nebula') {
      const field =
        Math.sin(cube.nx * 3.2 + t * 1.5) +
        Math.cos(cube.ny * 4.1 - t * 1.1) +
        Math.sin(cube.nz * 3.7 + t * 0.8 + cube.nx * 2);
      const threshold = 0.9 - level * 1.7;
      const cloud = smoothstep(threshold, threshold + 1.1, field);
      return cloud * (0.35 + cube.noise * 0.38 + level * 0.55);
    }

    const wavePosition = cube.nx * 2.8 + cube.nz * 1.05 - t * 3.2;
    const voiceWave = Math.sin(wavePosition + Math.sin(cube.ny * 2.4 + t) * 0.55);
    const bandShape = 0.5 + bandX * 0.72 + bandZ * 0.28;
    const ribbon = Math.exp(-Math.abs(cube.ny - voiceWave * 0.62 * bandShape) * 6.2);
    const secondRibbon = Math.exp(-Math.abs(cube.ny + voiceWave * 0.32) * 8.5) * 0.35;
    return (ribbon + secondRibbon) * (0.46 + level * 0.95);
  }

  updateCubes(delta, now) {
    const baseline = this.settings.idleLevel;
    const voiceBoost = this.signal.level * this.settings.response;
    const breathTarget = 1 + Math.sin(this.time * 1.35) * 0.018 + this.signal.level * 0.045;
    this.breath = lerp(this.breath, breathTarget, 1 - Math.pow(0.88, delta * 60));
    this.peakLevel = Math.max(this.signal.level, this.peakLevel * Math.pow(0.94, delta * 60));
    this.hueShift = lerp(this.hueShift, this.signal.level * 0.22, 1 - Math.pow(0.9, delta * 60));

    for (const cube of this.cubes) {
      let target = this.patternValue(cube, this.time);
      target *= baseline + voiceBoost * 1.55 + this.settings.energy * 0.28;
      if (this.settings.formation === 'polygon') {
        // Keep all three orthogonal outlines legible while the selected voice
        // pattern travels across them.
        target += 0.13 + this.signal.level * 0.045;
      }

      for (const pulse of this.pulses) {
        const progress = (now - pulse.start) / pulse.duration;
        if (progress < 0 || progress > 1) continue;
        const sweep = -1.25 + progress * 2.5;
        const distance = Math.abs(cube.nx - sweep);
        target += Math.exp(-distance * distance * 42) * pulse.strength * (1 - progress * 0.28);
      }

      cube.target = clamp(target * this.settings.energy * 1.4, 0, 1.45);
      const attack = 1 - Math.pow(0.62, delta * 60);
      const decay = 1 - Math.pow(0.9, delta * 60);
      cube.activity = lerp(cube.activity, cube.target, cube.target > cube.activity ? attack : decay);
    }

    this.pulses = this.pulses.filter((pulse) => now - pulse.start <= pulse.duration);
  }

  project(point) {
    const depth = point.z + this.camera.distance;
    if (depth < 80) return null;
    const scale = this.viewport.fov / depth;
    return {
      x: this.viewport.centerX + point.x * scale,
      y: this.viewport.centerY + point.y * scale,
      scale,
      depth,
    };
  }

  drawBackground(context) {
    const { width, height, centerX, centerY } = this.viewport;
    context.fillStyle = '#050606';
    context.fillRect(0, 0, width, height);

    const accent = mixColor(this.settings.primary, this.settings.secondary, this.hueShift);
    const focusField = context.createLinearGradient(
      centerX - width * 0.34,
      0,
      centerX + width * 0.34,
      0,
    );
    focusField.addColorStop(0, rgba(accent, 0));
    focusField.addColorStop(0.38, rgba(accent, 0.012 + this.signal.level * 0.012));
    focusField.addColorStop(0.5, rgba(accent, 0.026 + this.signal.level * 0.018));
    focusField.addColorStop(0.62, rgba(accent, 0.012 + this.signal.level * 0.012));
    focusField.addColorStop(1, rgba(accent, 0));
    context.fillStyle = focusField;
    context.fillRect(0, 0, width, height);

    const vignette = context.createRadialGradient(
      width / 2,
      height / 2,
      Math.min(width, height) * 0.12,
      width / 2,
      height / 2,
      Math.max(width, height) * 0.72,
    );
    vignette.addColorStop(0, 'rgba(0,0,0,0)');
    vignette.addColorStop(1, 'rgba(0,0,0,0.68)');
    context.fillStyle = vignette;
    context.fillRect(0, 0, width, height);
  }

  formationVisible(cube) {
    const { formation, density, polygonSides, gridSize } = this.settings;
    const l1 = Math.abs(cube.nx) + Math.abs(cube.ny) + Math.abs(cube.nz);
    let inside = true;

    if (formation === 'focus') {
      const ellipse = (cube.nx / 1.04) ** 2 + (cube.ny / 0.84) ** 2;
      inside = Math.abs(cube.nz) <= 0.21 && ellipse <= 1;
    }
    if (formation === 'polygon') {
      const half = Math.max((gridSize - 1) / 2, 1);
      const shellThickness = Math.max(0.095, 0.48 / half);
      const onXY = cube.z === 0 && onRegularPolygonBoundary(cube.nx, cube.ny, polygonSides, shellThickness);
      const onXZ = cube.y === 0 && onRegularPolygonBoundary(cube.nx, cube.nz, polygonSides, shellThickness);
      const onYZ = cube.x === 0 && onRegularPolygonBoundary(cube.ny, cube.nz, polygonSides, shellThickness);
      inside = onXY || onXZ || onYZ;
    }
    if (formation === 'octahedron') inside = l1 <= 1.52;
    const retainedByDensity = cube.noise <= clamp(density, 0.08, 1) || cube.radius < 0.08;
    return inside && retainedByDensity;
  }

  formationPoint(cube, spacing) {
    const half = (this.settings.gridSize - 1) / 2;
    const radius = half * spacing * this.breath;

    if (this.settings.formation === 'focus') {
      const shallowCurve = (cube.nx * cube.nx + cube.ny * cube.ny) * spacing * 0.22;
      return {
        x: cube.nx * radius * 1.34 + cube.z * spacing * 0.16,
        y: cube.ny * radius * 0.88 + cube.z * spacing * 0.07,
        z: cube.z * spacing * 0.56 + shallowCurve,
      };
    }

    if (this.settings.formation === 'polygon') {
      return {
        x: cube.nx * radius * 1.04,
        y: cube.ny * radius * 1.04,
        z: cube.nz * radius * 1.04,
      };
    }

    return {
      x: cube.x * spacing * this.breath,
      y: cube.y * spacing * this.breath,
      z: cube.z * spacing * this.breath,
    };
  }

  cubeGeometry(cube, spacing, cubeSize) {
    const center = cube.world || this.formationPoint(cube, spacing);
    const animatedSize = cubeSize * (0.9 + cube.activity * 0.24);
    const half = animatedSize / 2;
    const source = [
      [-half, -half, -half], [half, -half, -half], [half, half, -half], [-half, half, -half],
      [-half, -half, half], [half, -half, half], [half, half, half], [-half, half, half],
    ];
    const transformed = source.map(([x, y, z]) => rotatePoint({
      x: center.x + x,
      y: center.y + y,
      z: center.z + z,
    }, this.camera.yaw, this.camera.pitch, this.camera.roll));
    const projected = transformed.map((point) => this.project(point));
    return {
      center: rotatePoint(center, this.camera.yaw, this.camera.pitch, this.camera.roll),
      transformed,
      projected,
    };
  }

  drawBounds(context, extent) {
    const corners = [
      [-extent, -extent, -extent], [extent, -extent, -extent], [extent, extent, -extent], [-extent, extent, -extent],
      [-extent, -extent, extent], [extent, -extent, extent], [extent, extent, extent], [-extent, extent, extent],
    ].map(([x, y, z]) => this.project(rotatePoint(
      { x, y, z },
      this.camera.yaw,
      this.camera.pitch,
      this.camera.roll,
    )));
    const edges = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
    context.lineWidth = 0.55;
    context.strokeStyle = rgba(
      this.settings.primary,
      (0.105 + this.signal.level * 0.06) * this.settings.opacity,
    );
    for (const [a, b] of edges) {
      if (!corners[a] || !corners[b]) continue;
      context.beginPath();
      context.moveTo(corners[a].x, corners[a].y);
      context.lineTo(corners[b].x, corners[b].y);
      context.stroke();
    }
  }

  drawLinks(context, ordered) {
    if (!this.settings.showLinks) return;
    const map = new Map(this.cubes.map((cube) => [`${cube.x},${cube.y},${cube.z}`, cube]));
    const directions = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    const speakBoost = 0.55 + this.signal.level * 1.4;
    context.lineCap = 'round';

    for (const cube of ordered) {
      if (!cube.visible || cube.activity < 0.14 || !cube.screen) continue;
      for (const [dx, dy, dz] of directions) {
        const neighbor = map.get(`${cube.x + dx},${cube.y + dy},${cube.z + dz}`);
        if (!neighbor?.visible || neighbor.activity < 0.14 || !neighbor.screen) continue;
        const amount = Math.min(cube.activity, neighbor.activity);
        const alpha = clamp(amount * 0.28 * this.settings.bloom * this.settings.opacity * speakBoost, 0, 0.42);
        const color = mixColor(
          this.settings.primary,
          this.settings.secondary,
          (cube.nz + 1) / 2 * 0.6 + this.hueShift,
        );
        context.strokeStyle = rgba(color, alpha);
        context.lineWidth = 0.4 + amount * (0.9 + this.signal.level);
        context.beginPath();
        context.moveTo(cube.screen.x, cube.screen.y);
        context.lineTo(neighbor.screen.x, neighbor.screen.y);
        context.stroke();
      }
    }
  }

  drawCube(context, cube, geometry) {
    const { projected } = geometry;
    if (projected.some((point) => !point)) return;
    const faces = [
      { indices: [0, 1, 2, 3], normal: { x: 0, y: 0, z: -1 }, shade: 1 },
      { indices: [5, 4, 7, 6], normal: { x: 0, y: 0, z: 1 }, shade: 0.48 },
      { indices: [4, 0, 3, 7], normal: { x: -1, y: 0, z: 0 }, shade: 0.58 },
      { indices: [1, 5, 6, 2], normal: { x: 1, y: 0, z: 0 }, shade: 0.82 },
      { indices: [4, 5, 1, 0], normal: { x: 0, y: -1, z: 0 }, shade: 1.15 },
      { indices: [3, 2, 6, 7], normal: { x: 0, y: 1, z: 0 }, shade: 0.38 },
    ];
    const colorBlend = clamp((cube.ny + cube.nz + 2) / 4 * 0.72 + cube.noise * 0.18);
    const color = mixColor(this.settings.primary, this.settings.secondary, colorBlend);
    const activity = clamp(cube.activity);
    const visualOpacity = clamp(this.settings.opacity, 0.02, 1);
    const baseAlpha = (0.018 + this.settings.energy * 0.008) * visualOpacity;

    for (const face of faces) {
      const normal = rotatePoint(face.normal, this.camera.yaw, this.camera.pitch, this.camera.roll);
      if (normal.z >= 0.02) continue;
      const path = new Path2D();
      face.indices.forEach((index, faceIndex) => {
        const point = projected[index];
        if (faceIndex === 0) path.moveTo(point.x, point.y);
        else path.lineTo(point.x, point.y);
      });
      path.closePath();

      if (activity > 0.025) {
        const fillAlpha = clamp(
          baseAlpha + activity * (0.42 + this.settings.bloom * 0.4) * face.shade * visualOpacity,
          0,
          0.95,
        );
        context.fillStyle = rgba(color, fillAlpha);
        context.fill(path);
        context.strokeStyle = rgba(
          mixColor(color, '#ffffff', 0.22),
          clamp((0.07 + activity * 0.42) * visualOpacity, 0, 0.7),
        );
        context.lineWidth = 0.48 + activity * 0.46;
        context.stroke(path);
      } else {
        context.fillStyle = `rgba(12, 17, 17, ${0.045 * visualOpacity})`;
        context.fill(path);
        context.strokeStyle = `rgba(115, 140, 137, ${0.085 * visualOpacity})`;
        context.lineWidth = 0.38;
        context.stroke(path);
      }
    }

    if (activity > 0.13 && cube.screen) {
      const glowRadius = Math.max(2, cube.screen.scale * (7 + activity * 10) * (0.4 + this.settings.bloom));
      const glow = context.createRadialGradient(
        cube.screen.x,
        cube.screen.y,
        0,
        cube.screen.x,
        cube.screen.y,
        glowRadius,
      );
      glow.addColorStop(0, rgba(color, activity * this.settings.bloom * 0.31 * visualOpacity));
      glow.addColorStop(1, rgba(color, 0));
      context.fillStyle = glow;
      context.beginPath();
      context.arc(cube.screen.x, cube.screen.y, glowRadius, 0, TAU);
      context.fill();
    }
  }

  drawSprite(context, cube, cubeSize) {
    if (!cube.screen) return;
    const activity = clamp(cube.activity);
    const visualOpacity = clamp(this.settings.opacity, 0.02, 1);
    const colorBlend = this.settings.formation === 'focus'
      ? clamp(0.22 + (cube.nx + 1) * 0.18 + cube.nz * 0.08)
      : clamp((cube.ny + cube.nz + 2) / 4 * 0.7 + cube.noise * 0.2);
    const color = mixColor(this.settings.primary, this.settings.secondary, colorBlend);
    const radius = Math.max(1.2, cube.screen.scale * cubeSize * (0.37 + activity * 0.075));
    const alpha = clamp((0.03 + activity * (1.18 + this.settings.bloom * 0.14)) * visualOpacity, 0, 0.94);

    if (activity > 0.055 && this.settings.bloom > 0.02) {
      const glowRadius = radius * (1.8 + this.settings.bloom * 2.8);
      const glow = context.createRadialGradient(
        cube.screen.x,
        cube.screen.y,
        0,
        cube.screen.x,
        cube.screen.y,
        glowRadius,
      );
      glow.addColorStop(0, rgba(color, activity * this.settings.bloom * 0.28 * visualOpacity));
      glow.addColorStop(1, rgba(color, 0));
      context.fillStyle = glow;
      context.beginPath();
      context.arc(cube.screen.x, cube.screen.y, glowRadius, 0, TAU);
      context.fill();
    }

    if (this.settings.elementShape === 'orb') {
      const orb = context.createRadialGradient(
        cube.screen.x - radius * 0.26,
        cube.screen.y - radius * 0.3,
        radius * 0.05,
        cube.screen.x,
        cube.screen.y,
        radius,
      );
      orb.addColorStop(0, rgba(mixColor(color, '#ffffff', 0.58), alpha));
      orb.addColorStop(0.48, rgba(color, alpha * 0.88));
      orb.addColorStop(1, rgba(color, alpha * 0.16));
      context.fillStyle = orb;
      context.beginPath();
      context.arc(cube.screen.x, cube.screen.y, radius, 0, TAU);
      context.fill();
      return;
    }

    const sides = this.settings.elementShape === 'diamond' ? 4 : 8;
    const rotation = this.settings.elementShape === 'diamond' ? Math.PI / 4 : Math.PI / 8;
    const path = new Path2D();
    for (let index = 0; index < sides; index += 1) {
      const angle = rotation + index / sides * TAU;
      const x = cube.screen.x + Math.cos(angle) * radius;
      const y = cube.screen.y + Math.sin(angle) * radius;
      if (index === 0) path.moveTo(x, y);
      else path.lineTo(x, y);
    }
    path.closePath();

    const fill = context.createLinearGradient(
      cube.screen.x,
      cube.screen.y - radius,
      cube.screen.x,
      cube.screen.y + radius,
    );
    fill.addColorStop(0, rgba(mixColor(color, '#ffffff', 0.24), alpha));
    fill.addColorStop(1, rgba(color, alpha * 0.68));
    context.fillStyle = fill;
    context.fill(path);
  }

  drawNode(context, cube, geometry, cubeSize) {
    if (this.settings.elementShape === 'voxel') this.drawCube(context, cube, geometry);
    else this.drawSprite(context, cube, cubeSize);
  }

  render() {
    const context = this.context;
    const { dpr, width, height } = this.viewport;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    this.drawBackground(context);

    const gridSize = this.settings.gridSize;
    const spacing = 48 * this.settings.spread * (5 / gridSize);
    const cubeSize = clamp(spacing * 0.31, 5.5, 16);
    const extent = ((gridSize - 1) / 2) * spacing + cubeSize * 0.78;

    const ordered = this.cubes.map((cube) => {
      cube.visible = this.formationVisible(cube);
      cube.world = this.formationPoint(cube, spacing);
      if (!cube.visible) {
        cube.screen = null;
        return cube;
      }
      const center = rotatePoint(cube.world, this.camera.yaw, this.camera.pitch, this.camera.roll);
      cube.depth = center.z;
      cube.screen = this.project(center);
      return cube;
    }).filter((cube) => cube.visible).sort((a, b) => b.depth - a.depth);

    if (this.settings.showFrame) this.drawBounds(context, extent);
    this.drawLinks(context, ordered);

    for (const cube of ordered) {
      const geometry = this.cubeGeometry(cube, spacing, cubeSize);
      this.drawNode(context, cube, geometry, cubeSize);
    }

  }

  updateAutoRotation(delta) {
    if (!this.settings.autoRotate || this.reducedMotion) return;

    const { rotationAxis, rotationSpeed, formation } = this.settings;
    const level = this.signal.level;

    if (rotationAxis === 'voice') {
      const average = (from, to) => {
        let total = 0;
        for (let index = from; index <= to; index += 1) total += this.signal.bands[index] || 0;
        return total / (to - from + 1);
      };
      const low = average(0, 2);
      const mid = average(3, 5);
      const high = average(6, 8);
      const scores = [
        0.12 + low * 1.4 + (Math.sin(this.time * 0.43) + 1) * 0.06,
        0.12 + mid * 1.4 + (Math.sin(this.time * 0.37 + 2.1) + 1) * 0.06,
        0.12 + high * 1.4 + (Math.sin(this.time * 0.41 + 4.2) + 1) * 0.06,
      ];
      const dominant = scores.indexOf(Math.max(...scores));
      const directions = [
        Math.sin(this.time * 0.19 + low * 3.2) >= 0 ? 1 : -1,
        Math.sin(this.time * 0.17 + mid * 3.2 + 2.2) >= 0 ? 1 : -1,
        Math.sin(this.time * 0.21 + high * 3.2 + 4.4) >= 0 ? 1 : -1,
      ];
      const target = {
        x: dominant === 0 ? directions[0] : 0,
        y: dominant === 1 ? directions[1] : 0,
        z: dominant === 2 ? directions[2] : 0,
      };
      const shiftRate = 1 - Math.exp(-delta * (0.72 + level * 5.4));
      this.rotationVector.x = lerp(this.rotationVector.x, target.x, shiftRate);
      this.rotationVector.y = lerp(this.rotationVector.y, target.y, shiftRate);
      this.rotationVector.z = lerp(this.rotationVector.z, target.z, shiftRate);

      const amount = delta * rotationSpeed * (0.28 + level * 0.58);
      this.camera.pitch += amount * this.rotationVector.x;
      this.camera.yaw += amount * this.rotationVector.y;
      this.camera.roll += amount * this.rotationVector.z;
    } else {
      const amount = formation === 'focus'
        ? delta * rotationSpeed * 0.34 * Math.cos(this.time * 0.22)
        : delta * rotationSpeed * (0.14 + level * 0.18);
      if (rotationAxis === 'x') this.camera.pitch += amount;
      if (rotationAxis === 'y') this.camera.yaw += amount;
      if (rotationAxis === 'z') this.camera.roll += amount;
    }

    this.camera.yaw = normalizeAngle(this.camera.yaw);
    this.camera.pitch = normalizeAngle(this.camera.pitch);
    this.camera.roll = normalizeAngle(this.camera.roll);
  }

  frame(now) {
    if (this.destroyed) return;
    const delta = Math.min((now - this.lastFrame) / 1000, 0.05);
    this.lastFrame = now;
    this.time += delta;

    this.updateSignal(delta);
    this.updateCubes(delta, now);

    if (!this.drag) {
      this.updateAutoRotation(delta);
      this.camera.yaw = normalizeAngle(this.camera.yaw + this.camera.dragVelocityX);
      this.camera.pitch = normalizeAngle(this.camera.pitch + this.camera.dragVelocityY);
      this.camera.dragVelocityX *= Math.pow(0.9, delta * 60);
      this.camera.dragVelocityY *= Math.pow(0.9, delta * 60);
    }
    this.camera.distance = lerp(this.camera.distance, this.camera.targetDistance, 1 - Math.pow(0.83, delta * 60));

    this.render();
    this.frameRequest = requestAnimationFrame((nextNow) => this.frame(nextNow));
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this.frameRequest);
    this.resizeObserver.disconnect();
    const { onPointerDown, onPointerMove, onPointerUp, onWheel, onDoubleClick } = this.boundHandlers;
    this.canvas.removeEventListener('pointerdown', onPointerDown);
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    this.canvas.removeEventListener('wheel', onWheel);
    this.canvas.removeEventListener('dblclick', onDoubleClick);
    this.disconnectAnalyser();
  }
}

export {
  DEFAULT_PROFILE,
  DEFAULT_SETTINGS,
  PATTERN_NAMES,
  FORMATION_NAMES,
  ELEMENT_NAMES,
  ROTATION_AXIS_NAMES,
  PROFILE_SCHEMA,
  PROFILE_VERSION,
};
