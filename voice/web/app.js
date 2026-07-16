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
const voiceSelect = document.getElementById("voice-select");
const voicePreviewBtn = document.getElementById("voice-preview-btn");
const speedSlider = document.getElementById("speed-slider");
const speedValue = document.getElementById("speed-value");
const modelSelect = document.getElementById("model-select");
const sttSelect = document.getElementById("stt-select");
const vadSelect = document.getElementById("vad-select");

// VAD dropdown (phone line): mirrors the STT/LLM/Voice pipeline selectors.
// Applies to NEW phone calls; served by GET/POST /api/phone/vad.
fetch("/api/phone/vad").then(function (r) { return r.json(); }).then(function (v) {
    v.options.forEach(function (mode) {
        var o = document.createElement("option");
        o.value = mode;
        o.textContent = mode === "silero"
            ? "silero (neural)" + (v.silero_available ? "" : " — unavailable")
            : "energy (threshold)";
        o.disabled = mode === "silero" && !v.silero_available;
        vadSelect.appendChild(o);
    });
    vadSelect.value = v.active;
}).catch(function () {
    var o = document.createElement("option");
    o.textContent = "n/a (phone disabled)";
    vadSelect.appendChild(o);
    vadSelect.disabled = true;
});
vadSelect.addEventListener("change", function () {
    fetch("/api/phone/vad", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: vadSelect.value }),
    });
});
const settingsBtn = document.getElementById("settings-btn");
const pipelinePanel = document.getElementById("pipeline-panel");

// ── State ────────────────────────────────────────────────────
let ws = null;
let pc = null;
let audioEl = null;
let micStream = null;
let isRecording = false;
let agentSpeaking = false;
let phoneModeEnabled = false;
let autoTurnPending = false;
let audioConnected = false;
let vadAudioContext = null;
let vadAnalyser = null;
let vadSamples = null;
let vadGate = null;
let vadFrameRequest = null;
let vadCalibration = [];
let vadCalibrationUntil = 0;
let vadRearmAt = 0;
let bargeInEnabled = false;
let bargeDetector = null;

const PHONE_CALIBRATION_MS = 700;
const PHONE_REARM_MS = 650;

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
    return bubble;
}

function showThinking() {
    clearThinking();
    const el = document.createElement("div");
    el.className = "msg msg-agent thinking";
    el.textContent = "Thinking...";
    chatLog.appendChild(el);
    chatLog.scrollTop = chatLog.scrollHeight;
}

var streamingBubble = null;

function appendAgentDelta(text) {
    if (!streamingBubble) {
        streamingBubble = addBubble("", "agent");
    }
    // addBubble returns the bubble element; append text with a leading space if needed
    streamingBubble.textContent = (streamingBubble.textContent + " " + text).trim();
    chatLog.scrollTop = chatLog.scrollHeight;
}

function finalizeAgentBubble() {
    streamingBubble = null;
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

    if (info.firstTokenMs !== undefined || info.durationMs !== undefined) {
        var ttft = info.firstTokenMs !== undefined ? info.firstTokenMs + "ms" : "—";
        var total = info.durationMs !== undefined ? info.durationMs + "ms" : "—";
        fields.push(["llm", "TTFT " + ttft + " · total " + total]);
    }

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
            key: "Cache read/write",
            value: info.tokenUsage && (info.tokenUsage.cacheRead || info.tokenUsage.cacheWrite)
                ? (info.tokenUsage.cacheRead || 0) + "/" + (info.tokenUsage.cacheWrite || 0)
                : "—",
            cls: "tok",
            desc: "Prompt tokens served from / written to the provider's prompt cache. A large read count means the stable prefix (persona + site knowledge) was cached — those tokens cost ~10% and skip prefill.",
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

function setPhoneStatus(text) {
    if (phoneModeEnabled) statusText.textContent = text;
}

// ── WebSocket ────────────────────────────────────────────────
function sendMsg(type, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(Object.assign({ type: type }, payload || {})));
}

// ── Voice picker ─────────────────────────────────────────────
var LS_VOICE = "nanoclaw.voiceId";
var LS_SPEED = "nanoclaw.speed";
var currentVoiceId = localStorage.getItem(LS_VOICE) || "af_heart";
var currentSpeed = parseFloat(localStorage.getItem(LS_SPEED) || "1") || 1;
var previewAudio = new Audio();

