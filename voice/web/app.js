"use strict";

// ── DOM refs ─────────────────────────────────────────────────
const statusText = document.getElementById("status-text");
const chatLog = document.getElementById("chat-log");
const talkBtn = document.getElementById("talk-btn");
const stopBtn = document.getElementById("stop-btn");
const textInput = document.getElementById("text-input");
const sendBtn = document.getElementById("send-btn");
const debugPanel = document.getElementById("debug-panel");
const debugToggle = document.getElementById("debug-toggle");
const debugContent = document.getElementById("debug-content");
const debugModalOverlay = document.getElementById("debug-modal-overlay");
const debugModalBody = document.getElementById("debug-modal-body");
const debugModalClose = document.getElementById("debug-modal-close");

// ── State ────────────────────────────────────────────────────
let ws = null;
let pc = null;
let audioEl = null;
let micStream = null;
let isRecording = false;
let agentSpeaking = false;

// ── Markdown rendering (safe DOM, no innerHTML) ──────────────
function renderMarkdown(container, text) {
    const paragraphs = text.split(/\n{2,}/);
    paragraphs.forEach(function (para) {
        const trimmed = para.trim();
        if (!trimmed) return;
        const lines = trimmed.split("\n");
        const isList = lines.every(function (l) {
            return /^\s*[-*]\s+/.test(l) || /^\s*\d+\.\s+/.test(l) || !l.trim();
        });
        if (isList) {
            const ul = document.createElement("ul");
            lines.forEach(function (line) {
                const content = line.replace(/^\s*[-*]\s+/, "").replace(/^\s*\d+\.\s+/, "").trim();
                if (content) {
                    const li = document.createElement("li");
                    renderInline(li, content);
                    ul.appendChild(li);
                }
            });
            container.appendChild(ul);
        } else {
            const p = document.createElement("p");
            renderInline(p, lines.join(" "));
            container.appendChild(p);
        }
    });
}

function renderInline(el, text) {
    const pattern = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(`(.+?)`)|(\[([^\]]+)\]\(([^)]+)\))|(https?:\/\/[^\s),]+)/g;
    let lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
        if (match.index > lastIndex) {
            el.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        if (match[1]) {
            const strong = document.createElement("strong");
            strong.textContent = match[2];
            el.appendChild(strong);
        } else if (match[3]) {
            const em = document.createElement("em");
            em.textContent = match[4];
            el.appendChild(em);
        } else if (match[5]) {
            const code = document.createElement("code");
            code.textContent = match[6];
            el.appendChild(code);
        } else if (match[7]) {
            const a = document.createElement("a");
            a.textContent = match[8];
            a.href = match[9];
            a.target = "_blank";
            a.rel = "noopener";
            el.appendChild(a);
        } else if (match[10]) {
            const a = document.createElement("a");
            try {
                a.textContent = new URL(match[10]).hostname.replace("www.", "");
            } catch (_e) {
                a.textContent = match[10];
            }
            a.href = match[10];
            a.target = "_blank";
            a.rel = "noopener";
            el.appendChild(a);
        }
        lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
        el.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
}

// ── Chat bubbles ─────────────────────────────────────────────
function addBubble(text, role) {
    clearThinking();
    const bubble = document.createElement("div");
    bubble.className = "msg msg-" + role;
    if (role === "agent") {
        renderMarkdown(bubble, text);
    } else {
        bubble.textContent = text;
    }
    chatLog.appendChild(bubble);
    chatLog.scrollTop = chatLog.scrollHeight;
}

function showThinking() {
    clearThinking();
    const el = document.createElement("div");
    el.className = "msg msg-agent thinking";
    el.textContent = "Thinking...";
    chatLog.appendChild(el);
    chatLog.scrollTop = chatLog.scrollHeight;
}

function clearThinking() {
    const el = chatLog.querySelector(".thinking");
    if (el) el.remove();
}

