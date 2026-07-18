import {
    DEFAULT_PROFILE,
    PROFILE_SCHEMA,
    PROFILE_VERSION,
    TalkingCubeRenderer,
} from "./talking-cube.js";
import {
    EMOTION_PROFILES,
    PRESENCE_PROFILES,
    applyEmotionLayer,
    inferEmotion,
} from "./emotion-layer.js";

"use strict";

// ── DOM refs ─────────────────────────────────────────────────
const statusText = document.getElementById("status-text");
const chatLog = document.getElementById("chat-log");
const talkBtn = document.getElementById("talk-btn");
const stopBtn = document.getElementById("stop-btn");
const textInput = document.getElementById("text-input");
const sendBtn = document.getElementById("send-btn");
const debugPanel = document.getElementById("debug-panel");
const debugToggle = document.getElementById("debug-toggle");
const debugContent = document.getElementById("debug-content");
const debugModalOverlay = document.getElementById("debug-modal-overlay");
const debugModalBody = document.getElementById("debug-modal-body");
const debugModalClose = document.getElementById("debug-modal-close");
const bargeInToggle = document.getElementById("barge-in-toggle");
const bargeInSensitivitySelect = document.getElementById("barge-in-sensitivity");
const bargeInAdaptiveToggle = document.getElementById("barge-in-adaptive");
const bargeInDebug = document.getElementById("barge-in-debug");
const voiceSelect = document.getElementById("voice-select");
const voicePreviewBtn = document.getElementById("voice-preview-btn");
const speedSlider = document.getElementById("speed-slider");
const speedValue = document.getElementById("speed-value");
const modelSelect = document.getElementById("model-select");
const sttSelect = document.getElementById("stt-select");
const vadSelect = document.getElementById("vad-select");
const phoneVoiceSelect = document.getElementById("phone-voice-select");
const phoneModelSelect = document.getElementById("phone-model-select");
const phoneSttSelect = document.getElementById("phone-stt-select");
const phoneSpeedSlider = document.getElementById("phone-speed-slider");
const phoneSpeedValue = document.getElementById("phone-speed-value");
const phoneCallStatus = document.getElementById("phone-call-status");
const flowSelect = document.getElementById("flow-select");
const flowWarning = document.getElementById("flow-warning");
const regionModelSelect = document.getElementById("region-model-select");
const goalRegionCard = document.getElementById("goal-region-card");
const flowGoal = document.getElementById("flow-goal");
const flowOutcome = document.getElementById("flow-outcome");
const flowSlotJob = document.getElementById("flow-slot-job");
const flowSlotJobValue = document.getElementById("flow-slot-job-value");
const flowSlotStart = document.getElementById("flow-slot-start");
const flowSlotStartValue = document.getElementById("flow-slot-start-value");
const flowSlotDuration = document.getElementById("flow-slot-duration");
const flowSlotDurationValue = document.getElementById("flow-slot-duration-value");
const flowBudget = document.getElementById("flow-budget");
const flowModel = document.getElementById("flow-model");
const flowLatency = document.getElementById("flow-latency");
const flowRejections = document.getElementById("flow-rejections");
const flowRejectionsList = document.getElementById("flow-rejections-list");
const benchmarkSupervisor = document.getElementById("benchmark-supervisor");
const benchmarkP50 = document.getElementById("benchmark-p50");
const benchmarkTurns = document.getElementById("benchmark-turns");
const latencyStt = document.getElementById("latency-stt");
const latencyLlm = document.getElementById("latency-llm");
const latencyTts = document.getElementById("latency-tts");
const latencyOverall = document.getElementById("latency-overall");
const talkingCubeCanvas = document.getElementById("talking-cube");
const talkingCubeStatus = document.getElementById("talking-cube-status");
const cubeScene = document.getElementById("cube-scene");
const cubePattern = document.getElementById("cube-pattern");
const cubeFormation = document.getElementById("cube-formation");
const cubePolygonSides = document.getElementById("cube-polygon-sides");
const cubeRotationAxis = document.getElementById("cube-rotation-axis");
const cubeElementShape = document.getElementById("cube-element-shape");
const cubeGridSize = document.getElementById("cube-grid-size");
const cubePalette = document.getElementById("cube-palette");
const cubePrimaryColor = document.getElementById("cube-primary-color");
const cubeSecondaryColor = document.getElementById("cube-secondary-color");
const cubeShowFrame = document.getElementById("cube-show-frame");
const cubeShowLinks = document.getElementById("cube-show-links");
const cubeAutoRotate = document.getElementById("cube-auto-rotate");
const cubePulse = document.getElementById("cube-pulse");
const cubeReset = document.getElementById("cube-reset");
const cubeExportProfile = document.getElementById("cube-export-profile");
const cubeImportProfile = document.getElementById("cube-import-profile");
const cubeProfileFile = document.getElementById("cube-profile-file");
const cubeProfileStatus = document.getElementById("cube-profile-status");

function setTalkButtonLabel(label) {
    const labelNode = talkBtn.querySelector(".talk-button-label");
    if (labelNode) labelNode.textContent = label;
    else talkBtn.textContent = label;
}

// ── Talking Cube visualization ───────────────────────────────
// The renderer calls its sphere-like shallow constellation a "focus" field,
// and its ripple animation a "wave". Keep those source API names here while
// presenting them as the calm field and voice wave in the nano-claw UI.
const VISUALIZATION_STORAGE_KEY = "nanoclaw.visualization.profile.v2";
const DEFAULT_VISUALIZATION_SETTINGS = DEFAULT_PROFILE.settings;

const VISUALIZATION_SCENES = Object.freeze({
    calm: DEFAULT_VISUALIZATION_SETTINGS,
    focus: {
        gridSize: 15,
        pattern: "focus",
        formation: "focus",
        elementShape: "orb",
        energy: 0.68,
        response: 0.78,
        speed: 0.36,
        bloom: 0.5,
        opacity: 0.68,
        density: 1,
        spread: 1.02,
        rotationSpeed: 0.16,
        rotationAxis: "y",
        showFrame: false,
        showLinks: false,
    },
    matrix: {
        gridSize: 15,
        pattern: "spectrum",
        formation: "focus",
        elementShape: "octagon",
        energy: 0.72,
        response: 0.84,
        speed: 0.45,
        bloom: 0.34,
        opacity: 0.76,
        density: 1,
        spread: 1,
        rotationSpeed: 0.1,
        rotationAxis: "y",
        showFrame: false,
        showLinks: false,
    },
    polygon: {
        gridSize: 15,
        pattern: "wave",
        formation: "polygon",
        polygonSides: 8,
        elementShape: "orb",
        energy: 0.8,
        response: 0.82,
        speed: 0.46,
        bloom: 0.52,
        opacity: 0.82,
        density: 1,
        spread: 1.08,
        rotationSpeed: 0.28,
        rotationAxis: "voice",
        showFrame: false,
        showLinks: false,
    },
    octahedron: {
        gridSize: 9,
        pattern: "wave",
        formation: "octahedron",
        elementShape: "octagon",
        energy: 0.68,
        response: 0.78,
        speed: 0.5,
        bloom: 0.48,
        opacity: 0.7,
        density: 0.9,
        spread: 1.04,
        rotationSpeed: 0.22,
        rotationAxis: "voice",
        showFrame: false,
        showLinks: false,
    },
    classic: {
        gridSize: 7,
        pattern: "wave",
        formation: "cube",
        elementShape: "voxel",
        energy: 0.72,
        response: 0.78,
        speed: 0.78,
        bloom: 0.68,
        opacity: 0.78,
        density: 1,
        spread: 1,
        rotationSpeed: 0.42,
        rotationAxis: "y",
        showFrame: true,
        showLinks: true,
    },
});

const CONSOLE_ACCENT = getComputedStyle(document.documentElement)
    .getPropertyValue("--accent").trim() || "#ff775f";
const CONSOLE_ACCENT_SECONDARY = getComputedStyle(document.documentElement)
    .getPropertyValue("--accent-2").trim() || "#ffca5c";

const VISUALIZATION_PALETTES = Object.freeze({
    nanoclaw: ["#2563eb", "#60a5fa"],
    electric: [CONSOLE_ACCENT, CONSOLE_ACCENT_SECONDARY],
    aurora: ["#52f5a8", "#31d7ff"],
    violet: ["#d86cff", "#715cff"],
    sunset: ["#ff775f", "#ffca5c"],
    mono: ["#f4f7f8", "#8da2ad"],
});

const VISUALIZATION_ENUMS = Object.freeze({
    pattern: ["focus", "wave", "spectrum", "helix", "scan", "nebula"],
    formation: ["focus", "polygon", "octahedron", "cube"],
    rotationAxis: ["x", "y", "z", "voice"],
    elementShape: ["orb", "octagon", "diamond", "voxel"],
});
const VISUALIZATION_RANGES = Object.freeze({
    energy: [0, 1],
    response: [0, 1],
    speed: [0.08, 1.4],
    bloom: [0, 1],
    opacity: [0.08, 1],
    density: [0.2, 1],
    spread: [0.68, 1.35],
    rotationSpeed: [0, 1.2],
    idleLevel: [0, 0.2],
});
const VISUALIZATION_GRID_SIZES = [5, 7, 9, 11, 13, 15];
const VISUALIZATION_POLYGON_SIDES = [3, 5, 8];
const VISUALIZATION_PATTERN_LABELS = Object.freeze({
    focus: "Lens drift",
    wave: "Voice wave",
    spectrum: "Spectrum",
    helix: "Double helix",
    scan: "Signal scan",
    nebula: "Nebula",
});

