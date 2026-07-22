// AudioWorklet mic capture for the WebSocket transport. Browser input usually
// runs at 44.1 or 48 kHz; this processor emits fixed 20 ms PCM16/16 kHz frames.

class NanoClawPcm16MicProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const config = (options && options.processorOptions) || {};
        this.targetRate = Number(config.targetRate) || 16000;
        this.frameSamples = Number(config.frameSamples) || 320;
        this.sourcePerOutput = sampleRate / this.targetRate;
        this.sourceSamples = [];
        this.sourcePosition = 0;
        this.frame = new Int16Array(this.frameSamples);
        this.frameOffset = 0;
    }

    _pushSample(value) {
        const clipped = Math.max(-1, Math.min(1, value));
        this.frame[this.frameOffset] = clipped < 0
            ? Math.round(clipped * 32768)
            : Math.round(clipped * 32767);
        this.frameOffset += 1;
        if (this.frameOffset !== this.frameSamples) return;

        const completed = this.frame;
        this.frame = new Int16Array(this.frameSamples);
        this.frameOffset = 0;
        this.port.postMessage(completed.buffer, [completed.buffer]);
    }

    process(inputs, outputs) {
        const output = outputs[0] && outputs[0][0];
        if (output) output.fill(0);

        const input = inputs[0] && inputs[0][0];
        if (!input || !input.length) return true;
        for (let index = 0; index < input.length; index += 1) {
            this.sourceSamples.push(input[index]);
        }

        while (this.sourcePosition + 1 < this.sourceSamples.length) {
            const before = Math.floor(this.sourcePosition);
            const fraction = this.sourcePosition - before;
            const value = this.sourceSamples[before]
                + ((this.sourceSamples[before + 1] - this.sourceSamples[before]) * fraction);
            this._pushSample(value);
            this.sourcePosition += this.sourcePerOutput;
        }

        // Preserve the final source sample so interpolation can bridge into
        // the next 128-sample render quantum.
        const consumed = Math.min(
            Math.floor(this.sourcePosition),
            this.sourceSamples.length - 1,
        );
        if (consumed > 0) {
            this.sourceSamples = this.sourceSamples.slice(consumed);
            this.sourcePosition -= consumed;
        }
        return true;
    }
}

registerProcessor("nano-claw-pcm16-mic", NanoClawPcm16MicProcessor);