// ── Tool approval card ───────────────────────────────────────
function showToolCard(requestId, tools) {
    clearThinking();
    const card = document.createElement("div");
    card.className = "tool-card";

    const header = document.createElement("div");
    header.className = "tool-card-header";
    header.textContent = "Tool Approval Required";
    card.appendChild(header);

    tools.forEach(function (tool) {
        const item = document.createElement("div");
        item.className = "tool-item";

        const name = document.createElement("div");
        name.className = "tool-name";
        name.textContent = tool.name;
        item.appendChild(name);

        const args = document.createElement("pre");
        args.className = "tool-args";
        args.textContent = JSON.stringify(tool.args, null, 2);
        item.appendChild(args);

        card.appendChild(item);
    });

    const actions = document.createElement("div");
    actions.className = "tool-actions";

    const approveBtn = document.createElement("button");
    approveBtn.className = "tool-btn tool-approve";
    approveBtn.textContent = "Approve";
    approveBtn.addEventListener("click", function () {
        card.classList.add("tool-decided");
        approveBtn.disabled = true;
        rejectBtn.disabled = true;
        showThinking();
        sendMsg("tool_approve", { requestId: requestId });
    });
    actions.appendChild(approveBtn);

    const rejectBtn = document.createElement("button");
    rejectBtn.className = "tool-btn tool-reject";
    rejectBtn.textContent = "Reject";
    rejectBtn.addEventListener("click", function () {
        card.classList.add("tool-decided");
        approveBtn.disabled = true;
        rejectBtn.disabled = true;
        showThinking();
        sendMsg("tool_reject", { requestId: requestId });
    });
    actions.appendChild(rejectBtn);

    card.appendChild(actions);
    chatLog.appendChild(card);
    chatLog.scrollTop = chatLog.scrollHeight;
}

// ── Debug panel ──────────────────────────────────────────────
debugToggle.addEventListener("click", function () {
    debugPanel.classList.toggle("debug-collapsed");
    debugPanel.classList.toggle("debug-expanded");
});

function addDebugEntry(info) {
    var row = document.createElement("div");
    row.className = "debug-row";

    var tokens = info.tokenUsage
        ? info.tokenUsage.prompt + "/" + info.tokenUsage.completion + "/" + info.tokenUsage.total
        : "—";

    var fields = [
        ["iter", String(info.iteration)],
        ["msgs", String(info.messageCount)],
        ["model", info.model],
        ["tok", tokens],
        ["dur", info.durationMs + "ms"],
        ["finish", info.finishReason || "—"],
    ];

    fields.forEach(function (pair, i) {
        if (i > 0) row.appendChild(document.createTextNode("  "));
        var label = document.createElement("span");
        label.className = "debug-label";
        label.textContent = pair[0];
        row.appendChild(label);
        var value = document.createElement("span");
        value.className = pair[0] === "tok" ? "debug-tokens"
            : pair[0] === "dur" ? "debug-duration"
            : "";
        value.textContent = " " + pair[1];
        row.appendChild(value);
    });

    row.addEventListener("click", function () {
        showDebugDetail(info);
    });

    debugContent.appendChild(row);
    debugContent.scrollTop = debugContent.scrollHeight;
}