function validVisualizationColor(value) {
    return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function normalizeVisualizationSettings(candidate) {
    var input = candidate && typeof candidate === "object" ? candidate : {};
    var settings = Object.assign({}, DEFAULT_VISUALIZATION_SETTINGS);
    Object.keys(VISUALIZATION_ENUMS).forEach(function (key) {
        if (VISUALIZATION_ENUMS[key].indexOf(input[key]) >= 0) settings[key] = input[key];
    });
    Object.keys(VISUALIZATION_RANGES).forEach(function (key) {
        var value = Number(input[key]);
        if (!Number.isFinite(value)) return;
        var range = VISUALIZATION_RANGES[key];
        settings[key] = Math.max(range[0], Math.min(range[1], value));
    });
    var gridSize = Number(input.gridSize);
    if (VISUALIZATION_GRID_SIZES.indexOf(gridSize) >= 0) settings.gridSize = gridSize;
    var polygonSides = Number(input.polygonSides);
    if (VISUALIZATION_POLYGON_SIDES.indexOf(polygonSides) >= 0) settings.polygonSides = polygonSides;
    if (validVisualizationColor(input.primary)) settings.primary = input.primary;
    if (validVisualizationColor(input.secondary)) settings.secondary = input.secondary;
    ["autoRotate", "showLinks", "showFrame"].forEach(function (key) {
        if (typeof input[key] === "boolean") settings[key] = input[key];
    });
    return settings;
}

function defaultVisualizationProfile() {
    return {
        schema: PROFILE_SCHEMA,
        version: PROFILE_VERSION,
        settings: Object.assign({}, DEFAULT_PROFILE.settings),
        camera: Object.assign({}, DEFAULT_PROFILE.camera),
    };
}

function validStoredVisualizationProfile(profile) {
    if (!profile || typeof profile !== "object" || Array.isArray(profile)) return false;
    if (profile.schema !== PROFILE_SCHEMA || profile.version !== PROFILE_VERSION) return false;
    if (!profile.settings || typeof profile.settings !== "object" || Array.isArray(profile.settings)) return false;
    if (!profile.camera || typeof profile.camera !== "object" || Array.isArray(profile.camera)) return false;
    return ["yaw", "pitch", "roll", "distance"].every(function (key) {
        return Number.isFinite(Number(profile.camera[key]));
    });
}

function loadVisualizationSettings() {
    var fallback = { scene: "calm", profile: defaultVisualizationProfile() };
    try {
        var stored = JSON.parse(localStorage.getItem(VISUALIZATION_STORAGE_KEY) || "null");
        if (!validStoredVisualizationProfile(stored)) return fallback;
        var scene = stored && Object.prototype.hasOwnProperty.call(VISUALIZATION_SCENES, stored.scene)
            ? stored.scene
            : "custom";
        return {
            scene: scene,
            profile: {
                schema: PROFILE_SCHEMA,
                version: PROFILE_VERSION,
                settings: normalizeVisualizationSettings(stored.settings),
                camera: {
                    yaw: Number(stored.camera.yaw),
                    pitch: Number(stored.camera.pitch),
                    roll: Number(stored.camera.roll),
                    distance: Number(stored.camera.distance),
                },
            },
        };
    } catch (_e) {
        return fallback;
    }
}

var loadedVisualization = loadVisualizationSettings();
var visualizationScene = loadedVisualization.scene;
var visualizationSettings = loadedVisualization.profile.settings;
var visualizationSpeaking = false;
var callerVisualizationActive = false;
var visualizationMoment = null;
var visualizationMomentTimer = null;
var visualizationMomentVersion = 0;
var lastFlowOutcomeSignature = "";
var lastFlowRejectionsSignature = "";
var lastFlowTranscriptState = null;
var supervisorSamples = [];
var agentAudioContext = null;
var agentAudioSource = null;
var agentAudioAnalyser = null;

const talkingCube = new TalkingCubeRenderer(talkingCubeCanvas, visualizationSettings);
talkingCube.importProfile(loadedVisualization.profile);
talkingCube.setPanelOpen(false);
window.TalkingCube = talkingCube;
window.VoiceCube = talkingCube;

// ── Emotion layer state ──────────────────────────────────────
// Sits between the user's base profile and the caller/moment/speaking
// overlays (see effectiveVisualizationSettings). Auto mode infers an
// emotion from each agent reply; manual mode (panel or window.VoiceEmotion)
// pins one until auto is re-enabled.
var emotionState = { emotion: "neutral", intensity: 0.7, presence: "idle" };
var emotionAuto = true;

function setVisualEmotion(name, opts) {
    opts = opts || {};
    if (!Object.prototype.hasOwnProperty.call(EMOTION_PROFILES, name)) return false;
    emotionState.emotion = name;
    if (opts.intensity != null) {
        emotionState.intensity = Math.max(0, Math.min(1, Number(opts.intensity) || 0));
    }
    applyVisualizationLayers();
    return true;
}

function setVisualPresence(name) {
    if (!Object.prototype.hasOwnProperty.call(PRESENCE_PROFILES, name)) return false;
    if (emotionState.presence === name) return true;
    emotionState.presence = name;
    applyVisualizationLayers();
    return true;
}

function inferEmotionFromReply(text) {
    if (!emotionAuto) return;
    var inferred = inferEmotion(text);
    setVisualEmotion(inferred.emotion, { intensity: inferred.intensity });
}

window.VoiceEmotion = {
    set: function (name, opts) { emotionAuto = false; return setVisualEmotion(name, opts); },
    presence: setVisualPresence,
    auto: function (on) { emotionAuto = on !== false; return emotionAuto; },
    state: function () { return Object.assign({ auto: emotionAuto }, emotionState); },
    profiles: Object.keys(EMOTION_PROFILES),
};

function effectiveVisualizationSettings() {
    var base = applyEmotionLayer(visualizationSettings, emotionState);
    var primary = base.primary;
    var secondary = base.secondary;
    if (callerVisualizationActive && !visualizationSpeaking) {
        primary = base.secondary;
        secondary = base.primary;
    }
    if (visualizationMoment) {
        primary = visualizationMoment.primary;
        secondary = visualizationMoment.secondary;
    }
    return Object.assign({}, base, {
        pattern: visualizationSpeaking ? "wave" : base.pattern,
        energy: visualizationSpeaking
            ? Math.max(0.58, base.energy)
            : base.energy,
        primary: primary,
        secondary: secondary,
    });
}

function updateTalkingCubeStatus() {
    if (visualizationSpeaking) {
        talkingCubeStatus.textContent = "Agent speaking · Voice wave";
    } else if (visualizationMoment && visualizationMoment.label) {
        talkingCubeStatus.textContent = visualizationMoment.label;
    } else if (callerVisualizationActive) {
        talkingCubeStatus.textContent = "Caller speaking · Secondary color";
    } else {
        var idleLabel = "Idle · " + VISUALIZATION_PATTERN_LABELS[visualizationSettings.pattern];
        if (emotionState.emotion !== "neutral") {
            idleLabel += " · " + emotionState.emotion + (emotionAuto ? " (auto)" : "");
        }
        talkingCubeStatus.textContent = idleLabel;
    }
}

function applyVisualizationLayers() {
    var effective = effectiveVisualizationSettings();
    talkingCube.configure(effective);
    talkingCube.setSpeaking(visualizationSpeaking);
    updateTalkingCubeStatus();
}

function currentVisualizationProfile() {
    return {
        schema: PROFILE_SCHEMA,
        version: PROFILE_VERSION,
        settings: Object.assign({}, visualizationSettings),
        camera: Object.assign({}, talkingCube.getProfile().camera),
    };
}

function persistVisualizationSettings() {
    try {
        var profile = currentVisualizationProfile();
        profile.scene = visualizationScene;
        localStorage.setItem(VISUALIZATION_STORAGE_KEY, JSON.stringify(profile));
    } catch (_e) {
        // Storage can be unavailable in privacy modes; live controls still work.
    }
}

function visualizationRangeLabel(input) {
    var value = Number(input.value);
    if (input.dataset.cubeSetting === "spread") return Math.round(value * 100) + "%";
    if (input.dataset.cubeSetting === "speed") {
        return Math.round((value - Number(input.min)) / (Number(input.max) - Number(input.min)) * 100) + "%";
    }
    if (input.dataset.cubeSetting === "rotationSpeed") {
        return Math.round(value / Number(input.max) * 100) + "%";
    }
    return Math.round(value * 100) + "%";
}

function matchingVisualizationPalette() {
    return Object.keys(VISUALIZATION_PALETTES).find(function (name) {
        var colors = VISUALIZATION_PALETTES[name];
        return colors[0].toLowerCase() === visualizationSettings.primary.toLowerCase()
            && colors[1].toLowerCase() === visualizationSettings.secondary.toLowerCase();
    }) || "custom";
}

function syncVisualizationControls() {
    cubeScene.value = visualizationScene;
    cubePattern.value = visualizationSettings.pattern;
    cubeFormation.value = visualizationSettings.formation;
    cubePolygonSides.value = String(visualizationSettings.polygonSides);
    cubePolygonSides.disabled = visualizationSettings.formation !== "polygon";
    cubePolygonSides.closest(".pipe-row").classList.toggle(
        "is-disabled",
        visualizationSettings.formation !== "polygon",
    );
    cubeRotationAxis.value = visualizationSettings.rotationAxis;
    cubeElementShape.value = visualizationSettings.elementShape;
    cubeGridSize.value = String(visualizationSettings.gridSize);
    cubePalette.value = matchingVisualizationPalette();
    cubePrimaryColor.value = visualizationSettings.primary;
    cubeSecondaryColor.value = visualizationSettings.secondary;
    cubeShowFrame.checked = visualizationSettings.showFrame;
    cubeShowLinks.checked = visualizationSettings.showLinks;
    cubeAutoRotate.checked = visualizationSettings.autoRotate;
    document.querySelectorAll("[data-cube-setting]").forEach(function (input) {
        input.value = String(visualizationSettings[input.dataset.cubeSetting]);
        var output = input.parentElement.querySelector("output");
        if (output) output.textContent = visualizationRangeLabel(input);
    });
}

function storeVisualizationChange(partial) {
    visualizationSettings = normalizeVisualizationSettings(Object.assign({}, visualizationSettings, partial));
    visualizationScene = "custom";
    applyVisualizationLayers();
    persistVisualizationSettings();
    syncVisualizationControls();
}

cubeScene.addEventListener("change", function () {
    var name = cubeScene.value;
    var preset = VISUALIZATION_SCENES[name];
    if (!preset) return;
    visualizationSettings = name === "calm"
        ? Object.assign({}, DEFAULT_VISUALIZATION_SETTINGS)
        : normalizeVisualizationSettings(Object.assign({}, visualizationSettings, preset));
    visualizationScene = name;
    if (name === "calm") talkingCube.importProfile(DEFAULT_PROFILE);
    applyVisualizationLayers();
    persistVisualizationSettings();
    syncVisualizationControls();
    talkingCube.pulse({ strength: 0.9, duration: 980 });
});

const cubeEmotion = document.getElementById("cube-emotion");
cubeEmotion.addEventListener("change", function () {
    if (cubeEmotion.value === "auto") {
        emotionAuto = true;
        setVisualEmotion("neutral");
    } else {
        emotionAuto = false;
        setVisualEmotion(cubeEmotion.value, { intensity: 0.85 });
    }
});

cubePattern.addEventListener("change", function () {
    storeVisualizationChange({ pattern: cubePattern.value });
    if (!visualizationSpeaking) talkingCube.setPattern(visualizationSettings.pattern);
});
cubeFormation.addEventListener("change", function () {
    storeVisualizationChange({ formation: cubeFormation.value });
});
cubePolygonSides.addEventListener("change", function () {
    storeVisualizationChange({ polygonSides: Number(cubePolygonSides.value) });
    talkingCube.pulse({ strength: 0.92, duration: 900 });
});
cubeRotationAxis.addEventListener("change", function () {
    storeVisualizationChange({ rotationAxis: cubeRotationAxis.value });
});
cubeElementShape.addEventListener("change", function () {
    storeVisualizationChange({ elementShape: cubeElementShape.value });
});
cubeGridSize.addEventListener("change", function () {
    storeVisualizationChange({ gridSize: Number(cubeGridSize.value) });
    talkingCube.pulse({ strength: 0.8, duration: 780 });
});

cubePalette.addEventListener("change", function () {
    var colors = VISUALIZATION_PALETTES[cubePalette.value];
    if (!colors) return;
    storeVisualizationChange({ primary: colors[0], secondary: colors[1] });
    var effective = effectiveVisualizationSettings();
    talkingCube.setColors(effective.primary, effective.secondary);
    talkingCube.pulse({ strength: 0.55, color: colors[0], duration: 720 });
});

[cubePrimaryColor, cubeSecondaryColor].forEach(function (input) {
    input.addEventListener("input", function () {
        storeVisualizationChange({
            primary: cubePrimaryColor.value,
            secondary: cubeSecondaryColor.value,
        });
        var effective = effectiveVisualizationSettings();
        talkingCube.setColors(effective.primary, effective.secondary);
    });
});

document.querySelectorAll("[data-cube-setting]").forEach(function (input) {
    input.addEventListener("input", function () {
        var output = input.parentElement.querySelector("output");
        if (output) output.textContent = visualizationRangeLabel(input);
        storeVisualizationChange({ [input.dataset.cubeSetting]: Number(input.value) });
    });
});

[
    [cubeShowFrame, "showFrame"],
    [cubeShowLinks, "showLinks"],
    [cubeAutoRotate, "autoRotate"],
].forEach(function (entry) {
    entry[0].addEventListener("change", function () {
        storeVisualizationChange({ [entry[1]]: entry[0].checked });
    });
});

cubePulse.addEventListener("click", function () {
    talkingCube.pulse({ strength: 1.25, duration: 1100 });
});
cubeReset.addEventListener("click", function () {
    visualizationSettings = Object.assign({}, DEFAULT_VISUALIZATION_SETTINGS);
    visualizationScene = "calm";
    talkingCube.importProfile(DEFAULT_PROFILE);
    applyVisualizationLayers();
    persistVisualizationSettings();
    syncVisualizationControls();
    talkingCube.pulse({ strength: 0.8, duration: 900 });
});

var profileStatusTimer = 0;

function showVisualizationProfileStatus(message, type) {
    clearTimeout(profileStatusTimer);
    cubeProfileStatus.textContent = message;
    cubeProfileStatus.classList.toggle("success", type === "success");
    cubeProfileStatus.classList.toggle("error", type === "error");
    profileStatusTimer = window.setTimeout(function () {
        cubeProfileStatus.textContent = "PROFILE V2";
        cubeProfileStatus.classList.remove("success", "error");
    }, 2800);
}

cubeExportProfile.addEventListener("click", function () {
    var json = JSON.stringify(currentVisualizationProfile(), null, 2);
    var blob = new Blob([json], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "voxels-profile-" + new Date().toISOString().slice(0, 10) + ".json";
    link.click();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
    showVisualizationProfileStatus("JSON SAVED", "success");
});

cubeImportProfile.addEventListener("click", function () {
    cubeProfileFile.click();
});

cubeProfileFile.addEventListener("change", async function () {
    var file = cubeProfileFile.files[0];
    if (!file) return;
    try {
        var state = talkingCube.importProfile(await file.text());
        visualizationSettings = normalizeVisualizationSettings(state.settings);
        visualizationScene = "custom";
        applyVisualizationLayers();
        persistVisualizationSettings();
        syncVisualizationControls();
        talkingCube.pulse({ strength: 1.05, duration: 1000 });
        showVisualizationProfileStatus("PROFILE LOADED", "success");
    } catch (error) {
        console.error(error);
        showVisualizationProfileStatus("INVALID PROFILE", "error");
    } finally {
        cubeProfileFile.value = "";
    }
});

var visualizationCameraDrag = false;
var visualizationCameraPersistTimer = 0;

function scheduleVisualizationCameraPersist() {
    clearTimeout(visualizationCameraPersistTimer);
    visualizationCameraPersistTimer = window.setTimeout(persistVisualizationSettings, 160);
}

talkingCubeCanvas.addEventListener("pointerdown", function () {
    visualizationCameraDrag = true;
});
window.addEventListener("pointerup", function () {
    if (!visualizationCameraDrag) return;
    visualizationCameraDrag = false;
    scheduleVisualizationCameraPersist();
});
talkingCubeCanvas.addEventListener("wheel", scheduleVisualizationCameraPersist);
talkingCubeCanvas.addEventListener("dblclick", scheduleVisualizationCameraPersist);
window.addEventListener("pagehide", persistVisualizationSettings);

function startVisualizationMoment(options) {
    visualizationMomentVersion += 1;
    var version = visualizationMomentVersion;
    if (visualizationMomentTimer) clearTimeout(visualizationMomentTimer);
    visualizationMoment = {
        primary: options.primary,
        secondary: options.secondary,
        label: options.label,
    };
    applyVisualizationLayers();
    talkingCube.setColors(options.primary, options.secondary);
    talkingCube.pulse({
        strength: options.strength,
        duration: options.pulseDuration,
        color: options.primary,
    });
    visualizationMomentTimer = setTimeout(function () {
        if (version !== visualizationMomentVersion) return;
        visualizationMoment = null;
        visualizationMomentTimer = null;
        applyVisualizationLayers();
    }, options.duration);
}

function updateFlowVisualization(state, outcome, rejected) {
    var slots = state.slots && typeof state.slots === "object" ? state.slots : {};
    var outcomeSignature = outcome ? JSON.stringify([
        state.goal || "",
        outcome,
        slots.job || "",
        slots.slot_start || "",
        slots.duration_minutes || "",
    ]) : "";
    if (outcomeSignature && outcomeSignature !== lastFlowOutcomeSignature) {
        if (outcome === "booked") {
            startVisualizationMoment({
                primary: "#22c55e",
                secondary: "#86efac",
                label: "Booked · Celebration",
                strength: 1.3,
                pulseDuration: 1500,
                duration: 2000,
            });
        } else if (outcome === "escape" || outcome === "budget") {
            startVisualizationMoment({
                primary: "#64748b",
                secondary: "#334155",
                label: outcome === "escape" ? "Flow ended · Muted" : "Budget reached · Muted",
                strength: 0.5,
                pulseDuration: 700,
                duration: 1400,
            });
        }
    }
    lastFlowOutcomeSignature = outcomeSignature;

    var rejectionSignature = JSON.stringify(rejected);
    if (rejected.length && rejectionSignature !== lastFlowRejectionsSignature) {
        talkingCube.pulse({ strength: 0.38, duration: 480, color: "#ef4444" });
    }
    lastFlowRejectionsSignature = rejectionSignature;
}

function setVisualizationSpeaking(speaking) {
    var next = Boolean(speaking);
    if (next === visualizationSpeaking) return;
    visualizationSpeaking = next;
    callerVisualizationActive = false;
    talkingCube.setSpeaking(next);
    if (next) {
        if (agentAudioAnalyser) talkingCube.connectAnalyser(agentAudioAnalyser);
        if (agentAudioContext && agentAudioContext.state === "suspended") {
            agentAudioContext.resume().catch(function () {});
        }
    } else {
        talkingCube.disconnectAnalyser();
    }
    applyVisualizationLayers();
    var effective = effectiveVisualizationSettings();
    talkingCube.setPattern(effective.pattern, { energy: effective.energy });
    talkingCube.setColors(effective.primary, effective.secondary);
}

function teardownAgentAudioAnalyser() {
    if (agentAudioAnalyser) talkingCube.disconnectAnalyser();
    if (agentAudioSource) {
        try { agentAudioSource.disconnect(); } catch (_e) { /* already disconnected */ }
    }
    if (agentAudioAnalyser) {
        try { agentAudioAnalyser.disconnect(); } catch (_e) { /* already disconnected */ }
    }
    if (agentAudioContext && agentAudioContext.state !== "closed") {
        agentAudioContext.close().catch(function () {});
    }
    agentAudioSource = null;
    agentAudioAnalyser = null;
    agentAudioContext = null;
}

function setupAgentAudioAnalyser(stream) {
    teardownAgentAudioAnalyser();
    var AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !stream) return;
    try {
        agentAudioContext = new AudioContextClass();
        agentAudioSource = agentAudioContext.createMediaStreamSource(stream);
        agentAudioAnalyser = agentAudioContext.createAnalyser();
        agentAudioAnalyser.fftSize = 512;
        agentAudioAnalyser.smoothingTimeConstant = 0.72;
        agentAudioSource.connect(agentAudioAnalyser);
        if (visualizationSpeaking) {
            talkingCube.connectAnalyser(agentAudioAnalyser);
            agentAudioContext.resume().catch(function () {});
        }
    } catch (_e) {
        teardownAgentAudioAnalyser();
    }
}

function stopCallerVisualization() {
    if (!callerVisualizationActive) return;
    callerVisualizationActive = false;
    if (!visualizationSpeaking) applyVisualizationLayers();
}

function driveCallerVisualization(rms) {
    if (visualizationSpeaking) return;
    // Agent analysis uses a 4.2x RMS gain inside the renderer; 2.2x keeps the
    // caller intentionally quieter while still making barge-in visible.
    var level = Math.max(0, Math.min(1, Number(rms) * 2.2));
    var active = level > 0.04;
    if (active !== callerVisualizationActive) {
        callerVisualizationActive = active;
        applyVisualizationLayers();
        if (active) {
            talkingCube.setColors(visualizationSettings.secondary, visualizationSettings.primary);
            talkingCube.pulse({ strength: 0.28, duration: 420, color: visualizationSettings.secondary });
        }
    }
    talkingCube.pushAudioFrame({
        level: level,
        speaking: active,
        source: "caller-mic",
    });
}

syncVisualizationControls();
applyVisualizationLayers();

// VAD dropdown (phone line): mirrors the STT/LLM/Voice pipeline selectors.
// Applies to NEW phone calls; served by GET/POST /api/phone/vad.
fetch("/api/phone/vad").then(function (r) { return r.json(); }).then(function (v) {
    v.options.forEach(function (mode) {
        var o = document.createElement("option");
        o.value = mode;
        o.textContent = mode === "silero"
            ? "silero (neural)" + (v.silero_available ? "" : " — unavailable")
            : "energy (threshold)";
        o.disabled = mode === "silero" && !v.silero_available;
        vadSelect.appendChild(o);
    });
    vadSelect.value = v.active;
}).catch(function () {
    var o = document.createElement("option");
    o.textContent = "n/a (phone disabled)";
    vadSelect.appendChild(o);
    vadSelect.disabled = true;
});
vadSelect.addEventListener("change", function () {
    fetch("/api/phone/vad", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: vadSelect.value }),
    });
});

