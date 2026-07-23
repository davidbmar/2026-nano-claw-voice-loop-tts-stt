import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

import {
    DEFAULT_INITIAL_LEAD_SECONDS,
    Pcm16AudioPlayer,
} from "../voice/web/ws-audio-player.js";


const workletSource = await readFile(
    new URL("../voice/web/pcm-player-worklet.js", import.meta.url),
    "utf8",
);


function loadPlayerProcessor(outputSampleRate) {
    let processorName = null;
    let ProcessorClass = null;
    class FakeAudioWorkletProcessor {
        constructor() {
            this.port = { onmessage: null };
        }
    }
    const context = vm.createContext({
        ArrayBuffer,
        AudioWorkletProcessor: FakeAudioWorkletProcessor,
        Float32Array,
        Math,
        Number,
        sampleRate: outputSampleRate,
        registerProcessor(name, registeredClass) {
            processorName = name;
            ProcessorClass = registeredClass;
        },
    });
    vm.runInContext(workletSource, context, { filename: "pcm-player-worklet.js" });
    assert.equal(processorName, "nano-claw-pcm-player");
    assert.ok(ProcessorClass, "the player worklet registers a processor class");
    return ProcessorClass;
}


const PlayerProcessor48000 = loadPlayerProcessor(48000);


class FakeAnalyser {
    connect(destination) { this.destination = destination; }
    disconnect() { this.disconnected = true; }
}


class FakeSource {
    constructor() {
        this.starts = [];
        this.stopped = false;
    }
    connect() {}
    disconnect() {}
    addEventListener() {}
    start(at) { this.starts.push(at); }
    stop() { this.stopped = true; }
}


class FakeAudioContext {
    constructor(options) {
        this.options = options || {};
        this.currentTime = 1;
        this.sampleRate = this.options.sampleRate || 44100;
        this.destination = {};
        this.state = "running";
        this.createdBuffers = [];
        this.createdSources = [];
        this.createdWorkletNodes = [];
        this.addedModules = [];
        this.audioWorklet = {
            addModule: async (url) => { this.addedModules.push(url); },
        };
    }
    createAnalyser() { return new FakeAnalyser(); }
    createBuffer(_channels, samples, rate) {
        const data = new Float32Array(samples);
        const buffer = {
            duration: samples / rate,
            getChannelData() { return data; },
        };
        this.createdBuffers.push({ samples, rate, buffer });
        return buffer;
    }
    createBufferSource() {
        const source = new FakeSource();
        this.createdSources.push(source);
        return source;
    }
    async resume() { this.state = "running"; }
    async close() { this.state = "closed"; }
}


class FakeAudioWorkletNode {
    constructor(context, name, options) {
        this.name = name;
        this.options = options;
        this.messages = [];
        this.processor = new PlayerProcessor48000(options);
        this.port = {
            postMessage: (message, transfer) => {
                const transferList = transfer || [];
                const transferredSamples = message.type === "samples"
                    && transferList.length === 1
                    && transferList[0] === message.samples;
                const delivered = transferList.length
                    ? structuredClone(message, { transfer: transferList })
                    : structuredClone(message);
                this.messages.push({
                    type: delivered.type,
                    sampleCount: delivered.samples ? delivered.samples.byteLength / 4 : 0,
                    transferredSamples,
                });
                this.processor.port.onmessage({ data: delivered });
            },
        };
        context.createdWorkletNodes.push(this);
    }
    connect(target) { this.connectedTo = target; }
    disconnect() { this.disconnected = true; }
    render(sampleCount, channelCount = 1) {
        const channels = Array.from(
            { length: channelCount },
            () => new Float32Array(sampleCount),
        );
        assert.equal(this.processor.process([], [channels]), true);
        return channels;
    }
}


assert.throws(
    () => new Pcm16AudioPlayer({ AudioContextClass: FakeAudioContext }),
    /sample rate was not announced/,
    "playback cannot fall back to a shared mic rate",
);

