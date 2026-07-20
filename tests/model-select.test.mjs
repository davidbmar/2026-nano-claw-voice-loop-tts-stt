// Test cases for the LLM model selector never rendering blank.
//
// Root cause of the reported bug: the dropdown was populated only from
// `socket.onopen`, and a browser <select> whose `.value` is set to an id that
// is not among its <option>s silently shows a BLANK box (selectedIndex = -1).
// These cases pin down the fix in voice/web/pipeline.js:
//   - resolveModelSelection() always yields an id that IS in the list, and
//   - applyModelOptions() populates + selects with NO WebSocket dependency,
//     so the control is filled on page load rather than on socket-open.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

await import("../voice/web/pipeline.js");
const Pipeline = globalThis.Pipeline;
const appSource = await readFile(
    new URL("../voice/web/app.js", import.meta.url),
    "utf8",
);

function extractFunction(source, name) {
    const match = new RegExp("function\\s+" + name + "\\s*\\(").exec(source);
    assert.ok(match, "missing function " + name);
    const bodyStart = source.indexOf("{", match.index);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error("unterminated function " + name);
}

const selectionHelpersSource = [
    "findEnabledSelectOption",
    "currentEnabledSelectOption",
    "explicitlySelectOption",
    "resolveEnabledSelectSelection",
    "replaceSelectOptions",
].map((name) => extractFunction(appSource, name)).join("\n");
const loadModelsSource = [
    selectionHelpersSource,
    extractFunction(appSource, "syncModelToServer"),
    extractFunction(appSource, "loadModels"),
].join("\n");
const renderVoiceOptionsSource = extractFunction(appSource, "renderVoiceOptions");
const renderRegionModelConfigSource = extractFunction(appSource, "renderRegionModelConfig");

// ── Minimal fake <select> that reproduces real DOM value semantics ──────────
// Setting `.value` to a value not present among options yields selectedIndex
// -1 and a blank `.value` getter — the precise failure we are guarding against.
class FakeOption {
    constructor() {
        this.value = "";
        this.textContent = "";
        this.disabled = false;
        this.selected = false;
    }

    cloneNode() {
        const clone = new FakeOption();
        clone.value = this.value;
        clone.textContent = this.textContent;
        clone.disabled = this.disabled;
        clone.selected = this.selected;
        return clone;
    }
}
class FakeOptGroup {
    constructor() { this.children = []; this.label = ""; }
    appendChild(node) { this.children.push(node); return node; }
}
class FakeSelect {
    constructor() {
        this._children = [];
        this._selectedIndex = -1;
        this.disabled = false;
        this.replacementCount = 0;
        this.liveClearCount = 0;
    }
    get children() { return this._children; }
    get firstChild() { return this._children[0] || null; }
    get options() {
        return this._children.flatMap((node) => (
            node instanceof FakeOptGroup ? node.children : [node]
        ));
    }
    get selectedIndex() { return this._selectedIndex; }
    set selectedIndex(index) {
        const options = this.options;
        this._selectedIndex = Number.isInteger(index) && index >= 0 && index < options.length
            ? index
            : -1;
        options.forEach((option, optionIndex) => {
            option.selected = optionIndex === this._selectedIndex;
        });
    }
    set innerHTML(v) {
        if (v === "") {
            if (this.options.length) this.liveClearCount += 1;
            this._children = [];
            this.selectedIndex = -1;
        }
    }
    get innerHTML() { return ""; }
    appendChild(node) {
        this._children.push(node);
        if (this.selectedIndex < 0) {
            this.selectedIndex = this.options.findIndex((option) => !option.disabled);
        }
        return node;
    }
    insertBefore(node, reference) {
        const index = this._children.indexOf(reference);
        this._children.splice(index < 0 ? this._children.length : index, 0, node);
        if (this.selectedIndex < 0) {
            this.selectedIndex = this.options.findIndex((option) => !option.disabled);
        }
        return node;
    }
    replaceChildren(...nodes) {
        this._children = nodes;
        const selectedIndex = this.options.findIndex((option) => option.selected && !option.disabled);
        this.selectedIndex = selectedIndex >= 0
            ? selectedIndex
            : this.options.findIndex((option) => !option.disabled);
        this.replacementCount += 1;
    }
    get value() {
        return this.selectedIndex >= 0 ? this.options[this.selectedIndex].value : "";
    }
    set value(v) {
        // This deliberately refuses disabled options, matching the reported
        // device behavior that left the live control blank.
        this.selectedIndex = this.options.findIndex((option) => (
            option.value === v && !option.disabled
        ));
    }
}
const fakeDoc = {
    createElement(tagName) {
        if (tagName === "select") return new FakeSelect();
        if (tagName === "optgroup") return new FakeOptGroup();
        return new FakeOption();
    },
};