// ── Phone line controls (voice/model/speed) ──────────────────
// Served by GET/POST /api/phone/config. Voice + speed apply live (next
// spoken sentence, even mid-call); model applies on the next agent turn.
var phonePendingModel = null; // config may load before the models list

function applyPhoneConfig(cfg) {
    // Don't yank a control out from under the user mid-edit (the 5s poll
    // would otherwise snap an open dropdown back); the lamp always updates.
    var editing = [phoneVoiceSelect, phoneModelSelect, phoneSttSelect, phoneSpeedSlider]
        .indexOf(document.activeElement) >= 0;
    if (!editing) {
        phoneVoiceSelect.value = cfg.voice;
        if (phoneModelSelect.options.length > 0) {
            phoneModelSelect.value = cfg.model || "";
        } else {
            phonePendingModel = cfg.model || "";
        }
        phoneSttSelect.value = cfg.stt_size;
        phoneSpeedSlider.value = String(cfg.speed);
        phoneSpeedValue.textContent = cfg.speed.toFixed(1) + "×";
    }
    phoneCallStatus.classList.remove("offline");
    if (cfg.active_calls > 0) {
        phoneCallStatus.textContent = "● live · " + cfg.active_calls +
            (cfg.active_calls === 1 ? " caller" : " callers");
        phoneCallStatus.title = "A call is up — voice, STT, speed, and flow changes apply mid-call";
        phoneCallStatus.classList.add("live");
    } else {
        phoneCallStatus.textContent = "● idle";
        phoneCallStatus.title = "No call in progress";
        phoneCallStatus.classList.remove("live");
    }
}