const player = new Pcm16AudioPlayer({
    AudioContextClass: FakeAudioContext,
    AudioWorkletNodeClass: FakeAudioWorkletNode,
    sampleRate: 48000,
});
await player.ready;

assert.deepEqual(
    player.context.options,
    { sampleRate: 48000, latencyHint: "interactive" },
    "the playback context requests the announced agent rate",
);
assert.equal(player.context.createdWorkletNodes.length, 1, "one continuous player node is created");
assert.equal(player.context.createdSources.length, 0, "the worklet path creates no buffer sources");
assert.equal(player.context.createdBuffers.length, 0, "the worklet path creates no per-frame buffers");
assert.match(player.context.addedModules[0], /pcm-player-worklet\.js\?v=/);
assert.equal(player.analyser.destination, player.context.destination);

const node = player.context.createdWorkletNodes[0];
assert.equal(node.name, "nano-claw-pcm-player");
assert.equal(node.connectedTo, player.analyser, "the one player node feeds the existing analyser");
assert.deepEqual(
    node.options.processorOptions,
    { sourceSampleRate: 48000, prebufferSamples: 7200 },
    "the worklet receives a 150 ms source-rate fill threshold",
);

const frameSamples = new Int16Array(960);
frameSamples.fill(16384);
const frame = frameSamples.buffer;

player.enqueue(frame);
assert.equal(node.messages.length, 0, "frames wait for agent_audio_start");

player.begin();
assert.deepEqual(node.messages.map(({ type }) => type), ["flush"]);
for (let index = 0; index < 7; index += 1) player.enqueue(frame);
assert.equal(player.context.createdSources.length, 0, "frames are posted, never individually scheduled");
assert.ok(
    node.messages.filter(({ type }) => type === "samples").every(
        ({ sampleCount, transferredSamples }) => sampleCount === 960 && transferredSamples,
    ),
    "each converted Float32 frame transfers its ArrayBuffer to the worklet",
);
assert.ok(
    node.render(128)[0].every((sample) => sample === 0),
    "the processor emits silence below the prebuffer threshold",
);

player.enqueue(frame);
assert.ok(
    node.render(128)[0].every((sample) => sample === 0.5),
    "crossing the prebuffer threshold starts PCM playback",
);

const drained = node.render(8000)[0];
// The steady body plays at full amplitude; only the last ~1ms before the
// underrun ramps down so the drop into the zero-filled remainder is not a
// step (a click). The remainder stays clean zero — no repeated garbage.
assert.ok(
    drained.subarray(0, 7552 - 48).every((sample) => sample === 0.5),
    "the body before an underrun plays unaltered",
);
assert.ok(
    drained[7552 - 1] < 0.5 && drained[7552 - 1] >= 0,
    "the underrun edge ramps down instead of stepping to silence",
);
assert.ok(
    drained.subarray(7552).every((sample) => sample === 0),
    "an underrun zero-fills the rest of the output instead of repeating samples",
);
assert.ok(
    node.render(128)[0].every((sample) => sample === 0),
    "an empty ring remains clean silence",
);

player.enqueue(frame);
// The first block after an underrun ramps its head up from zero (declicking
// the resume tick heard right before the next word), then plays at full level.
const resumed = node.render(128)[0];
assert.ok(resumed[0] < 0.5, "the resume edge ramps up instead of stepping from silence");
assert.ok(
    resumed.subarray(48).every((sample) => sample === 0.5),
    "an already-started stream resumes at full level after the short fade-in",
);
player.enqueue(frame);
player.stop();
assert.equal(node.messages.at(-1).type, "flush");
assert.ok(
    node.render(128)[0].every((sample) => sample === 0),
    "stop flushes queued audio and produces immediate silence",
);

player.enqueue(frame);
assert.ok(
    node.render(128)[0].every((sample) => sample === 0),
    "a flush re-arms the initial fill threshold",
);
player.pause();
const messagesAtPause = node.messages.length;
player.enqueue(frame);
assert.equal(node.messages.length, messagesAtPause, "paused playback drops in-flight frames");

