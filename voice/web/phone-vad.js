(function (global) {
    "use strict";

    function clamp(value, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, value));
    }

    class PhoneVadGate {
        constructor(options) {
            const config = options || {};
            this.startThreshold = config.startThreshold || 0.018;
            this.stopThreshold = config.stopThreshold || 0.011;
            this.startHoldMs = config.startHoldMs || 120;
            this.silenceMs = config.silenceMs || 850;
            this.minSpeechMs = config.minSpeechMs || 280;
            this.maxSpeechMs = config.maxSpeechMs || 25000;
            this.reset();
        }

        configureFromNoise(samples) {
            const values = (samples || [])
                .filter(function (value) { return Number.isFinite(value) && value >= 0; })
                .sort(function (a, b) { return a - b; });
            if (!values.length) {
                return {
                    startThreshold: this.startThreshold,
                    stopThreshold: this.stopThreshold,
                };
            }
            const percentileIndex = Math.min(
                values.length - 1,
                Math.floor(values.length * 0.8),
            );
            const noiseFloor = values[percentileIndex];
            this.startThreshold = clamp(noiseFloor * 3.5, 0.015, 0.08);
            this.stopThreshold = clamp(this.startThreshold * 0.62, 0.009, 0.05);
            return {
                noiseFloor: noiseFloor,
                startThreshold: this.startThreshold,
                stopThreshold: this.stopThreshold,
            };
        }

        reset() {
            this.speaking = false;
            this.aboveSince = null;
            this.speechStartedAt = null;
            this.silenceSince = null;
        }

        sample(rms, nowMs) {
            const level = Number.isFinite(rms) ? Math.max(0, rms) : 0;
            const now = Number.isFinite(nowMs) ? nowMs : 0;

            if (!this.speaking) {
                if (level >= this.startThreshold) {
                    if (this.aboveSince === null) this.aboveSince = now;
                    if (now - this.aboveSince >= this.startHoldMs) {
                        this.speaking = true;
                        this.speechStartedAt = this.aboveSince;
                        this.silenceSince = null;
                        return {type: "speech_start", rms: level};
                    }
                } else {
                    this.aboveSince = null;
                }
                return null;
            }

            if (now - this.speechStartedAt >= this.maxSpeechMs) {
                this.reset();
                return {type: "speech_stop", reason: "maximum_duration", rms: level};
            }

            if (level <= this.stopThreshold) {
                if (this.silenceSince === null) this.silenceSince = now;
                const speechDuration = now - this.speechStartedAt;
                if (
                    speechDuration >= this.minSpeechMs
                    && now - this.silenceSince >= this.silenceMs
                ) {
                    this.reset();
                    return {type: "speech_stop", reason: "silence", rms: level};
                }
            } else {
                this.silenceSince = null;
            }
            return null;
        }
    }

    global.PhoneVadGate = PhoneVadGate;
}(typeof window !== "undefined" ? window : globalThis));
