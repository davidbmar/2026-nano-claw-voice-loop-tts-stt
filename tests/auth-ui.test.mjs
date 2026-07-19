import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
    AUTH_DOM_IDS,
    AuthHistoryUI,
    GIS_SCRIPT_URL,
    createConversationListItem,
    createHistoryTurn,
    renderIdentity,
} from "../voice/web/auth.js";

class FakeClassList {
    constructor(element) {
        this.element = element;
        this.values = new Set();
    }

    add(...names) {
        names.forEach((name) => this.values.add(name));
        this.element.className = Array.from(this.values).join(" ");
    }

    remove(...names) {
        names.forEach((name) => this.values.delete(name));
        this.element.className = Array.from(this.values).join(" ");
    }

    contains(name) {
        return this.values.has(name);
    }
}

class FakeElement {
    constructor(tagName, documentRef) {
        this.tagName = String(tagName).toUpperCase();
        this.ownerDocument = documentRef;
        this.parentElement = null;
        this.children = [];
        this.listeners = new Map();
        this.attributes = new Map();
        this.className = "";
        this.classList = new FakeClassList(this);
        this.hidden = false;
        this.disabled = false;
        this._text = "";
        this._id = "";
    }

    set id(value) {
        this._id = String(value);
    }

    get id() {
        return this._id;
    }

    set textContent(value) {
        this.ownerDocument.textWrites += 1;
        this._text = String(value);
        this.children = [];
    }

    get textContent() {
        return this._text + this.children.map((child) => child.textContent).join("");
    }

    set innerHTML(_value) {
        this.ownerDocument.innerHtmlWrites += 1;
        throw new Error("innerHTML is forbidden in the auth/history UI");
    }

    get firstChild() {
        return this.children[0] || null;
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        this.ownerDocument.registerTree(child);
        return child;
    }

    replaceChildren(...children) {
        this._text = "";
        this.children = [];
        children.forEach((child) => this.appendChild(child));
    }

    addEventListener(name, handler) {
        const handlers = this.listeners.get(name) || [];
        handlers.push(handler);
        this.listeners.set(name, handlers);
    }

    removeEventListener(name, handler) {
        const handlers = this.listeners.get(name) || [];
        this.listeners.set(name, handlers.filter((candidate) => candidate !== handler));
    }

    dispatchEvent(event) {
        event.target = event.target || this;
        for (const handler of this.listeners.get(event.type) || []) handler.call(this, event);
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    getAttribute(name) {
        return this.attributes.get(name) ?? null;
    }

    contains(candidate) {
        if (candidate === this) return true;
        return this.children.some((child) => child.contains(candidate));
    }

    focus() {
        this.ownerDocument.activeElement = this;
    }

    scrollIntoView() {
        this.scrolledIntoView = true;
    }
}

class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.listeners = new Map();
        this.innerHtmlWrites = 0;
        this.textWrites = 0;
        this.activeElement = null;
        this.head = new FakeElement("head", this);
        this.body = new FakeElement("body", this);
    }

    createElement(tagName) {
        return new FakeElement(tagName, this);
    }

    getElementById(id) {
        return this.elements.get(id) || null;
    }

    addEventListener(name, handler) {
        const handlers = this.listeners.get(name) || [];
        handlers.push(handler);
        this.listeners.set(name, handlers);
    }

    removeEventListener(name, handler) {
        const handlers = this.listeners.get(name) || [];
        this.listeners.set(name, handlers.filter((candidate) => candidate !== handler));
    }

    registerTree(element) {
        if (element.id) this.elements.set(element.id, element);
        element.children.forEach((child) => this.registerTree(child));
    }

    addStatic(id, tagName = "div") {
        const element = this.createElement(tagName);
        element.id = id;
        this.elements.set(id, element);
        return element;
    }
}

function memoryStorage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}

