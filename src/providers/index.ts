import { Config } from '../config/schema';
import { Message, LLMResponse, ToolDefinition, ProviderConfig, StreamEvent } from '../types';
import { ProviderError } from '../utils/errors';
import { logger } from '../utils/logger';
import { BaseProvider, OpenRouterProvider, AnthropicProvider, OpenAIProvider } from './base';
import { findProviderByModel } from './registry';
import { completeWithFallback, streamWithFallback } from './fallback';

/** Gateway providers that can route an arbitrary model id (so a fallback model
 * is reachable even without its own direct provider key). */
const GATEWAY_PROVIDERS = ['openrouter', 'aihubmix'];
const DEFAULT_FALLBACK_TIMEOUT_MS = 4000;

/**
 * Provider manager - handles provider selection and instantiation
 */
export class ProviderManager {
  private config: Config;
  private providerCache: Map<string, BaseProvider> = new Map();

  constructor(config: Config) {
    this.config = config;
  }

  /**
   * Get or create provider instance
   */
  private getProviderInstance(providerName: string): BaseProvider {
    if (this.providerCache.has(providerName)) {
      return this.providerCache.get(providerName)!;
    }

    const providerConfig = (this.config.providers as Record<string, ProviderConfig>)?.[
      providerName
    ];

    if (!providerConfig || !providerConfig.apiKey) {
      throw new ProviderError(`Provider ${providerName} is not configured`);
    }

    let provider: BaseProvider;

    switch (providerName) {
      case 'openrouter':
        provider = new OpenRouterProvider(providerConfig.apiKey, providerConfig.apiBase);
        break;
      case 'anthropic':
        provider = new AnthropicProvider(providerConfig.apiKey, providerConfig.apiBase);
        break;
      case 'openai':
        provider = new OpenAIProvider(providerConfig.apiKey, providerConfig.apiBase);
        break;
      case 'deepseek':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://api.deepseek.com/v1'
        );
        break;
      case 'groq':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://api.groq.com/openai/v1'
        );
        break;
      case 'gemini':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://generativelanguage.googleapis.com/v1beta/openai'
        );
        break;
      case 'minimax':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://api.minimax.chat/v1'
        );
        break;
      case 'dashscope':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        );
        break;
      case 'moonshot':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://api.moonshot.cn/v1'
        );
        break;
      case 'zhipu':
        provider = new OpenAIProvider(
          providerConfig.apiKey,
          providerConfig.apiBase || 'https://open.bigmodel.cn/api/paas/v4'
        );
        break;
      case 'vllm':
        if (!providerConfig.apiBase) {
          throw new ProviderError('vLLM provider requires apiBase configuration');
        }
        provider = new OpenAIProvider(providerConfig.apiKey, providerConfig.apiBase);
        break;
      default:
        throw new ProviderError(`Unknown provider: ${providerName}`);
    }

    this.providerCache.set(providerName, provider);
    return provider;
  }

  /**
   * Detect provider from model name or configuration
   */
  private detectProvider(model: string): string {
    // First, try to detect by model name
    const providerSpec = findProviderByModel(model);
    if (providerSpec) {
      const providerConfig = (this.config.providers as Record<string, ProviderConfig>)?.[
        providerSpec.name
      ];
      if (providerConfig && providerConfig.apiKey) {
        logger.debug({ provider: providerSpec.name, model }, 'Provider detected from model name');
        return providerSpec.name;
      }
    }

    // Try to find gateway provider (like OpenRouter)
    const gatewayProviders = ['openrouter', 'aihubmix'];
    for (const providerName of gatewayProviders) {
      const providerConfig = (this.config.providers as Record<string, ProviderConfig>)?.[
        providerName
      ];
      if (providerConfig && providerConfig.apiKey) {
        logger.debug({ provider: providerName, model }, 'Using gateway provider');
        return providerName;
      }
    }

    // Fall back to first configured provider
    const providersConfig = this.config.providers as Record<string, ProviderConfig>;
    const firstConfigured = Object.keys(providersConfig).find(
      (key) => providersConfig[key]?.apiKey
    );

    if (firstConfigured) {
      logger.debug({ provider: firstConfigured, model }, 'Using first configured provider');
      return firstConfigured;
    }

    throw new ProviderError('No provider configured');
  }

  /**
   * True when `model` can actually be routed: its own provider has a key, or a
   * gateway provider (which can serve any model id) is configured. Used to skip
   * fallback models whose provider isn't set up, rather than misrouting them.
   */
  private isModelRoutable(model: string): boolean {
    const providers = (this.config.providers as Record<string, ProviderConfig>) || {};
    const spec = findProviderByModel(model);
    if (spec && providers[spec.name]?.apiKey) return true;
    return GATEWAY_PROVIDERS.some((g) => providers[g]?.apiKey);
  }

  /**
   * Ordered model chain: the requested model first (always tried, preserving
   * prior behavior), then each configured fallback whose provider is routable.
   * Duplicates and unroutable fallbacks are dropped.
   */
  private resolveModelChain(model: string): string[] {
    const fallbacks = this.config.agents?.defaults?.fallbackModels || [];
    const chain = [model];
    const seen = new Set([model]);
    for (const m of fallbacks) {
      if (!m || seen.has(m)) continue;
      seen.add(m);
      if (this.isModelRoutable(m)) {
        chain.push(m);
      } else {
        logger.debug({ model: m }, 'Fallback model skipped (provider not configured)');
      }
    }
    return chain;
  }

  private fallbackTimeoutMs(): number {
    return this.config.agents?.defaults?.fallbackTimeoutMs ?? DEFAULT_FALLBACK_TIMEOUT_MS;
  }

  /**
   * Complete a chat conversation, falling back through the configured model
   * chain on error or timeout. With no fallbacks configured the chain is just
   * the requested model, so behavior is unchanged.
   */
  async complete(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): Promise<LLMResponse> {
    const chain = this.resolveModelChain(model);
    return completeWithFallback(
      chain.map((m) => ({
        label: m,
        run: () => {
          const providerName = this.detectProvider(m);
          logger.info(
            { provider: providerName, model: m, messageCount: messages.length },
            'Completing chat'
          );
          return this.getProviderInstance(providerName).complete(
            messages,
            m,
            temperature,
            maxTokens,
            tools
          );
        },
      })),
      this.fallbackTimeoutMs()
    );
  }

  /**
   * Streaming variant of complete() with time-to-first-token fallback. If the
   * current model produces no first token within the timeout (or errors before
   * one), the next routable model in the chain is tried. Once the first token
   * streams, the model is committed for the rest of the reply.
   */
  async *completeStream(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const chain = this.resolveModelChain(model);
    yield* streamWithFallback(
      chain.map((m) => ({
        label: m,
        run: () => {
          const providerName = this.detectProvider(m);
          logger.info(
            { provider: providerName, model: m, messageCount: messages.length },
            'Completing chat (stream)'
          );
          return this.getProviderInstance(providerName).completeStream(
            messages,
            m,
            temperature,
            maxTokens,
            tools
          );
        },
      })),
      this.fallbackTimeoutMs()
    );
  }
}
