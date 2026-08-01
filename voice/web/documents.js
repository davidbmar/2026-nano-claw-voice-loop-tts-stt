"use strict";

/**
 * The Documents panel: spaces, the files inside them, and which are in scope.
 *
 * Modelled on auth.js rather than on app.js — a frozen id manifest, an injected
 * document so rows can be built and asserted on without a browser, and a
 * destroy() that leaves nothing behind. app.js keeps its element references in
 * ~150 module globals, which is exactly what this panel should not add to.
 *
 * Every mutation goes through the operator gate. Ticking a checkbox changes
 * what the phone line answers from, so it is a deployment change, not a
 * per-tab preference, and the server enforces that with the same password it
 * uses for the other controls.
 */

export const DOCUMENTS_DOM_IDS = Object.freeze([
    "documents-panel",
    "documents-heading",
    "space-list",
    "space-new-button",
    "documents-space-name",
    "documents-list",
    "documents-status",
    "documents-file-input",
    "documents-upload-button",
    "documents-scope-summary",
    "documents-trash-toggle",
]);

const ACCEPTED_TYPES = ".pdf,.docx,.txt,.md";

function stringValue(value) {
    return typeof value === "string" ? value : "";
}

/** Human-readable size; the exact byte count helps nobody scanning a list. */
export function formatSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return Math.round(value / 1024) + " KB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
}

/**
 * A one-line description of what the assistant can currently see.
 *
 * The empty-selection case gets its own wording because it is a real state a
 * customer can land in, and "0 of 3" alone reads like a bug rather than a
 * choice they made.
 */
export function scopeSummary(scope) {
    if (!scope) return "No space selected.";
    const ready = Number(scope.readyCount) || 0;
    const selected = Number(scope.selectedCount) || 0;
    if (ready === 0) return "Nothing indexed yet.";
    if (selected === 0) return "Nothing ticked — the assistant has no documents to answer from.";
    if (selected === ready) return "All " + ready + " in this conversation.";
    return selected + " of " + ready + " in this conversation.";
}

/**
 * One row: a checkbox that changes scope, the title, and the actions.
 *
 * Built through an injected document so tests can drive it with a fake DOM —
 * the same reason auth.js takes documentRef.
 */
export function createDocumentListItem(documentRef, doc, actions) {
    const item = documentRef.createElement("article");
    item.className = "document-item";
    if (doc.deletedAt) item.className += " document-item-trashed";

    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "document-item-check";
    checkbox.checked = doc.selected === true;
    // A document still extracting, failed, or in the trash has nothing to
    // contribute, so the tick would be a lie.
    checkbox.disabled = doc.status !== "ready" || !!doc.deletedAt;
    checkbox.setAttribute("aria-label", "Include " + stringValue(doc.title) + " in the conversation");
    checkbox.addEventListener("change", function () {
        actions.onToggle(doc, checkbox);
    });

    const label = documentRef.createElement("span");
    label.className = "document-item-body";

    const title = documentRef.createElement("span");
    title.className = "document-item-title";
    title.textContent = stringValue(doc.title) || stringValue(doc.filename);
    label.appendChild(title);

    const meta = documentRef.createElement("span");
    meta.className = "document-item-meta";
    if (doc.status === "ready") {
        meta.textContent = formatSize(doc.bytes);
    } else if (doc.status === "failed") {
        // The error is the whole reason the row is still here.
        meta.textContent = stringValue(doc.error) || "Could not be read";
        meta.className += " document-item-error";
    } else {
        meta.textContent = doc.status === "indexing" ? "Indexing…" : "Reading…";
    }
    label.appendChild(meta);

    const action = documentRef.createElement("button");
    action.type = "button";
    action.className = "document-item-delete";
    action.textContent = doc.deletedAt ? "Undo" : "Delete";
    action.setAttribute(
        "aria-label",
        (doc.deletedAt ? "Restore " : "Delete ") + stringValue(doc.title),
    );
    action.addEventListener("click", function () {
        if (doc.deletedAt) actions.onRestore(doc, action);
        else actions.onDelete(doc, action);
    });

    item.appendChild(checkbox);
    item.appendChild(label);
    item.appendChild(action);
    return item;
}