function renderVoiceOptions(uiCatalog) {
    voiceSelect.innerHTML = "";
    VoiceUI.groupVoices(uiCatalog).forEach(function (group) {
        var og = document.createElement("optgroup");
        og.label = group.label;
        group.options.forEach(function (opt) {
            var o = document.createElement("option");
            o.value = opt.id;
            o.textContent = opt.label;
            og.appendChild(o);
        });
        voiceSelect.appendChild(og);
    });
    voiceSelect.value = currentVoiceId;
    if (!voiceSelect.value) {
        currentVoiceId = uiCatalog.default;
        voiceSelect.value = currentVoiceId;
    }
    voiceSelect.disabled = false;
    voicePreviewBtn.disabled = false;
}

function pushVoice() {
    sendMsg("set_voice", { voiceId: currentVoiceId, speed: currentSpeed });
}

function loadVoices() {
    fetch("/api/voices")
        .then(function (r) { return r.json(); })
        .then(function (uiCatalog) {
            renderVoiceOptions(uiCatalog);
            speedSlider.value = String(currentSpeed);
            speedValue.textContent = currentSpeed.toFixed(1) + "×";
            pushVoice();
        })
        .catch(function () { statusText.textContent = "Could not load voices"; });
}

voiceSelect.addEventListener("change", function () {
    currentVoiceId = voiceSelect.value;
    localStorage.setItem(LS_VOICE, currentVoiceId);
    pushVoice();
});

speedSlider.addEventListener("input", function () {
    currentSpeed = parseFloat(speedSlider.value);
    speedValue.textContent = currentSpeed.toFixed(1) + "×";
    localStorage.setItem(LS_SPEED, String(currentSpeed));
});
speedSlider.addEventListener("change", pushVoice);

voicePreviewBtn.addEventListener("click", function () {
    voicePreviewBtn.disabled = true;
    fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voiceId: currentVoiceId }),
    })
        .then(function (r) { return r.blob(); })
        .then(function (blob) {
            previewAudio.src = URL.createObjectURL(blob);
            return previewAudio.play();
        })
        .catch(function () { /* ignore preview errors */ })
        .finally(function () { voicePreviewBtn.disabled = false; });
});

// ── Pipeline settings (STT / LLM / TTS) ─────────────────────
var LS_MODEL = "nanoclaw.model", LS_STT = "nanoclaw.stt";
var currentModel = localStorage.getItem(LS_MODEL) || "anthropic/claude-haiku-4-5";
var currentStt = localStorage.getItem(LS_STT) || "base";

settingsBtn.addEventListener("click", function () { pipelinePanel.classList.toggle("hidden"); });

function loadModels() {
    fetch("/api/models").then(function (r) { return r.json(); }).then(function (data) {
        modelSelect.innerHTML = "";
        Pipeline.buildModelOptions(data.models).forEach(function (o) {
            var el = document.createElement("option");
            el.value = o.id; el.textContent = o.label; el.disabled = o.disabled;
            modelSelect.appendChild(el);
        });
        // keep stored model if still available, else fall back to default
        var chosen = data.models.find(function (m) { return m.id === currentModel && m.available; });
        currentModel = chosen ? currentModel : data.default;
        modelSelect.value = currentModel;
        sttSelect.value = currentStt;
        sendMsg("set_model", { modelId: currentModel });
        sendMsg("set_stt", { size: currentStt });
    }).catch(function () {});
}

modelSelect.addEventListener("change", function () {
    currentModel = modelSelect.value; localStorage.setItem(LS_MODEL, currentModel);
    sendMsg("set_model", { modelId: currentModel });
});
sttSelect.addEventListener("change", function () {
    currentStt = sttSelect.value; localStorage.setItem(LS_STT, currentStt);
    sendMsg("set_stt", { size: currentStt });
});

