(function (global) {
  'use strict';

  const BARGE_IN_SENSITIVITY_LEVELS = Object.freeze({
    low: Object.freeze({ startThreshold: 0.09, sustainThreshold: 0.054 }),
    medium: Object.freeze({ startThreshold: 0.05, sustainThreshold: 0.03 }),
    high: Object.freeze({ startThreshold: 0.03, sustainThreshold: 0.018 }),
  });

  // Detects a barge-in during agent playback: pause on loud onset, then
  // confirm real speech (stayed loud) vs false alarm (fell silent) within a
  // short window. Only sample() while the agent is speaking.
  class BargeInDetector {
    constructor(options) {
      const cfg = options || {};
      this.startThreshold = cfg.startThreshold || 0.05;
      this.sustainThreshold = cfg.sustainThreshold || 0.03;
      this.confirmMs = cfg.confirmMs || 650;
      // Ignore the first acoustic tail after local playback is flushed,
      // then require enough voiced time to distinguish a listener from
      // the agent's own speaker echo. Accumulating evidence also avoids
      // treating a natural between-syllable gap at exactly confirmMs as
      // a false alarm.
      this.echoGuardMs = Number.isFinite(cfg.echoGuardMs) ? Math.max(0, cfg.echoGuardMs) : 150;
      this.requiredSpeechMs = Number.isFinite(cfg.requiredSpeechMs)
        ? Math.max(0, cfg.requiredSpeechMs)
        : 180;
      this.reset();
    }

    reset() {
      this.pending = false;
      this.pendingSince = 0;
      this.lastSampleAt = 0;
      this.confirmedSpeechMs = 0;
    }

    sample(rms, now) {
      if (!this.pending) {
        if (rms >= this.startThreshold) {
          this.pending = true;
          this.pendingSince = now;
          this.lastSampleAt = now;
          this.confirmedSpeechMs = 0;
          return { type: 'barge_in' };
        }
        return null;
      }
      const elapsed = Math.max(0, now - this.pendingSince);
      const guardedStart = this.pendingSince + this.echoGuardMs;
      if (rms >= this.sustainThreshold && now > guardedStart) {
        // Cap one observation's contribution so a throttled animation
        // frame cannot manufacture a long stretch of speech.
        const evidenceStart = Math.max(this.lastSampleAt, guardedStart);
        this.confirmedSpeechMs += Math.min(50, Math.max(0, now - evidenceStart));
      }
      this.lastSampleAt = now;

      if (this.confirmedSpeechMs >= this.requiredSpeechMs) {
        this.reset();
        return { type: 'barge_in_commit' };
      }
      if (elapsed >= this.confirmMs) {
        this.reset();
        return { type: 'barge_in_false' };
      }
      return null;
    }
  }

  function positiveNumber(value, fallback) {
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }

  function positiveInteger(value, fallback) {
    return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
  }

  /**
   * Wraps a BargeInDetector with deterministic false-alarm adaptation.
   *
   * The controller keeps a rolling outcome window (8 commits/falses by
   * default). At least 3 falses and a false ratio >= 0.5 raises both RMS
   * thresholds by 1.4x, up to 3x the user's base. After 60,000 ms without a
   * false, or after 3 consecutive commits, it divides the thresholds by 1.4
   * toward that base. RMS thresholds are unitless [0, 1] amplitudes; all
   * clocks and recovery durations are milliseconds. There are no timers:
   * sample() and handleOutcome() make every decision from the supplied now.
   */
  class AdaptiveBargeInController {
    constructor(detector, options) {
      let cfg = options || {};
      let wrappedDetector = detector;

      // Also accept a single options object for convenient console use.
      if (!wrappedDetector || typeof wrappedDetector.sample !== 'function') {
        cfg = detector || {};
        wrappedDetector = cfg.detector;
        if (!wrappedDetector || typeof wrappedDetector.sample !== 'function') {
          wrappedDetector = new BargeInDetector({
            startThreshold: cfg.startThreshold || cfg.baseStartThreshold,
            sustainThreshold: cfg.sustainThreshold || cfg.baseSustainThreshold,
            confirmMs: cfg.confirmMs,
            echoGuardMs: cfg.echoGuardMs,
            requiredSpeechMs: cfg.requiredSpeechMs,
          });
        }
      }

      this.detector = wrappedDetector;
      this.windowSize = positiveInteger(cfg.windowSize || cfg.window, 8);
      this.minFalses = positiveInteger(cfg.minFalses || cfg.falseThreshold, 3);
      this.minFalseRatio = positiveNumber(cfg.minFalseRatio || cfg.falseRatio, 0.5);
      this.stepFactor = positiveNumber(cfg.stepFactor || cfg.step, 1.4);
      this.maxMultiplier = positiveNumber(cfg.maxMultiplier || cfg.cap, 3);
      this.recoveryMs = positiveNumber(cfg.recoveryMs, 60000);
      this.recoveryCommits = positiveInteger(cfg.recoveryCommits || cfg.commitRecoveryCount, 3);
      this.adaptiveEnabled = cfg.adaptive !== false && cfg.enabled !== false;

      const detectorStart = positiveNumber(this.detector.startThreshold, 0.05);
      const detectorSustain = positiveNumber(this.detector.sustainThreshold, 0.03);
      this.baseStartThreshold = positiveNumber(
        cfg.baseStartThreshold || cfg.startThreshold,
        detectorStart
      );
      this.baseSustainThreshold = positiveNumber(
        cfg.baseSustainThreshold || cfg.sustainThreshold,
        detectorSustain * (this.baseStartThreshold / detectorStart)
      );

      this.outcomes = [];
      this.commitCount = 0;
      this.falseCount = 0;
      this.adjustmentCount = 0;
      this.consecutiveCommits = 0;
      this.lastFalseAt = null;
      this.recoveryAnchorAt = null;
      this._applyThreshold(this.baseStartThreshold);
    }

    _applyThreshold(startThreshold) {
      const bounded = Math.max(
        this.baseStartThreshold,
        Math.min(this.baseStartThreshold * this.maxMultiplier, startThreshold)
      );
      const sustainRatio = this.baseSustainThreshold / this.baseStartThreshold;
      this.detector.startThreshold = bounded;
      this.detector.sustainThreshold = bounded * sustainRatio;
    }

    _rememberOutcome(outcome) {
      this.outcomes.push(outcome);
      if (this.outcomes.length > this.windowSize) this.outcomes.shift();
    }

    _raiseThreshold(now) {
      const current = this.detector.startThreshold;
      const next = Math.min(
        this.baseStartThreshold * this.maxMultiplier,
        current * this.stepFactor
      );
      if (next <= current) return false;
      this._applyThreshold(next);
      this.adjustmentCount += 1;
      this.outcomes = [];
      this.consecutiveCommits = 0;
      this.recoveryAnchorAt = now;
      return true;
    }

    _recoverThreshold(now) {
      const current = this.detector.startThreshold;
      if (current <= this.baseStartThreshold) return false;
      this._applyThreshold(current / this.stepFactor);
      this.adjustmentCount += 1;
      this.outcomes = [];
      this.consecutiveCommits = 0;
      this.recoveryAnchorAt = now;
      return true;
    }

    _shouldRaise() {
      const falses = this.outcomes.reduce(function (count, outcome) {
        return count + (outcome === 'false' ? 1 : 0);
      }, 0);
      return falses >= this.minFalses && falses / this.outcomes.length >= this.minFalseRatio;
    }

    _recoverAfterQuietPeriod(now) {
      if (!this.adaptiveEnabled || this.detector.startThreshold <= this.baseStartThreshold) {
        return false;
      }
      const anchor = this.recoveryAnchorAt === null ? this.lastFalseAt : this.recoveryAnchorAt;
      if (anchor === null || now - anchor < this.recoveryMs) return false;
      return this._recoverThreshold(now);
    }

    sample(rms, now) {
      const event = this.detector.sample(rms, now);
      if (event && (event.type === 'barge_in_commit' || event.type === 'barge_in_false')) {
        this.handleOutcome(event, now);
      } else if (!event && !this.detector.pending) {
        this._recoverAfterQuietPeriod(now);
      }
      return event;
    }

    handleOutcome(outcome, now) {
      const type = typeof outcome === 'string' ? outcome : outcome && outcome.type;
      if (type !== 'barge_in_commit' && type !== 'barge_in_false') return false;

      if (type === 'barge_in_false') {
        this.falseCount += 1;
        this.consecutiveCommits = 0;
        this.lastFalseAt = now;
        this.recoveryAnchorAt = now;
        this._rememberOutcome('false');
        if (this.adaptiveEnabled && this._shouldRaise()) {
          return this._raiseThreshold(now);
        }
        return false;
      }

      this.commitCount += 1;
      this.consecutiveCommits += 1;
      this._rememberOutcome('commit');
      if (this.adaptiveEnabled && this.consecutiveCommits >= this.recoveryCommits) {
        return this._recoverThreshold(now);
      }
      return false;
    }

    reset() {
      this.detector.reset();
    }

    setAdaptiveEnabled(enabled) {
      this.adaptiveEnabled = Boolean(enabled);
      this.outcomes = [];
      this.consecutiveCommits = 0;
      this.recoveryAnchorAt = null;
      if (!this.adaptiveEnabled) this._applyThreshold(this.baseStartThreshold);
      return this.stats();
    }

    setBaseThreshold(startThreshold, sustainThreshold) {
      const start = positiveNumber(startThreshold, NaN);
      const sustain = positiveNumber(sustainThreshold, NaN);
      if (!Number.isFinite(start) || !Number.isFinite(sustain)) {
        throw new TypeError('barge-in thresholds must be positive numbers');
      }
      this.baseStartThreshold = start;
      this.baseSustainThreshold = sustain;
      this.outcomes = [];
      this.consecutiveCommits = 0;
      this.lastFalseAt = null;
      this.recoveryAnchorAt = null;
      this._applyThreshold(start);
      return this.stats();
    }

    setSensitivity(level) {
      const thresholds = BARGE_IN_SENSITIVITY_LEVELS[level];
      if (!thresholds) throw new RangeError('unknown barge-in sensitivity: ' + level);
      return this.setBaseThreshold(thresholds.startThreshold, thresholds.sustainThreshold);
    }

    stats() {
      return {
        currentThreshold: this.detector.startThreshold,
        baseThreshold: this.baseStartThreshold,
        commits: this.commitCount,
        falses: this.falseCount,
        adjustments: this.adjustmentCount,
      };
    }
  }

  global.BargeInDetector = BargeInDetector;
  global.AdaptiveBargeInController = AdaptiveBargeInController;
  global.BargeInSensitivityLevels = BARGE_IN_SENSITIVITY_LEVELS;
  global.BargeIn = Object.assign(global.BargeIn || {}, {
    BargeInDetector: BargeInDetector,
    AdaptiveBargeInController: AdaptiveBargeInController,
    sensitivityLevels: BARGE_IN_SENSITIVITY_LEVELS,
    controller: null,
  });
  BargeInDetector.AdaptiveController = AdaptiveBargeInController;
  BargeInDetector.SENSITIVITY_LEVELS = BARGE_IN_SENSITIVITY_LEVELS;
})(typeof window !== 'undefined' ? window : globalThis);
