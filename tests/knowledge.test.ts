import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync, utimesSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { loadKnowledge, resolveKnowledgeFiles } from '../src/agent/knowledge';
import { ContextBuilder } from '../src/agent/context';

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
