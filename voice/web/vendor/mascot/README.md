# Vendored: computer-mascot character renderer

Synced from `~/src/computer-agent-animation` on **2026-07-30**.

Vendored rather than added as a build dependency because `voice/web/` is served
as plain ES modules with no bundler. `talking-cube.js` was vendored the same way
from `~/src/talking_visualization`; this follows that precedent.

## What was copied

| From | To | Notes |
|---|---|---|
| `src/*.js` (14 modules) | `./` | `src/app.js` deliberately NOT copied — it is the upstream demo shell. nano-claw owns its own mount (`voice/web/mascot-renderer.js`), exactly as the cube port did. |
| `public/rig.json` | `public/` | Asset paths inside are relative **to this file**, not to the page. `mascot-renderer.js` rewrites them. |
| `public/character-director.schema.json` | `public/` | |
| `public/layers/body{,@2x}.webp` | `public/layers/` | ~1.2 MB. The 26 MB in the upstream `assets/` tree is source art, generated intermediates, and reference material — none of it ships. |

## Why this drops in cleanly

`nano-claw-adapter.js` was written against nano-claw's *actual* renderer call
sites, not a guess. Its `NANO_CLAW_RENDERER_METHODS` list and per-method usage
counts match a grep of `voice/web/app.js` exactly:

    pulse 10, setColors 5, importProfile 4, setSpeaking 2, setPattern 2,
    disconnectAnalyser 2, connectAnalyser 2, setPanelOpen 1, pushAudioFrame 1,
    getProfile 1, destroy 1, configure 1

It also reuses nano-claw's emotion vocabulary verbatim — the same eleven names —
so one command stream drives either renderer. Cube-specific calls (`setColors`,
`setPattern`) are deliberate no-ops returning `true`: the mascot is monochrome
line art, and throwing would break a host entitled to call them.

## Drift is a real risk — the guard is a test

The shim is verified against **today's** `app.js`. Adding a renderer method call
in nano-claw silently breaks the mascot with no error at the call site.

`tests/mascot-renderer-contract.test.ts` runs the adapter's own `rendererGaps()`
and `coverageGaps()`. **Treat a failure there as a genuine break, not a flaky
test** — it means the two repos have diverged.

## Re-syncing

Copy `src/*.js` (minus `app.js`) and `public/` again, then run the contract test.
If it fails, the upstream API changed and `voice/web/mascot-renderer.js` needs
updating too. Update the date at the top of this file.
