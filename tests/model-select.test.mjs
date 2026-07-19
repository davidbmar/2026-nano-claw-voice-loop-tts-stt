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

await import("../voice/web/pipeline.js");
const Pipeline = globalThis.Pipeline;

// ── Minimal fake <select> that reproduces real DOM value semantics ──────────
// Setting `.value` to a value not present among options yields selectedIndex
// -1 and a blank `.value` getter — the precise failure we are guarding against.
class FakeOption {
    constructor() { this.value = ""; this.textContent = ""; this.disabled = false; }
}
class FakeSelect {
    constructor() { this._opts = []; this.selectedIndex = -1; }
    get options() { return this._opts; }
    set innerHTML(v) { if (v === "") { this._opts = []; this.selectedIndex = -1; } }
    get innerHTML() { return ""; }
    appendChild(o) { this._opts.push(o); }
    get value() { return this.selectedIndex >= 0 ? this._opts[this.selectedIndex].value : ""; }
    set value(v) { this.selectedIndex = this._opts.findIndex((o) => o.value === v); }
}
const fakeDoc = { createElement: () => new FakeOption() };

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
