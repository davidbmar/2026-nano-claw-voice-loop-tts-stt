import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

import { Pcm16AudioPlayer } from "../voice/web/ws-audio-player.js";


const appSource = await readFile(
    new URL("../voice/web/app.js", import.meta.url),
    "utf8",
);


function extractFunction(source, name) {
    const match = new RegExp("(?:async\\s+)?function\\s+" + name + "\\s*\\(").exec(source);
    assert.ok(match, "missing function " + name);
    const start = match.index;
    const bodyStart = source.indexOf("{", start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    throw new Error("unterminated function " + name);
}


class FakeAnalyser {
    connect() {}
    disconnect() {}
}


class FakeSource {
    connect() {}
    disconnect() {}
    addEventListener() {}
    start() {}
    stop() {}
}


class SuspendedAudioContext {
    static gestureActive = false;

    constructor() {
        this.currentTime = 1;
        this.destination = {};
        this.state = "suspended";
        this.resumeCalls = [];
        this.gestureResumeAttempts = 0;
        this.createdSources = [];
    }

    createAnalyser() { return new FakeAnalyser(); }

    createBuffer(_channels, samples, rate) {
        const data = new Float32Array(samples);
        return {
            duration: samples / rate,
            getChannelData() { return data; },
        };
    }

    createBufferSource() {
        const source = new FakeSource();
        this.createdSources.push(source);
        return source;
    }

    async resume() {
        this.resumeCalls.push(SuspendedAudioContext.gestureActive);
        if (!SuspendedAudioContext.gestureActive) return;
        this.gestureResumeAttempts += 1;
        // Model a browser that declines the first activation. The app must
        // leave its gesture listener armed and retry on the next activation.
        if (this.gestureResumeAttempts >= 2) this.state = "running";
    }

    async close() { this.state = "closed"; }
}


test("Start mic resumes suspended WS playback and retries on the next gesture", async () => {
    assert.match(
        appSource,
        /document\.addEventListener\("pointerdown", resumeWsAudioFromGesture/,
        "document pointer gestures must eagerly unlock WS playback",
    );
    assert.match(
        appSource,
        /talkBtn\.addEventListener\("click", handleTalkButtonClick\)/,
        "the Start mic click must use the tested gesture handler",
    );

    const player = new Pcm16AudioPlayer({
        AudioContextClass: SuspendedAudioContext,
        sampleRate: 16000,
    });
    player.begin();
    await Promise.resolve();
    assert.deepEqual(player.context.resumeCalls, [false], "startup resume is outside a gesture");
    assert.equal(player.context.state, "suspended");

    const functions = [
        "enqueueWsAudioFrame",
        "resumeWsAudioFromGesture",
        "handleTalkButtonClick",
    ].map((name) => extractFunction(appSource, name)).join("\n");
    const context = vm.createContext({ console, player });
    vm.runInContext(`
        let wsAudioEnabled = true;
        let wsAudioPlayer = player;
        let wsAudioFirstFrameLogged = false;
        const captureContext = {
            state: "suspended",
            resumeCalls: 0,
            resume() {
                this.resumeCalls += 1;
                this.state = "running";
                return Promise.resolve();
            },
        };
        let wsMicAudioContext = captureContext;
        let phoneModeEnabled = false;
        const logs = [];
        function pageLog(message) { logs.push(message); }
        function startPhoneMode() {}
        function stopPhoneMode() {}
        ${functions}
        globalThis.harness = {
            captureContext,
            click: handleTalkButtonClick,
            enqueue: enqueueWsAudioFrame,
            logs,
        };
    `, context);

    SuspendedAudioContext.gestureActive = true;
    context.harness.click();
    SuspendedAudioContext.gestureActive = false;
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(player.context.gestureResumeAttempts, 1);
    assert.equal(player.context.state, "suspended", "first gesture may still be declined");
    assert.equal(context.harness.captureContext.resumeCalls, 1, "capture resumes in the same gesture");
    assert.ok(context.harness.logs.includes("agent playback gesture resume, ctx=suspended"));

    context.harness.enqueue(new Int16Array(320).buffer);
    assert.equal(player.context.createdSources.length, 1, "PCM remains scheduled while awaiting retry");
    assert.ok(context.harness.logs.includes("agent frame, ctx=suspended"));

    SuspendedAudioContext.gestureActive = true;
    context.harness.click();
    SuspendedAudioContext.gestureActive = false;
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(player.context.gestureResumeAttempts, 2, "the next gesture retries playback");
    assert.equal(player.context.state, "running");
    assert.ok(context.harness.logs.includes("agent playback gesture resume, ctx=running"));

    context.harness.enqueue(new Int16Array(320).buffer);
    assert.equal(
        context.harness.logs.filter((line) => line.startsWith("agent frame, ctx=")).length,
        1,
        "only the first agent frame logs its context state",
    );
});
