# Transcribe Mode — How nano-claw Learned to Listen Without Talking

Most of nano-claw is built to answer you. Transcribe Mode is built to do the
opposite: listen to everything, say nothing, and write down what it heard.

It exists for research. Speech goes to a local model running on another machine
so its internals can be studied. That purpose changes what "good" means. An
assistant that misses a word can ask you to repeat it. A research recorder that
misses a word has destroyed evidence, and nobody finds out.

This is the story of getting that right. Every number below came from a real
session on 6 August 2026.

---

## The case of the counting that started at eleven

A test, as simple as tests get: count out loud from one to twenty.

The transcript began at **eleven**.

Not garbled. Not quiet. The numbers one through ten were simply absent, as
though they had never been spoken. The page looked healthy the whole time.

The cause was four lines of code:

```python
if self._recording:
    self._mic_frames.append(frame)     # keep it
else:
    self._mic_preroll.append(frame)    # ring buffer, 600ms
```

Between one turn and the next, audio landed in a ring buffer that holds
**600 milliseconds**. Counting to ten takes about twelve seconds. The last
0.6 seconds survived. The other 11.4 were overwritten, one frame at a time.

Here is the part worth sitting with: **the audio was arriving the whole time.**
The microphone never stopped. The frames reached the server and were thrown away
on arrival. No network problem, no microphone problem, no transcription problem
would ever have explained it, because none of those things were wrong.

**The fix.** In this mode, keep every frame:

```python
if self._recording or self._continuous:
    self._mic_frames.append(frame)
```

The log now says so out loud:

```
Mic recording started (continuous; 277 frames already buffered)
```

277 frames is 5.5 seconds of speech carried across a gap that used to hold 0.6.

**Why not everywhere?** Because in normal conversation, the gap between turns is
when the assistant is *talking*. Recording through it would feed nano-claw's own
voice back into its ears. The old behaviour is correct — just not here.

---

## The case of the tab that went deaf

The next report was stranger: *"sometimes it stops recording."*

Sometimes. Not always. No error, no warning, no crash. The page sat there looking
connected, and it was — it was still politely asking the server for its
configuration every five seconds.

The answer was one line:

```javascript
vadFrameRequest = window.requestAnimationFrame(monitorPhoneAudio);
```

The code that decides *"someone is speaking, start a turn"* runs on
`requestAnimationFrame` — a browser function for animation. Chrome **stops
firing it entirely** when a tab is hidden, minimized, or covered by another
window.

So the symptom wasn't random at all. It tracked **which window was in front.**
Every test where someone switched to a video player to play audio at the page was,
structurally, a test with the microphone paused.

And once again the microphone was innocent. `getUserMedia` runs on the audio
thread and is never throttled, so speech kept streaming to the server the entire
time. Only the watcher deciding *when to listen* had stopped.

**The fix.** Transcribe Mode stops asking the browser when to listen. It opens the
microphone once and lets the server cut segments on its own clock, every eight
seconds. A server clock has no opinion about which window is in front.

Every other mode keeps the browser's voice detection, where it belongs: it ends a
turn when a human stops speaking, and no fixed interval can know that.

---

## The case of the words nobody said

Capture was now complete. The same session also produced this:

```
"So, that's it for today's video, thank you so much for watching..."
"Thank you very much for watching this video and I'll see you in the next one."
"Thank you so much for watching this video."
```

Six variations. Nobody said any of them.

This is Whisper's most famous failure. It was trained on captioned video, where
silence is rarely truly empty — so when you hand it silence, it produces the
words most likely to accompany silence. A YouTube sign-off.

For a research recorder this is worse than losing a word. A missing word leaves a
gap you might notice. An invented sentence looks exactly like real speech, gets
stored as real speech, and gets fed to the model as though a human said it.

**What we found.** Whisper ships two defences and both were switched off. The
call site passed three arguments:

```python
model.transcribe(samples, beam_size=5, language="en", word_timestamps=want_words)
```

Measured through the running service — twenty seconds of near-silence:

| what we sent | text returned | Whisper's own no-speech score |
|---|---|---|
| nothing extra (the old behaviour) | `"You"` | **0.86** |
| `vad_filter` on, no self-conditioning | `""` | — |

