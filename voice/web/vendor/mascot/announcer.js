/**
 * A text equivalent for the character's status.
 *
 * The mascot conveys what the agent is doing — thinking, working, confused —
 * entirely through pixels. Assistive technology sees a static image label and
 * nothing else, and the accessibility tree never changes as the state does.
 *
 * That matters more here than in most products: this ships into a voice
 * interface, which is disproportionately likely to have users who cannot see it.
 *
 * What is announced, and what is not:
 *
 *   - PRESENCE is announced. It is the actionable status — whether the agent is
 *     listening, thinking, working or speaking is information the user needs.
 *   - EMOTION is not, with one exception. Emotional colour is decorative, and
 *     narrating it ("warm", "proud") would be both patronising and exhausting.
 *     `confused` is the exception because it means "I did not understand you",
 *     which is a fact about the conversation rather than a mood.
 *
 * Announcements are debounced, because a burst of state changes during one turn
 * should produce one utterance rather than five.
 */

/** Human phrasing, not raw identifiers. */
const PRESENCE_TEXT = {
  idle: 'Ready',
  listening: 'Listening',
  silent: 'Waiting',
  thinking: 'Thinking',
  working: 'Working',
  confused: 'Did not understand',
  speaking: 'Speaking',
  paused: 'Paused',
};

/** The only emotion that is a fact about the conversation rather than a mood. */
const EMOTION_TEXT = {
  confused: 'Did not understand',
};

/**
 * Never returns null for a real state.
 *
 * Returning "nothing to say" let the region go STALE: clearing the presence at
 * the end of a turn left it still announcing "Working", so a screen-reader user
 * believed the agent was busy after it had finished. A stale status is worse than
 * no status — silence is merely ambiguous, but "Working" after the work is done is
 * actively wrong.
 *
 * With no presence the agent is doing nothing, and saying so is accurate.
 */
export function describeState(state) {
  if (!state || typeof state !== 'object') return null;
  const { presence, emotion } = state;
  if (presence && PRESENCE_TEXT[presence]) return PRESENCE_TEXT[presence];
  if (emotion && EMOTION_TEXT[emotion]) return EMOTION_TEXT[emotion];
  return PRESENCE_TEXT.idle;
}

/**
 * Create a polite live region and keep it in step with the director's state.
 *
 * @param options.container  where to append the region
 * @param options.debounceMs how long to coalesce a burst of changes
 * @param options.now        injectable clock, for tests
 * @param options.schedule   injectable timer, for tests
 */
export function createAnnouncer({
  container,
  debounceMs = 400,
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (h) => clearTimeout(h),
} = {}) {
  const region = (container?.ownerDocument ?? globalThis.document)?.createElement('div');
  if (!region) throw new Error('createAnnouncer: no document available');

  region.setAttribute('role', 'status');
  region.setAttribute('aria-live', 'polite');
  // Announcements should not interrupt a screen reader mid-sentence.
  region.setAttribute('aria-atomic', 'true');
  // Visually hidden but still announced — display:none would silence it.
  region.style.cssText =
    'position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;' +
    'clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0';
  container?.append(region);

  let last = null;
  let pending = null;
  let timer = null;
  let enabled = true;

  function flush() {
    timer = null;
    if (!enabled || pending === null || pending === last) return;
    last = pending;
    // Setting textContent is what triggers the announcement.
    region.textContent = pending;
    pending = null;
  }

  /** Offer a state; announced only if it differs and survives the debounce. */
  function update(state) {
    const text = describeState(state);
    if (text === null || text === last) return false;
    pending = text;
    if (timer !== null) cancel(timer);
    timer = schedule(flush, debounceMs);
    return true;
  }

  return {
    region,
    update,
    /** Say something immediately, bypassing the debounce. For errors. */
    say(text) {
      if (!enabled || !text) return false;
      if (timer !== null) { cancel(timer); timer = null; }
      last = text;
      region.textContent = text;
      return true;
    },
    setEnabled(on) {
      enabled = on !== false;
      if (!enabled) region.textContent = '';
      return enabled;
    },
    isEnabled: () => enabled,
    current: () => region.textContent || null,
    destroy() {
      if (timer !== null) cancel(timer);
      region.remove();
    },
  };
}
