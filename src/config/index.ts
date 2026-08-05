import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { Config, ConfigSchema } from './schema';
import { getConfigPath, getHomeDir } from '../utils/helpers';
import { ConfigError } from '../utils/errors';
import { logger } from '../utils/logger';

// Re-export getConfigDir for external use
export { getConfigDir } from '../utils/helpers';

const EXPLICIT_TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);
const EXPLICIT_FALSE_VALUES = new Set(['0', 'false', 'no', 'off']);
const BUILT_IN_TOOL_NAMES = ['shell', 'read_file', 'write_file'] as const;

/**
 * Parse an environment flag that gates a dangerous capability.
 *
 * Unset and empty values are always off. Recognized values are
 * case-insensitive, but whitespace is not trimmed so deployment mistakes fail
 * loudly instead of silently enabling a capability. Configuration examples
 * should spell values as the words "true" or "false".
 */
export function requireExplicitBoolean(name: string, value: string | undefined): boolean {
  if (value === undefined || value === '') return false;

  const normalized = value.toLowerCase();
  if (EXPLICIT_TRUE_VALUES.has(normalized)) return true;
  if (EXPLICIT_FALSE_VALUES.has(normalized)) return false;

  throw new ConfigError(
    `${name} must be one of true, false, 1, 0, yes, no, on, or off; received ${JSON.stringify(value)}`
  );
}

/**
 * Load configuration from file
 */
export function loadConfig(): Config {
  const configPath = getConfigPath();

  if (!existsSync(configPath)) {
    throw new ConfigError(
      `Configuration file not found at ${configPath}. Please run 'nano-claw onboard' first.`
    );
  }

  try {
    const configData = readFileSync(configPath, 'utf-8');
    const configJson = JSON.parse(configData) as unknown;
    const config = ConfigSchema.parse(configJson);
    logger.debug({ config }, 'Configuration loaded');
    return config;
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new ConfigError(`Invalid JSON in configuration file: ${error.message}`);
    }
    throw error;
  }
}

/**
 * Save configuration to file
 */
export function saveConfig(config: Config): void {
  const configPath = getConfigPath();
  const homeDir = getHomeDir();

  // Ensure directory exists
  if (!existsSync(homeDir)) {
    mkdirSync(homeDir, { recursive: true });
  }

  try {
    const configJson = JSON.stringify(config, null, 2);
    writeFileSync(configPath, configJson, 'utf-8');
    logger.info({ path: configPath }, 'Configuration saved');
  } catch (error) {
    throw new ConfigError(`Failed to save configuration: ${(error as Error).message}`);
  }
}

/**
 * Create default configuration
 */
export function createDefaultConfig(): Config {
  return ConfigSchema.parse({
    providers: {},
    agents: {
      defaults: {
        model: 'anthropic/claude-opus-4-5',
        temperature: 0.7,
        maxTokens: 4096,
      },
    },
    tools: {
      enabled: false,
      restrictToWorkspace: false,
    },
    channels: {},
  });
}

/**
 * An integer from the environment, or undefined so the schema default applies.
 *
 * `Number('')` is 0 and `Number.isInteger(0)` is true, so the obvious guard —
 * `Number.isInteger(Number(process.env.X))` — accepts an EMPTY string as a
 * legitimate zero. That is not hypothetical: `run.sh` passes 76 variables as
 * `-e VAR="$VAR"`, which sends an empty string when the host var is unset rather
 * than omitting the variable. Three of those feed schema fields with a minimum
 * (`deepReasoning.threshold` min 1, `deepReasoning.taskTimeoutMs` min 1000,
 * `intelligence.timeoutMs` min 10), so an unset var became 0, failed validation,
 * and took the WHOLE config down with it — the Node API never started, the
 * container died every three minutes, and the public console served 502 until a
 * human looked (2026-07-29 22:40, `logs/voice_watchdog.ALERT`).
 *
 * Treating empty as absent is what the callers already meant: fall back to the
 * documented default. Garbage like "abc" is still rejected, as before.
 */
function envInt(name: string): number | undefined {
  const raw = process.env[name]?.trim();
  if (!raw) return undefined;
  const value = Number(raw);
  return Number.isInteger(value) ? value : undefined;
}

/**
 * Merge environment variables into configuration
 */
