/**
 * Inbound sentiment: the display reacts to the CALLER, not only to the reply.
 *
 * Before this, `emotion-layer.js` inferred an emotion from the agent's own
 * replies and nothing at all from the caller — so the centre animation sat
 * still until the agent answered.
 *
 * Two properties matter beyond "a rule fires":
 *  - inbound and outbound rules must stay SEPARATE. The outbound set is scoped
 *    to agent replies and misfires on caller speech in a specific, testable way
 *    (see the somber regression below);
 *  - every emotion a rule can emit must exist in EMOTION_PROFILES, because
 *    `setVisualEmotion` silently returns false for an unknown name. An
 *    off-vocabulary rule would look exactly like a display that never moves.
 */

import { describe, it, expect } from 'vitest';
import {
  EMOTION_PROFILES,
  inferEmotion,
  inferInboundEmotion,
} from '../voice/web/emotion-layer.js';

/** One representative utterance per rule, plus the shapes that must stay neutral. */
const INBOUND_CASES: Array<[string, string]> = [
  ["This is the third time I've called about this and nobody has helped me.", 'urgent'],
  ['My basement is flooding right now, I need someone immediately!', 'urgent'],
  ["Wait, sorry, I don't understand what you just said.", 'confused'],
  ['Sorry what? Come again?', 'confused'],
  ['Nobody has called me back and this is ridiculous.', 'tense'],
  ['Yeah that works great, thanks so much.', 'warm'],
  ['What time do you open?', 'curious'],
  ['uh. hmm. i guess. maybe?', 'curious'],
  ['I was hoping to get someone out to look at my water heater.', 'neutral'],
  ['', 'neutral'],
];

describe('inferInboundEmotion', () => {
  it.each(INBOUND_CASES)('%j reads as %s', (text, expected) => {
    expect(inferInboundEmotion(text).emotion).toBe(expected);
  });

  it('only ever emits emotions the renderer actually knows', () => {
    // setVisualEmotion rejects unknown names by returning false and doing
    // nothing — the quietest possible failure, so assert the vocabulary.
    for (const [text] of INBOUND_CASES) {
      const { emotion, intensity } = inferInboundEmotion(text);
      expect(Object.prototype.hasOwnProperty.call(EMOTION_PROFILES, emotion)).toBe(true);
      expect(intensity).toBeGreaterThan(0);
      expect(intensity).toBeLessThanOrEqual(1);
    }
  });

  it('is pure — the same text always reads the same way', () => {
    const text = 'My basement is flooding right now!';
    expect(inferInboundEmotion(text)).toEqual(inferInboundEmotion(text));
  });
});

describe('inbound and outbound rules stay separate', () => {
  it('REGRESSION: a confused caller must not render as somber', () => {
    // The outbound rule set matches the caller's polite "sorry" and calls this
    // somber — the display would look sad at someone asking for clarification.
    // This is the specific misfire that justifies two rule sets, so it is
    // asserted on both sides rather than described in a comment.
    const text = "Wait, sorry, I don't understand what you just said.";
    expect(inferEmotion(text).emotion).toBe('somber');
    expect(inferInboundEmotion(text).emotion).toBe('confused');
  });

  it("REGRESSION: a caller's question must not read as the agent's joy", () => {
    // Mirror of the comment above EMOTION_RULES: affirmative outcome words
    // belong to the agent, not to a caller asking whether something happened.
    expect(inferInboundEmotion('Is it booked?').emotion).toBe('curious');
  });

  it('outbound behaviour is unchanged by this feature', () => {
    // Guards against someone later "unifying" the two rule sets: outbound is
    // measurably correct on agent replies and must keep working.
    expect(inferEmotion("You're all set — I've booked you for Tuesday.").emotion).toBe('joyful');
    expect(inferEmotion("I'm sorry, we're completely full that week.").emotion).toBe('somber');
  });
});