function connect() {
    statusText.textContent = "Connecting...";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onopen = function () {
        statusText.textContent = "Authenticating...";
        sendMsg("hello");
        loadVoices();
        loadModels();
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
            bargeInEnabled = !!msg.bargeIn;
            if (bargeInEnabled && typeof BargeInDetector !== "undefined") {
                bargeDetector = new BargeInDetector({});
            }
            startWebRTC();
            break;

        case "webrtc_answer":
            handleWebRTCAnswer(msg.sdp);
            break;

        case "transcription":
            if (msg.text) {
                addBubble(msg.text, "user");
                showThinking();
                setPhoneStatus("Claude is thinking...");
            } else {
                clearThinking();
                rearmPhoneMode("No speech detected; listening again...");
            }
            break;

        case "agent_reply":
            clearThinking();
            addBubble(msg.text, "agent");
            setAgentSpeaking(true);
            setPhoneStatus("Claude is speaking to the phone...");
            break;

        case "agent_reply_delta":
            clearThinking();
            appendAgentDelta(msg.text);
            setAgentSpeaking(true);
            setPhoneStatus("Claude is speaking to the phone...");
            break;

        case "agent_reply_done":
            finalizeAgentBubble();
            break;

        case "agent_audio_start":
            setAgentSpeaking(true);
            setPhoneStatus("Claude is speaking to the phone...");
            break;

        case "agent_audio_end":
            finalizeAgentBubble();
            setAgentSpeaking(false);
            if (bargeDetector) bargeDetector.reset();
            rearmPhoneMode("Waiting for the phone side...");
            break;

        case "tool_pending":
            showToolCard(msg.requestId, msg.tools);
            setAgentSpeaking(false);
            setPhoneStatus("Tool approval required before the call can continue");
            break;

        case "debug":
            addDebugEntry(msg);
            break;

        case "voice_notice":
            statusText.textContent = msg.text;
            break;

        case "pong":
            break;

        case "error":
            console.error("Server error:", msg.message);
            finalizeAgentBubble();
            rearmPhoneMode("Voice error; listening again...");
            break;
    }
}

