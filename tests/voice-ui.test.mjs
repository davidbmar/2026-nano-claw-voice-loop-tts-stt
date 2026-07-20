import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

await import("../voice/web/voice-ui.js");
const VoiceUI = globalThis.VoiceUI;
assert.ok(VoiceUI, "VoiceUI must be exported to the global scope");

const ui = {
    groups: [
        { label: "American English", voices: [
            { id: "af_heart", name: "Heart", engine: "kokoro", grade: "A" },
        ]},
        { label: "Piper — fast", voices: [
            { id: "en_US-lessac-medium", name: "Lessac (US)", engine: "piper", grade: null },
        ]},
    ],
    default: "af_heart",
};

const grouped = VoiceUI.groupVoices(ui);
assert.equal(grouped.length, 2);
assert.equal(grouped[0].label, "American English");
assert.equal(grouped[0].options[0].id, "af_heart");
assert.equal(grouped[0].options[0].label, "Heart (A)", "Kokoro options show the grade");
assert.equal(grouped[1].options[0].label, "Lessac (US)", "Piper options have no grade");

assert.equal(VoiceUI.sampleTextForLang("e").startsWith("Hola"), true);
assert.equal(VoiceUI.sampleTextForLang("a").length > 0, true);

const voiceHtml = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
const appSource = await readFile(new URL("../voice/web/app.js", import.meta.url), "utf8");
assert.match(voiceHtml, /talking-cube\.js/, "voice UI must reference the Talking Cube module");
assert.match(voiceHtml, /id="configuration-sidebar"/, "configuration must live in the left rail");
assert.match(voiceHtml, /id="transcription-panel"/, "transcription must live in the right rail");
assert.match(voiceHtml, /id="benchmark-p50"/, "sidebar must expose scheduler benchmarks");
assert.match(voiceHtml, /id="latency-overall"/, "sidebar must expose pipeline latency");
assert.doesNotMatch(voiceHtml, /id="settings-btn"/, "the settings popover trigger must be removed");

const flowSelect = voiceHtml.match(/<select id="flow-select"[\s\S]*?<\/select>/)?.[0] || "";
const flowOptions = [...flowSelect.matchAll(/<option value="([^"]+)"(?: selected)?>([^<]+)<\/option>/g)]
    .map((match) => [match[1], match[2]]);
assert.deepEqual(flowOptions, [
    ["none", "None"],
    ["spacechannel", "Space Channel"],
    ["replicantpm", "Replicant PM"],
    ["scheduler", "Plumber Scheduler"],
]);
assert.match(
    flowSelect,
    /<option value="spacechannel" selected>Space Channel<\/option>/,
    "assistant mode must render Space Channel instead of a blank value before fetch resolves",
);
assert.match(
    appSource,
    /Pipeline\.applyModelOptions\(\s*flowSelect,/,
    "assistant mode must reuse the non-blank resilient dropdown helper",
);
assert.ok(
    appSource.indexOf("// Populate independently on page load") < appSource.indexOf("function connect()"),
    "assistant modes must load on page load, independently of socket open",
);

console.log("voice-ui tests passed");
