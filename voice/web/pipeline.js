(function (global) {
    "use strict";
    function buildModelOptions(models) {
        return (models || []).map(function (m) {
            return { id: m.id, label: m.available ? m.label : m.label + " — no key", disabled: !m.available };
        });
    }

    // Choose which model id the selector should land on, guaranteeing a
    // non-blank and (where possible) enabled selection. Preference order:
    //   1. the stored/current model, if it is still available
    //   2. the server's default model, if available
    //   3. the first available model
    //   4. the first model in the list (so the control is never blank even
    //      when nothing has a key), or "" only when the list is empty.
    function resolveModelSelection(models, storedId, defaultId) {
        var list = models || [];
        var enabled = function (id) {
            return !!id && list.some(function (m) { return m.id === id && m.available; });
        };
        if (enabled(storedId)) return storedId;
        if (enabled(defaultId)) return defaultId;
        var firstAvail = list.find(function (m) { return m.available; });
        if (firstAvail) return firstAvail.id;
        return list.length ? list[0].id : "";
    }

    // Populate a model <select> from the API payload and set a guaranteed
    // non-blank selection. Deliberately takes no WebSocket — the dropdown must
    // be fillable the instant the page's /api/models fetch resolves, never
    // gated on the socket opening. Returns the chosen model id.
    function applyModelOptions(selectEl, models, storedId, defaultId, doc) {
        doc = doc || (typeof document !== "undefined" ? document : null);
        selectEl.innerHTML = "";
        buildModelOptions(models).forEach(function (o) {
            var el = doc.createElement("option");
            el.value = o.id; el.textContent = o.label; el.disabled = o.disabled;
            selectEl.appendChild(el);
        });
        var chosen = resolveModelSelection(models, storedId, defaultId);
        selectEl.value = chosen;
        // Defensive: if the assignment left nothing selected (value not among
        // the options), fall back to the first option rather than a blank box.
        if ((selectEl.selectedIndex == null || selectEl.selectedIndex < 0) &&
            selectEl.options && selectEl.options.length) {
            selectEl.selectedIndex = 0;
        }
        return chosen;
    }

    global.Pipeline = {
        buildModelOptions: buildModelOptions,
        resolveModelSelection: resolveModelSelection,
        applyModelOptions: applyModelOptions,
    };
}(typeof window !== "undefined" ? window : globalThis));