function showDebugDetail(info) {
    // Clear previous content
    while (debugModalBody.firstChild) debugModalBody.removeChild(debugModalBody.firstChild);

    var details = [
        {
            key: "Iteration",
            value: String(info.iteration),
            cls: "",
            desc: "Which pass through the agent loop. The agent may loop multiple times if it calls tools.",
        },
        {
            key: "Messages",
            value: String(info.messageCount),
            cls: "",
            desc: "Total messages in the conversation history sent to the LLM. Grows as tool calls and results are added.",
        },
        {
            key: "Model",
            value: info.model,
            cls: "",
            desc: "The LLM model used for this call.",
        },
        {
            key: "Prompt tokens",
            value: info.tokenUsage ? String(info.tokenUsage.prompt) : "—",
            cls: "tok",
            desc: "Tokens in the input sent to the LLM (system prompt + conversation history + tool definitions).",
        },
        {
            key: "Completion tokens",
            value: info.tokenUsage ? String(info.tokenUsage.completion) : "—",
            cls: "tok",
            desc: "Tokens the LLM generated in its response. More tokens = longer/more detailed response.",
        },
        {
            key: "Total tokens",
            value: info.tokenUsage ? String(info.tokenUsage.total) : "—",
            cls: "tok",
            desc: "Prompt + completion. This determines API cost.",
        },
        {
            key: "Duration",
            value: info.durationMs + " ms",
            cls: "dur",
            desc: "Wall-clock time for this LLM call (network + inference). Does not include tool execution time.",
        },
        {
            key: "Finish reason",
            value: info.finishReason || "—",
            cls: "",
            desc: "Why the LLM stopped generating. 'end_turn' = final answer. 'tool_use' = wants to call a tool. 'max_tokens' = hit token limit.",
        },
    ];

    details.forEach(function (d) {
        var row = document.createElement("div");
        row.className = "debug-detail-row";

        var left = document.createElement("div");

        var keyEl = document.createElement("div");
        keyEl.className = "debug-detail-key";
        keyEl.textContent = d.key;
        left.appendChild(keyEl);

        var descEl = document.createElement("div");
        descEl.className = "debug-detail-desc";
        descEl.textContent = d.desc;
        left.appendChild(descEl);

        var valueEl = document.createElement("div");
        valueEl.className = "debug-detail-value" + (d.cls ? " " + d.cls : "");
        valueEl.textContent = d.value;

        row.appendChild(left);
        row.appendChild(valueEl);
        debugModalBody.appendChild(row);
    });

    debugModalOverlay.classList.add("visible");
}

debugModalClose.addEventListener("click", function () {
    debugModalOverlay.classList.remove("visible");
});

debugModalOverlay.addEventListener("click", function (e) {
    if (e.target === debugModalOverlay) {
        debugModalOverlay.classList.remove("visible");
    }
});

// ── Agent speaking state ─────────────────────────────────────
function setAgentSpeaking(speaking) {
    agentSpeaking = speaking;
    stopBtn.classList.toggle("hidden", !speaking);
}

// ── WebSocket ────────────────────────────────────────────────
function sendMsg(type, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(Object.assign({ type: type }, payload || {})));
}

function connect() {
    statusText.textContent = "Connecting...";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onopen = function () {
        statusText.textContent = "Authenticating...";
        sendMsg("hello");
    };

    ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        handleMessage(msg);
    };

    ws.onerror = function () {
        statusText.textContent = "Connection failed";
    };

    ws.onclose = function () {
        statusText.textContent = "Disconnected";
        talkBtn.disabled = true;
        cleanupWebRTC();
    };
}

function handleMessage(msg) {
    switch (msg.type) {
        case "hello_ack":
            startWebRTC();
            break;

        case "webrtc_answer":
            handleWebRTCAnswer(msg.sdp);
            break;

        case "transcription":
            if (msg.text) addBubble(msg.text, "user");
            showThinking();
            break;

        case "agent_reply":
            clearThinking();
            addBubble(msg.text, "agent");
            setAgentSpeaking(true);
            break;

        case "tool_pending":
            showToolCard(msg.requestId, msg.tools);
            break;

        case "debug":
            addDebugEntry(msg);
            break;

        case "pong":
            break;

        case "error":
            console.error("Server error:", msg.message);
            break;
    }
}