function makeDom() {
    const documentRef = new FakeDocument();
    const buttonIds = new Set(AUTH_DOM_IDS.filter((id) => id.endsWith("button")));
    for (const id of AUTH_DOM_IDS) {
        documentRef.addStatic(id, buttonIds.has(id) ? "button" : "div");
    }

    const hiddenIds = [
        "auth-root",
        "auth-user-button",
        "auth-menu",
        "auth-status-wrap",
        "auth-retry-button",
        "past-conversations",
        "history-more-button",
        "history-detail-view",
        "history-turns-more-button",
    ];
    hiddenIds.forEach((id) => { documentRef.getElementById(id).hidden = true; });

    const root = documentRef.getElementById("auth-root");
    [
        "google-signin-button",
        "auth-user-button",
        "auth-menu",
        "auth-status-wrap",
    ].forEach((id) => root.appendChild(documentRef.getElementById(id)));
    documentRef.getElementById("auth-status-wrap")
        .appendChild(documentRef.getElementById("auth-status"));
    documentRef.getElementById("auth-status-wrap")
        .appendChild(documentRef.getElementById("auth-retry-button"));

    const history = documentRef.getElementById("past-conversations");
    history.appendChild(documentRef.getElementById("history-list-view"));
    history.appendChild(documentRef.getElementById("history-detail-view"));
    ["history-status", "history-list", "history-more-button"].forEach((id) => {
        documentRef.getElementById("history-list-view")
            .appendChild(documentRef.getElementById(id));
    });
    [
        "history-detail-title",
        "history-detail-meta",
        "history-transcript",
        "history-detail-status",
        "history-turns-more-button",
    ].forEach((id) => {
        documentRef.getElementById("history-detail-view")
            .appendChild(documentRef.getElementById(id));
    });
    documentRef.getElementById("transcription-panel").appendChild(history);
    documentRef.body.appendChild(documentRef.getElementById("header-channel"));
    documentRef.body.appendChild(root);
    documentRef.body.appendChild(documentRef.getElementById("transcription-panel"));

    const windowRef = {
        sessionStorage: memoryStorage(),
        confirm() { return true; },
    };
    return { documentRef, windowRef };
}

function response(status, body) {
    return {
        status,
        ok: status >= 200 && status < 300,
        async json() { return body; },
    };
}

function descendants(element) {
    return [element, ...element.children.flatMap((child) => descendants(child))];
}

const tests = [];
function test(name, callback) {
    tests.push({ name, callback });
}

test("auth off or missing client id leaves the original console and makes no GIS request", async () => {
    for (const configBody of [{ mode: "off" }, { mode: "optional" }]) {
        const { documentRef, windowRef } = makeDom();
        const calls = [];
        const ui = new AuthHistoryUI({
            document: documentRef,
            window: windowRef,
            fetch: async (url) => {
                calls.push(url);
                return response(200, configBody);
            },
        });
        await ui.initialize();
        assert.deepEqual(calls, ["/api/auth/config"]);
        assert.equal(documentRef.getElementById("header-channel").hidden, false);
        assert.equal(documentRef.getElementById("auth-root").hidden, true);
        assert.equal(documentRef.getElementById("past-conversations").hidden, true);
        assert.equal(documentRef.getElementById("google-identity-services"), null);
        ui.destroy();
    }
});

test("blocked or slow GIS degrades to explicit login-unavailable UI without reconnecting voice", async () => {
    const { documentRef, windowRef } = makeDom();
    let reconnects = 0;
    const replies = [
        response(200, { mode: "optional", clientId: "client.apps.googleusercontent.com", nonce: "n-1" }),
        response(401, { error: "unauthenticated" }),
    ];
    const ui = new AuthHistoryUI({
        document: documentRef,
        window: windowRef,
        fetch: async () => replies.shift(),
        onIdentityChange() { reconnects += 1; },
        gisTimeoutMs: 5,
        setTimeout(callback) { callback(); return 1; },
        clearTimeout() {},
    });
    await ui.initialize();
    const script = documentRef.getElementById("google-identity-services");
    assert.ok(script, "configured auth may append the GIS script");
    assert.equal(script.src, GIS_SCRIPT_URL);
    assert.equal(documentRef.getElementById("header-channel").hidden, true);
    assert.equal(documentRef.getElementById("auth-root").hidden, false);
    assert.match(documentRef.getElementById("auth-status").textContent, /LOGIN UNAVAILABLE/i);
    assert.match(documentRef.getElementById("history-status").textContent, /Live voice sessions/i);
    assert.equal(reconnects, 0, "GIS failure must not disturb the live app socket");
    ui.destroy();
});

