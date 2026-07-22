import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  applyAnalysisNavigation,
  createAnalysisConversationState,
  parseAnalysisArtifact,
  resolveAnalysisFollowUp,
} from '../src/agent/analysis-navigation';
import { Memory } from '../src/agent/memory';
import { getMemoryDir } from '../src/utils/helpers';
import { analysisArtifactFixture } from './fixtures/analysis-artifact';

describe('analysis artifact navigation', () => {
  it('parses the validated platform artifact and rejects broken references', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture());
    expect(artifact).toMatchObject({
      artifactId: 'analysis_task_1',
      recommendedTopicId: 'topic_validation',
    });
    expect(artifact?.topics[0]).toMatchObject({ label: 'Acquisition risk' });

    const broken = analysisArtifactFixture();
    broken.topics[0].finding_ids = ['missing_finding'];
    expect(parseAnalysisArtifact(broken)).toBeUndefined();

    const cyclic = analysisArtifactFixture();
    cyclic.topics[0].child_topic_ids = ['topic_validation'];
    cyclic.topics[2].child_topic_ids = ['topic_acquisition'];
    expect(parseAnalysisArtifact(cyclic)).toBeUndefined();
  });

  it('uses the exact offered order for ordinal choices', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const state = createAnalysisConversationState(artifact, artifact.taskId);
    const decision = resolveAnalysisFollowUp('Tell me about the second one.', state)!;

    expect(decision).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_economics'],
      confidence: 1,
      reason: 'ordinal_menu_selection',
    });
    expect(applyAnalysisNavigation(state, decision)).toMatchObject({
      activeTopicId: 'topic_economics',
      visitedTopicIds: ['topic_economics'],
    });
  });

  it('resolves aliases, evidence requests, menu overflow, and changed scenarios', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const initial = createAnalysisConversationState(artifact, artifact.taskId);

    expect(resolveAnalysisFollowUp('Tell me more about distribution.', initial)).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_acquisition'],
      reason: 'topic_label_or_alias',
    });

    const active = { ...initial, activeTopicId: 'topic_acquisition' };
    expect(resolveAnalysisFollowUp('What evidence supports that?', active)).toMatchObject({
      action: 'show_evidence',
      selectedTopicIds: ['topic_acquisition'],
    });
    expect(resolveAnalysisFollowUp('What other topics are there?', active)).toMatchObject({
      action: 'list_topics',
      selectedTopicIds: ['topic_positioning'],
      reason: 'menu_overflow',
    });
    expect(resolveAnalysisFollowUp('Explain the economics measurement.', active)).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_economics'],
      reason: 'lexical_topic_match',
    });
    expect(resolveAnalysisFollowUp('Give me the complete written report.', active)).toMatchObject({
      action: 'render_report',
      selectedTopicIds: [
        'topic_acquisition',
        'topic_economics',
        'topic_validation',
        'topic_positioning',
      ],
    });
    expect(
      resolveAnalysisFollowUp('What if paid acquisition is unavailable?', active)
    ).toMatchObject({
      action: 'reanalyze',
      reason: 'changed_analytical_question',
    });
  });

  it('uses stable history for next and back controls', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const initial = createAnalysisConversationState(artifact, artifact.taskId);
    const first = applyAnalysisNavigation(initial, {
      action: 'open_topic',
      selectedTopicIds: ['topic_acquisition'],
      confidence: 1,
      reason: 'test',
    });
    const nextDecision = resolveAnalysisFollowUp('Next one.', first)!;
    expect(nextDecision).toMatchObject({ selectedTopicIds: ['topic_economics'] });
    const second = applyAnalysisNavigation(first, nextDecision);
    expect(resolveAnalysisFollowUp('Go back.', second)).toMatchObject({
      action: 'open_topic',
      selectedTopicIds: ['topic_acquisition'],
      reason: 'previous_visited_topic',
    });
  });

  it('does not hijack an unrelated source lookup', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const state = createAnalysisConversationState(artifact, artifact.taskId);
    expect(
      resolveAnalysisFollowUp('How many phases are named in the document?', state)
    ).toBeUndefined();
  });
});

describe('analysis conversation state sidecar', () => {
  const originalHome = process.env.HOME;
  let testHome: string;

  beforeEach(() => {
    testHome = mkdtempSync(join(tmpdir(), 'nano-claw-analysis-'));
    process.env.HOME = testHome;
  });

  afterEach(() => {
    if (originalHome === undefined) delete process.env.HOME;
    else process.env.HOME = originalHome;
    rmSync(testHome, { recursive: true, force: true });
  });

  it('persists separately from transcript memory and deletes with the session', () => {
    const artifact = parseAnalysisArtifact(analysisArtifactFixture())!;
    const memory = new Memory('analysis-session');
    memory.addMessage({ role: 'user', content: 'Review this strategy.' });
    memory.setAnalysisState(createAnalysisConversationState(artifact, artifact.taskId));

    const transcriptPath = join(getMemoryDir(), 'analysis-session.json');
    const analysisPath = join(getMemoryDir(), 'analysis-session.analysis.json');
    expect(existsSync(transcriptPath)).toBe(true);
    expect(existsSync(analysisPath)).toBe(true);
    expect(new Memory('analysis-session').getAnalysisState()?.artifact.artifactId).toBe(
      'analysis_task_1'
    );
    expect(new Memory('analysis-session').getAnalysisState()?.analysisStyle).toBe('topic_map');

    memory.delete();
    expect(existsSync(transcriptPath)).toBe(false);
    expect(existsSync(analysisPath)).toBe(false);
  });

  it('persists the selected analysis style and infers it for older sidecars', () => {
    const raw = analysisArtifactFixture();
    raw.prompt_version = 'strategy_review_principle_graph_experimental_v1';
    const artifact = parseAnalysisArtifact(raw)!;
    const state = createAnalysisConversationState(
      artifact,
      artifact.taskId,
      'principle_graph'
    );
    const memory = new Memory('principle-session');
    memory.setAnalysisState(state);

    expect(new Memory('principle-session').getAnalysisState()?.analysisStyle).toBe(
      'principle_graph'
    );
  });
});