const MODELS = [
    { id: "anthropic/claude-haiku-4-5", label: "Claude Haiku 4.5", available: true },
    { id: "deepseek/deepseek-v4-flash", label: "DeepSeek V4 Flash", available: true },
    { id: "gemini/gemini-flash-latest", label: "Gemini Flash", available: false },
];
const DEFAULT = "deepseek/deepseek-v4-flash";

test("resolveModelSelection: keeps the stored model when it is still available", () => {
    assert.equal(
        Pipeline.resolveModelSelection(MODELS, "anthropic/claude-haiku-4-5", DEFAULT),
        "anthropic/claude-haiku-4-5",
    );
});

test("resolveModelSelection: falls back to the server default when stored is gone", () => {
    // Stored model no longer offered by the server → must not go blank.
    assert.equal(Pipeline.resolveModelSelection(MODELS, "openai/gpt-legacy", DEFAULT), DEFAULT);
});

test("resolveModelSelection: falls back to default when stored is present but disabled", () => {
    // Gemini is in the list but available:false (no key) → skip it.
    assert.equal(
        Pipeline.resolveModelSelection(MODELS, "gemini/gemini-flash-latest", DEFAULT),
        DEFAULT,
    );
});

test("resolveModelSelection: falls back to first available when default is unavailable", () => {
    const models = [
        { id: "a", label: "A", available: false },
        { id: "b", label: "B", available: true },
    ];
    assert.equal(Pipeline.resolveModelSelection(models, null, "a"), "b");
});

test("resolveModelSelection: never blank even when nothing has a key", () => {
    const models = [
        { id: "a", label: "A", available: false },
        { id: "b", label: "B", available: false },
    ];
    // No enabled option exists, but the control must still show something.
    assert.equal(Pipeline.resolveModelSelection(models, "x", "y"), "a");
});

test("resolveModelSelection: empty list yields empty string (documented edge)", () => {
    assert.equal(Pipeline.resolveModelSelection([], "x", "y"), "");
    assert.equal(Pipeline.resolveModelSelection(null, "x", "y"), "");
});

test("applyModelOptions: populates every option with value/label/disabled", () => {
    const sel = new FakeSelect();
    Pipeline.applyModelOptions(sel, MODELS, "anthropic/claude-haiku-4-5", DEFAULT, fakeDoc);
    assert.equal(sel.options.length, 3);
    assert.equal(sel.options[0].value, "anthropic/claude-haiku-4-5");
    assert.equal(sel.options[2].textContent, "Gemini Flash — no key");
    assert.equal(sel.options[2].disabled, true);
});

test("applyModelOptions: lands on a NON-BLANK selection (the reported bug)", () => {
    const sel = new FakeSelect();
    const chosen = Pipeline.applyModelOptions(sel, MODELS, "anthropic/claude-haiku-4-5", DEFAULT, fakeDoc);
    assert.equal(chosen, "anthropic/claude-haiku-4-5");
    assert.ok(sel.selectedIndex >= 0, "selectedIndex must not be -1");
    assert.notEqual(sel.value, "", "the <select> must not render blank");
});

test("applyModelOptions: stale stored id still yields a non-blank selection", () => {
    const sel = new FakeSelect();
    const chosen = Pipeline.applyModelOptions(sel, MODELS, "removed/old-model", DEFAULT, fakeDoc);
    assert.equal(chosen, DEFAULT);
    assert.notEqual(sel.value, "", "must recover to a valid option, not blank");
});

test("applyModelOptions: re-running (reconnect) keeps a valid selection, never blank", () => {
    const sel = new FakeSelect();
    Pipeline.applyModelOptions(sel, MODELS, DEFAULT, DEFAULT, fakeDoc);
    // Simulate a reconnect re-populating the same control.
    const chosen = Pipeline.applyModelOptions(sel, MODELS, DEFAULT, DEFAULT, fakeDoc);
    assert.equal(sel.options.length, 3, "must not accumulate duplicate options");
    assert.equal(chosen, DEFAULT);
    assert.notEqual(sel.value, "");
});

test("applyModelOptions is decoupled from the socket (no ws/sendMsg reference)", () => {
    // The population path must not depend on the WebSocket — that coupling was
    // the root cause. Guard the invariant at the source level.
    const src = Pipeline.applyModelOptions.toString();
    assert.ok(!/sendMsg|WebSocket|socket/.test(src), "populator must not touch the socket");
});

function jsonResponse(payload) {
    return Promise.resolve({
        ok: true,
        json() { return Promise.resolve(payload); },
    });
}

