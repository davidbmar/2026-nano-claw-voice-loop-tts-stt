import assert from "node:assert/strict";

import { Pcm16AudioPlayer } from "../voice/web/ws-audio-player.js";


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
        this.createdSources = [];
    }
    createAnalyser() { return new FakeAnalyser(); }
    createBuffer(_channels, samples, rate) {
        const data = new Float32Array(samples);
        return {
            duration: samples / rate,
            getChannelData() { return data; },
        };
    }
    createBufferSource() {
        const source = new FakeSource();
        this.createdSources.push(source);
        return source;
    }
    async resume() { this.state = "running"; }
    async close() { this.state = "closed"; }
}


const player = new Pcm16AudioPlayer({
    AudioContextClass: FakeAudioContext,
    sampleRate: 16000,
});
const frame = new Int16Array(320).buffer;

player.enqueue(frame);
assert.equal(player.context.createdSources.length, 0, "frames wait for agent_audio_start");

player.begin();
player.enqueue(frame);
player.enqueue(frame);
assert.deepEqual(
    player.context.createdSources.map((source) => source.starts[0]),
    [1.04, 1.06],
    "adjacent PCM frames use one contiguous Web Audio timeline",
);

player.pause();
player.enqueue(frame);
assert.equal(player.context.createdSources.length, 2, "paused playback drops in-flight frames");

player.context.currentTime = 2;
player.unpause();
player.enqueue(frame);
assert.equal(player.context.createdSources[2].starts[0], 2.04);

await player.close();
console.log("WebSocket audio player tests passed");