export function createSpaceListItem(documentRef, space, actions) {
    const item = documentRef.createElement("button");
    item.type = "button";
    item.className = "space-item";
    if (space.isActive) item.className += " space-item-active";
    item.setAttribute("aria-pressed", space.isActive ? "true" : "false");

    const name = documentRef.createElement("span");
    name.className = "space-item-name";
    name.textContent = stringValue(space.name);
    item.appendChild(name);

    const count = documentRef.createElement("span");
    count.className = "space-item-count";
    const total = Number(space.documentCount) || 0;
    count.textContent = total === 1 ? "1 doc" : total + " docs";
    item.appendChild(count);

    item.addEventListener("click", function () {
        actions.onSelect(space);
    });
    return item;
}

export class DocumentsUI {
    constructor(options) {
        const config = options || {};
        this.document = config.document;
        this.fetch = config.fetch;
        // Supplied by app.js: prompts for the operator password once and
        // retries, so this module never learns how that secret is stored.
        this.operatorHeaders = config.operatorHeaders;
        this.window = config.window || null;
        this.elements = {};
        this.spaces = [];
        this.activeSpaceId = null;
        this.showTrashed = false;
        this.destroyed = false;

        const missing = [];
        DOCUMENTS_DOM_IDS.forEach((id) => {
            const element = this.document.getElementById(id);
            if (!element) missing.push(id);
            this.elements[id] = element;
        });
        if (missing.length) {
            throw new Error("documents panel is missing elements: " + missing.join(", "));
        }
        this._bind();
    }

    _element(id) {
        return this.elements[id];
    }

    _bind() {
        this._element("documents-upload-button").addEventListener("click", () => {
            this._element("documents-file-input").click();
        });
        this._element("documents-file-input").addEventListener("change", (event) => {
            const files = Array.from((event.target && event.target.files) || []);
            // Clearing lets the same file be re-picked after a failure, which
            // is exactly when someone tries again.
            event.target.value = "";
            this._uploadAll(files);
        });
        this._element("space-new-button").addEventListener("click", () => {
            this._createSpace();
        });
        this._element("documents-trash-toggle").addEventListener("click", () => {
            this.showTrashed = !this.showTrashed;
            this._element("documents-trash-toggle").textContent = this.showTrashed
                ? "Hide deleted"
                : "Show deleted";
            this.refreshDocuments();
        });
    }

    async _json(path, options) {
        const response = await this.fetch(path, Object.assign({
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        }, options || {}));
        let data = null;
        try {
            data = await response.json();
        } catch (_error) {
            data = null;
        }
        return { response, data };
    }

    _mutation(method, body) {
        const headers = Object.assign(
            { Accept: "application/json" },
            this.operatorHeaders(),
        );
        const options = { method, headers };
        if (body !== undefined) {
            headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }
        return options;
    }

    _status(message, isError) {
        const element = this._element("documents-status");
        element.textContent = message || "";
        element.className = isError ? "documents-status documents-status-error" : "documents-status";
    }

    async refresh() {
        if (this.destroyed) return;
        const { response, data } = await this._json("/api/spaces");
        if (!response.ok || !data) {
            this._status("Could not load document spaces.", true);
            return;
        }
        this.spaces = Array.isArray(data.spaces) ? data.spaces : [];
        const active = this.spaces.find((space) => space.isActive);
        this.activeSpaceId = this.activeSpaceId || (active && active.id) || null;
        this._renderSpaces();
        this._renderScope(data.scope);
        await this.refreshDocuments();
    }

    _renderSpaces() {
        const list = this._element("space-list");
        list.replaceChildren();
        this.spaces.forEach((space) => {
            list.appendChild(
                createSpaceListItem(this.document, space, {
                    onSelect: (chosen) => this._activateSpace(chosen),
                }),
            );
        });
        const current = this.spaces.find((space) => space.id === this.activeSpaceId);
        this._element("documents-space-name").textContent = current ? current.name : "";
    }

    _renderScope(scope) {
        this._element("documents-scope-summary").textContent = scopeSummary(scope);
    }