// ── WebRTC ───────────────────────────────────────────────────
async function startWebRTC() {
    statusText.textContent = "Requesting mic...";

    try {
        micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
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
            audioConnected = true;
            statusText.textContent = "Connected";
            talkBtn.disabled = false;
            textInput.disabled = false;
            sendBtn.disabled = false;
            if (audioEl) audioEl.play().catch(function () {});
        } else if (state === "failed") {
            audioConnected = false;
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
    stopPhoneMode({sendCancel: false, status: false});
    if (pc) { pc.close(); pc = null; }
    if (audioEl) { audioEl.srcObject = null; audioEl.remove(); audioEl = null; }
    if (micStream) {
        micStream.getTracks().forEach(function (t) { t.stop(); });
        micStream = null;
    }
    audioConnected = false;
    isRecording = false;
    talkBtn.textContent = "Start Hands-Free Phone Mode";
    talkBtn.classList.remove("recording", "phone-active");
    talkBtn.setAttribute("aria-pressed", "false");
    talkBtn.disabled = true;
    textInput.disabled = true;
    sendBtn.disabled = true;
    setAgentSpeaking(false);
}

// ── Hands-free phone mode ────────────────────────────────────
async function ensureVadAnalyser() {
    if (!micStream || typeof PhoneVadGate === "undefined") return false;
    if (!vadAudioContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return false;
        vadAudioContext = new AudioContextClass();
        vadAnalyser = vadAudioContext.createAnalyser();
        vadAnalyser.fftSize = 1024;
        vadAnalyser.smoothingTimeConstant = 0.15;
        vadSamples = new Float32Array(vadAnalyser.fftSize);
        const source = vadAudioContext.createMediaStreamSource(micStream);
        source.connect(vadAnalyser);
    }
    if (vadAudioContext.state === "suspended") await vadAudioContext.resume();
    return true;
}

function currentMicRms() {
    if (!vadAnalyser || !vadSamples) return 0;
    vadAnalyser.getFloatTimeDomainData(vadSamples);
    let sumSquares = 0;
    for (let i = 0; i < vadSamples.length; i += 1) {
        sumSquares += vadSamples[i] * vadSamples[i];
    }
    return Math.sqrt(sumSquares / vadSamples.length);
}

function beginAutomaticTurn() {
    if (!phoneModeEnabled || isRecording || autoTurnPending || agentSpeaking) return;
    isRecording = true;
    talkBtn.classList.add("recording");
    setPhoneStatus("Hearing the phone side...");
    sendMsg("mic_start");
}

function finishAutomaticTurn(reason) {
    if (!isRecording) return;
    isRecording = false;
    talkBtn.classList.remove("recording");
    autoTurnPending = true;
    setPhoneStatus(reason === "maximum_duration" ? "Maximum turn reached; transcribing..." : "Transcribing phone audio...");
    sendMsg("mic_stop");
}

function monitorPhoneAudio(timestamp) {
    if (!phoneModeEnabled) return;

    const rms = currentMicRms();
    if (timestamp < vadCalibrationUntil) {
        vadCalibration.push(rms);
    } else if (vadCalibration.length) {
        const thresholds = vadGate.configureFromNoise(vadCalibration);
        vadCalibration = [];
        console.info("Phone VAD calibrated", thresholds);
        setPhoneStatus("Waiting for the phone side...");
    } else if (agentSpeaking) {
        if (bargeInEnabled && bargeDetector) {
            const evt = bargeDetector.sample(rms, timestamp);
            if (evt && evt.type === "barge_in") {
                sendMsg("barge_in");
                setPhoneStatus("Heard you — pausing...");
            } else if (evt && evt.type === "barge_in_commit") {
                sendMsg("barge_in_commit");
                // The server re-arms the mic (agent_audio_end); the user's
                // speech is captured by the normal VAD turn on the next frames.
            } else if (evt && evt.type === "barge_in_false") {
                sendMsg("barge_in_false");
                setPhoneStatus("False alarm — resuming...");
            }
        }
        vadGate.reset();  // don't let the normal turn-VAD fire while agent speaks
    } else if (autoTurnPending || timestamp < vadRearmAt) {
        vadGate.reset();
    } else {
        const event = vadGate.sample(rms, timestamp);
        if (event?.type === "speech_start") beginAutomaticTurn();
        if (event?.type === "speech_stop") finishAutomaticTurn(event.reason);
    }

    vadFrameRequest = window.requestAnimationFrame(monitorPhoneAudio);
}

function rearmPhoneMode(message) {
    autoTurnPending = false;
    if (bargeDetector) bargeDetector.reset();
    if (!phoneModeEnabled || !vadGate) return;
    vadGate.reset();
    vadRearmAt = performance.now() + PHONE_REARM_MS;
    setPhoneStatus(message || "Waiting for the phone side...");
}

async function startPhoneMode() {
    if (!audioConnected || !micStream || phoneModeEnabled) return;
    if (!await ensureVadAnalyser()) {
        statusText.textContent = "Automatic voice detection is unavailable";
        return;
    }

    phoneModeEnabled = true;
    autoTurnPending = false;
    vadGate = new PhoneVadGate();
    vadCalibration = [];
    vadCalibrationUntil = performance.now() + PHONE_CALIBRATION_MS;
    vadRearmAt = vadCalibrationUntil;
    talkBtn.classList.add("phone-active");
    talkBtn.setAttribute("aria-pressed", "true");
    talkBtn.textContent = "Stop Hands-Free Phone Mode";
    statusText.textContent = "Calibrating room noise...";
    if (vadFrameRequest !== null) window.cancelAnimationFrame(vadFrameRequest);
    vadFrameRequest = window.requestAnimationFrame(monitorPhoneAudio);
}

function stopPhoneMode(options) {
    const config = options || {};
    if (!phoneModeEnabled && vadFrameRequest === null) return;
    phoneModeEnabled = false;
    if (vadFrameRequest !== null) {
        window.cancelAnimationFrame(vadFrameRequest);
        vadFrameRequest = null;
    }
    if (isRecording && config.sendCancel !== false) sendMsg("mic_cancel");
    isRecording = false;
    autoTurnPending = false;
    if (vadGate) vadGate.reset();
    talkBtn.classList.remove("recording", "phone-active");
    talkBtn.setAttribute("aria-pressed", "false");
    talkBtn.textContent = "Start Hands-Free Phone Mode";
    if (config.status !== false && audioConnected) statusText.textContent = "Phone mode stopped";
}

talkBtn.addEventListener("click", function () {
    if (phoneModeEnabled) stopPhoneMode();
    else startPhoneMode();
});

// ── Text input ───────────────────────────────────────────────
function sendTextMessage() {
    var text = textInput.value.trim();
    if (!text) return;
    textInput.value = "";
    if (phoneModeEnabled) {
        autoTurnPending = true;
        setPhoneStatus("Claude is thinking...");
    }
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
    setPhoneStatus("Stopping Claude audio...");
});

// Keepalive
setInterval(function () { sendMsg("ping"); }, 15000);

// Auto-connect
connect();