function phoneControlsUnavailable() {
    [phoneVoiceSelect, phoneModelSelect, phoneSttSelect, phoneSpeedSlider].forEach(function (el) { el.disabled = true; });
    var o = document.createElement("option");
    o.textContent = "n/a (phone disabled)";
    phoneVoiceSelect.appendChild(o);
    phoneCallStatus.textContent = "● offline";
    phoneCallStatus.title = "Phone gateway is not enabled on this node";
    phoneCallStatus.classList.remove("live");
    phoneCallStatus.classList.add("offline");
}

function loadPhoneConfig() {
    fetch("/api/phone/config").then(function (r) {
        if (!r.ok) { throw new Error("phone disabled"); }
        return r.json();
    }).then(applyPhoneConfig).catch(phoneControlsUnavailable);
}

function pushPhoneConfig(partial) {
    fetch("/api/phone/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partial),
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cfg) { if (cfg) { applyPhoneConfig(cfg); } });
}

phoneVoiceSelect.addEventListener("change", function () {
    pushPhoneConfig({ voice: phoneVoiceSelect.value });
});
phoneModelSelect.addEventListener("change", function () {
    pushPhoneConfig({ model: phoneModelSelect.value });
});
phoneSttSelect.addEventListener("change", function () {
    pushPhoneConfig({ stt_size: phoneSttSelect.value });
});
phoneSpeedSlider.addEventListener("input", function () {
    phoneSpeedValue.textContent = parseFloat(phoneSpeedSlider.value).toFixed(1) + "×";
});
phoneSpeedSlider.addEventListener("change", function () {
    pushPhoneConfig({ speed: parseFloat(phoneSpeedSlider.value) });
});

function renderFlowConfig(config) {
    var options = Array.isArray(config.options) ? config.options : ["off", "scheduler"];
    flowSelect.innerHTML = "";
    options.forEach(function (mode) {
        var o = document.createElement("option");
        o.value = mode;
        o.textContent = mode === "scheduler" ? "Plumber scheduler" : "Off";
        flowSelect.appendChild(o);
    });
    flowSelect.value = options.indexOf(config.active) >= 0 ? config.active : "off";
    flowWarning.textContent = "Scheduler availability is unavailable";
    flowWarning.classList.toggle("hidden", config.availability_ok === true);
}

function loadFlowConfig() {
    return fetch("/api/voice/flow").then(function (r) { return r.json(); }).then(function (config) {
        renderFlowConfig(config || {});
        flowSelect.disabled = false;
    }).catch(function () {
        flowSelect.innerHTML = "";
        var o = document.createElement("option");
        o.textContent = "n/a";
        flowSelect.appendChild(o);
        flowSelect.disabled = true;
        flowWarning.classList.remove("hidden");
        flowWarning.textContent = "Could not load flow settings";
    });
}

flowSelect.addEventListener("change", function () {
    flowSelect.disabled = true;
    fetch("/api/voice/flow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: flowSelect.value }),
    }).then(function (r) {
        if (!r.ok) throw new Error("flow update failed");
        return r.json();
    }).then(function (config) {
        renderFlowConfig(config || {});
        statusText.textContent = "Flow updated — phone applies it on the caller's next utterance";
    }).catch(loadFlowConfig).finally(function () {
        flowSelect.disabled = false;
    });
});

loadFlowConfig();
var activeRegionModel = "";

function renderRegionModelConfig(config) {
    var options = Array.isArray(config.options) ? config.options : [];
    var active = typeof config.active === "string" ? config.active : "";
    regionModelSelect.innerHTML = "";
    options.forEach(function (option) {
        if (!option || typeof option.value !== "string") return;
        var o = document.createElement("option");
        o.value = option.value;
        o.textContent = typeof option.label === "string" ? option.label : option.value;
        regionModelSelect.appendChild(o);
    });
    if (active && !Array.from(regionModelSelect.options).some(function (o) { return o.value === active; })) {
        var current = document.createElement("option");
        current.value = active;
        current.textContent = active + " — environment default";
        regionModelSelect.insertBefore(current, regionModelSelect.firstChild);
    }
    regionModelSelect.value = active;
    activeRegionModel = active;
    flowModel.textContent = active ? "model " + active : "model —";
}

function loadRegionModelConfig() {
    return fetch("/api/voice/region-model").then(function (r) {
        if (!r.ok) throw new Error("scheduler model unavailable");
        return r.json();
    }).then(function (config) {
        renderRegionModelConfig(config || {});
        regionModelSelect.disabled = false;
    }).catch(function () {
        regionModelSelect.innerHTML = "";
        var o = document.createElement("option");
        o.textContent = "n/a";
        regionModelSelect.appendChild(o);
        regionModelSelect.disabled = true;
        activeRegionModel = "";
        flowModel.textContent = "model —";
    });
}

regionModelSelect.addEventListener("change", function () {
    regionModelSelect.disabled = true;
    fetch("/api/voice/region-model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: regionModelSelect.value }),
    }).then(function (r) {
        if (!r.ok) throw new Error("scheduler model update failed");
        return r.json();
    }).then(function (config) {
        renderRegionModelConfig(config || {});
        statusText.textContent = "Scheduler model updated — new turns use it immediately";
    }).catch(loadRegionModelConfig).finally(function () {
        regionModelSelect.disabled = false;
    });
});

loadRegionModelConfig();

// ── State ────────────────────────────────────────────────────
let ws = null;
let pc = null;
let audioEl = null;
let micStream = null;
let isRecording = false;
let agentSpeaking = false;
let phoneModeEnabled = false;
let autoTurnPending = false;
let audioConnected = false;
let vadAudioContext = null;
let vadAnalyser = null;
let vadSamples = null;
let vadGate = null;
let vadFrameRequest = null;
let vadCalibration = [];
let vadCalibrationUntil = 0;
let vadRearmAt = 0;
let bargeInEnabled = false;
let bargeInServerAvailable = null;
let bargeDetector = null;
let bargeInController = null;
let bargeInFlashTimer = null;
let activeTurnStartedAt = null;
let sttRequestStartedAt = null;
let firstAgentTextAt = null;

const PHONE_CALIBRATION_MS = 700;
const PHONE_REARM_MS = 650;
const LS_BARGE_IN_ENABLED = "nanoclaw.bargeIn.enabled";
const LS_BARGE_IN_SENSITIVITY = "nanoclaw.bargeIn.sensitivity";
const LS_BARGE_IN_ADAPTIVE = "nanoclaw.bargeIn.adaptive";
const BARGE_IN_LEVELS = window.BargeInSensitivityLevels || {
    low: { startThreshold: 0.09, sustainThreshold: 0.054 },
    medium: { startThreshold: 0.05, sustainThreshold: 0.03 },
    high: { startThreshold: 0.03, sustainThreshold: 0.018 },
};

let bargeInUserEnabled = localStorage.getItem(LS_BARGE_IN_ENABLED) !== "false";
let bargeInSensitivity = localStorage.getItem(LS_BARGE_IN_SENSITIVITY) || "medium";
let bargeInAdaptiveEnabled = localStorage.getItem(LS_BARGE_IN_ADAPTIVE) !== "false";
if (!Object.prototype.hasOwnProperty.call(BARGE_IN_LEVELS, bargeInSensitivity)) {
    bargeInSensitivity = "medium";
}

