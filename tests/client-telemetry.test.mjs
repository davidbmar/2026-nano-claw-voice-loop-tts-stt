import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const appSource = await readFile(
    new URL("../voice/web/app.js", import.meta.url),
    "utf8",
);
const telemetryStart = appSource.indexOf("var DIAG_ON");
const telemetryEndMarker =
    'pageLog("client diagnostics on · ua=" + navigator.userAgent.slice(0, 40));';
const telemetryEnd = appSource.indexOf(telemetryEndMarker, telemetryStart);
assert.ok(telemetryStart >= 0, "missing telemetry flag block");
assert.ok(telemetryEnd >= 0, "missing initial diagnostic line");
const telemetrySource = appSource.slice(
    telemetryStart,
    telemetryEnd + telemetryEndMarker.length,
);


function createHarness(search) {
    const fetchCalls = [];
    const timers = new Map();
    const listeners = new Map();
    const appended = [];
    let nextTimer = 1;
    const windowRef = {
        setTimeout(callback, delay) {
            const id = nextTimer++;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
        addEventListener(name, callback) {
            const callbacks = listeners.get(name) || [];
            callbacks.push(callback);
            listeners.set(name, callbacks);
        },
    };
    const documentRef = {
        body: {
            appendChild(element) {
                appended.push(element);
                return element;
            },
        },
        documentElement: {
            appendChild(element) {
                appended.push(element);
                return element;
            },
        },
        createElement() {
            return {
                addEventListener() {},
                style: {},
                textContent: "",
            };
        },
    };
    const context = vm.createContext({
        console,
        document: documentRef,
        fetch(url, options) {
            fetchCalls.push({ url, options });
            return Promise.resolve({ ok: true });
        },
        location: { search },
        navigator: {
            clipboard: { writeText() {} },
            userAgent: "Nano Telemetry Test Browser/1.0",
        },
        window: windowRef,
    });
    vm.runInContext(telemetrySource, context);
    return {
        appended,
        context,
        dispatch(name) {
            for (const callback of listeners.get(name) || []) callback({ type: name });
        },
        fetchCalls,
        runFirstTimer() {
            const entry = timers.entries().next().value;
            assert.ok(entry, "expected a scheduled telemetry flush");
            const [id, timer] = entry;
            timers.delete(id);
            timer.callback();
            return timer.delay;
        },
        timers,
    };
}


test("pageLog batches ten lifecycle lines into a guarded telemetry POST", () => {
    const harness = createHarness("?telemetry");
    for (let index = 0; index < 9; index += 1) {
        harness.context.pageLog("WS OPEN gen=" + index);
    }

    assert.equal(harness.fetchCalls.length, 1);
    const [{ url, options }] = harness.fetchCalls;
    const payload = JSON.parse(options.body);
    assert.equal(url, "/api/client-log");
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers["X-NC-Auth"], "1");
    assert.equal(options.keepalive, false);
    assert.equal(payload.events.length, 10);
    assert.equal(payload.events[0].tag, "lifecycle");
    assert.equal(payload.events[9].tag, "ws");
    assert.equal(payload.events[9].msg, "WS OPEN gen=8");
    assert.equal(payload.conv, null);
    assert.equal(payload.ua, "Nano Telemetry Test Browser/1.0");
    assert.equal(harness.context._clientTelemetryQueue.length, 0);
});


test("pageLog flushes a partial batch after two seconds", () => {
    const harness = createHarness("?telemetry");
    harness.context.pageLog("getUserMedia OK -> sending mic_audio_start");

    assert.equal(harness.fetchCalls.length, 0);
    assert.equal(harness.runFirstTimer(), 2000);
    assert.equal(harness.fetchCalls.length, 1);
    const payload = JSON.parse(harness.fetchCalls[0].options.body);
    assert.equal(payload.events.length, 2);
    assert.equal(payload.events[1].tag, "media");
});


test("pagehide flushes queued lines with fetch keepalive", () => {
    const harness = createHarness("?telemetry");
    harness.context._clientTelemetryConversation = "voice-server-owned";
    harness.context.pageLog("mic_audio_ready -> Voice ready");
    harness.dispatch("pagehide");

    assert.equal(harness.fetchCalls.length, 1);
    const [{ options }] = harness.fetchCalls;
    const payload = JSON.parse(options.body);
    assert.equal(options.keepalive, true);
    assert.equal(payload.conv, "voice-server-owned");
    assert.equal(payload.events[1].tag, "voice");
    assert.equal(harness.timers.size, 0);
});


test("shipping is completely off without diag or telemetry flags", () => {
    const harness = createHarness("");
    harness.context.pageLog("WS OPEN gen=1");
    harness.dispatch("pagehide");

    assert.equal(harness.fetchCalls.length, 0);
    assert.equal(harness.timers.size, 0);
    assert.equal(harness.context._clientTelemetryQueue.length, 0);
    assert.equal(harness.appended.length, 0);
});


test("diag keeps the overlay and opts into the same shipping queue", () => {
    const harness = createHarness("?diag");

    assert.equal(harness.appended.length, 1);
    assert.equal(harness.context._clientTelemetryQueue.length, 1);
    assert.equal(harness.fetchCalls.length, 0);
});


test("hello_ack stores only the server-provided conversation hint", () => {
    assert.match(
        appSource,
        /case "hello_ack":[\s\S]*typeof msg\.conversationId === "string"[\s\S]*_clientTelemetryConversation = msg\.conversationId/,
    );
});
