# Stitching a continuous transcript from overlapping chunks

**Status:** design, 2026-08-06
**Depends on:** Transcribe Mode (`voice/gemma_probe.py`, `voice/transcript_overlap.py`)

## The problem

Transcribe Mode captures speech in 8-second chunks cut by a server clock. Each
chunk overlaps the previous one so no word is lost at a seam, and the overlapping
words are de-duplicated as they are recorded.

That produces a correct **capture**. It does not produce a readable
**transcript**. Three things stand between the JSONL file and a document a human
or an analysis script can use.

### 1. The file holds more than one conversation

A single 52-entry capture from 2026-08-06 contained **eight interleaved
sessions**:

```
seq order: [None ×10, 1,2,3,4,5, 1,2,3,4,5,6,7,8,9, ...]
```

Every browser tab, health check, and reconnect starts its own `seq` at 1 and
appends to the same file. Joining by file order would splice a health probe into
the middle of a recording and emit a fluent transcript of something nobody said —
the same failure as a hallucination, one level up.

**Detecting sessions by watching `seq` decrease is not sufficient.** It works on
sequential sessions and fails on concurrent ones: two live sessions interleave as
`A1, B1, A2, B2` and `seq` never decreases. The heuristic does not fail loudly;
it fails into plausible nonsense. The capture needs a real session identifier.

### 2. File order is not speech order

Probes run concurrently, so a slow one lands after a fast one that was spoken
later. `seq` is assigned before the model call and is the only reliable ordering.

### 3. The seam join is running at the edge of what it can do

`OVERLAP_FRAMES = 50` (1 second) was sized to span *a word*. But the join needs
enough matching **tokens** to be confident, and 1 second is 2–4 words at
conversational speed against a 2-word minimum match.

That is fragile in a specific way: Whisper transcribes a boundary region
differently on each side ("13" vs "thirteen"), one token mismatches, exact
prefix matching finds nothing, and the entire duplicate survives. The minimum
cannot be raised to compensate, because there are not enough words to match.

## The design

### Widen the overlap to 2 seconds

Not because a word needs 2 seconds, but because the **algorithm** does.

| overlap | words | what it enables |
|---|---|---|
| 1s | 2–4 | exact match only, minimum 2, fragile |
| **2s** | **~5** | 3-word minimum, fuzzy alignment viable |
| 3s | 7–8 | robust, 37% STT overhead for little gain |

At ~5 words the join can move from *longest exact suffix/prefix* to fuzzy
alignment (`difflib.SequenceMatcher`), which finds the longest matching block
even when one word inside it differs. With 3 words that is impossible — a single
mismatch destroys the only match available.

The unusual property: more overlap makes the join **simultaneously more
aggressive and safer**. A longer required match means genuine repetition
("count, count, count with me" — from a real recording) stops resembling a seam,
while a true 5-word overlap still joins cleanly. Those normally trade against
each other.

**Cost:** 10s transcribed per 8s of speech, 25% more STT. Nothing waits on it —
this mode never speaks — so it is CPU only.

### Add a session id to the capture

The one change that cannot be applied retroactively. Data recorded without it is
permanently ambiguous about which utterances belong together, so it must land
before a large capture is made rather than after.

### Stitch at analysis time, not capture time

The live de-duplication stays deliberately conservative: it sees only the
previous chunk, and a mistake destroys speech irrecoverably.

The script has neither constraint. It sees the whole sequence, and it is
re-runnable — improving the join later becomes a re-run rather than a re-record.
That is why every entry carries `transcript_raw`.

**Capture raw, join at analysis time.** The live dedup serves the screen; the
script produces the research artifact.

### Report the joins, do not just perform them

In a research transcript a wrongly-deleted word is worse than a duplicated one.
The script reports what it removed at each seam so the joins can be inspected
rather than trusted — the same principle as recording gated chunks instead of
silently dropping them.

## Shape

```
jspace.jsonl
  → group by session_id          (a lookup, not an inference)
  → sort each group by seq       (file order is arrival order)
  → drop gated entries           (silence / no-speech rejections)
  → join transcript_raw with fuzzy overlap removal
  → one continuous transcript per session, plus a join report
```

## What this is not

- **Not a re-transcription.** The script never calls STT; it operates purely on
  recorded text, so it is deterministic and free to re-run.
- **Not a fix for lost audio.** Anything the capture missed stays missing. The
  script's job is to stop the *record* from being garbled, not to recover speech.
- **Not a hallucination filter.** Gated entries are excluded because they were
  already rejected upstream, on signals (energy, `no_speech_prob`) that no
  text-level pass can reconstruct.
