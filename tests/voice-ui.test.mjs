import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

await import('../voice/web/voice-ui.js');
const VoiceUI = globalThis.VoiceUI;
assert.ok(VoiceUI, 'VoiceUI must be exported to the global scope');

const ui = {
  groups: [
    {
      label: 'American English',
      voices: [{ id: 'af_heart', name: 'Heart', engine: 'kokoro', grade: 'A' }],
    },
    {
      label: 'Piper — fast',
      voices: [{ id: 'en_US-lessac-medium', name: 'Lessac (US)', engine: 'piper', grade: null }],
    },
  ],
  default: 'af_heart',
};

const grouped = VoiceUI.groupVoices(ui);
assert.equal(grouped.length, 2);
assert.equal(grouped[0].label, 'American English');
assert.equal(grouped[0].options[0].id, 'af_heart');
assert.equal(grouped[0].options[0].label, 'Heart (A)', 'Kokoro options show the grade');
assert.equal(grouped[1].options[0].label, 'Lessac (US)', 'Piper options have no grade');

assert.equal(VoiceUI.sampleTextForLang('e').startsWith('Hola'), true);
assert.equal(VoiceUI.sampleTextForLang('a').length > 0, true);

const voiceHtml = await readFile(new URL('../voice/web/index.html', import.meta.url), 'utf8');
const appSource = await readFile(new URL('../voice/web/app.js', import.meta.url), 'utf8');
assert.match(voiceHtml, /talking-cube\.js/, 'voice UI must reference the Talking Cube module');
assert.match(voiceHtml, /id="configuration-sidebar"/, 'configuration must live in the left rail');
assert.match(voiceHtml, /id="transcription-panel"/, 'transcription must live in the right rail');
assert.match(voiceHtml, /id="context-panel"/, 'sidebar must expose the context panel');
assert.match(
  voiceHtml,
  /id="context-collections"/,
  'context panel must show the loaded knowledge collections'
);
assert.match(voiceHtml, /id="latency-overall"/, 'sidebar must expose pipeline latency');
assert.doesNotMatch(voiceHtml, /id="settings-btn"/, 'the settings popover trigger must be removed');
assert.match(voiceHtml, /<strong>HYPERRIFF<\/strong>/, 'the masthead must use HYPERRIFF');
assert.match(
  voiceHtml,
  /id="app-version"[^>]*>v\d+\.\d+\.\d+<\/small>/,
  'the current release must remain visibly versioned in the masthead'
);
assert.match(
  appSource,
  /appVersionBadge\.textContent = 'v' \+ appVersion/,
  'the visible release must reconcile with the running server version'
);
assert.doesNotMatch(
  appSource,
  /-pass ceiling/,
  'the deep UI must not present a conditional pass budget as scheduled work'
);
assert.match(
  appSource,
  /waiting for model response · up to [\s\S]*passes if needed/,
  'a long reasoning call must explain why its pass counter has not advanced'
);
assert.match(
  appSource,
  /backend heartbeat current/,
  'heartbeats must be labeled as connectivity rather than reasoning progress'
);

const flowSelect = voiceHtml.match(/<select id="flow-select"[\s\S]*?<\/select>/)?.[0] || '';
const flowOptions = [
  ...flowSelect.matchAll(/<option value="([^"]+)"(?: selected)?>([^<]+)<\/option>/g),
].map((match) => [match[1], match[2]]);
assert.deepEqual(flowOptions, [
  ['none', 'None'],
  ['spacechannel', 'HYPERRIFF'],
  ['intelligence', 'Document Intelligence'],
  ['replicantpm', 'Replicant PM'],
  ['scheduler', 'Plumber Scheduler'],
]);
assert.match(
  flowSelect,
  /<option value="spacechannel" selected>HYPERRIFF<\/option>/,
  'assistant mode must render HYPERRIFF instead of a blank value before fetch resolves'
);
assert.match(
  appSource,
  /Pipeline\.applyModelOptions\(\s*flowSelect,/,
  'assistant mode must reuse the non-blank resilient dropdown helper'
);
assert.match(
  appSource,
  /localStorage\.getItem\(LS_BARGE_IN_ENABLED\) === 'true'/,
  'experimental browser barge-in must require an explicit listener opt-in'
);
assert.match(
  appSource,
  /nanoclaw\.bargeIn\.v3\.enabled/,
  'old inherited barge-in preferences must be reset to the safe v3 default'
);
assert.match(
  voiceHtml,
  /id="barge-in-hint"[\s\S]*?Off by default/,
  'the browser control must explain its safe default and manual fallback'
);
assert.match(
  appSource,
  /LS_BARGE_IN_SENSITIVITY\) \|\| 'low'/,
  'opt-in browser barge-in must begin at the least sensitive setting'
);
assert.match(
  voiceHtml,
  /id="analysis-style-toggle"[\s\S]*?experimental principle-graph analysis/i,
  'the configuration rail must expose the principle-graph experiment'
);
assert.match(
  voiceHtml,
  /id="speech-preparation-toggle"[\s\S]*?checked/,
  'prepared natural delivery must be visible and enabled by default'
);
assert.match(
  appSource,
  /nanoclaw\.speechPreparation\.v1\.enabled/,
  'the prepared/raw comparison must persist independently of the voice model'
);
assert.match(
  appSource,
  /set_speech_mode[\s\S]*?prepared[\s\S]*?raw/,
  'the browser must send its prepared/raw selection to the voice session'
);
assert.match(
  appSource,
  /case 'speech_plan':[\s\S]*?compilerVersion/,
  'the browser must display the compiler version confirmed by each speech plan'
);
assert.match(
  appSource,
  /set_analysis_style[\s\S]*?analysisStyle: currentAnalysisStyle/,
  'the browser must send the selected analysis style to the voice session'
);
assert.ok(
  appSource.indexOf('// Populate independently on page load') <
    appSource.indexOf('function connect()'),
  'assistant modes must load on page load, independently of socket open'
);

console.log('voice-ui tests passed');