test("bad GIS configuration and a JWKS outage fail closed without disturbing app sessions", async () => {
    {
        const { documentRef, windowRef } = makeDom();
        windowRef.google = {
            accounts: {
                id: {
                    initialize() { throw new Error("invalid client id"); },
                    renderButton() {},
                },
            },
        };
        const replies = [
            response(200, { mode: "optional", clientId: "bad-client", nonce: "n-1" }),
            response(401, { error: "unauthenticated" }),
        ];
        const ui = new AuthHistoryUI({
            document: documentRef,
            window: windowRef,
            fetch: async () => replies.shift(),
        });
        await ui.initialize();
        assert.match(documentRef.getElementById("auth-status").textContent, /LOGIN UNAVAILABLE/i);
        ui.destroy();
    }

    {
        const { documentRef, windowRef } = makeDom();
        let reconnects = 0;
        let loginOptions = null;
        windowRef.google = {
            accounts: {
                id: {
                    initialize() {},
                    renderButton(mount) { mount.appendChild(documentRef.createElement("iframe")); },
                },
            },
        };
        const fetchRef = async (url, options) => {
            if (url === "/api/auth/config") {
                return response(200, { mode: "optional", clientId: "client", nonce: "n-2" });
            }
            if (url === "/api/me") return response(401, { error: "unauthenticated" });
            if (url === "/api/auth/google") {
                loginOptions = options;
                return response(503, { error: "login_unavailable" });
            }
            throw new Error("unexpected request " + url);
        };
        const ui = new AuthHistoryUI({
            document: documentRef,
            window: windowRef,
            fetch: fetchRef,
            onIdentityChange() { reconnects += 1; },
        });
        await ui.initialize();
        await ui.handleCredentialResponse({ credential: "signed-google-id-token" });
        assert.equal(loginOptions.headers["X-NC-Auth"], "1");
        assert.match(documentRef.getElementById("auth-status").textContent, /LOGIN UNAVAILABLE/i);
        assert.equal(reconnects, 0, "a failed new login cannot replace the current app socket");
        ui.destroy();
    }
});

test("XSS-shaped names, titles, and turns are inert text with no innerHTML path", () => {
    const { documentRef } = makeDom();
    const attack = `"><img src=x onerror="globalThis.__authXss = true"><script>boom()</script>`;
    const name = documentRef.createElement("span");
    const avatar = documentRef.createElement("span");
    renderIdentity(name, avatar, { name: attack, email: "attacker@example.test" });
    const listItem = createConversationListItem(
        documentRef,
        { id: "voice-safe", title: attack, startedAt: "2026-07-18T12:00:00Z" },
        { onOpen() {}, onDelete() {} },
        { now: Date.parse("2026-07-18T12:01:00Z") },
    );
    const turn = createHistoryTurn(
        documentRef,
        { role: "agent", text: attack, ts: "2026-07-18T12:00:30Z" },
    );

    assert.equal(name.textContent, attack);
    assert.ok(listItem.textContent.includes(attack));
    assert.ok(turn.textContent.includes(attack));
    const createdTags = [...descendants(listItem), ...descendants(turn), name, avatar]
        .map((element) => element.tagName);
    assert.equal(createdTags.includes("IMG"), false);
    assert.equal(createdTags.includes("SCRIPT"), false);
    assert.equal(documentRef.innerHtmlWrites, 0);
    assert.equal(globalThis.__authXss, undefined);
});