function createLoadModelsHarness(storedModel, fetchModelCatalog) {
    const modelSelect = new FakeSelect();
    const phoneModelSelect = new FakeSelect();
    const diagnosticLines = [];
    const sentMessages = [];
    const retryTimers = [];
    const storage = new Map([["nanoclaw.model", storedModel]]);
    const localStorage = {
        getItem(key) { return storage.has(key) ? storage.get(key) : null; },
        setItem(key, value) { storage.set(key, String(value)); },
    };
    const context = vm.createContext({
        console,
        currentModelInitial: storedModel,
        diagnosticLines,
        document: fakeDoc,
        fetch: fetchModelCatalog,
        localStorage,
        modelSelect,
        phoneModelSelect,
        Pipeline,
        retryTimers,
        sentMessages,
        setTimeout(callback, delay) {
            retryTimers.push({ callback, delay });
            return retryTimers.length;
        },
        sttSelect: { value: "" },
    });
    vm.runInContext(`
        var LS_MODEL = "nanoclaw.model", LS_STT = "nanoclaw.stt";
        var currentModel = currentModelInitial;
        var currentStt = "base";
        var phonePendingModel = null;
        var modelsLoaded = false, modelsLoading = false, modelsRetryTimer = null;
        function pageLog(message) { diagnosticLines.push(message); }
        function sendMsg(type, payload) {
            sentMessages.push({ type: type, payload: payload });
            return true;
        }
        ${loadModelsSource}
        globalThis.modelHarness = {
            loadModels: loadModels,
            currentModel: function () { return currentModel; },
            modelsLoaded: function () { return modelsLoaded; },
        };
    `, context);
    return {
        context,
        diagnosticLines,
        localStorage,
        modelSelect,
        phoneModelSelect,
        retryTimers,
        sentMessages,
    };
}

test("loadModels: stale stored model resolves to the enabled default and persists it", async () => {
    const stored = "removed/old-model";
    const harness = createLoadModelsHarness(stored, () => jsonResponse({
        models: MODELS,
        default: DEFAULT,
    }));

    assert.equal(await harness.context.modelHarness.loadModels(), true);
    assert.equal(harness.modelSelect.value, DEFAULT);
    assert.ok(harness.modelSelect.selectedIndex >= 0, "the live selector must be visible");
    const selected = harness.modelSelect.options[harness.modelSelect.selectedIndex];
    assert.equal(selected.value, DEFAULT);
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);
    assert.equal(harness.context.modelHarness.currentModel(), DEFAULT);
    assert.equal(harness.localStorage.getItem("nanoclaw.model"), DEFAULT);
    assert.match(
        harness.diagnosticLines.at(-1),
        /stored=removed\/old-model default=deepseek\/deepseek-v4-flash resolved=deepseek\/deepseek-v4-flash source=default fallback=true/,
    );
    assert.equal(harness.sentMessages[0].type, "set_model");
    assert.equal(harness.sentMessages[0].payload.modelId, DEFAULT);
    assert.equal(harness.sentMessages[1].type, "set_stt");
    assert.equal(harness.sentMessages[1].payload.size, "base");
});

test("loadModels: a valid stored model remains enabled and selected", async () => {
    const stored = "anthropic/claude-haiku-4-5";
    const harness = createLoadModelsHarness(stored, () => jsonResponse({
        models: MODELS,
        default: DEFAULT,
    }));

    assert.equal(await harness.context.modelHarness.loadModels(), true);
    assert.equal(harness.modelSelect.value, stored);
    const selected = harness.modelSelect.options[harness.modelSelect.selectedIndex];
    assert.equal(selected.value, stored);
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);
    assert.equal(harness.localStorage.getItem("nanoclaw.model"), stored);
    assert.match(harness.diagnosticLines.at(-1), /source=stored fallback=false/);
});

test("loadModels: reconnect refresh keeps the populated selection visible until atomic swap", async () => {
    const stored = "anthropic/claude-haiku-4-5";
    let callCount = 0;
    let resolveRefresh;
    const refreshResponse = new Promise((resolve) => { resolveRefresh = resolve; });
    const harness = createLoadModelsHarness(stored, () => {
        callCount += 1;
        if (callCount === 1) return jsonResponse({ models: MODELS, default: DEFAULT });
        return refreshResponse;
    });

    assert.equal(await harness.context.modelHarness.loadModels(), true);
    const replacementCount = harness.modelSelect.replacementCount;
    const visibleOptions = harness.modelSelect.options.slice();
    const refresh = harness.context.modelHarness.loadModels();

    assert.equal(harness.modelSelect.value, stored, "selection must stay visible while fetch is pending");
    assert.equal(harness.modelSelect.disabled, false);
    assert.equal(harness.modelSelect.replacementCount, replacementCount);
    assert.deepEqual(harness.modelSelect.options, visibleOptions);

    resolveRefresh({
        ok: true,
        json() { return Promise.resolve({ models: MODELS.slice().reverse(), default: DEFAULT }); },
    });
    assert.equal(await refresh, true);
    assert.equal(harness.modelSelect.replacementCount, replacementCount + 1);
    assert.equal(harness.modelSelect.liveClearCount, 0, "the live control must never be cleared first");
    assert.equal(harness.modelSelect.value, stored);
    const selected = harness.modelSelect.options[harness.modelSelect.selectedIndex];
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);
});

