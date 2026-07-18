import { Config } from '../config/schema';
import { ProviderConfig } from '../types';

export interface CatalogModel { id: string; label: string; provider: string; }

/** Curated, voice-friendly models. `provider` must match a registry provider name. */
export const MODEL_CATALOG: CatalogModel[] = [
  { id: 'anthropic/claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'anthropic' },
  { id: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5', provider: 'anthropic' },
  { id: 'gemini/gemini-flash-lite-latest', label: 'Gemini Flash-Lite (fast/cheap)', provider: 'gemini' },
  { id: 'gemini/gemini-flash-latest', label: 'Gemini Flash', provider: 'gemini' },
  { id: 'deepseek/deepseek-v4-flash', label: 'DeepSeek V4 Flash', provider: 'deepseek' },
  { id: 'groq/llama-3.3-70b-versatile', label: 'Groq Llama 3.3 70B', provider: 'groq' },
  { id: 'dashscope/qwen-plus', label: 'Qwen Plus (Alibaba)', provider: 'dashscope' },
  { id: 'openai/gpt-4o-mini', label: 'GPT-4o mini', provider: 'openai' },
];

export const DEFAULT_MODEL = 'anthropic/claude-haiku-4-5';

export function modelsWithAvailability(config: Config) {
  const providers = (config.providers as Record<string, ProviderConfig>) || {};
  return MODEL_CATALOG.map((m) => ({ ...m, available: !!providers[m.provider]?.apiKey }));
}
