// This module intentionally has no top-level browser side effects so it can be
// syntax-checked outside a page. Agent frames are converted to Float32 and fed
// into one continuous AudioWorklet ring buffer instead of separate source nodes.

export const DEFAULT_INITIAL_LEAD_SECONDS = 0.15;
const MAX_INITIAL_LEAD_SECONDS = 0.18;
const PLAYER_PROCESSOR_NAME = "nano-claw-pcm-player";
const DEFAULT_WORKLET_MODULE_URL = new URL("./pcm-player-worklet.js", import.meta.url).href;

function browserAudioContextClass() {
    if (typeof window === "undefined") return null;
    return window.AudioContext || window.webkitAudioContext || null;
}

function browserAudioWorkletNodeClass() {
    if (typeof globalThis.AudioWorkletNode === "undefined") return null;
    return globalThis.AudioWorkletNode;
}

export class Pcm16AudioPlayer {
    constructor(options) {
        const config = options || {};
        const AudioContextClass = config.AudioContextClass || browserAudioContextClass();
        if (!AudioContextClass) throw new Error("Web Audio is unavailable");

        this.sampleRate = Number(config.sampleRate);
        if (!Number.isFinite(this.sampleRate) || this.sampleRate <= 0) {
            throw new Error("Agent PCM sample rate was not announced");
        }

        this.usedDefaultContextRate = false;
        try {
            this.context = new AudioContextClass({
                sampleRate: this.sampleRate,
                latencyHint: "interactive",
            });
        } catch (_rateError) {
            // Some browsers reject a non-native sample rate. The worklet uses
            // one continuous interpolation phase when this fallback is needed.
            this.context = new AudioContextClass({ latencyHint: "interactive" });
            this.usedDefaultContextRate = true;
        }

        this.analyser = this.context.createAnalyser();
        this.analyser.fftSize = 512;
        this.analyser.smoothingTimeConstant = 0.72;
        this.analyser.connect(this.context.destination);

        const requestedLead = Number(config.initialLeadSeconds);
        // 150 ms covers typical tunnel/cellular bursts without unbounded latency.
        this.initialLeadSeconds = Number.isFinite(requestedLead)
            ? Math.min(Math.max(requestedLead, 0), MAX_INITIAL_LEAD_SECONDS)
            : DEFAULT_INITIAL_LEAD_SECONDS;
        this.prebufferSamples = Math.round(this.sampleRate * this.initialLeadSeconds);

        this.acceptingFrames = false;
        this.closed = false;
        this.worklet = null;
        this._pendingMessages = [];
        this._AudioWorkletNodeClass = config.AudioWorkletNodeClass
            || browserAudioWorkletNodeClass();
        this._workletModuleUrl = config.workletModuleUrl || DEFAULT_WORKLET_MODULE_URL;

        // These fields support dependency-injected AudioContext test doubles
        // that predate AudioWorklet. Production app.js rejects that capability
        // combination before constructing the player.
        this._legacyScheduler = false;
        this.sources = new Set();
        this.nextStartTime = 0;

        this.ready = this._initialize();
        // Keep direct construction from causing an unhandled rejection while
        // still letting callers await the original rejecting readiness promise.
        this.ready.catch(function () {});
    }

    async _initialize() {
        const audioWorklet = this.context.audioWorklet;
        if (!audioWorklet || typeof audioWorklet.addModule !== "function"
                || !this._AudioWorkletNodeClass) {
            if (typeof window === "undefined"
                    && typeof this.context.createBuffer === "function"
                    && typeof this.context.createBufferSource === "function") {
                this._legacyScheduler = true;
                return;
            }
            throw new Error("AudioWorklet is unavailable");
        }

        await audioWorklet.addModule(this._workletModuleUrl);
        if (this.closed) return;

        const node = new this._AudioWorkletNodeClass(
            this.context,
            PLAYER_PROCESSOR_NAME,
            {
                numberOfInputs: 0,
                numberOfOutputs: 1,
                outputChannelCount: [1],
                processorOptions: {
                    sourceSampleRate: this.sampleRate,
                    prebufferSamples: this.prebufferSamples,
                },
            },
        );
        node.connect(this.analyser);
        this.worklet = node;

        const pending = this._pendingMessages;
        this._pendingMessages = [];
        pending.forEach(function (entry) {
            node.port.postMessage(entry.message, entry.transfer);
        });
    }

    _postToWorklet(message, transfer) {
        if (this.worklet) {
            this.worklet.port.postMessage(message, transfer);
            return;
        }
        this._pendingMessages.push({ message: message, transfer: transfer });
    }

    _enqueueLegacy(arrayBuffer) {
        const view = new DataView(arrayBuffer);
        const sampleCount = arrayBuffer.byteLength / 2;
        const buffer = this.context.createBuffer(1, sampleCount, this.sampleRate);
        const channel = buffer.getChannelData(0);
        for (let index = 0; index < sampleCount; index += 1) {
            channel[index] = view.getInt16(index * 2, true) / 32768;
        }

        const now = this.context.currentTime;
        if (this.nextStartTime < now) {
            this.nextStartTime = now + this.initialLeadSeconds;
        }
        const source = this.context.createBufferSource();
        source.buffer = buffer;
        source.connect(this.analyser);
        source.addEventListener("ended", () => {
            this.sources.delete(source);
            try { source.disconnect(); } catch (_error) { /* already disconnected */ }
        }, { once: true });
        this.sources.add(source);
        source.start(this.nextStartTime);
        this.nextStartTime += buffer.duration;
    }

    async resume() {
        if (!this.closed && this.context.state === "suspended") {
            await this.context.resume();
        }
    }

    begin() {
        if (this.closed) return;
        this.stop();
        this.acceptingFrames = true;
        this.resume().catch(function () {});
    }

    enqueue(arrayBuffer) {
        if (this.closed || !this.acceptingFrames
                || !(arrayBuffer instanceof ArrayBuffer) || !arrayBuffer.byteLength) return;
        if (arrayBuffer.byteLength % 2) throw new Error("Agent PCM16 frame has an odd byte length");

        if (this._legacyScheduler) {
            this._enqueueLegacy(arrayBuffer);
            return;
        }

        const view = new DataView(arrayBuffer);
        const samples = new Float32Array(arrayBuffer.byteLength / 2);
        for (let index = 0; index < samples.length; index += 1) {
            samples[index] = view.getInt16(index * 2, true) / 32768;
        }
        this._postToWorklet(
            { type: "samples", samples: samples.buffer },
            [samples.buffer],
        );
    }

    stop() {
        if (this._legacyScheduler) {
            this.sources.forEach(function (source) {
                try { source.stop(); } catch (_error) { /* already stopped */ }
                try { source.disconnect(); } catch (_error) { /* already disconnected */ }
            });
            this.sources.clear();
            this.nextStartTime = 0;
            return;
        }
        this._postToWorklet({ type: "flush" }, []);
    }

    pause() {
        this.acceptingFrames = false;
        this.stop();
    }

    unpause() {
        if (this.closed) return;
        this.acceptingFrames = true;
        this.resume().catch(function () {});
    }

    async close() {
        if (this.closed) return;
        this.acceptingFrames = false;
        this.stop();
        this.closed = true;
        try { await this.ready; } catch (_error) { /* initialization already failed */ }
        this._pendingMessages = [];
        if (this.worklet) {
            try { this.worklet.disconnect(); } catch (_error) { /* already disconnected */ }
            this.worklet = null;
        }
        try { this.analyser.disconnect(); } catch (_error) { /* already disconnected */ }
        if (this.context.state !== "closed") await this.context.close();
    }
}