test("loadModels: failed refresh preserves the prior enabled visible selection", async () => {
    const stored = "anthropic/claude-haiku-4-5";
    let callCount = 0;
    const harness = createLoadModelsHarness(stored, () => {
        callCount += 1;
        if (callCount === 1) return jsonResponse({ models: MODELS, default: DEFAULT });
        return Promise.reject(new Error("temporary tunnel failure"));
    });

    assert.equal(await harness.context.modelHarness.loadModels(), true);
    const replacementCount = harness.modelSelect.replacementCount;
    const visibleOptions = harness.modelSelect.options.slice();
    assert.equal(await harness.context.modelHarness.loadModels(), false);

    assert.equal(harness.modelSelect.replacementCount, replacementCount);
    assert.deepEqual(harness.modelSelect.options, visibleOptions);
    assert.equal(harness.modelSelect.value, stored);
    assert.equal(harness.modelSelect.disabled, false);
    assert.equal(harness.modelSelect.liveClearCount, 0);
    assert.match(harness.diagnosticLines.at(-1), /models load FAILED: temporary tunnel failure/);
});

test("voice renderer atomically falls back to an enabled catalog default", () => {
    const voiceSelect = new FakeSelect();
    const phoneVoiceSelect = new FakeSelect();
    const diagnosticLines = [];
    const storage = new Map();
    const context = vm.createContext({
        currentVoiceIdInitial: "retired_voice",
        diagnosticLines,
        document: fakeDoc,
        localStorage: {
            setItem(key, value) { storage.set(key, String(value)); },
        },
        phoneVoiceSelect,
        voicePreviewBtn: { disabled: true },
        voiceSelect,
        VoiceUI: {
            groupVoices(catalog) { return catalog.groups; },
        },
    });
    vm.runInContext(`
        var LS_VOICE = "nanoclaw.voiceId";
        var currentVoiceId = currentVoiceIdInitial;
        function loadPhoneConfig() {}
        function pageLog(message) { diagnosticLines.push(message); }
        ${selectionHelpersSource}
        ${renderVoiceOptionsSource}
        globalThis.voiceHarness = {
            render: renderVoiceOptions,
            currentVoice: function () { return currentVoiceId; },
        };
    `, context);

    context.voiceHarness.render({
        default: "af_heart",
        groups: [{
            label: "American English",
            options: [
                { id: "af_heart", label: "Heart" },
                { id: "af_bella", label: "Bella" },
            ],
        }],
    });

    assert.equal(voiceSelect.value, "af_heart");
    assert.equal(context.voiceHarness.currentVoice(), "af_heart");
    assert.equal(storage.get("nanoclaw.voiceId"), "af_heart");
    const selected = voiceSelect.options[voiceSelect.selectedIndex];
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);
    assert.equal(voiceSelect.liveClearCount, 0);
    assert.match(diagnosticLines.at(-1), /source=default fallback=true/);
});

test("region-model renderer always swaps in an enabled visible selection", () => {
    const regionModelSelect = new FakeSelect();
    const diagnosticLines = [];
    const flowModel = { textContent: "" };
    const context = vm.createContext({
        diagnosticLines,
        document: fakeDoc,
        flowModel,
        regionModelSelect,
    });
    vm.runInContext(`
        var activeRegionModel = "";
        function pageLog(message) { diagnosticLines.push(message); }
        ${selectionHelpersSource}
        ${renderRegionModelConfigSource}
        globalThis.regionHarness = {
            render: renderRegionModelConfig,
            active: function () { return activeRegionModel; },
        };
    `, context);

    context.regionHarness.render({
        active: "environment/model",
        options: [{ value: "catalog/model", label: "Catalog model" }],
    });
    assert.equal(regionModelSelect.value, "environment/model");
    assert.equal(context.regionHarness.active(), "environment/model");
    assert.equal(flowModel.textContent, "model environment/model");
    let selected = regionModelSelect.options[regionModelSelect.selectedIndex];
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);

    context.regionHarness.render({
        active: "",
        options: [{ value: "catalog/model", label: "Catalog model" }],
    });
    assert.equal(regionModelSelect.value, "catalog/model");
    assert.equal(context.regionHarness.active(), "catalog/model");
    selected = regionModelSelect.options[regionModelSelect.selectedIndex];
    assert.equal(selected.disabled, false);
    assert.equal(selected.selected, true);
    assert.equal(regionModelSelect.liveClearCount, 0);
});
