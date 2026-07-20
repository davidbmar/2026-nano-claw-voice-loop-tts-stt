import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, utimesSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { loadKnowledge, resolveKnowledgeFiles } from '../src/agent/knowledge';
import { ContextBuilder } from '../src/agent/context';
import { resolveAgentProfile } from '../src/api/server';
import { ConfigSchema } from '../src/config/schema';

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'knowledge-test-'));
  delete process.env.NANO_CLAW_KNOWLEDGE;
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  delete process.env.NANO_CLAW_KNOWLEDGE;
});

describe('resolveKnowledgeFiles', () => {
  it('merges config paths with the NANO_CLAW_KNOWLEDGE env var, deduped', () => {
    process.env.NANO_CLAW_KNOWLEDGE = '/b.md, /c.md ,/a.md';
    const cfg: any = { agents: { defaults: { knowledgeFiles: ['/a.md'] } } };
    expect(resolveKnowledgeFiles(cfg)).toEqual(['/a.md', '/b.md', '/c.md']);
  });

  it('returns empty when nothing is configured', () => {
    expect(resolveKnowledgeFiles({} as any)).toEqual([]);
  });
});

describe('assistant profile selection', () => {
  it('accepts and seeds the Space Channel and Replicant PM profile registry', () => {
    const raw = JSON.parse(
      readFileSync(new URL('../docker/default-config.json', import.meta.url), 'utf-8')
    );
    const seeded = ConfigSchema.parse(raw);

    expect(Object.keys(seeded.agents.profiles || {})).toEqual(['spacechannel', 'replicantpm']);
    expect(seeded.agents.profiles?.spacechannel.knowledgeFiles).toEqual([
      '/app/sites/spacechannel/knowledge.md',
    ]);
    expect(seeded.agents.profiles?.spacechannel.systemPrompt).toBe(
      seeded.agents.defaults?.systemPrompt
    );
    expect(seeded.agents.profiles?.replicantpm).toMatchObject({
      label: 'Replicant PM',
      knowledgeFiles: ['/app/sites/replicantpm/knowledge.md'],
    });
    expect(seeded.agents.profiles?.replicantpm.systemPrompt).toContain(
      'You are Riley, the Replicant PM assistant'
    );
  });

  it('uses a selected profile prompt and only that profile knowledge file', () => {
    const selectedPath = join(dir, 'selected.md');
    const otherPath = join(dir, 'other.md');
    const globalPath = join(dir, 'global.md');
    writeFileSync(selectedPath, 'SELECTED PROFILE FACT');
    writeFileSync(otherPath, 'OTHER PROFILE FACT');
    writeFileSync(globalPath, 'GLOBAL GLOB FACT');
    process.env.NANO_CLAW_KNOWLEDGE = globalPath;

    const config = ConfigSchema.parse({
      agents: {
        defaults: { systemPrompt: 'Default assistant prompt' },
        profiles: {
          selected: {
            label: 'Selected',
            systemPrompt: 'Selected profile prompt',
            knowledgeFiles: [selectedPath],
          },
          other: {
            label: 'Other',
            systemPrompt: 'Other profile prompt',
            knowledgeFiles: [otherPath],
          },
        },
      },
    });
    const profile = resolveAgentProfile(config, 'selected');
    const prompt = new ContextBuilder({ model: 'test', ...profile }).buildSystemPrompt([], []);

    expect(profile).toEqual({
      systemPrompt: 'Selected profile prompt',
      knowledgeFiles: [selectedPath],
    });
    expect(prompt).toContain('Selected profile prompt');
    expect(prompt).toContain('SELECTED PROFILE FACT');
    expect(prompt).not.toContain('OTHER PROFILE FACT');
    expect(prompt).not.toContain('GLOBAL GLOB FACT');
  });

  it('uses the default prompt and no site knowledge for none', () => {
    const globalPath = join(dir, 'global.md');
    writeFileSync(globalPath, 'GLOBAL GLOB FACT');
    process.env.NANO_CLAW_KNOWLEDGE = globalPath;
    const config = ConfigSchema.parse({
      agents: { defaults: { systemPrompt: 'Default assistant prompt' } },
    });
    const profile = resolveAgentProfile(config, 'none');
    const prompt = new ContextBuilder({ model: 'test', ...profile }).buildSystemPrompt([], []);

    expect(profile).toEqual({
      systemPrompt: 'Default assistant prompt',
      knowledgeFiles: [],
    });
    expect(prompt).toContain('Default assistant prompt');
    expect(prompt).not.toContain('GLOBAL GLOB FACT');
    expect(prompt).not.toContain('## Knowledge');
  });

  it('preserves global knowledge fallback when profiles are absent or unknown', () => {
    process.env.NANO_CLAW_KNOWLEDGE = '/env.md';
    const config = ConfigSchema.parse({
      agents: {
        defaults: {
          systemPrompt: 'Default assistant prompt',
          knowledgeFiles: ['/config.md'],
        },
      },
    });

    expect(resolveAgentProfile(config)).toEqual({
      systemPrompt: 'Default assistant prompt',
      knowledgeFiles: ['/config.md', '/env.md'],
    });
    expect(resolveAgentProfile(config, 'unknown')).toEqual({
      systemPrompt: 'Default assistant prompt',
      knowledgeFiles: ['/config.md', '/env.md'],
    });
  });
});

describe('loadKnowledge', () => {
  it('reads files and skips missing ones without throwing', () => {
    const good = join(dir, 'good.md');
    writeFileSync(good, '# Site facts\nfact one');
    const out = loadKnowledge([good, join(dir, 'missing.md')]);
    expect(out).toContain('fact one');
  });

  it('re-reads a file when its mtime changes', () => {
    const path = join(dir, 'k.md');
    writeFileSync(path, 'version one');
    expect(loadKnowledge([path])).toContain('version one');

    writeFileSync(path, 'version two');
    // Force a distinct mtime — same-millisecond writes would hide the change
    utimesSync(path, new Date(), new Date(Date.now() + 5000));
    expect(loadKnowledge([path])).toContain('version two');
  });
});

describe('ContextBuilder knowledge injection', () => {
  it('places knowledge between the persona and the timestamp (cacheable prefix)', () => {
    const path = join(dir, 'site.md');
    writeFileSync(path, '# Knowledge: example.com\nThe next launch is X.');
    const builder = new ContextBuilder({
      model: 'test',
      systemPrompt: 'You are the persona.',
      knowledgeFiles: [path],
    } as any);
    const prompt = builder.buildSystemPrompt([], []);

    const persona = prompt.indexOf('You are the persona.');
    const knowledge = prompt.indexOf('The next launch is X.');
    const time = prompt.indexOf('Current time:');
    expect(persona).toBeGreaterThanOrEqual(0);
    expect(knowledge).toBeGreaterThan(persona);
    expect(time).toBeGreaterThan(knowledge);
  });

  it('injects an unavailability note when configured files are missing', () => {
    const builder = new ContextBuilder({
      model: 'test',
      knowledgeFiles: [join(dir, 'nope.md')],
    } as any);
    const prompt = builder.buildSystemPrompt([], []);
    expect(prompt).toContain('knowledge base is unavailable');
    expect(prompt).not.toContain('## Knowledge');
  });

  it('adds nothing when no knowledge is configured', () => {
    const builder = new ContextBuilder({ model: 'test' } as any);
    const prompt = builder.buildSystemPrompt([], []);
    expect(prompt).not.toContain('## Knowledge');
    expect(prompt).not.toContain('knowledge base is unavailable');
  });
});
