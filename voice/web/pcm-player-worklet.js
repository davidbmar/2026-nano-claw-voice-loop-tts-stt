// Continuous PCM playback for the WebSocket transport. Incoming samples stay
// in one source-rate ring buffer, so frame boundaries never become Web Audio
// scheduling boundaries. If the AudioContext could not use the agent rate,
// interpolation also keeps one phase across every incoming frame.

class Float32RingBuffer {
    constructor(capacity) {
        this.buffer = new Float32Array(Math.max(1, capacity));
        this.readIndex = 0;
        this.writeIndex = 0;
        this.length = 0;
    }

    _ensureCapacity(required) {
        if (required <= this.buffer.length) return;

        let capacity = this.buffer.length;
        while (capacity < required) capacity *= 2;
        const grown = new Float32Array(capacity);
        const firstLength = Math.min(this.length, this.buffer.length - this.readIndex);
        grown.set(this.buffer.subarray(this.readIndex, this.readIndex + firstLength), 0);
        if (firstLength < this.length) {
            grown.set(this.buffer.subarray(0, this.length - firstLength), firstLength);
        }
        this.buffer = grown;
        this.readIndex = 0;
        this.writeIndex = this.length;
    }

    push(samples) {
        if (!samples.length) return;
        this._ensureCapacity(this.length + samples.length);

        const firstLength = Math.min(samples.length, this.buffer.length - this.writeIndex);
        this.buffer.set(samples.subarray(0, firstLength), this.writeIndex);
        if (firstLength < samples.length) {
            this.buffer.set(samples.subarray(firstLength), 0);
        }
        this.writeIndex = (this.writeIndex + samples.length) % this.buffer.length;
        this.length += samples.length;
    }

    peek(offset) {
        return this.buffer[(this.readIndex + offset) % this.buffer.length];
    }

    discard(count) {
        const discarded = Math.min(Math.max(0, count), this.length);
        this.readIndex = (this.readIndex + discarded) % this.buffer.length;
        this.length -= discarded;
        if (!this.length) {
            this.readIndex = 0;
            this.writeIndex = 0;
        }
    }

    clear() {
        this.readIndex = 0;
        this.writeIndex = 0;
        this.length = 0;
    }
}

// ~1ms at 48kHz: long enough to declick an underrun edge, short enough to be
// inaudible as a dip. The render quantum is 128 samples, so this never spans a
// whole block.
const FADE_SAMPLES = 48;

class NanoClawPcmPlayerProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const config = (options && options.processorOptions) || {};
        const configuredSourceRate = Number(config.sourceSampleRate);
        this.sourceSampleRate = Number.isFinite(configuredSourceRate)
            && configuredSourceRate > 0 ? configuredSourceRate : sampleRate;
        this.sourcePerOutput = this.sourceSampleRate / sampleRate;

        const configuredPrebuffer = Number(config.prebufferSamples);
        this.prebufferSamples = Number.isFinite(configuredPrebuffer)
            && configuredPrebuffer >= 0
            ? Math.floor(configuredPrebuffer)
            : Math.round(this.sourceSampleRate * 0.15);
        this.ring = new Float32RingBuffer(Math.max(2048, this.prebufferSamples + 128));
        this.started = false;
        this._underran = false;
        this._underrunCount = 0;
        this.sourcePosition = 0;

        this.port.onmessage = (event) => {
            const message = event && event.data;
            if (!message || typeof message !== "object") return;
            if (message.type === "flush") {
                this.ring.clear();
                this.started = false;
                this.sourcePosition = 0;
                return;
            }
            if (message.type !== "samples") return;

            const payload = message.samples;
            if (!payload || typeof payload.byteLength !== "number"
                    || !payload.byteLength || payload.byteLength % 4) return;
            try {
                this.ring.push(new Float32Array(payload));
            } catch (_error) {
                // Ignore malformed or already-detached buffers. The render
                // callback remains alive and continues producing silence.
            }
        };
    }

    _renderAtContextRate(output) {
        const rendered = Math.min(output.length, this.ring.length);
        for (let index = 0; index < rendered; index += 1) {
            output[index] = this.ring.peek(index);
        }
        this.ring.discard(rendered);
        return rendered;
    }

    _renderResampled(output) {
        let outputIndex = 0;
        while (outputIndex < output.length) {
            const before = Math.floor(this.sourcePosition);
            if (before >= this.ring.length) break;
            const fraction = this.sourcePosition - before;
            if (fraction > 0 && before + 1 >= this.ring.length) break;

            const first = this.ring.peek(before);
            output[outputIndex] = fraction > 0
                ? first + ((this.ring.peek(before + 1) - first) * fraction)
                : first;
            outputIndex += 1;
            this.sourcePosition += this.sourcePerOutput;
        }

        const consumed = Math.min(Math.floor(this.sourcePosition), this.ring.length);
        this.ring.discard(consumed);
        this.sourcePosition -= consumed;
        return outputIndex;
    }

    process(_inputs, outputs) {
        const channels = outputs[0] || [];
        for (let channelIndex = 0; channelIndex < channels.length; channelIndex += 1) {
            channels[channelIndex].fill(0);
        }
        const output = channels[0];
        if (!output) return true;

        const threshold = Math.max(1, this.prebufferSamples);
        if (!this.started) {
            if (this.ring.length < threshold) return true;
            this.started = true;
        }

        const rendered = Math.abs(this.sourcePerOutput - 1) < 1e-12
            ? this._renderAtContextRate(output)
            : this._renderResampled(output);

        // Declick buffer underruns. When the ring drains mid-stream (the next
        // synthesized chunk has not arrived), the block's unfilled remainder is
        // hard zero — a step from speech to silence, and the next block resumes
        // with an equal step back up. Both are heard as a tick right at a chunk
        // boundary. Ramp across both edges so the gap is a soft dip, not a pop.
        const fade = Math.min(FADE_SAMPLES, output.length);
        if (this._underran && rendered > 0) {
            const head = Math.min(fade, rendered);
            for (let i = 0; i < head; i += 1) output[i] *= i / head;
            this._underran = false;
        }
        if (rendered < output.length) {
            const tail = Math.min(fade, rendered);
            for (let i = 0; i < tail; i += 1) {
                output[rendered - tail + i] *= (tail - 1 - i) / tail;
            }
            if (!this._underran) {
                // Count only the transition INTO an underrun (one event per gap,
                // not per starved block). Reported to the main thread so the
                // page can show that underruns are the tick source and that the
                // declick is smoothing them.
                this._underrunCount += 1;
                if (this.port && typeof this.port.postMessage === "function") {
                    this.port.postMessage({ type: "underrun", count: this._underrunCount });
                }
            }
            this._underran = true;
        }

        for (let channelIndex = 1; channelIndex < channels.length; channelIndex += 1) {
            channels[channelIndex].set(output);
        }
        return true;
    }
}

registerProcessor("nano-claw-pcm-player", NanoClawPcmPlayerProcessor);