Look at **0.86**. On a scale where 1.0 means "definitely not speech", Whisper
scored its own output 0.86 and printed it anyway. The service computed that
number on every single transcription and **threw it away**.

**The fix, in four layers:**

1. **Never ask about silence.** An energy check runs before Whisper is called at
   all. Quiet audio is discarded without a transcription request.
2. **Turn on Whisper's own filter.** It bundles a speech detector; nothing needed
   downloading.
3. **Stop it feeding itself.** By default each result seeds the next window,
   which is how one phantom becomes six escalating variations.
4. **Write down every rejection.** With its score and the threshold that rejected
   it.

Layer 4 matters more than it sounds. A gate that discards audio silently has
recreated the original bug at a different layer: a too-strict threshold would
look *exactly* like a quiet room. Now it looks like a line in a file.

**And it doesn't eat real speech.** Tested with synthesized counting:

| | plain | with the filter |
|---|---|---|
| speech only | `1-2-3-4-5-6-7-8-9-10` | `1-2-3-4-5-6-7-8-9-10` |
| speech + 3s silence each side | `1, 2, … 9, 10.` | `1-2-3-4-5-6-7-8-9-10` |

All ten numbers, both ways.

---

## The case of the thirteen that fell between two chunks

Audio is cut into eight-second pieces. The clock does not care where you are in a
sentence — or a word.

The count came back:

```
"...ten, eleven, twelve."
"14, 15, 16, 17, 18, 19, 20."
```

**Thirteen was gone.** So were 3 and 4 at the next seam.

And no audio was lost. The word was split down the middle: one chunk ended with
half of "thirteen", the next began with the other half. Whisper discarded both
fragments as noise. The word survived in neither.

**The fix.** Carry audio from the end of each chunk into the start of the next, so
every word is whole in at least one of them. The overlapping second gets
transcribed twice, and the duplicate is removed from the text afterwards.

You can watch it work. From a real recording:

```
chunk A ends:   "...Everyone is watching."
chunk B starts: "the sky and the thing they're watching for is already in the room."
```

The words *"the sky"* were spoken across the boundary. They survived, and they
appear once. Under the old behaviour they would have been in neither.

**How much overlap?** This turned out to be the interesting question. One second
was chosen because it spans a word. But the *joining algorithm* needs enough
matching words to be confident, and one second is only two to four:

| overlap | words | what it allows |
|---|---|---|
| 1 second | 2–4 | exact matching only; fragile |
| **2 seconds** | **~5** | tolerates one mis-transcribed word |
| 3 seconds | 7–8 | robust, and 37% more transcription for little gain |

At five words the matcher can survive a boundary word rendered two ways —
"13" one side, "thirteen" the other — which at three words is fatal.

The pleasing part: **more overlap makes the join both bolder and safer.** With
more words to match, the rule can demand a longer match, so genuine repetition
stops looking like a seam. "Count, count, count with me" — a real line from a real
recording — is left alone.

---

## The case of the eight conversations in one file

A single 52-entry capture turned out to contain **eight different sessions.**

Every browser tab, every health check, every reconnect starts its own numbering at
1 and writes to the same file:

```
sequence: [1,2,3,4,5]  [1,2,3,4,5,6,7,8,9]  [1,2,3] ...
```

Stitch that in file order and you get a fluent transcript of a conversation that
never happened — a health-check probe spliced into the middle of an essay. It is
the hallucination problem again, one level up.

**The fix.** Every connection now gets an identifier, written on every line.

**What we deliberately did not do:** guess. You *can* detect a new session by
watching the numbering reset — and it works, right up until two sessions are live
at once. Then entries interleave as `A1, B1, A2, B2` and nothing ever resets. The
guess doesn't fail loudly; it fails into something that reads perfectly well and
is wrong.

Older files, recorded before identifiers existed, still get the guess — and a
warning in capital letters saying so.

---

## The scoreboard

The honest question: how does this compare to simply handing Whisper the whole
recording at once?

We can answer it, because we can synthesize speech from a known passage and score
both. Ground truth: 104 words.

