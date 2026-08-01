# How the Voice Line Listens, Thinks, and Speaks

*(Markdown twin of `speech-pipeline.html` — the user-facing explainer.)*

What happens between the moment you stop talking and the moment the
assistant answers — including what the little sounds mean.

## The sounds you'll hear

- **A soft two-note chime** right after you finish a sentence means
  *"I heard you."* Your words were captured and are on their way to being
  understood.
- **A quiet clock tick**, about twice a second, means *"working on it"* —
  your speech is being turned into text and the assistant is composing its
  answer. The ticking stops the instant the reply begins. If you ask the
  assistant "what's that ticking?", it knows.

## The journey of one exchange

1. **You speak; the line listens for you to finish.** A voice-activity
   detector calls your turn complete after a short natural silence. You can
   interrupt the assistant at any time just by talking. While you speak,
   local Whisper repeatedly checks the growing audio and keeps the words that
   consecutive passes agree on.
2. **Chime — you were heard.** Plays immediately, before any processing.
3. **Ticking — finish the tail, then think.** Most transcription happened
   while you were talking, on this machine. Whisper now resolves only the
   short unstable tail. The text goes to two language models racing: a local
   model gets a head start, a cloud model joins moments later if the local one
   is slow. First words win; the loser is cancelled.
4. **Speaking begins before the thinking ends.** Each completed sentence is
   immediately *compiled for the ear* and spoken while the model writes the
   rest: deliberate pauses after sentences, commas, and dashes (with a
   little human-like variation), and prices, dates, times, phone numbers,
   "No. 5" expanded into spoken words. Lists get spoken ordinals.
5. **A local voice says it.** LuxTTS renders the words on the same machine,
   paced to the phone network frame by frame.

## Why it's built this way

Silence on a phone line reads as a dead call — the chime and tick keep the
line honest about where it is. Sentence-by-sentence speaking recovers most
of the wait without sacrificing pause-and-phrasing polish. The two-model
race means a slow local model never stalls you, while a cloud outage still
leaves a working local voice.

## For the curious

Every call records a replayable timeline in the `/calls` review panel:
both sides of the conversation, which model actually wrote each reply, the
cost, and the timing of every chime, tick, and sentence. Engineering
details: `docs/design/2026-07-27-streaming-prepared-speech/`,
`docs/design/2026-07-26-thinking-cue/`,
`docs/design/2026-07-26-call-attribution-settings-costs/`.
