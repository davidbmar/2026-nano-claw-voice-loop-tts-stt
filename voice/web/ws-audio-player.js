// This module intentionally has no top-level browser side effects so it can be
// syntax-checked outside a page. It schedules every PCM frame immediately after
// the previous one, using a small initial lead to absorb WebSocket jitter.

export class Pcm16AudioPlayer {
    constructor(options) {
        const config = options || {};
        const AudioContextClass = config.AudioContextClass || window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) throw new Error("Web Audio is unavailable");

        this.sampleRate = Number(config.sampleRate) || 16000;
        this.context = new AudioContextClass({ latencyHint: "interactive" });
        this.analyser = this.context.createAnalyser();
        this.analyser.fftSize = 512;
        this.analyser.smoothingTimeConstant = 0.72;
        this.analyser.connect(this.context.destination);
        this.sources = new Set();
        this.nextStartTime = 0;
        this.initialLeadSeconds = 0.04;
        this.acceptingFrames = false;
        this.closed = false;
    }

    async resume() {
        if (!this.closed && this.context.state === "suspended") {
            await this.context.resume();
        }
    }

    begin() {
        this.stop();
        this.acceptingFrames = true;
        this.resume().catch(function () {});
    }

    enqueue(arrayBuffer) {
        if (this.closed || !this.acceptingFrames
                || !(arrayBuffer instanceof ArrayBuffer) || !arrayBuffer.byteLength) return;
        if (arrayBuffer.byteLength % 2) throw new Error("Agent PCM16 frame has an odd byte length");

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

    stop() {
        this.sources.forEach(function (source) {
            try { source.stop(); } catch (_error) { /* already stopped */ }
            try { source.disconnect(); } catch (_error) { /* already disconnected */ }
        });
        this.sources.clear();
        this.nextStartTime = 0;
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
        this.closed = true;
        this.acceptingFrames = false;
        this.stop();
        try { this.analyser.disconnect(); } catch (_error) { /* already disconnected */ }
        if (this.context.state !== "closed") await this.context.close();
    }
}