test("logout posts the mutation guard before replacing a live identity socket", async () => {
    const { documentRef, windowRef } = makeDom();
    const events = [];
    let logoutOptions = null;
    windowRef.google = {
        accounts: {
            id: {
                initialize() {},
                renderButton(mount) { mount.appendChild(documentRef.createElement("iframe")); },
            },
        },
    };
    const fetchRef = async (url, options) => {
        if (url === "/api/auth/config") {
            return response(200, {
                mode: "optional",
                clientId: "client.apps.googleusercontent.com",
                nonce: "server-nonce",
            });
        }
        if (url === "/api/me") {
            return response(200, { user: { sub: "sub-1", tenant: "nano-claw", name: "Live User" } });
        }
        if (url.startsWith("/api/conversations?")) {
            return response(200, { conversations: [], nextCursor: null });
        }
        if (url === "/api/auth/logout") {
            events.push("logout-post");
            logoutOptions = options;
            return response(200, { ok: true });
        }
        throw new Error("unexpected request " + url);
    };
    const socket = { closed: false };
    const ui = new AuthHistoryUI({
        document: documentRef,
        window: windowRef,
        fetch: fetchRef,
        onIdentityChange() {
            socket.closed = true;
            events.push("socket-reconnected");
        },
    });
    await ui.initialize();
    await ui.signOut();
    assert.deepEqual(events, ["logout-post", "socket-reconnected"]);
    assert.equal(logoutOptions.method, "POST");
    assert.equal(logoutOptions.headers["X-NC-Auth"], "1");
    assert.equal(socket.closed, true);
    assert.equal(documentRef.getElementById("auth-user-button").hidden, true);
    assert.ok(documentRef.getElementById("google-signin-button").firstChild);
    ui.destroy();
});

test("history list and transcript consume opaque cursors and retain stored text", async () => {
    const { documentRef, windowRef } = makeDom();
    const attack = "<svg onload=globalThis.__historyXss=true>stored</svg>";
    const urls = [];
    const conversation = {
        id: "voice-history-1",
        title: attack,
        startedAt: "2026-07-18T12:00:00Z",
        incomplete: false,
    };
    const fetchRef = async (url) => {
        urls.push(url);
        if (url === "/api/auth/config") {
            return response(200, { mode: "optional", clientId: "client", nonce: "nonce" });
        }
        if (url === "/api/me") {
            return response(200, { user: { sub: "sub-1", tenant: "nano-claw", name: attack } });
        }
        if (url === "/api/conversations?limit=20") {
            return response(200, { conversations: [conversation], nextCursor: "opaque-list" });
        }
        if (url === "/api/conversations?limit=20&cursor=opaque-list") {
            return response(200, {
                conversations: [{ ...conversation, id: "voice-history-2", title: "Second" }],
                nextCursor: null,
            });
        }
        if (url === "/api/conversations/voice-history-1?limit=50") {
            return response(200, {
                conversation,
                turns: [{ seq: 0, role: "user", text: attack, ts: conversation.startedAt }],
                nextCursor: "opaque-turns",
            });
        }
        if (url === "/api/conversations/voice-history-1?limit=50&cursor=opaque-turns") {
            return response(200, {
                conversation,
                turns: [{ seq: 1, role: "agent", text: "Second turn", ts: conversation.startedAt }],
                nextCursor: null,
            });
        }
        throw new Error("unexpected request " + url);
    };
    const ui = new AuthHistoryUI({
        document: documentRef,
        window: windowRef,
        fetch: fetchRef,
        now: () => Date.parse("2026-07-18T12:01:00Z"),
    });
    await ui.initialize();
    await ui.loadConversations(true);
    assert.equal(documentRef.getElementById("history-list").children.length, 2);
    await ui.openConversation(conversation);
    assert.equal(documentRef.getElementById("transcription-heading").textContent, "Saved transcript");
    assert.equal(documentRef.getElementById("transcription-live").hidden, true);
    await ui.loadConversationTurns(true);
    assert.equal(documentRef.getElementById("history-transcript").children.length, 2);
    assert.ok(documentRef.getElementById("history-transcript").textContent.includes(attack));
    assert.ok(urls.includes("/api/conversations?limit=20&cursor=opaque-list"));
    assert.ok(urls.includes("/api/conversations/voice-history-1?limit=50&cursor=opaque-turns"));
    assert.equal(documentRef.innerHtmlWrites, 0);
    assert.equal(globalThis.__historyXss, undefined);
    ui.closeConversation();
    assert.equal(documentRef.getElementById("transcription-heading").textContent, "Transcription");
    assert.equal(documentRef.getElementById("transcription-live").hidden, false);
    ui.destroy();
});

