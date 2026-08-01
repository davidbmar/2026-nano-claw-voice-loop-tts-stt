import { z } from 'zod';

/**
 * Provider configuration schema
 */
export const ProviderConfigSchema = z.object({
  apiKey: z.string().optional(),
  apiBase: z.string().optional(),
  enabled: z.boolean().optional().default(true),
});

/**
 * Providers configuration schema
 */
export const ProvidersConfigSchema = z.object({
  openrouter: ProviderConfigSchema.optional(),
  anthropic: ProviderConfigSchema.optional(),
  openai: ProviderConfigSchema.optional(),
  deepseek: ProviderConfigSchema.optional(),
  groq: ProviderConfigSchema.optional(),
  gemini: ProviderConfigSchema.optional(),
  minimax: ProviderConfigSchema.optional(),
  aihubmix: ProviderConfigSchema.optional(),
  dashscope: ProviderConfigSchema.optional(),
  moonshot: ProviderConfigSchema.optional(),
  zhipu: ProviderConfigSchema.optional(),
  vllm: ProviderConfigSchema.optional(),
  ollama: ProviderConfigSchema.optional(),
});

/** Evidence retrieval from the local intelligence-platform service. */
export const DeepReasoningConfigSchema = z.object({
  enabled: z.boolean().default(false),
  routingMode: z.enum(['auto', 'always', 'never']).default('auto'),
  threshold: z.number().int().min(1).max(20).default(4),
  acknowledgement: z.string().min(1).max(160).default('Let me think deeply about this.'),
  maxSteps: z.number().int().min(1).max(20).default(6),
  maxRetrievalQueries: z.number().int().min(1).max(50).default(10),
  pollIntervalMs: z.number().int().min(100).max(10000).default(750),
  requestTimeoutMs: z.number().int().min(100).max(30000).default(5000),
  taskTimeoutMs: z.number().int().min(1000).max(600000).default(240000),
  analysisStyle: z.enum(['topic_map', 'principle_graph']).default('topic_map'),
});

export const IntelligenceConfigSchema = z
  .object({
    enabled: z.boolean().default(false),
    apiUrl: z.string().url().default('http://127.0.0.1:8000'),
    tenantId: z.string().min(1).default('personal'),
    principalId: z.string().min(1).default('nano-claw'),
    collectionIds: z.array(z.string().min(1)).default([]),
    // Narrows retrieval to specific documents inside those collections. Empty
    // means "the whole collection"; the platform ANDs the two filters, so this
    // is what lets a customer tick two of five uploaded files.
    documentIds: z.array(z.string().min(1)).default([]),
    // Set when the active document space has documents but the customer has
    // ticked none of them. Distinct from an empty collectionIds, which the
    // platform reads as "search the whole tenant".
    documentScopeEmpty: z.boolean().optional(),
    limit: z.number().int().min(1).max(20).default(5),
    candidatePool: z.number().int().min(1).max(200).default(40),
    maxChars: z.number().int().min(1000).max(100000).default(16000),
    timeoutMs: z.number().int().min(10).max(10000).default(750),
    groundingMode: z.enum(['augment', 'strict']).default('augment'),
    deepReasoning: DeepReasoningConfigSchema.optional(),
  })
  .refine((value) => value.candidatePool >= value.limit, {
    message: 'candidatePool must be greater than or equal to limit',
    path: ['candidatePool'],
  });

/**
 * Agent defaults configuration schema
 */
export const AgentDefaultsSchema = z.object({
  model: z.string().default('anthropic/claude-opus-4-5'),
  temperature: z.number().min(0).max(2).optional().default(0.7),
  maxTokens: z.number().positive().optional().default(4096),
  systemPrompt: z.string().optional(),
  knowledgeFiles: z.array(z.string()).optional(),
  intelligence: IntelligenceConfigSchema.optional(),
  /** Ordered fallback models tried when the primary model errors or is too slow
   * to produce a first token. Fallbacks whose provider has no key are skipped.
   * Empty (default) = no fallback, i.e. unchanged single-model behavior. */
  fallbackModels: z.array(z.string()).optional().default([]),
  /** Time-to-first-token budget in ms. If the current model emits no first
   * token within this window, abort and try the next fallback (also the
   * per-attempt timeout for the non-streaming path). The last model in the
   * chain gets no deadline — a slow answer beats none. */
  fallbackTimeoutMs: z.number().positive().optional().default(4000),
  /** Streaming hedge delay in ms. When set (and fallbacks exist), streaming
   * turns race the chain instead of trying it sequentially: each fallback
   * starts this many ms after the previous one unless a first token already
   * arrived, first token wins, losers are cancelled. Removes the dead
   * fallbackTimeoutMs wait on every turn where the primary is slow. Unset =
   * sequential fallback (unchanged behavior). */
  fallbackHedgeMs: z.number().positive().optional(),
});