// ── WebRTC ───────────────────────────────────────────────────
async function startWebRTC() {
    statusText.textContent = "Requesting mic...";

    try {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (_e) {
        statusText.textContent = "Mic access denied";
        return;
    }

    statusText.textContent = "Connecting audio...";

    pc = new RTCPeerConnection();
    pc.addTrack(micStream.getAudioTracks()[0], micStream);

    pc.oniceconnectionstatechange = function () {
        var state = pc.iceConnectionState;
        if (state === "connected" || state === "completed") {
            statusText.textContent = "Connected";
            talkBtn.disabled = false;
            textInput.disabled = false;
            sendBtn.disabled = false;
            if (audioEl) audioEl.play().catch(function () {});
        } else if (state === "failed") {
            statusText.textContent = "Audio failed";
            cleanupWebRTC();
        }
    };

    pc.ontrack = function (ev) {
        if (audioEl) { audioEl.srcObject = null; audioEl.remove(); }
        audioEl = document.createElement("audio");
        audioEl.autoplay = true;
        audioEl.playsInline = true;
        audioEl.srcObject = ev.streams[0] || new MediaStream([ev.track]);
        document.body.appendChild(audioEl);
    };

    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc);
    sendMsg("webrtc_offer", { sdp: pc.localDescription.sdp });
}

function waitForIceGathering(peerConn) {
    return new Promise(function (resolve) {
        if (peerConn.iceGatheringState === "complete") { resolve(); return; }
        var timer = setTimeout(function () { resolve(); }, 10000);
        peerConn.onicegatheringstatechange = function () {
            if (peerConn.iceGatheringState === "complete") {
                clearTimeout(timer);
                resolve();
            }
        };
    });
}

async function handleWebRTCAnswer(sdp) {
    if (!pc) return;
    await pc.setRemoteDescription({ type: "answer", sdp: sdp });
}

function cleanupWebRTC() {
    if (pc) { pc.close(); pc = null; }
    if (audioEl) { audioEl.srcObject = null; audioEl.remove(); audioEl = null; }
    if (micStream) {
        micStream.getTracks().forEach(function (t) { t.stop(); });
        micStream = null;
    }
    isRecording = false;
    talkBtn.textContent = "Press & Hold to Speak";
    talkBtn.classList.remove("recording");
    talkBtn.disabled = true;
    textInput.disabled = true;
    sendBtn.disabled = true;
    setAgentSpeaking(false);
}

// ── Hold to talk ─────────────────────────────────────────────
function startTalking() {
    if (!micStream || isRecording) return;
    isRecording = true;
    talkBtn.classList.add("recording");
    talkBtn.textContent = "Listening...";

    if (agentSpeaking) {
        sendMsg("stop_speaking");
        setAgentSpeaking(false);
    }
    sendMsg("mic_start");
}

function stopTalking() {
    if (!isRecording) return;
    isRecording = false;
    talkBtn.classList.remove("recording");
    talkBtn.textContent = "Press & Hold to Speak";
    sendMsg("mic_stop");
}

// Touch events (mobile)
talkBtn.addEventListener("touchstart", function (e) {
    e.preventDefault();
    startTalking();
});
talkBtn.addEventListener("touchend", function (e) {
    e.preventDefault();
    stopTalking();
});
talkBtn.addEventListener("touchcancel", function (e) {
    e.preventDefault();
    stopTalking();
});

// Safety net: finger slides off button
document.addEventListener("touchend", function () {
    if (isRecording) stopTalking();
});

// Mouse events (desktop)
talkBtn.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    e.preventDefault();
    startTalking();
});
talkBtn.addEventListener("mouseup", function () { stopTalking(); });
talkBtn.addEventListener("mouseleave", function () {
    if (isRecording) stopTalking();
});

// ── Text input ───────────────────────────────────────────────
function sendTextMessage() {
    var text = textInput.value.trim();
    if (!text) return;
    textInput.value = "";
    sendMsg("text_message", { text: text });
}

sendBtn.addEventListener("click", sendTextMessage);
textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        e.preventDefault();
        sendTextMessage();
    }
});

// Stop agent audio
stopBtn.addEventListener("click", function () {
    sendMsg("stop_speaking");
    setAgentSpeaking(false);
});

// Keepalive
setInterval(function () { sendMsg("ping"); }, 15000);

// Auto-connect
connect();
