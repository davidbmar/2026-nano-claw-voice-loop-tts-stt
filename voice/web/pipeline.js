(function (global) {
    "use strict";
    function buildModelOptions(models) {
        return (models || []).map(function (m) {
            return { id: m.id, label: m.available ? m.label : m.label + " — no key", disabled: !m.available };
        });
    }
    global.Pipeline = { buildModelOptions: buildModelOptions };
}(typeof window !== "undefined" ? window : globalThis));