/**
 * Named assistant profile configuration schema
 */
export const AgentProfileSchema = z.object({
  label: z.string(),
  systemPrompt: z.string(),
  knowledgeFiles: z.array(z.string()),
  intelligence: IntelligenceConfigSchema.optional(),
});

/**
 * Agents configuration schema
 */
export const AgentsConfigSchema = z.object({
  defaults: AgentDefaultsSchema.optional(),
  profiles: z.record(AgentProfileSchema).optional(),
});

/**
 * Tools configuration schema
 */
export const ToolsConfigSchema = z.object({
  /** Dangerous built-in tools default off. NANO_CLAW_ENABLE_TOOLS=true is
   * required at the process boundary; NANO_CLAW_DISABLE_TOOLS=true remains a
   * temporary legacy kill switch. */
  enabled: z.boolean().optional().default(false),
  restrictToWorkspace: z.boolean().optional().default(false),
  allowedCommands: z.array(z.string()).optional(),
  deniedCommands: z.array(z.string()).optional(),
});

/**
 * Telegram channel configuration schema
 */
export const TelegramChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  token: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * Discord channel configuration schema
 */
export const DiscordChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  token: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * WhatsApp channel configuration schema
 */
export const WhatsAppChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * Feishu channel configuration schema
 */
export const FeishuChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  appId: z.string().optional(),
  appSecret: z.string().optional(),
  encryptKey: z.string().optional(),
  verificationToken: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * Slack channel configuration schema
 */
export const SlackChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  botToken: z.string().optional(),
  appToken: z.string().optional(),
  groupPolicy: z.enum(['mention', 'open', 'allowlist']).optional().default('mention'),
});

/**
 * Email channel configuration schema
 */
export const EmailChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  consentGranted: z.boolean().optional().default(false),
  imapHost: z.string().optional(),
  imapPort: z.number().optional().default(993),
  imapUsername: z.string().optional(),
  imapPassword: z.string().optional(),
  smtpHost: z.string().optional(),
  smtpPort: z.number().optional().default(587),
  smtpUsername: z.string().optional(),
  smtpPassword: z.string().optional(),
  fromAddress: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * QQ channel configuration schema
 */
export const QQChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  appId: z.string().optional(),
  secret: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * DingTalk channel configuration schema
 */
export const DingTalkChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  clientId: z.string().optional(),
  clientSecret: z.string().optional(),
  allowFrom: z.array(z.string()).optional().default([]),
});

/**
 * Mochat channel configuration schema
 */
export const MochatChannelSchema = z.object({
  enabled: z.boolean().optional().default(false),
  baseUrl: z.string().optional().default('https://mochat.io'),
  socketUrl: z.string().optional().default('https://mochat.io'),
  socketPath: z.string().optional().default('/socket.io'),
  clawToken: z.string().optional(),
  agentUserId: z.string().optional(),
  sessions: z.array(z.string()).optional().default(['*']),
  panels: z.array(z.string()).optional().default(['*']),
  replyDelayMode: z.string().optional().default('non-mention'),
  replyDelayMs: z.number().optional().default(120000),
});

/**
 * Channels configuration schema
 */
export const ChannelsConfigSchema = z.object({
  telegram: TelegramChannelSchema.optional(),
  discord: DiscordChannelSchema.optional(),
  whatsapp: WhatsAppChannelSchema.optional(),
  feishu: FeishuChannelSchema.optional(),
  slack: SlackChannelSchema.optional(),
  email: EmailChannelSchema.optional(),
  qq: QQChannelSchema.optional(),
  dingtalk: DingTalkChannelSchema.optional(),
  mochat: MochatChannelSchema.optional(),
});

/**
 * Main configuration schema
 */
export const ConfigSchema = z.object({
  providers: ProvidersConfigSchema.optional().default({}),
  agents: AgentsConfigSchema.optional().default({}),
  tools: ToolsConfigSchema.optional().default({}),
  channels: ChannelsConfigSchema.optional().default({}),
});

/**
 * Configuration type inferred from schema
 */
export type Config = z.infer<typeof ConfigSchema>;
export type IntelligenceConfig = z.infer<typeof IntelligenceConfigSchema>;
export type DeepReasoningConfig = z.infer<typeof DeepReasoningConfigSchema>;
export type ProvidersConfig = z.infer<typeof ProvidersConfigSchema>;
export type AgentProfile = z.infer<typeof AgentProfileSchema>;
export type AgentsConfig = z.infer<typeof AgentsConfigSchema>;
export type ToolsConfig = z.infer<typeof ToolsConfigSchema>;
export type ChannelsConfig = z.infer<typeof ChannelsConfigSchema>;