| approach | words emitted | ground-truth words captured |
|---|---|---|
| Whole-file Whisper | 102 | 100 / 104 |
| **This pipeline** | **132** | **99 / 104** |
| Chunked, no stitching | 140 | 99 / 104 |

**Reading this fairly:**

**Recall is excellent.** 99 of 104 words, one behind a whole-file pass. Live
capture supports this — a 2,743-word recording came through with sequence numbers
1 to 51 and **no gaps at all**.

**Duplication is the remaining flaw.** 132 words emitted for a 104-word passage.
Stitching removed 8 duplicated words; roughly 28 survived.

The cause is known. Joining requires the repeated run to sit exactly at the end of
one chunk and the start of the next. When the clock chops a word in half —
`"within"` becoming `"with"` — the runs are offset by a token and the join is
rejected:

```
previous chunk ends:  [read, what, he, wrote, or, to, come, with]
next chunk begins:    [to, read, what, he, wrote, or, to, come]
                       seven words match, but not at the edges
```

That is the next thing to fix, and it is now measurable rather than a matter of
opinion.

**The failure we have is the better one.** Duplicated text can be cleaned up later
from the recording. Lost speech cannot be recovered from anything.

---

## What it looks like in use

Choose **Transcribe Mode** in the console and start the microphone. The status
reads *"Recording continuously — VAD off"*. Then talk, or play something at it,
or switch to another window — it keeps listening.

The screen shows only what you said. No reply, ever.

Watch it live:

```bash
docker logs -f nano-claw-voice | grep TRANSCRIBE
tail -f ~/riff-dev-data/nano-claw-transcribe/jspace.jsonl
```

Every utterance is one line of JSON:

```json
{
  "seq": 34,
  "session_id": "b224f30b",
  "transcript": "Everyone is watching.",
  "transcript_raw": "So Everyone is watching.",
  "response": "...",
  "prompt_eval_count": 38,
  "gated": null,
  "rms": null
}
```

Two fields repay attention.

**`transcript_raw`** is what the transcriber actually returned, before duplicate
words were removed. The cleanup is a judgement call, so the original is always
kept beside it. No heuristic in this system is ever the only surviving copy.

**`prompt_eval_count`** should stay small and **not climb**. Each utterance is
sent to the model on its own, with no memory of what came before, so every one is
comparable to every other. If this number starts rising, history has crept in and
the recordings have stopped being independent.

That second one is not theoretical. Early on, two consecutive utterances scored
38 and then **28**. The model's second reply read as though it followed the first
— but the count going *down* proved it hadn't. Only the number knew.

To turn many chunks into one readable transcript:

```bash
scripts/stitch_transcript.py ~/riff-dev-data/nano-claw-transcribe/jspace.jsonl --verbose
```

`--verbose` prints every join and the exact words it removed. Worth running once.
In a research transcript, a wrongly deleted word is worse than a duplicated one,
so the joins are made inspectable rather than trustworthy.

---

## What is still imperfect

**Sentences break in the wrong places.** The clock cuts every eight seconds and
the transcriber punctuates each piece as if it were a complete thought:

> "...Everyone is watching. the sky and the thing they're watching for is already
> in the room."

Every word is there and none is duplicated. But the full stop marks where a timer
fired, not where a person stopped. Filed, with one rule for whoever fixes it:
punctuation and capitalisation may change, **never a word.** A transcript that
quietly improves what someone said is the same failure as inventing it.

**Roughly a quarter of the text is still duplicated at seams.** Measured above,
cause understood, fix identified.

**Nothing here reaches inside the model.** This records what went in and what came
out. Looking at how the model represents speech needs the model running somewhere
that exposes its internals — which is why every utterance is kept independent and
reproducible.

---

## The thread running through all of it

Four bugs, and not one was a missing capability.

The audio was already arriving — and being dropped into a 600ms ring.
The microphone was already recording — while nothing was listening.
Whisper already knew it wasn't speech — and scored it 0.86 while printing it.
The overlap was already captured — and thrown away by an alignment check.

Every one was a signal computed and discarded. Which is why none of them looked
like an error, and all of them looked like silence.
