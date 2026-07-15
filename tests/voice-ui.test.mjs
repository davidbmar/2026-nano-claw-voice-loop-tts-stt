import assert from "node:assert/strict";

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

console.log("voice-ui tests passed");
