import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
    DEFAULT_INITIAL_LEAD_SECONDS,
    Pcm16AudioPlayer,
} from "../voice/web/ws-audio-player.js";


class FakeAnalyser {
    connect() {}
    disconnect() {}
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
    constructor() {
        this.currentTime = 1;
        this.destination = {};
        this.state = "running";
        this.createdBuffers = [];
        this.createdSources = [];
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


assert.throws(
    () => new Pcm16AudioPlayer({ AudioContextClass: FakeAudioContext }),
    /sample rate was not announced/,
    "playback cannot fall back to a shared mic rate",
);

const player = new Pcm16AudioPlayer({
    AudioContextClass: FakeAudioContext,
    sampleRate: 48000,
});
const frame = new Int16Array(960).buffer;

player.enqueue(frame);
assert.equal(player.context.createdSources.length, 0, "frames wait for agent_audio_start");

player.begin();
player.enqueue(frame);
player.enqueue(frame);
assert.deepEqual(
    player.context.createdBuffers.map(({ samples, rate }) => ({ samples, rate })),
    [
        { samples: 960, rate: 48000 },
        { samples: 960, rate: 48000 },
    ],
    "announced 48 kHz agent frames create 960-sample buffers at that rate",
);
assert.equal(player.initialLeadSeconds, DEFAULT_INITIAL_LEAD_SECONDS);
assert.deepEqual(
    player.context.createdSources.map((source) => source.starts[0]),
    [1.15, 1.17],
    "adjacent PCM frames use one contiguous Web Audio timeline",
);

player.pause();
player.enqueue(frame);
assert.equal(player.context.createdSources.length, 2, "paused playback drops in-flight frames");

player.context.currentTime = 2;
player.unpause();
player.enqueue(frame);
assert.equal(player.context.createdSources[2].starts[0], 2.15);

const boundedLeadPlayer = new Pcm16AudioPlayer({
    AudioContextClass: FakeAudioContext,
    sampleRate: 48000,
    initialLeadSeconds: 10,
});
assert.equal(boundedLeadPlayer.initialLeadSeconds, 0.18, "configured jitter lead is bounded");
await boundedLeadPlayer.close();

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

await player.close();
console.log("WebSocket audio player tests passed");
