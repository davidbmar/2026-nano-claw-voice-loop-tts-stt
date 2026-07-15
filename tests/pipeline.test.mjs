import assert from "node:assert/strict";
await import("../voice/web/pipeline.js");
const Pipeline = globalThis.Pipeline;
assert.ok(Pipeline, "Pipeline global must exist");
const opts = Pipeline.buildModelOptions([
  { id: "anthropic/claude-haiku-4-5", label: "Claude Haiku 4.5", available: true },
  { id: "gemini/gemini-2.0-flash", label: "Gemini 2.0 Flash", available: false },
]);
assert.equal(opts[0].label, "Claude Haiku 4.5");
assert.equal(opts[0].disabled, false);
assert.equal(opts[1].label, "Gemini 2.0 Flash — no key");
assert.equal(opts[1].disabled, true);
console.log("pipeline tests passed");