    async refreshDocuments() {
        if (this.destroyed || !this.activeSpaceId) {
            this._element("documents-list").replaceChildren();
            return;
        }
        const query = this.showTrashed ? "?trashed=1" : "";
        const { response, data } = await this._json(
            "/api/spaces/" + encodeURIComponent(this.activeSpaceId) + "/documents" + query,
        );
        if (!response.ok || !data) {
            this._status("Could not load documents.", true);
            return;
        }
        const list = this._element("documents-list");
        list.replaceChildren();
        const documents = Array.isArray(data.documents) ? data.documents : [];
        documents.forEach((doc) => {
            list.appendChild(
                createDocumentListItem(this.document, doc, {
                    onToggle: (target, checkbox) => this._toggle(target, checkbox),
                    onDelete: (target, button) => this._trash(target, button),
                    onRestore: (target, button) => this._restore(target, button),
                }),
            );
        });
        if (!documents.length) {
            this._status(
                this.showTrashed ? "Nothing deleted." : "No documents yet — add a PDF, Word file, or text file.",
                false,
            );
        } else {
            this._status("", false);
        }
    }

    async _activateSpace(space) {
        this.activeSpaceId = space.id;
        const { response } = await this._json(
            "/api/spaces/" + encodeURIComponent(space.id),
            this._mutation("POST", { active: true }),
        );
        if (!response.ok) {
            this._status("Could not switch space.", true);
            return;
        }
        await this.refresh();
    }

    async _createSpace() {
        const name = this.window && this.window.prompt
            ? this.window.prompt("Name this space", "New space")
            : null;
        if (!name) return;
        const { response } = await this._json("/api/spaces", this._mutation("POST", { name }));
        if (!response.ok) {
            this._status("Could not create the space.", true);
            return;
        }
        await this.refresh();
    }

    async _toggle(doc, checkbox) {
        checkbox.disabled = true;
        const { response } = await this._json(
            "/api/documents/" + encodeURIComponent(doc.id),
            this._mutation("POST", { selected: checkbox.checked }),
        );
        checkbox.disabled = false;
        if (!response.ok) {
            // Put the box back where it was: leaving it ticked would claim a
            // scope the server does not have.
            checkbox.checked = !checkbox.checked;
            this._status("Could not change what the assistant sees.", true);
            return;
        }
        await this.refresh();
    }

    async _trash(doc, button) {
        button.disabled = true;
        const { response } = await this._json(
            "/api/documents/" + encodeURIComponent(doc.id) + "/delete",
            this._mutation("POST"),
        );
        button.disabled = false;
        if (!response.ok) {
            this._status("Could not delete that document.", true);
            return;
        }
        await this.refresh();
        this._status("Deleted. It stays recoverable until it is purged.", false);
    }

    async _restore(doc, button) {
        button.disabled = true;
        const { response } = await this._json(
            "/api/documents/" + encodeURIComponent(doc.id) + "/restore",
            this._mutation("POST"),
        );
        button.disabled = false;
        if (!response.ok) {
            this._status("Could not restore that document.", true);
            return;
        }
        await this.refresh();
    }

    async _uploadAll(files) {
        if (!this.activeSpaceId) {
            this._status("Create a space first.", true);
            return;
        }
        for (const file of files) {
            await this._upload(file);
        }
        await this.refresh();
    }

    async _upload(file) {
        this._status("Adding " + file.name + "…", false);
        const form = new FormData();
        form.append("file", file, file.name);
        const headers = Object.assign({ Accept: "application/json" }, this.operatorHeaders());
        const { response, data } = await this._json(
            "/api/spaces/" + encodeURIComponent(this.activeSpaceId) + "/documents",
            { method: "POST", headers, body: form },
        );
        if (!response.ok) {
            // The server's message names the actual problem — a scan with no
            // text layer, an unsupported type — and is more useful than
            // anything this side could invent.
            this._status((data && data.error) || "Could not add " + file.name, true);
        }
    }

    destroy() {
        this.destroyed = true;
        this.elements = {};
    }
}

export const DOCUMENT_ACCEPT = ACCEPTED_TYPES;
