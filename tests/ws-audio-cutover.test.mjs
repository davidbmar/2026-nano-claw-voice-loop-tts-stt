import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


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


test("WS audio is the run.sh default and WebRTC remains an explicit override", async () => {
    const runSource = await readFile(new URL("../run.sh", import.meta.url), "utf8");
    const docs = await readFile(new URL("../docs/WS-AUDIO.md", import.meta.url), "utf8");

    assert.match(runSource, /NANO_CLAW_WS_AUDIO="\$\{NANO_CLAW_WS_AUDIO:-1\}"/);
    assert.match(runSource, /-e NANO_CLAW_WS_AUDIO="\$NANO_CLAW_WS_AUDIO"/);
    assert.match(docs, /NANO_CLAW_WS_AUDIO=0/);
    assert.match(docs, /same-LAN, lower-latency compatibility path/);
});


test("the readiness contract has separate text and voice indicators and one control gate", async () => {
    const html = await readFile(new URL("../voice/web/index.html", import.meta.url), "utf8");
    assert.match(html, /id="link-status"[\s\S]*id="link-status-text"/);
    assert.match(html, /id="audio-status"[\s\S]*id="status-text"/);

    assert.equal(
        [...appSource.matchAll(/textInput\.disabled\s*=/g)].length,
        1,
        "text input must only be gated by syncReadinessControls",
    );
    assert.equal(
        [...appSource.matchAll(/sendBtn\.disabled\s*=/g)].length,
        1,
        "Send must only be gated by syncReadinessControls",
    );
    assert.match(
        extractFunction(appSource, "syncReadinessControls"),
        /textInput\.disabled = !linkReady;[\s\S]*sendBtn\.disabled = !linkReady;[\s\S]*talkBtn\.disabled = !audioConnected;/,
    );
    const webRtcStart = extractFunction(appSource, "startWebRTC");
    assert.ok(
        webRtcStart.indexOf("addTransceiver") < webRtcStart.indexOf("getUserMedia"),
        "the WebRTC text Session must negotiate without waiting for mic permission",
    );
    assert.match(webRtcStart, /direction: "sendrecv"/);
});


test("audio failure leaves text enabled for a WebSocket round trip and disables only mic", () => {
    const functionNames = [
        "syncReadinessControls",
        "renderReadinessState",
        "setLinkState",
        "setAudioState",
        "setAudioUnavailable",
        "setTextTransportReady",
        "sendMsg",
        "handleMessage",
        "sendTextMessage",
    ];
    const functions = functionNames.map((name) => extractFunction(appSource, name)).join("\n");
    const context = vm.createContext({ console });
    vm.runInContext(`
        const textInput = { disabled: true, value: "" };
        const sendBtn = { disabled: true };
        const talkBtn = { disabled: true };
        const linkStatus = { dataset: {} };
        const linkStatusText = { textContent: "" };
        const audioStatus = { dataset: {} };
        const statusText = { textContent: "" };
        const WebSocket = { OPEN: 1 };
        let ws = null;
        let linkReady = false;
        let audioConnected = false;
        let textTransportReady = false;
        let pendingTextMessages = [];
        let wsAudioEnabled = true;
        let wsAudioPlayer = null;
        let phoneModeEnabled = false;
        let autoTurnPending = false;
        let authHistory = null;
        let streamingBubble = null;
        let connectionGeneration = 7;
        const bubbles = [];
        function beginTurnLatency() {}
        function setPhoneStatus() {}
        function markTranscriptionLatency() {}
        function showThinking() {}
        function setVisualPresence() {}
        function markFirstAgentTextLatency() {}
        function inferEmotionFromReply() {}
        function setAgentSpeaking() {}
        function clearThinking() {}
        function finalizeAgentBubble() {}
        function setVisualizationSpeaking() {}
        function resetBargeInDetector() {}
        function rearmPhoneMode() {}
        function appendAgentDelta() {}
        function appendSystemLine() {}
        function showToolCard() {}
        function addDebugEntry() {}
        function renderFlowState() {}
        function addBubble(text, role) { bubbles.push({ text, role }); }
        ${functions}
        globalThis.cutover = {
            controls: { textInput, sendBtn, talkBtn },
            states: { linkStatus, linkStatusText, audioStatus, statusText },
            bubbles,
            bindSocket(socket) { ws = socket; },
            setLinkState,
            setAudioUnavailable,
            setTextTransportReady,
            sendTextMessage,
            handleMessage,
        };
    `, context);

    const received = [];
    const socket = {
        readyState: 1,
        send(raw) {
            const message = JSON.parse(raw);
            received.push(message);
            if (message.type !== "text_message") return;
            context.cutover.handleMessage(
                { type: "transcription", text: message.text },
                7,
            );
            context.cutover.handleMessage(
                { type: "agent_reply", text: "echo: " + message.text },
                7,
            );
        },
    };

    context.cutover.bindSocket(socket);
    context.cutover.setLinkState("ready", "Text ready");
    context.cutover.setTextTransportReady(true);
    context.cutover.setAudioUnavailable("Connection failed");

    assert.equal(context.cutover.controls.textInput.disabled, false);
    assert.equal(context.cutover.controls.sendBtn.disabled, false);
    assert.equal(context.cutover.controls.talkBtn.disabled, true);
    assert.equal(context.cutover.states.linkStatus.dataset.state, "ready");
    assert.equal(context.cutover.states.audioStatus.dataset.state, "unavailable");
    assert.match(context.cutover.states.statusText.textContent, /type below/);

    context.cutover.controls.textInput.value = "remote text still works";
    context.cutover.sendTextMessage();

    assert.deepEqual(received, [
        { type: "text_message", text: "remote text still works" },
    ]);
    assert.deepEqual(
        JSON.parse(JSON.stringify(context.cutover.bubbles)),
        [
            { text: "remote text still works", role: "user" },
            { text: "echo: remote text still works", role: "agent" },
        ],
    );
    assert.equal(context.cutover.controls.textInput.disabled, false);
    assert.equal(context.cutover.controls.sendBtn.disabled, false);
    assert.equal(context.cutover.controls.talkBtn.disabled, true);
});
