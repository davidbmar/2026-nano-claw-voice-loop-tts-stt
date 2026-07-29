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
 * Merge environment variables into configuration
 */
export function mergeEnvConfig(config: Config): Config {
  // Check for provider API keys in environment variables
  const envProviders: Record<string, { apiKey: string }> = {};

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
  const intelligenceTimeoutValue = Number(process.env.NANO_CLAW_INTELLIGENCE_TIMEOUT_MS);
  const intelligenceTimeoutMs = Number.isInteger(intelligenceTimeoutValue)
    ? intelligenceTimeoutValue
    : undefined;
  const intelligenceCollections = process.env.NANO_CLAW_INTELLIGENCE_COLLECTIONS?.split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  const existingIntelligence = config.agents?.defaults?.intelligence;
  const deepEnabledValue = process.env.NANO_CLAW_DEEP_REASONING?.trim().toLowerCase();
  const deepEnabled = deepEnabledValue
    ? ['1', 'true', 'yes'].includes(deepEnabledValue)
    : undefined;
  const deepThresholdValue = Number(process.env.NANO_CLAW_DEEP_THRESHOLD);
  const deepThreshold = Number.isInteger(deepThresholdValue) ? deepThresholdValue : undefined;
  const deepTimeoutValue = Number(process.env.NANO_CLAW_DEEP_TIMEOUT_MS);
  const deepTimeoutMs = Number.isInteger(deepTimeoutValue) ? deepTimeoutValue : undefined;
  const analysisStyle = process.env.NANO_CLAW_ANALYSIS_STYLE?.trim();
  const hasDeepOverride =
    deepEnabled !== undefined ||
    process.env.NANO_CLAW_DEEP_ROUTING !== undefined ||
    deepThreshold !== undefined ||
    deepTimeoutMs !== undefined ||
    analysisStyle !== undefined;
  const deepReasoning = hasDeepOverride
    ? {
        ...existingIntelligence?.deepReasoning,
        ...(deepEnabled !== undefined && { enabled: deepEnabled }),
        ...(process.env.NANO_CLAW_DEEP_ROUTING && {
          routingMode: process.env.NANO_CLAW_DEEP_ROUTING,
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
            intelligence,
          },
        }
      : existingProfiles;

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
  });

  logger.info(
    {
      toolsEnabled: merged.tools.enabled,
      registeredTools: merged.tools.enabled ? [...BUILT_IN_TOOL_NAMES] : [],
    },
    'Tool gate resolved'
  );

  return merged;
}

/**
 * Get configuration with environment variable overrides
 */
export function getConfig(): Config {
  const config = loadConfig();
  return mergeEnvConfig(config);
}