function syncBargeInControls() {
    const available = bargeInServerAvailable === true;
    bargeInEnabled = available && bargeInUserEnabled;
    bargeInToggle.checked = bargeInUserEnabled;
    bargeInToggle.disabled = !available;
    bargeInSensitivitySelect.value = bargeInSensitivity;
    bargeInSensitivitySelect.disabled = !available || !bargeInUserEnabled;
    bargeInAdaptiveToggle.checked = bargeInAdaptiveEnabled;
    bargeInAdaptiveToggle.disabled = !available || !bargeInUserEnabled;
}

function renderBargeInStats() {
    if (bargeInServerAvailable === null) {
        bargeInDebug.textContent = "barge-in · awaiting server capability";
        return;
    }
    if (!bargeInServerAvailable) {
        bargeInDebug.textContent = "barge-in · disabled by server";
        return;
    }
    if (!bargeInController) {
        bargeInDebug.textContent = "barge-in · adaptive controller unavailable";
        return;
    }
    const stats = bargeInController.stats();
    bargeInDebug.textContent = "barge-in · " + (bargeInEnabled ? "on" : "off") +
        " · " + (bargeInAdaptiveEnabled ? "adaptive" : "manual") +
        " · threshold " + stats.currentThreshold.toFixed(3) +
        " / base " + stats.baseThreshold.toFixed(3) +
        " · commits " + stats.commits +
        " · falses " + stats.falses +
        " · adjustments " + stats.adjustments;
}

function createBargeInController() {
    if (typeof window.BargeInDetector !== "function" ||
            typeof window.AdaptiveBargeInController !== "function") {
        bargeDetector = null;
        bargeInController = null;
        renderBargeInStats();
        return;
    }
    const thresholds = BARGE_IN_LEVELS[bargeInSensitivity];
    bargeDetector = new window.BargeInDetector({
        startThreshold: thresholds.startThreshold,
        sustainThreshold: thresholds.sustainThreshold,
    });
    bargeInController = new window.AdaptiveBargeInController(bargeDetector, {
        baseStartThreshold: thresholds.startThreshold,
        baseSustainThreshold: thresholds.sustainThreshold,
        adaptive: bargeInAdaptiveEnabled,
    });
    if (window.BargeIn) window.BargeIn.controller = bargeInController;
    renderBargeInStats();
}

function resetBargeInDetector() {
    if (bargeInController) bargeInController.reset();
    else if (bargeDetector) bargeDetector.reset();
}

function flashBargeInAdjustment(message) {
    const previous = statusText.textContent;
    if (bargeInFlashTimer) clearTimeout(bargeInFlashTimer);
    statusText.textContent = message;
    bargeInFlashTimer = setTimeout(function () {
        if (statusText.textContent === message) statusText.textContent = previous;
        bargeInFlashTimer = null;
    }, 3500);
}

function sampleBargeIn(rms, now) {
    if (!bargeInController) return { event: null, adjustmentMessage: null };
    const before = bargeInController.stats();
    const event = bargeInController.sample(rms, now);
    const after = bargeInController.stats();
    const outcome = event && (event.type === "barge_in_commit" || event.type === "barge_in_false");
    if (outcome || after.adjustments !== before.adjustments) renderBargeInStats();

    let adjustmentMessage = null;
    if (after.adjustments !== before.adjustments) {
        adjustmentMessage = after.currentThreshold > before.currentThreshold
            ? "Barge-in sensitivity auto-raised (" + bargeInController.minFalses + " false alarms)"
            : "Barge-in sensitivity auto-recovered toward " + bargeInSensitivity;
    }
    return { event: event, adjustmentMessage: adjustmentMessage };
}

bargeInToggle.addEventListener("change", function () {
    const wasEnabled = bargeInEnabled;
    bargeInUserEnabled = bargeInToggle.checked;
    localStorage.setItem(LS_BARGE_IN_ENABLED, String(bargeInUserEnabled));
    syncBargeInControls();
    if (wasEnabled && !bargeInEnabled && bargeDetector && bargeDetector.pending) {
        sendMsg("barge_in_false");
    }
    resetBargeInDetector();
    renderBargeInStats();
});

bargeInSensitivitySelect.addEventListener("change", function () {
    bargeInSensitivity = bargeInSensitivitySelect.value;
    localStorage.setItem(LS_BARGE_IN_SENSITIVITY, bargeInSensitivity);
    if (bargeInController) bargeInController.setSensitivity(bargeInSensitivity);
    renderBargeInStats();
});

bargeInAdaptiveToggle.addEventListener("change", function () {
    bargeInAdaptiveEnabled = bargeInAdaptiveToggle.checked;
    localStorage.setItem(LS_BARGE_IN_ADAPTIVE, String(bargeInAdaptiveEnabled));
    if (bargeInController) bargeInController.setAdaptiveEnabled(bargeInAdaptiveEnabled);
    renderBargeInStats();
});

syncBargeInControls();
renderBargeInStats();

function resetTurnLatency() {
    latencyStt.textContent = "–";
    latencyLlm.textContent = "–";
    latencyTts.textContent = "–";
    latencyOverall.textContent = "–";
}

function beginTurnLatency(measureStt) {
    activeTurnStartedAt = performance.now();
    sttRequestStartedAt = measureStt ? activeTurnStartedAt : null;
    firstAgentTextAt = null;
    resetTurnLatency();
}

function markTranscriptionLatency() {
    if (sttRequestStartedAt === null) return;
    latencyStt.textContent = formatLatency(performance.now() - sttRequestStartedAt);
    sttRequestStartedAt = null;
}

function markFirstAgentTextLatency() {
    if (firstAgentTextAt !== null) return;
    firstAgentTextAt = performance.now();
    if (activeTurnStartedAt !== null) {
        latencyOverall.textContent = formatLatency(firstAgentTextAt - activeTurnStartedAt);
    }
}

// ── Markdown rendering (safe DOM, no innerHTML) ──────────────
function renderMarkdown(container, text) {
    const paragraphs = text.split(/\n{2,}/);
    paragraphs.forEach(function (para) {
        const trimmed = para.trim();
        if (!trimmed) return;
        const lines = trimmed.split("\n");
        const isList = lines.every(function (l) {
            return /^\s*[-*]\s+/.test(l) || /^\s*\d+\.\s+/.test(l) || !l.trim();
        });
        if (isList) {
            const ul = document.createElement("ul");
            lines.forEach(function (line) {
                const content = line.replace(/^\s*[-*]\s+/, "").replace(/^\s*\d+\.\s+/, "").trim();
                if (content) {
                    const li = document.createElement("li");
                    renderInline(li, content);
                    ul.appendChild(li);
                }
            });
            container.appendChild(ul);
        } else {
            const p = document.createElement("p");
            renderInline(p, lines.join(" "));
            container.appendChild(p);
        }
    });
}

function renderInline(el, text) {
    const pattern = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(`(.+?)`)|(\[([^\]]+)\]\(([^)]+)\))|(https?:\/\/[^\s),]+)/g;
    let lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
        if (match.index > lastIndex) {
            el.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        if (match[1]) {
            const strong = document.createElement("strong");
            strong.textContent = match[2];
            el.appendChild(strong);
        } else if (match[3]) {
            const em = document.createElement("em");
            em.textContent = match[4];
            el.appendChild(em);
        } else if (match[5]) {
            const code = document.createElement("code");
            code.textContent = match[6];
            el.appendChild(code);
        } else if (match[7]) {
            const a = document.createElement("a");
            a.textContent = match[8];
            a.href = match[9];
            a.target = "_blank";
            a.rel = "noopener";
            el.appendChild(a);
        } else if (match[10]) {
            const a = document.createElement("a");
            try {
                a.textContent = new URL(match[10]).hostname.replace("www.", "");
            } catch (_e) {
                a.textContent = match[10];
            }
            a.href = match[10];
            a.target = "_blank";
            a.rel = "noopener";
            el.appendChild(a);
        }
        lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
        el.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
}

// ── Transcript lines ─────────────────────────────────────────
function createTranscriptLine(role) {
    const line = document.createElement("div");
    line.className = "msg msg-" + role;

    const speaker = document.createElement("span");
    speaker.className = "transcript-speaker";
    speaker.textContent = role === "agent" ? "AGENT:" : "CALLER:";

    const content = document.createElement("div");
    content.className = "transcript-content";
    line.appendChild(speaker);
    line.appendChild(content);
    chatLog.appendChild(line);
    return { line: line, content: content };
}

function addBubble(text, role) {
    clearThinking();
    const transcriptLine = createTranscriptLine(role);
    if (role === "agent") {
        renderMarkdown(transcriptLine.content, text);
    } else {
        transcriptLine.content.textContent = text;
    }
    chatLog.scrollTop = chatLog.scrollHeight;
    return transcriptLine.content;
}

function showThinking() {
    clearThinking();
    const transcriptLine = createTranscriptLine("agent");
    transcriptLine.line.classList.add("thinking");
    transcriptLine.content.textContent = "Thinking…";
    chatLog.scrollTop = chatLog.scrollHeight;
}

var streamingBubble = null;

function appendAgentDelta(text) {
    if (!streamingBubble) {
        streamingBubble = addBubble("", "agent");
    }
    // addBubble returns the bubble element; append text with a leading space if needed
    streamingBubble.textContent = (streamingBubble.textContent + " " + text).trim();
    chatLog.scrollTop = chatLog.scrollHeight;
}

function finalizeAgentBubble() {
    streamingBubble = null;
}

function clearThinking() {
    const el = chatLog.querySelector(".thinking");
    if (el) el.remove();
}

function appendSystemLine(text, variant) {
    const line = document.createElement("div");
    line.className = "transcript-system" + (variant ? " transcript-system-" + variant : "");
    line.textContent = text;
    chatLog.appendChild(line);
    chatLog.scrollTop = chatLog.scrollHeight;
}

function formatLatency(value) {
    return typeof value === "number" && Number.isFinite(value)
        ? Math.round(value) + " ms"
        : "–";
}

function median(values) {
    if (!values.length) return null;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var middle = Math.floor(sorted.length / 2);
    if (sorted.length % 2) return sorted[middle];
    return (sorted[middle - 1] + sorted[middle]) / 2;
}

function updateFlowBenchmarks(state) {
    var supervisorMs = typeof state.supervisor_ms === "number"
        && Number.isFinite(state.supervisor_ms)
        ? state.supervisor_ms
        : null;
    if (supervisorMs !== null) {
        supervisorSamples.push(supervisorMs);
        benchmarkSupervisor.textContent = formatLatency(supervisorMs);
        benchmarkP50.textContent = formatLatency(median(supervisorSamples));
        latencyLlm.textContent = formatLatency(supervisorMs);
    }
    var turnsUsed = Number.isInteger(state.turns_used) ? state.turns_used : 0;
    var maxTurns = Number.isInteger(state.max_turns) ? state.max_turns : 0;
    benchmarkTurns.textContent = turnsUsed + " / " + maxTurns;
}