export function mergeEnvConfig(config: Config): Config {
  // Check for provider API keys in environment variables
  const envProviders: Record<string, { apiKey?: string; apiBase?: string }> = {};

  // Ollama needs no key, but its endpoint IS deployment-specific: the local
  // Docker host, or a bigger box on the LAN holding models this machine has no
  // memory for. Keep it in env so moving the local model to another host is a
  // restart with a changed variable, not a hand-edit of config.json inside a
  // running container. Note the model catalog is host-dependent — a host that
  // lacks a catalogued model 404s that entry (see src/agent/models.ts).
  const ollamaBase = process.env.NANO_CLAW_OLLAMA_BASE?.trim();
  if (ollamaBase) {
    envProviders.ollama = { apiBase: ollamaBase };
  }

  if (process.env.OPENROUTER_API_KEY) {
    envProviders.openrouter = { apiKey: process.env.OPENROUTER_API_KEY };
  }
  if (process.env.ANTHROPIC_API_KEY) {
    envProviders.anthropic = { apiKey: process.env.ANTHROPIC_API_KEY };
  }
  if (process.env.OPENAI_API_KEY) {
    envProviders.openai = { apiKey: process.env.OPENAI_API_KEY };
  }
  if (process.env.DEEPSEEK_API_KEY) {
    envProviders.deepseek = { apiKey: process.env.DEEPSEEK_API_KEY };
  }
  if (process.env.GROQ_API_KEY) {
    envProviders.groq = { apiKey: process.env.GROQ_API_KEY };
  }
  if (process.env.GEMINI_API_KEY) {
    envProviders.gemini = { apiKey: process.env.GEMINI_API_KEY };
  }
  if (process.env.DASHSCOPE_API_KEY) {
    envProviders.dashscope = { apiKey: process.env.DASHSCOPE_API_KEY };
  }
  if (process.env.MOONSHOT_API_KEY) {
    envProviders.moonshot = { apiKey: process.env.MOONSHOT_API_KEY };
  }
  if (process.env.ZHIPUAI_API_KEY) {
    envProviders.zhipu = { apiKey: process.env.ZHIPUAI_API_KEY };
  }
  if (process.env.MINIMAX_API_KEY) {
    envProviders.minimax = { apiKey: process.env.MINIMAX_API_KEY };
  }

  // Merge with existing config (env vars take precedence)
  const mergedProviders = { ...config.providers };
  for (const [key, value] of Object.entries(envProviders)) {
    mergedProviders[key as keyof typeof mergedProviders] = {
      ...(mergedProviders[key as keyof typeof mergedProviders] || {}),
      ...value,
    } as never;
  }

  // Dangerous capabilities require a positive enable at the process boundary.
  // The legacy negative flag remains a kill switch for one migration release.
  const enableTools = requireExplicitBoolean(
    'NANO_CLAW_ENABLE_TOOLS',
    process.env.NANO_CLAW_ENABLE_TOOLS
  );
  const disableTools = requireExplicitBoolean(
    'NANO_CLAW_DISABLE_TOOLS',
    process.env.NANO_CLAW_DISABLE_TOOLS
  );
  if (enableTools && disableTools) {
    throw new ConfigError(
      'NANO_CLAW_ENABLE_TOOLS and NANO_CLAW_DISABLE_TOOLS conflict: tools cannot be both enabled and disabled'
    );
  }
  const toolsEnabled = enableTools && !disableTools;

  const intelligenceUrl = process.env.NANO_CLAW_INTELLIGENCE_URL?.trim();
  const intelligenceEnabledValue = process.env.NANO_CLAW_INTELLIGENCE_ENABLED?.trim().toLowerCase();
  const intelligenceEnabled = intelligenceEnabledValue
    ? ['1', 'true', 'yes'].includes(intelligenceEnabledValue)
    : intelligenceUrl
      ? true
      : undefined;
  // Retrieval timeout: the schema default (750ms) is tuned for snappy voice
  // turns, but under GPU contention (local LLM generating while the platform
  // embeds the query) retrieval routinely exceeds it and the document-
  // intelligence mode silently answers without evidence. Deployments that
  // prioritize document access set this higher (voice pays the wait).
  const intelligenceTimeoutMs = envInt('NANO_CLAW_INTELLIGENCE_TIMEOUT_MS');
  const intelligenceCollections = process.env.NANO_CLAW_INTELLIGENCE_COLLECTIONS?.split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  const existingIntelligence = config.agents?.defaults?.intelligence;
  const deepEnabledValue = process.env.NANO_CLAW_DEEP_REASONING?.trim().toLowerCase();
  const deepEnabled = deepEnabledValue
    ? ['1', 'true', 'yes'].includes(deepEnabledValue)
    : undefined;
  const deepThreshold = envInt('NANO_CLAW_DEEP_THRESHOLD');
  const deepTimeoutMs = envInt('NANO_CLAW_DEEP_TIMEOUT_MS');
  // `|| undefined` so an EMPTY value is absent, not "the operator asked for the
  // empty string". run.sh sends unset host vars as empty (-e VAR="$VAR"), and
  // `''.trim()` is `''`, which is `!== undefined` — so without this an unset
  // NANO_CLAW_ANALYSIS_STYLE or NANO_CLAW_DEEP_ROUTING silently materialises a
  // whole deepReasoning block out of nothing.
  const analysisStyle = process.env.NANO_CLAW_ANALYSIS_STYLE?.trim() || undefined;
  const deepRouting = process.env.NANO_CLAW_DEEP_ROUTING?.trim() || undefined;
  const hasDeepOverride =
    deepEnabled !== undefined ||
    deepRouting !== undefined ||
    deepThreshold !== undefined ||
    deepTimeoutMs !== undefined ||
    analysisStyle !== undefined;
  const deepReasoning = hasDeepOverride
    ? {
        ...existingIntelligence?.deepReasoning,
        ...(deepEnabled !== undefined && { enabled: deepEnabled }),
        ...(deepRouting && {
          routingMode: deepRouting,
        }),
        ...(deepThreshold !== undefined && { threshold: deepThreshold }),
        ...(deepTimeoutMs !== undefined && { taskTimeoutMs: deepTimeoutMs }),
        ...(analysisStyle && { analysisStyle }),
      }
    : existingIntelligence?.deepReasoning;
  const hasIntelligenceOverride =
    intelligenceEnabled !== undefined ||
    intelligenceUrl !== undefined ||
    process.env.NANO_CLAW_INTELLIGENCE_TENANT !== undefined ||
    intelligenceCollections !== undefined ||
    process.env.NANO_CLAW_INTELLIGENCE_GROUNDING !== undefined ||
    intelligenceTimeoutMs !== undefined ||
    hasDeepOverride;
  const intelligence = hasIntelligenceOverride
    ? {
        ...existingIntelligence,
        ...(intelligenceEnabled !== undefined && { enabled: intelligenceEnabled }),
        ...(intelligenceUrl && { apiUrl: intelligenceUrl }),
        ...(process.env.NANO_CLAW_INTELLIGENCE_TENANT && {
          tenantId: process.env.NANO_CLAW_INTELLIGENCE_TENANT,
        }),
        ...(intelligenceCollections && { collectionIds: intelligenceCollections }),
        ...(process.env.NANO_CLAW_INTELLIGENCE_GROUNDING && {
          groundingMode: process.env.NANO_CLAW_INTELLIGENCE_GROUNDING,
        }),
        ...(intelligenceTimeoutMs !== undefined && { timeoutMs: intelligenceTimeoutMs }),
        ...(deepReasoning && { deepReasoning }),
      }
    : existingIntelligence;
  const intelligenceProfileId = process.env.NANO_CLAW_INTELLIGENCE_PROFILE?.trim();
  const existingProfiles = config.agents?.profiles;
  const profiles =
    intelligence && intelligenceProfileId && existingProfiles?.[intelligenceProfileId]
      ? {
          ...existingProfiles,
          [intelligenceProfileId]: {
            ...existingProfiles[intelligenceProfileId],
            // Environment intelligence config supplies the default for the
            // selected profile; an explicit profile block owns its tenant and
            // must not be replaced by process-wide settings.
            intelligence: existingProfiles[intelligenceProfileId].intelligence ?? intelligence,
          },
        }
      : existingProfiles;

  // Decision Core shadow mode: positive enable, mirroring the tools gate.
  const decisionShadowValue = process.env.NANO_CLAW_DECISION_SHADOW?.trim().toLowerCase();
  const decisionShadowEnabled = decisionShadowValue
    ? ['1', 'true', 'yes'].includes(decisionShadowValue)
    : undefined;
  const decisionCoreRoot = process.env.DECISION_CORE_ROOT?.trim() || undefined;
  // Per-line domain pins, same env-map discipline as NANO_CLAW_PHONE_MODE_PINS:
  // bad JSON is dropped with a warning — a typo must not take the config down.
  let decisionDomainPins: Record<string, string> | undefined;
  const rawDomainPins = process.env.NANO_CLAW_DECISION_DOMAIN_PINS?.trim();
  if (rawDomainPins) {
    try {
      const parsed = JSON.parse(rawDomainPins) as unknown;
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        decisionDomainPins = Object.fromEntries(
          Object.entries(parsed as Record<string, unknown>).filter(
            ([, value]) => typeof value === 'string'
          )
        ) as Record<string, string>;
      }
    } catch {
      logger.warn(
        { raw: rawDomainPins },
        'NANO_CLAW_DECISION_DOMAIN_PINS is not valid JSON; ignoring'
      );
    }
  }

  const merged = ConfigSchema.parse({
    ...config,
    providers: mergedProviders,
    agents: {
      ...config.agents,
      defaults: {
        ...config.agents?.defaults,
        ...(intelligence && { intelligence }),
      },
      ...(profiles && { profiles }),
    },
    tools: {
      ...config.tools,
      enabled: toolsEnabled,
    },
    decisionCore: {
      ...config.decisionCore,
      ...(decisionShadowEnabled !== undefined && { shadowEnabled: decisionShadowEnabled }),
      ...(decisionCoreRoot && { root: decisionCoreRoot }),
      ...(decisionDomainPins && { domainPins: decisionDomainPins }),
    },
  });

  logger.info(
    {
      toolsEnabled: merged.tools.enabled,
      registeredTools: merged.tools.enabled ? [...BUILT_IN_TOOL_NAMES] : [],
    },
    'Tool gate resolved'
  );

  if (merged.decisionCore.shadowEnabled) {
    logger.info(
      { root: merged.decisionCore.root ?? '(default)' },
      'Decision Core shadow mode enabled'
    );
  }

  return merged;
}

/**
 * Get configuration with environment variable overrides
 */
export function getConfig(): Config {
  const config = loadConfig();
  return mergeEnvConfig(config);
}
