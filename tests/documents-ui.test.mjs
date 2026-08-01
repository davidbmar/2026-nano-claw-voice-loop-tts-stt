/**
 * The Documents panel's rendering, against a fake DOM.
 *
 * Same approach as auth-ui.test.mjs, and the same reason documents.js takes an
 * injected document: the rows are where a customer reads what the assistant
 * can see, so they are worth asserting on directly rather than through a
 * browser.
 */

import { strict as assert } from "node:assert";
import test from "node:test";
import { readFile } from "node:fs/promises";

import {
    DOCUMENTS_DOM_IDS,
    createDocumentListItem,
    createSpaceListItem,
    formatSize,
    scopeSummary,
} from "../voice/web/documents.js";

class FakeClassList {
    constructor(element) {
        this.element = element;
    }
    add(name) {
        const parts = this.element.className.split(" ").filter(Boolean);
        if (!parts.includes(name)) parts.push(name);
        this.element.className = parts.join(" ");
    }
    contains(name) {
        return this.element.className.split(" ").filter(Boolean).includes(name);
    }
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName;
        this.className = "";
        this.children = [];
        this.attributes = {};
        this.listeners = {};
        this.textContent = "";
        this.checked = false;
        this.disabled = false;
        this.type = "";
        this.classList = new FakeClassList(this);
    }
    appendChild(child) {
        this.children.push(child);
        return child;
    }
    setAttribute(name, value) {
        this.attributes[name] = value;
    }
    getAttribute(name) {
        return this.attributes[name];
    }
    addEventListener(name, handler) {
        (this.listeners[name] = this.listeners[name] || []).push(handler);
    }
    dispatch(name) {
        (this.listeners[name] || []).forEach((handler) => handler());
    }
    find(className) {
        if (this.classList.contains(className)) return this;
        for (const child of this.children) {
            const found = child.find ? child.find(className) : null;
            if (found) return found;
        }
        return null;
    }
}

const fakeDocument = {
    createElement(tagName) {
        return new FakeElement(tagName);
    },
};

const READY = {
    id: "doc_1",
    title: "W-2 2025",
    filename: "w2.pdf",
    status: "ready",
    selected: true,
    bytes: 240000,
    deletedAt: null,
};

const noopActions = { onToggle() {}, onDelete() {}, onRestore() {}, onSelect() {} };

test("a ready document renders a ticked, enabled checkbox", () => {
    const item = createDocumentListItem(fakeDocument, READY, noopActions);
    const checkbox = item.find("document-item-check");
    assert.equal(checkbox.checked, true);
    assert.equal(checkbox.disabled, false);
    assert.equal(item.find("document-item-title").textContent, "W-2 2025");
});

test("a document that is not ready yet cannot be ticked", () => {
    // The tick claims the assistant can see it. Until indexing finishes that
    // would be a lie, so the control is disabled rather than merely ignored.
    for (const status of ["extracting", "indexing", "failed"]) {
        const item = createDocumentListItem(
            fakeDocument,
            { ...READY, status, selected: false },
            noopActions,
        );
        assert.equal(item.find("document-item-check").disabled, true, status);
    }
});

test("a failed document shows its error rather than a size", () => {
    const item = createDocumentListItem(
        fakeDocument,
        { ...READY, status: "failed", error: "This PDF has no text layer — it looks like a scan." },
        noopActions,
    );
    const meta = item.find("document-item-meta");
    assert.match(meta.textContent, /no text layer/);
    assert.ok(meta.classList.contains("document-item-error"));
});

test("a trashed document offers Undo instead of Delete", () => {
    const item = createDocumentListItem(
        fakeDocument,
        { ...READY, deletedAt: 1750000000 },
        noopActions,
    );
    assert.equal(item.find("document-item-delete").textContent, "Undo");
    assert.ok(item.classList.contains("document-item-trashed"));
    assert.equal(item.find("document-item-check").disabled, true);
});

test("toggling and deleting call through to their actions", () => {
    const calls = [];
    const item = createDocumentListItem(fakeDocument, READY, {
        ...noopActions,
        onToggle: (doc) => calls.push(["toggle", doc.id]),
        onDelete: (doc) => calls.push(["delete", doc.id]),
    });
    item.find("document-item-check").dispatch("change");
    item.find("document-item-delete").dispatch("click");
    assert.deepEqual(calls, [["toggle", "doc_1"], ["delete", "doc_1"]]);
});

test("the active space is marked, because it is what the phone answers from", () => {
    const active = createSpaceListItem(
        fakeDocument,
        { id: "spc_1", name: "Taxes 2025", documentCount: 3, isActive: true },
        noopActions,
    );
    const idle = createSpaceListItem(
        fakeDocument,
        { id: "spc_2", name: "Handbook", documentCount: 1, isActive: false },
        noopActions,
    );
    assert.ok(active.classList.contains("space-item-active"));
    assert.equal(active.getAttribute("aria-pressed"), "true");
    assert.equal(idle.getAttribute("aria-pressed"), "false");
    assert.equal(idle.find("space-item-count").textContent, "1 doc");
    assert.equal(active.find("space-item-count").textContent, "3 docs");
});

test("the scope summary distinguishes nothing-ticked from nothing-uploaded", () => {
    // These are different states with different fixes, and "0 of 3" alone
    // reads like a bug rather than a choice the customer made.
    assert.match(scopeSummary({ readyCount: 3, selectedCount: 0 }), /Nothing ticked/);
    assert.match(scopeSummary({ readyCount: 0, selectedCount: 0 }), /Nothing indexed/);
    assert.match(scopeSummary({ readyCount: 3, selectedCount: 3 }), /All 3/);
    assert.match(scopeSummary({ readyCount: 3, selectedCount: 2 }), /2 of 3/);
});

test("the first-run message says what to do, not just what is missing", () => {
    // A brand-new customer sees this before anything else; "No space selected"
    // reports a state and leaves them with nothing to act on.
    assert.match(scopeSummary(null, false), /Create a space/);
    assert.match(scopeSummary(null, true), /No space selected/);
});

test("sizes are readable rather than exact", () => {
    assert.equal(formatSize(512), "512 B");
    assert.equal(formatSize(2048), "2 KB");
    assert.equal(formatSize(3 * 1024 * 1024), "3.0 MB");
    assert.equal(formatSize(0), "");
});

test("every id the panel requires exists in the markup", async () => {
    // The constructor throws when one is missing; this catches a rename in
    // index.html before it becomes a panel that silently fails to mount.
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    for (const id of DOCUMENTS_DOM_IDS) {
        assert.ok(html.includes(`id="${id}"`), `index.html is missing id="${id}"`);
    }
});

test("the upload input accepts the formats the server can actually read", async () => {
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    const accept = html.match(/id="documents-file-input"[\s\S]{0,200}?accept="([^"]+)"/);
    assert.ok(accept, "the file input must declare what it accepts");
    for (const suffix of [".pdf", ".docx", ".txt", ".md"]) {
        assert.ok(accept[1].includes(suffix), `accept is missing ${suffix}`);
    }
});