function appendFlowTranscriptEvents(state, slots, rejected, outcome) {
    var previous = lastFlowTranscriptState;
    var goal = typeof state.goal === "string" ? state.goal : "";
    if ((!previous || previous.goal !== goal) && goal) {
        appendSystemLine("Flow started · " + goal);
    }

    if (typeof state.supervisor_ms === "number" && Number.isFinite(state.supervisor_ms)) {
        var turnLabel = Number.isInteger(state.turns_used) ? state.turns_used : 0;
        var maxLabel = Number.isInteger(state.max_turns) ? state.max_turns : 0;
        appendSystemLine(
            "Scheduler turn " + turnLabel + " / " + maxLabel
            + " · supervisor " + formatLatency(state.supervisor_ms)
        );
    }

    var priorSlots = previous ? previous.slots : {};
    var changedSlots = [];
    [
        ["job", "job", slots.job],
        ["start", "start", slots.slot_start ? formatFlowStart(slots.slot_start) : ""],
        [
            "duration_minutes",
            "duration",
            typeof slots.duration_minutes === "number"
                ? slots.duration_minutes + " minutes"
                : slots.duration_minutes,
        ],
    ].forEach(function (entry) {
        var key = entry[0];
        var label = entry[1];
        var displayValue = entry[2];
        if (slots[key] !== null && slots[key] !== undefined && slots[key] !== ""
            && slots[key] !== priorSlots[key]) {
            changedSlots.push(label + " → " + displayValue);
        }
    });
    if (changedSlots.length) {
        appendSystemLine("Slots updated · " + changedSlots.join(" · "));
    }

    var priorRejected = previous ? previous.rejected : [];
    if (rejected.length && JSON.stringify(rejected) !== JSON.stringify(priorRejected)) {
        appendSystemLine("Validator rejected · " + rejected.join(" · "), "rejected");
    }

    var previousOutcome = previous ? previous.outcome : "";
    if (outcome && outcome !== previousOutcome) {
        var outcomeText = outcome.toUpperCase();
        if (outcome === "booked") {
            var booking = [
                slots.job,
                slots.slot_start ? formatFlowStart(slots.slot_start) : "",
                typeof slots.duration_minutes === "number"
                    ? slots.duration_minutes + " minutes"
                    : slots.duration_minutes,
            ].filter(Boolean);
            outcomeText += booking.length ? " · " + booking.join(" · ") : "";
        }
        appendSystemLine(outcomeText, outcome === "booked" ? "booked" : "");
    }

    lastFlowTranscriptState = {
        goal: goal,
        slots: Object.assign({}, slots),
        rejected: rejected.slice(),
        outcome: outcome,
    };
}

function formatFlowStart(value) {
    if (typeof value !== "string" || !value) return "empty";
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    try {
        return new Intl.DateTimeFormat(undefined, {
            weekday: "short",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
        }).format(parsed);
    } catch (_e) {
        return value;
    }
}

function setFlowSlot(chip, valueEl, rawValue, displayValue) {
    var filled = rawValue !== null && rawValue !== undefined && rawValue !== "";
    chip.classList.toggle("flow-slot-empty", !filled);
    chip.classList.toggle("flow-slot-filled", filled);
    valueEl.textContent = filled ? String(displayValue) : "empty";
}

function renderFlowState(state) {
    state = state && typeof state === "object" ? state : {};
    goalRegionCard.classList.add("hidden");
    flowGoal.textContent = typeof state.goal === "string" ? state.goal : "";

    var slots = state.slots && typeof state.slots === "object" ? state.slots : {};
    setFlowSlot(flowSlotJob, flowSlotJobValue, slots.job, slots.job);
    setFlowSlot(
        flowSlotStart,
        flowSlotStartValue,
        slots.slot_start,
        formatFlowStart(slots.slot_start)
    );
    var duration = typeof slots.duration_minutes === "number"
        ? slots.duration_minutes + " minutes"
        : slots.duration_minutes;
    setFlowSlot(
        flowSlotDuration,
        flowSlotDurationValue,
        slots.duration_minutes,
        duration
    );

    var turnsUsed = Number.isInteger(state.turns_used) ? state.turns_used : 0;
    var maxTurns = Number.isInteger(state.max_turns) ? state.max_turns : 0;
    flowBudget.textContent = "turn " + turnsUsed + " / " + maxTurns;
    flowModel.textContent = activeRegionModel ? "model " + activeRegionModel : "model —";
    flowLatency.textContent = typeof state.supervisor_ms === "number"
        ? "supervisor " + Math.round(state.supervisor_ms) + " ms"
        : "supervisor —";

    var rejected = Array.isArray(state.rejected) ? state.rejected : [];
    while (flowRejectionsList.firstChild) {
        flowRejectionsList.removeChild(flowRejectionsList.firstChild);
    }
    rejected.forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = String(item);
        flowRejectionsList.appendChild(li);
    });
    flowRejections.classList.toggle("hidden", rejected.length === 0);

    var outcome = typeof state.outcome === "string" ? state.outcome.toLowerCase() : "";
    flowOutcome.className = "flow-outcome hidden";
    flowOutcome.textContent = "";
    if (outcome === "booked") {
        var readback = [
            slots.job,
            formatFlowStart(slots.slot_start),
            typeof slots.duration_minutes === "number"
                ? slots.duration_minutes + " minutes"
                : slots.duration_minutes,
        ].filter(function (value) { return value && value !== "empty"; });
        flowOutcome.textContent = "BOOKED" + (readback.length ? " — " + readback.join(" · ") : "");
    } else if (outcome === "escape") {
        flowOutcome.textContent = "ESCAPE";
    } else if (outcome === "budget") {
        flowOutcome.textContent = "BUDGET";
    }
    if (flowOutcome.textContent) {
        flowOutcome.className = "flow-outcome flow-outcome-" + outcome;
    }
    updateFlowBenchmarks(state);
    appendFlowTranscriptEvents(state, slots, rejected, outcome);
    updateFlowVisualization(state, outcome, rejected);
}

// ── Tool approval card ───────────────────────────────────────
function showToolCard(requestId, tools) {
    clearThinking();
    const card = document.createElement("div");
    card.className = "tool-card";

    const header = document.createElement("div");
    header.className = "tool-card-header";
    header.textContent = "Tool Approval Required";
    card.appendChild(header);

    tools.forEach(function (tool) {
        const item = document.createElement("div");
        item.className = "tool-item";

        const name = document.createElement("div");
        name.className = "tool-name";
        name.textContent = tool.name;
        item.appendChild(name);

        const args = document.createElement("pre");
        args.className = "tool-args";
        args.textContent = JSON.stringify(tool.args, null, 2);
        item.appendChild(args);

        card.appendChild(item);
    });

    const actions = document.createElement("div");
    actions.className = "tool-actions";

    const approveBtn = document.createElement("button");
    approveBtn.className = "tool-btn tool-approve";
    approveBtn.textContent = "Approve";
    approveBtn.addEventListener("click", function () {
        card.classList.add("tool-decided");
        approveBtn.disabled = true;
        rejectBtn.disabled = true;
        showThinking();
        sendMsg("tool_approve", { requestId: requestId });
    });
    actions.appendChild(approveBtn);

    const rejectBtn = document.createElement("button");
    rejectBtn.className = "tool-btn tool-reject";
    rejectBtn.textContent = "Reject";
    rejectBtn.addEventListener("click", function () {
        card.classList.add("tool-decided");
        approveBtn.disabled = true;
        rejectBtn.disabled = true;
        showThinking();
        sendMsg("tool_reject", { requestId: requestId });
    });
    actions.appendChild(rejectBtn);

    card.appendChild(actions);
    chatLog.appendChild(card);
    chatLog.scrollTop = chatLog.scrollHeight;
}

// ── Debug panel ──────────────────────────────────────────────
debugToggle.addEventListener("click", function () {
    debugPanel.classList.toggle("debug-collapsed");
    debugPanel.classList.toggle("debug-expanded");
    debugToggle.setAttribute(
        "aria-expanded",
        String(debugPanel.classList.contains("debug-expanded"))
    );
});

function addDebugEntry(info) {
    if (typeof info.durationMs === "number" && Number.isFinite(info.durationMs)) {
        latencyLlm.textContent = formatLatency(info.durationMs);
    }
    var row = document.createElement("div");
    row.className = "debug-row";

    var tokens = info.tokenUsage
        ? info.tokenUsage.prompt + "/" + info.tokenUsage.completion + "/" + info.tokenUsage.total
        : "—";

    var fields = [
        ["iter", String(info.iteration)],
        ["msgs", String(info.messageCount)],
        ["model", info.model],
        ["tok", tokens],
        ["dur", info.durationMs + "ms"],
        ["finish", info.finishReason || "—"],
    ];

    if (info.firstTokenMs !== undefined || info.durationMs !== undefined) {
        var ttft = info.firstTokenMs !== undefined ? info.firstTokenMs + "ms" : "—";
        var total = info.durationMs !== undefined ? info.durationMs + "ms" : "—";
        fields.push(["llm", "TTFT " + ttft + " · total " + total]);
    }

    fields.forEach(function (pair, i) {
        if (i > 0) row.appendChild(document.createTextNode("  "));
        var label = document.createElement("span");
        label.className = "debug-label";
        label.textContent = pair[0];
        row.appendChild(label);
        var value = document.createElement("span");
        value.className = pair[0] === "tok" ? "debug-tokens"
            : pair[0] === "dur" ? "debug-duration"
            : "";
        value.textContent = " " + pair[1];
        row.appendChild(value);
    });

    row.addEventListener("click", function () {
        showDebugDetail(info);
    });

    debugContent.appendChild(row);
    debugContent.scrollTop = debugContent.scrollHeight;
}

