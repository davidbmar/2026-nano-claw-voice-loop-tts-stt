(function (global) {
    "use strict";

    // Detects a barge-in during agent playback: pause on loud onset, then
    // confirm real speech (stayed loud) vs false alarm (fell silent) within a
    // short window. Only sample() while the agent is speaking.
    class BargeInDetector {
        constructor(options) {
            const cfg = options || {};
            this.startThreshold = cfg.startThreshold || 0.05;
            this.sustainThreshold = cfg.sustainThreshold || 0.03;
            this.confirmMs = cfg.confirmMs || 400;
            this.reset();
        }

        reset() {
            this.pending = false;
            this.pendingSince = 0;
            this.sawSpeech = false;
        }

        sample(rms, now) {
            if (!this.pending) {
                if (rms >= this.startThreshold) {
                    this.pending = true;
                    this.pendingSince = now;
                    this.sawSpeech = true;
                    return { type: "barge_in" };
                }
                return null;
            }
            // Within the confirm window.
            if (rms >= this.sustainThreshold) this.sawSpeech = true;
            else this.sawSpeech = false; // track the most recent state
            if (now - this.pendingSince >= this.confirmMs) {
                const committed = this.sawSpeech;
                this.reset();
                return { type: committed ? "barge_in_commit" : "barge_in_false" };
            }
            return null;
        }
    }

    global.BargeInDetector = BargeInDetector;
}(typeof window !== "undefined" ? window : globalThis));