player.unpause();
player.enqueue(frame);
assert.equal(node.messages.at(-1).type, "samples");
const messagesBeforeEnd = node.messages.length;
player.end();
player.enqueue(frame);
assert.equal(
    node.messages.length,
    messagesBeforeEnd,
    "normal completion rejects late network frames without flushing buffered audio",
);

const boundedLeadPlayer = new Pcm16AudioPlayer({
    AudioContextClass: FakeAudioContext,
    AudioWorkletNodeClass: FakeAudioWorkletNode,
    sampleRate: 48000,
    initialLeadSeconds: 10,
});
await boundedLeadPlayer.ready;
assert.equal(boundedLeadPlayer.initialLeadSeconds, 0.18, "configured jitter lead is bounded");
assert.equal(boundedLeadPlayer.prebufferSamples, 8640);
await boundedLeadPlayer.close();

class RejectingRateAudioContext extends FakeAudioContext {
    static attempts = [];

    constructor(options) {
        RejectingRateAudioContext.attempts.push(options);
        if (options && Object.hasOwn(options, "sampleRate")) {
            throw new Error("unsupported sample rate");
        }
        super(options);
    }
}

const fallbackPlayer = new Pcm16AudioPlayer({
    AudioContextClass: RejectingRateAudioContext,
    AudioWorkletNodeClass: FakeAudioWorkletNode,
    sampleRate: 48000,
});
await fallbackPlayer.ready;
assert.equal(fallbackPlayer.usedDefaultContextRate, true);
assert.deepEqual(
    RejectingRateAudioContext.attempts,
    [
        { sampleRate: 48000, latencyHint: "interactive" },
        { latencyHint: "interactive" },
    ],
    "a rejected agent rate falls back to the browser default context rate",
);
assert.equal(
    fallbackPlayer.context.createdWorkletNodes[0].options.processorOptions.sourceSampleRate,
    48000,
    "the worklet retains the agent rate so it can resample continuously",
);
await fallbackPlayer.close();

const ResamplingProcessor = loadPlayerProcessor(2);
const resampler = new ResamplingProcessor({
    processorOptions: { sourceSampleRate: 3, prebufferSamples: 1 },
});
resampler.port.onmessage({
    data: { type: "samples", samples: new Float32Array([0, 0.2]).buffer },
});
const firstResampled = new Float32Array(1);
resampler.process([], [[firstResampled]]);
assert.equal(firstResampled[0], 0);
resampler.port.onmessage({
    data: { type: "samples", samples: new Float32Array([0.4, 0.6]).buffer },
});
const nextResampled = new Float32Array(2);
resampler.process([], [[nextResampled]]);
assert.ok(Math.abs(nextResampled[0] - 0.3) < 1e-6);
assert.ok(Math.abs(nextResampled[1] - 0.6) < 1e-6);

const appSource = await readFile(new URL("../voice/web/app.js", import.meta.url), "utf8");
assert.match(
    appSource,
    /startWsAudio\(generation, msg\.wsAudioFormat\)/,
    "hello_ack forwards its announced directional formats",
);
assert.match(
    appSource,
    /const agentFormat = format\.agent \|\| \{\};[\s\S]*sampleRate: agentFormat\.sampleRate/,
    "the player rate comes from hello_ack.wsAudioFormat.agent",
);
assert.match(
    appSource,
    /agentFormat\.sampleRate !== 48000[\s\S]*agentFormat\.frameSamples !== 960/,
    "the page accepts the announced 48 kHz, 960-sample agent format",
);
assert.match(
    appSource,
    /const player = new Pcm16AudioPlayer\([\s\S]*await player\.ready;/,
    "audio setup waits for the playback worklet module",
);

await player.close();
console.log("WebSocket AudioWorklet player tests passed");
