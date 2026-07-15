(function (global) {
    "use strict";

    var SAMPLES = {
        a: "Hi, this is how I sound.",
        b: "Hi, this is how I sound.",
        e: "Hola, así es como sueno.",
    };

    function sampleTextForLang(lang) {
        return SAMPLES[lang] || SAMPLES.a;
    }

    function optionLabel(voice) {
        if (voice.engine === "kokoro" && voice.grade) {
            return voice.name + " (" + voice.grade + ")";
        }
        return voice.name;
    }

    function groupVoices(uiCatalog) {
        return (uiCatalog.groups || []).map(function (group) {
            return {
                label: group.label,
                options: group.voices.map(function (v) {
                    return { id: v.id, label: optionLabel(v) };
                }),
            };
        });
    }

    global.VoiceUI = {
        groupVoices: groupVoices,
        sampleTextForLang: sampleTextForLang,
    };
}(typeof window !== "undefined" ? window : globalThis));