test("mobile rules keep auth and history surfaces bounded", async () => {
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    const css = await readFile(new URL("../voice/web/styles.css", import.meta.url), "utf8");
    assert.match(html, /name="viewport"[^>]*width=device-width/);
    assert.match(css, /@media \(max-width: 760px\)[\s\S]*#transcription-panel[\s\S]*height: 460px/);
    assert.match(css, /@media \(max-width: 500px\)[\s\S]*\.auth-root[\s\S]*max-width: 148px/);
    assert.match(css, /@media \(max-width: 390px\)[\s\S]*\.auth-user-name[\s\S]*display: none/);
});

test("the browser allowlist contains only declared GIS origins and no remote avatar", async () => {
    const authSource = await readFile(new URL("../voice/web/auth.js", import.meta.url), "utf8");
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    const serverSource = await readFile(new URL("../voice/webauth/aiohttp_adapter.py", import.meta.url), "utf8");
    const externalUrls = [...new Set(authSource.match(/https:\/\/[^"'\s)]+/g) || [])];
    assert.deepEqual(externalUrls, [GIS_SCRIPT_URL]);
    assert.doesNotMatch(authSource + html + serverSource, /googleusercontent\.com\/|<img\b/i);
    assert.match(authSource, /size: "medium"[\s\S]*width: 184/);
    assert.match(serverSource, /script-src 'self' https:\/\/accounts\.google\.com\/gsi\/client/);
    assert.match(serverSource, /frame-src https:\/\/accounts\.google\.com\/gsi\//);
    assert.match(serverSource, /connect-src 'self' ws:\/\/localhost:9090[\s\S]*https:\/\/accounts\.google\.com\/gsi\//);
    assert.match(serverSource, /style-src 'self' 'unsafe-inline' https:\/\/accounts\.google\.com\/gsi\/style/);
    assert.match(serverSource, /img-src 'self' data:/);
    assert.match(serverSource, /"Referrer-Policy": "strict-origin-when-cross-origin"/);
    assert.match(serverSource, /"X-Content-Type-Options": "nosniff"/);
});

test("all JavaScript DOM ids survive and auth rendering has no HTML sink", async () => {
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    const appSource = await readFile(new URL("../voice/web/app.js", import.meta.url), "utf8");
    const authSource = await readFile(new URL("../voice/web/auth.js", import.meta.url), "utf8");
    const htmlIds = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
    assert.equal(new Set(htmlIds).size, htmlIds.length, "HTML ids must be unique");
    const appIds = [...appSource.matchAll(/getElementById\("([^"]+)"\)/g)]
        .map((match) => match[1]);
    for (const id of [...new Set([...appIds, ...AUTH_DOM_IDS])]) {
        assert.ok(htmlIds.includes(id), "missing DOM id: " + id);
    }
    assert.doesNotMatch(authSource, /\.innerHTML\b|insertAdjacentHTML|document\.write/);
    assert.match(appSource, /function reconnectForIdentityChange\(\)[\s\S]*previous\.close[\s\S]*connect\(\)/);
});

let passed = 0;
let failed = 0;
for (const entry of tests) {
    try {
        await entry.callback();
        passed += 1;
        console.log(`ok ${passed} - ${entry.name}`);
    } catch (error) {
        failed += 1;
        console.error(`not ok ${passed + failed} - ${entry.name}`);
        console.error(error);
    }
}

console.log(`auth-ui tests: ${passed} passed, ${failed} failed`);
if (failed) process.exitCode = 1;