function showDebugDetail(info) {
    // Clear previous content
    while (debugModalBody.firstChild) debugModalBody.removeChild(debugModalBody.firstChild);

    var details = [
        {
            key: "Iteration",
            value: String(info.iteration),
            cls: "",
            desc: "Which pass through the agent loop. The agent may loop multiple times if it calls tools.",
        },
        {
            key: "Messages",
            value: String(info.messageCount),
            cls: "",
            desc: "Total messages in the conversation history sent to the LLM. Grows as tool calls and results are added.",
        },
        {
            key: "Model",
            value: info.model,
            cls: "",
            desc: "The LLM model used for this call.",
        },
        {
            key: "Prompt tokens",
            value: info.tokenUsage ? String(info.tokenUsage.prompt) : "—",
            cls: "tok",
            desc: "Tokens in the input sent to the LLM (system prompt + conversation history + tool definitions).",
        },
        {
            key: "Completion tokens",
            value: info.tokenUsage ? String(info.tokenUsage.completion) : "—",
            cls: "tok",
            desc: "Tokens the LLM generated in its response. More tokens = longer/more detailed response.",
        },
        {
            key: "Total tokens",
            value: info.tokenUsage ? String(info.tokenUsage.total) : "—",
            cls: "tok",
            desc: "Prompt + completion. This determines API cost.",
        },
        {
            key: "Cache read/write",
            value: info.tokenUsage && (info.tokenUsage.cacheRead || info.tokenUsage.cacheWrite)
                ? (info.tokenUsage.cacheRead || 0) + "/" + (info.tokenUsage.cacheWrite || 0)
                : "—",
            cls: "tok",
            desc: "Prompt tokens served from / written to the provider's prompt cache. A large read count means the stable prefix (persona + site knowledge) was cached — those tokens cost ~10% and skip prefill.",
        },
        {
            key: "Duration",
            value: info.durationMs + " ms",
            cls: "dur",
            desc: "Wall-clock time for this LLM call (network + inference). Does not include tool execution time.",
        },
        {
            key: "Finish reason",
            value: info.finishReason || "—",
            cls: "",
            desc: "Why the LLM stopped generating. 'end_turn' = final answer. 'tool_use' = wants to call a tool. 'max_tokens' = hit token limit.",
        },
    ];

    details.forEach(function (d) {
        var row = document.createElement("div");
        row.className = "debug-detail-row";

        var left = document.createElement("div");

        var keyEl = document.createElement("div");
        keyEl.className = "debug-detail-key";
        keyEl.textContent = d.key;
        left.appendChild(keyEl);

        var descEl = document.createElement("div");
        descEl.className = "debug-detail-desc";
        descEl.textContent = d.desc;
        left.appendChild(descEl);

        var valueEl = document.createElement("div");
        valueEl.className = "debug-detail-value" + (d.cls ? " " + d.cls : "");
        valueEl.textContent = d.value;

        row.appendChild(left);
        row.appendChild(valueEl);
        debugModalBody.appendChild(row);
    });

    debugModalOverlay.classList.add("visible");
}

debugModalClose.addEventListener("click", function () {
    debugModalOverlay.classList.remove("visible");
});

debugModalOverlay.addEventListener("click", function (e) {
    if (e.target === debugModalOverlay) {
        debugModalOverlay.classList.remove("visible");
    }
});

// ── Agent speaking state ─────────────────────────────────────
function setAgentSpeaking(speaking) {
    agentSpeaking = speaking;
    stopBtn.classList.toggle("hidden", !speaking);
}

function setPhoneStatus(text) {
    if (phoneModeEnabled) statusText.textContent = text;
}

// ── WebSocket ────────────────────────────────────────────────
function sendMsg(type, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(Object.assign({ type: type }, payload || {})));
}

// ── Voice picker ─────────────────────────────────────────────
var LS_VOICE = "nanoclaw.voiceId";
var LS_SPEED = "nanoclaw.speed";
var currentVoiceId = localStorage.getItem(LS_VOICE) || "af_heart";
var currentSpeed = parseFloat(localStorage.getItem(LS_SPEED) || "1") || 1;
var previewAudio = new Audio();

function renderVoiceOptions(uiCatalog) {
    voiceSelect.innerHTML = "";
    phoneVoiceSelect.innerHTML = "";
    VoiceUI.groupVoices(uiCatalog).forEach(function (group) {
        var og = document.createElement("optgroup");
        og.label = group.label;
        var ogPhone = document.createElement("optgroup");
        ogPhone.label = group.label;
        group.options.forEach(function (opt) {
            var o = document.createElement("option");
            o.value = opt.id;
            o.textContent = opt.label;
            og.appendChild(o);
            ogPhone.appendChild(o.cloneNode(true));
        });
        voiceSelect.appendChild(og);
        phoneVoiceSelect.appendChild(ogPhone);
    });
    loadPhoneConfig();
    voiceSelect.value = currentVoiceId;
    if (!voiceSelect.value) {
        currentVoiceId = uiCatalog.default;
        voiceSelect.value = currentVoiceId;
    }
    voiceSelect.disabled = false;
    voicePreviewBtn.disabled = false;
}

function pushVoice() {
    sendMsg("set_voice", { voiceId: currentVoiceId, speed: currentSpeed });
}

function loadVoices() {
    fetch("/api/voices")
        .then(function (r) { return r.json(); })
        .then(function (uiCatalog) {
            renderVoiceOptions(uiCatalog);
            speedSlider.value = String(currentSpeed);
            speedValue.textContent = currentSpeed.toFixed(1) + "×";
            pushVoice();
        })
        .catch(function () { statusText.textContent = "Could not load voices"; });
}

voiceSelect.addEventListener("change", function () {
    currentVoiceId = voiceSelect.value;
    localStorage.setItem(LS_VOICE, currentVoiceId);
    pushVoice();
});

speedSlider.addEventListener("input", function () {
    currentSpeed = parseFloat(speedSlider.value);
    speedValue.textContent = currentSpeed.toFixed(1) + "×";
    localStorage.setItem(LS_SPEED, String(currentSpeed));
});
speedSlider.addEventListener("change", pushVoice);

voicePreviewBtn.addEventListener("click", function () {
    voicePreviewBtn.disabled = true;
    fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voiceId: currentVoiceId }),
    })
        .then(function (r) { return r.blob(); })
        .then(function (blob) {
            previewAudio.src = URL.createObjectURL(blob);
            return previewAudio.play();
        })
        .catch(function () { /* ignore preview errors */ })
        .finally(function () { voicePreviewBtn.disabled = false; });
});

// ── Pipeline settings (STT / LLM / TTS) ─────────────────────
var LS_MODEL = "nanoclaw.model", LS_STT = "nanoclaw.stt";
var currentModel = localStorage.getItem(LS_MODEL) || "anthropic/claude-haiku-4-5";
var currentStt = localStorage.getItem(LS_STT) || "base";

// The configuration rail is always present, so keep its phone-line lamp and
// values fresh whenever this tab is visible.
var phonePollTimer = setInterval(function () {
    if (!document.hidden && !phoneVoiceSelect.disabled) loadPhoneConfig();
}, 5000);

function loadModels() {
    fetch("/api/models").then(function (r) { return r.json(); }).then(function (data) {
        modelSelect.innerHTML = "";
        Pipeline.buildModelOptions(data.models).forEach(function (o) {
            var el = document.createElement("option");
            el.value = o.id; el.textContent = o.label; el.disabled = o.disabled;
            modelSelect.appendChild(el);
        });
        // Phone LLM mirror: "(server default)" + every available model.
        phoneModelSelect.innerHTML = "";
        var def = document.createElement("option");
        def.value = ""; def.textContent = "server default (" + data.default + ")";
        phoneModelSelect.appendChild(def);
        Pipeline.buildModelOptions(data.models).forEach(function (o) {
            var el = document.createElement("option");
            el.value = o.id; el.textContent = o.label; el.disabled = o.disabled;
            phoneModelSelect.appendChild(el);
        });
        if (phonePendingModel !== null) { phoneModelSelect.value = phonePendingModel; phonePendingModel = null; }
        // keep stored model if still available, else fall back to default
        var chosen = data.models.find(function (m) { return m.id === currentModel && m.available; });
        currentModel = chosen ? currentModel : data.default;
        modelSelect.value = currentModel;
        sttSelect.value = currentStt;
        sendMsg("set_model", { modelId: currentModel });
        sendMsg("set_stt", { size: currentStt });
    }).catch(function () {});
}

modelSelect.addEventListener("change", function () {
    currentModel = modelSelect.value; localStorage.setItem(LS_MODEL, currentModel);
    sendMsg("set_model", { modelId: currentModel });
});
sttSelect.addEventListener("change", function () {
    currentStt = sttSelect.value; localStorage.setItem(LS_STT, currentStt);
    sendMsg("set_stt", { size: currentStt });
});

function connect() {
    statusText.textContent = "Connecting...";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onopen = function () {
        statusText.textContent = "Authenticating...";
        sendMsg("hello");
        loadVoices();
        loadModels();
    };

    ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        handleMessage(msg);
    };

    ws.onerror = function () {
        statusText.textContent = "Connection failed";
    };

    ws.onclose = function () {
        statusText.textContent = "Disconnected";
        talkBtn.disabled = true;
        cleanupWebRTC();
    };
}

function handleMessage(msg) {
    switch (msg.type) {
        case "hello_ack":
            bargeInServerAvailable = !!msg.bargeIn;
            syncBargeInControls();
            createBargeInController();
            startWebRTC();
            break;

        case "webrtc_answer":
            handleWebRTCAnswer(msg.sdp);
            break;

        case "transcription":
            markTranscriptionLatency();
            if (msg.text) {
                addBubble(msg.text, "user");
                showThinking();
                setVisualPresence("thinking");
                setPhoneStatus("Thinking...");
            } else {
                clearThinking();
                setVisualPresence("idle");
                rearmPhoneMode("No speech detected; listening again...");
            }
            break;

        case "agent_reply":
            clearThinking();
            markFirstAgentTextLatency();
            addBubble(msg.text, "agent");
            inferEmotionFromReply(msg.text);
            setVisualPresence("speaking");
            setAgentSpeaking(true);
            setPhoneStatus("Speaking to the phone...");
            break;

        case "agent_reply_delta":
            clearThinking();
            markFirstAgentTextLatency();
            appendAgentDelta(msg.text);
            setAgentSpeaking(true);
            setPhoneStatus("Speaking to the phone...");
            break;

        case "agent_reply_done":
            if (streamingBubble) inferEmotionFromReply(streamingBubble.textContent);
            finalizeAgentBubble();
            break;

        case "agent_audio_start":
            setAgentSpeaking(true);
            setVisualizationSpeaking(true);
            setPhoneStatus("Speaking to the phone...");
            break;

        case "agent_audio_end":
            finalizeAgentBubble();
            setAgentSpeaking(false);
            setVisualizationSpeaking(false);
            setVisualPresence("idle");
            resetBargeInDetector();
            rearmPhoneMode("Waiting for the phone side...");
            break;

        case "tool_pending":
            showToolCard(msg.requestId, msg.tools);
            setAgentSpeaking(false);
            setVisualizationSpeaking(false);
            setPhoneStatus("Tool approval required before the call can continue");
            break;

        case "debug":
            addDebugEntry(msg);
            break;

        case "flow_state":
            renderFlowState(msg);
            break;

        case "voice_notice":
            statusText.textContent = msg.text;
            break;

        case "pong":
            break;

        case "error":
            console.error("Server error:", msg.message);
            finalizeAgentBubble();
            setVisualizationSpeaking(false);
            rearmPhoneMode("Voice error; listening again...");
            break;
    }
}

