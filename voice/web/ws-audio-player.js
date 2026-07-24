// This module intentionally has no top-level browser side effects so it can be
// syntax-checked outside a page. Agent frames are converted to Float32 and fed
// into one continuous AudioWorklet ring buffer instead of separate source nodes.

export const DEFAULT_INITIAL_LEAD_SECONDS = 0.15;
const MAX_INITIAL_LEAD_SECONDS = 0.18;
const PLAYER_PROCESSOR_NAME = "nano-claw-pcm-player";
// AudioWorklet modules cache aggressively and the browser keeps an old copy
// forever unless the URL changes — a repeated source of "the fix didn't load".
// Append a per-page-load nonce so every fresh page always fetches the current
// worklet, without relying on remembering to bump a version. The static file is
// tiny; re-fetching it per load is free. WORKLET_VERSION is kept only as a
// human-readable marker in the URL and logs.
const WORKLET_VERSION = "0.4.8";
const DEFAULT_WORKLET_MODULE_URL = new URL(
    "./pcm-player-worklet.js?v=" + WORKLET_VERSION + "&t=" + Date.now(),
    import.meta.url,
).href;

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

        // Diagnostics: prove which worklet build is live and surface playback
        // underruns (the source of the between-sentence tick) as they happen.
        if (typeof console !== "undefined" && console.info) {
            console.info("[nano-claw] audio player worklet v" + WORKLET_VERSION + " loaded");
        }
        node.port.onmessage = function (event) {
            const data = event && event.data;
            if (data && data.type === "underrun" && typeof console !== "undefined" && console.warn) {
                console.warn(
                    "[nano-claw] playback underrun #" + data.count +
                    " (buffer ran dry between chunks; declicked)",
                );
            }
        };

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
        // Track how much speech audio this utterance enqueues, so its duration
        // is known for timing (logged on end()). PCM is at this.sampleRate.
        this._enqueuedSamples = 0;
        this.resume().catch(function () {});
    }

    /** Duration of speech audio enqueued for the current utterance, in ms. */
    playedDurationMs() {
        return Math.round((this._enqueuedSamples || 0) / this.sampleRate * 1000);
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
        this._enqueuedSamples = (this._enqueuedSamples || 0) + samples.length;
        // Diagnostic: a tick is a large discontinuity. Report the biggest step
        // between consecutive samples of this frame AND the seam step from the
        // previous frame's last sample to this frame's first, so a
        // between-sentence tick shows up in the console with a magnitude.
        if (samples.length && typeof console !== "undefined" && console.warn) {
            let maxStep = 0;
            for (let i = 1; i < samples.length; i += 1) {
                const s = Math.abs(samples[i] - samples[i - 1]);
                if (s > maxStep) maxStep = s;
            }
            const seamStep = this._lastSample === undefined
                ? 0 : Math.abs(samples[0] - this._lastSample);
            this._lastSample = samples[samples.length - 1];
            if (maxStep > 0.12 || seamStep > 0.12) {
                console.warn(
                    "[nano-claw] frame discontinuity: internal step=" + maxStep.toFixed(3) +
                    " seam step=" + seamStep.toFixed(3) + " (>0.12 is an audible tick)",
                );
            }
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

    end() {
        // Stop accepting network frames without flushing audio that is
        // already scheduled. Cancellation uses pause(); normal completion
        // uses end() so the final buffered phonemes can finish naturally.
        this.acceptingFrames = false;
        if (typeof console !== "undefined" && console.info) {
            console.info("[nano-claw] utterance audio length: " + this.playedDurationMs() + " ms");
        }
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
