# LuxTTS patches

`LuxTTS/` is a vendored upstream clone and is gitignored, so local edits to it
are NOT tracked by the nano-claw repo. Any change we make to the model code
lives here as a patch and must be re-applied after a fresh LuxTTS checkout.

## tail-pad-widen.patch
Adds `tail_pad_frames` to `ZipVoice.sample()` and threads it through
`generate()`. It appends N render frames that repeat the final token's
condition, so the last word finishes its release instead of being clipped by
the ratio duration predictor — at normal speed, no slowdown. The Lux server
(`lux-service/server.py`, tracked) defaults it to 24 and accepts a per-request
`tail_pad_frames` override.

Apply from `lux-service/`:

    cd LuxTTS && git apply ../patches/tail-pad-widen.patch

Verify: a synth of "never pitch a promise." round-trips through Whisper with
"promise" intact (clipped to "prom" without the patch).