// ── WebRTC ───────────────────────────────────────────────────
async function startWebRTC() {
    statusText.textContent = "Requesting mic...";

    try {
        micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
    } catch (_e) {
        statusText.textContent = "Mic access denied";
        return;
    }

    statusText.textContent = "Connecting audio...";

    pc = new RTCPeerConnection();
    pc.addTrack(micStream.getAudioTracks()[0], micStream);

    pc.oniceconnectionstatechange = function () {
        var state = pc.iceConnectionState;
        if (state === "connected" || state === "completed") {
            audioConnected = true;
            statusText.textContent = "Connected";
            talkBtn.disabled = false;
            textInput.disabled = false;
            sendBtn.disabled = false;
            if (audioEl) audioEl.play().catch(function () {});
        } else if (state === "failed") {
            audioConnected = false;
            statusText.textContent = "Audio failed";
            cleanupWebRTC();
        }
    };

    pc.ontrack = function (ev) {
        if (audioEl) { audioEl.srcObject = null; audioEl.remove(); }
        audioEl = document.createElement("audio");
        audioEl.autoplay = true;
        audioEl.playsInline = true;
        var agentStream = ev.streams[0] || new MediaStream([ev.track]);
        audioEl.srcObject = agentStream;
        document.body.appendChild(audioEl);
        setupAgentAudioAnalyser(agentStream);
    };

    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc);
    sendMsg("webrtc_offer", { sdp: pc.localDescription.sdp });
}

function waitForIceGathering(peerConn) {
    return new Promise(function (resolve) {
        if (peerConn.iceGatheringState === "complete") { resolve(); return; }
        var timer = setTimeout(function () { resolve(); }, 10000);
        peerConn.onicegatheringstatechange = function () {
            if (peerConn.iceGatheringState === "complete") {
                clearTimeout(timer);
                resolve();
            }
        };
    });
}

async function handleWebRTCAnswer(sdp) {
    if (!pc) return;
    await pc.setRemoteDescription({ type: "answer", sdp: sdp });
}

function cleanupWebRTC() {
    stopPhoneMode({sendCancel: false, status: false});
    stopCallerVisualization();
    setVisualizationSpeaking(false);
    teardownAgentAudioAnalyser();
    if (pc) { pc.close(); pc = null; }
    if (audioEl) { audioEl.srcObject = null; audioEl.remove(); audioEl = null; }
    if (micStream) {
        micStream.getTracks().forEach(function (t) { t.stop(); });
        micStream = null;
    }
    audioConnected = false;
    isRecording = false;
    setTalkButtonLabel("Start mic");
    talkBtn.classList.remove("recording", "phone-active");
    talkBtn.setAttribute("aria-pressed", "false");
    talkBtn.disabled = true;
    textInput.disabled = true;
    sendBtn.disabled = true;
    setAgentSpeaking(false);
}

// ── Hands-free phone mode ────────────────────────────────────
async function ensureVadAnalyser() {
    if (!micStream || typeof PhoneVadGate === "undefined") return false;
    if (!vadAudioContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return false;
        vadAudioContext = new AudioContextClass();
        vadAnalyser = vadAudioContext.createAnalyser();
        vadAnalyser.fftSize = 1024;
        vadAnalyser.smoothingTimeConstant = 0.15;
        vadSamples = new Float32Array(vadAnalyser.fftSize);
        const source = vadAudioContext.createMediaStreamSource(micStream);
        source.connect(vadAnalyser);
    }
    if (vadAudioContext.state === "suspended") await vadAudioContext.resume();
    return true;
}

function currentMicRms() {
    if (!vadAnalyser || !vadSamples) return 0;
    vadAnalyser.getFloatTimeDomainData(vadSamples);
    let sumSquares = 0;
    for (let i = 0; i < vadSamples.length; i += 1) {
        sumSquares += vadSamples[i] * vadSamples[i];
    }
    return Math.sqrt(sumSquares / vadSamples.length);
}

function beginAutomaticTurn() {
    if (!phoneModeEnabled || isRecording || autoTurnPending || agentSpeaking) return;
    isRecording = true;
    talkBtn.classList.add("recording");
    setPhoneStatus("Hearing the phone side...");
    sendMsg("mic_start");
}

function finishAutomaticTurn(reason) {
    if (!isRecording) return;
    isRecording = false;
    talkBtn.classList.remove("recording");
    autoTurnPending = true;
    setPhoneStatus(reason === "maximum_duration" ? "Maximum turn reached; transcribing..." : "Transcribing phone audio...");
    beginTurnLatency(true);
    sendMsg("mic_stop");
}

function monitorPhoneAudio(timestamp) {
    if (!phoneModeEnabled) return;

    const rms = currentMicRms();
    driveCallerVisualization(rms);
    if (timestamp < vadCalibrationUntil) {
        vadCalibration.push(rms);
    } else if (vadCalibration.length) {
        const thresholds = vadGate.configureFromNoise(vadCalibration);
        vadCalibration = [];
        console.info("Phone VAD calibrated", thresholds);
        setPhoneStatus("Waiting for the phone side...");
    } else if (agentSpeaking) {
        if (bargeInEnabled && bargeInController) {
            const observation = sampleBargeIn(rms, timestamp);
            const evt = observation.event;
            if (evt && evt.type === "barge_in") {
                sendMsg("barge_in");
                setPhoneStatus("Heard you — pausing...");
            } else if (evt && evt.type === "barge_in_commit") {
                sendMsg("barge_in_commit");
                // The server re-arms the mic (agent_audio_end); the user's
                // speech is captured by the normal VAD turn on the next frames.
            } else if (evt && evt.type === "barge_in_false") {
                sendMsg("barge_in_false");
                setPhoneStatus("False alarm — resuming...");
            }
            if (observation.adjustmentMessage) {
                flashBargeInAdjustment(observation.adjustmentMessage);
            }
        }
        vadGate.reset();  // don't let the normal turn-VAD fire while agent speaks
    } else if (autoTurnPending || timestamp < vadRearmAt) {
        vadGate.reset();
    } else {
        const event = vadGate.sample(rms, timestamp);
        if (event?.type === "speech_start") beginAutomaticTurn();
        if (event?.type === "speech_stop") finishAutomaticTurn(event.reason);
    }

    vadFrameRequest = window.requestAnimationFrame(monitorPhoneAudio);
}

function rearmPhoneMode(message) {
    autoTurnPending = false;
    resetBargeInDetector();
    if (!phoneModeEnabled || !vadGate) return;
    vadGate.reset();
    vadRearmAt = performance.now() + PHONE_REARM_MS;
    setPhoneStatus(message || "Waiting for the phone side...");
}

async function startPhoneMode() {
    if (!audioConnected || !micStream || phoneModeEnabled) return;
    if (!await ensureVadAnalyser()) {
        statusText.textContent = "Automatic voice detection is unavailable";
        return;
    }

    phoneModeEnabled = true;
    autoTurnPending = false;
    vadGate = new PhoneVadGate();
    vadCalibration = [];
    vadCalibrationUntil = performance.now() + PHONE_CALIBRATION_MS;
    vadRearmAt = vadCalibrationUntil;
    talkBtn.classList.add("phone-active");
    talkBtn.setAttribute("aria-pressed", "true");
    setTalkButtonLabel("Stop mic");
    statusText.textContent = "Calibrating room noise...";
    if (vadFrameRequest !== null) window.cancelAnimationFrame(vadFrameRequest);
    vadFrameRequest = window.requestAnimationFrame(monitorPhoneAudio);
}

function stopPhoneMode(options) {
    const config = options || {};
    if (!phoneModeEnabled && vadFrameRequest === null) return;
    phoneModeEnabled = false;
    if (vadFrameRequest !== null) {
        window.cancelAnimationFrame(vadFrameRequest);
        vadFrameRequest = null;
    }
    if (isRecording && config.sendCancel !== false) sendMsg("mic_cancel");
    isRecording = false;
    autoTurnPending = false;
    if (vadGate) vadGate.reset();
    stopCallerVisualization();
    talkBtn.classList.remove("recording", "phone-active");
    talkBtn.setAttribute("aria-pressed", "false");
    setTalkButtonLabel("Start mic");
    if (config.status !== false && audioConnected) statusText.textContent = "Phone mode stopped";
}

talkBtn.addEventListener("click", function () {
    if (phoneModeEnabled) stopPhoneMode();
    else startPhoneMode();
});

// ── Text input ───────────────────────────────────────────────
function sendTextMessage() {
    var text = textInput.value.trim();
    if (!text) return;
    textInput.value = "";
    beginTurnLatency(false);
    if (phoneModeEnabled) {
        autoTurnPending = true;
        setPhoneStatus("Thinking...");
    }
    sendMsg("text_message", { text: text });
}

sendBtn.addEventListener("click", sendTextMessage);
textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        e.preventDefault();
        sendTextMessage();
    }
});

// Stop agent audio
stopBtn.addEventListener("click", function () {
    sendMsg("stop_speaking");
    setVisualizationSpeaking(false);
    setPhoneStatus("Stopping Claude audio...");
});

window.addEventListener("beforeunload", function () {
    if (visualizationMomentTimer) clearTimeout(visualizationMomentTimer);
    if (bargeInFlashTimer) clearTimeout(bargeInFlashTimer);
    if (phonePollTimer) clearInterval(phonePollTimer);
    teardownAgentAudioAnalyser();
    talkingCube.destroy();
});

// Keepalive
setInterval(function () { sendMsg("ping"); }, 15000);

// Auto-connect
connect();
