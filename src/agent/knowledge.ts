import { readFileSync, statSync } from 'fs';
import { Config } from '../config/schema';
import { logger } from '../utils/logger';

/**
 * Knowledge loader — injects site digests (built by scripts/build_knowledge.py)
 * into the system prompt so personas answer from crawled data with zero tool
 * round-trips (tool calls pause the voice loop for approval).
 *
 * Files are re-read only when their mtime changes, so a cron re-crawl on the
 * host is picked up on the next turn without restarting the server.
 */

interface CacheEntry {
  mtimeMs: number;
  content: string;
}

const cache = new Map<string, CacheEntry>();

/**
 * Knowledge file paths: config agents.defaults.knowledgeFiles plus the
 * NANO_CLAW_KNOWLEDGE env var (comma-separated), env last so it can extend
 * a baked-in config from `docker run -e`.
 */
export function resolveKnowledgeFiles(config: Config): string[] {
  const fromConfig = config.agents?.defaults?.knowledgeFiles || [];
  const fromEnv = (process.env.NANO_CLAW_KNOWLEDGE || '')
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);
  return [...new Set([...fromConfig, ...fromEnv])];
}

/**
 * Read knowledge files (mtime-cached). Missing/unreadable files are logged
 * and skipped — a stale or absent digest must never take the assistant down.
 */
export function loadKnowledge(paths: string[]): string {
  const parts: string[] = [];
  for (const path of paths) {
    try {
      const { mtimeMs } = statSync(path);
      let entry = cache.get(path);
      if (!entry || entry.mtimeMs !== mtimeMs) {
        entry = { mtimeMs, content: readFileSync(path, 'utf-8').trim() };
        cache.set(path, entry);
        logger.info({ path, chars: entry.content.length }, 'Knowledge file loaded');
      }
      if (entry.content) parts.push(entry.content);
    } catch (error) {
      logger.warn({ path, error: (error as Error).message }, 'Knowledge file unavailable');
    }
  }
  return parts.join('\n\n');
}
