import axios, { AxiosInstance } from 'axios';
import type { Readable } from 'node:stream';
import { StringDecoder } from 'node:string_decoder';
import { Message, LLMResponse, ToolDefinition, ToolCall, StreamEvent } from '../types';
import { ProviderError } from '../utils/errors';
import { logger } from '../utils/logger';

/**
 * Read a Node Readable SSE body and yield {event, data} frames.
 * Frames are separated by a blank line; `event:` and `data:` lines accumulate.
 */
export async function* readSSEFrames(
  stream: Readable
): AsyncGenerator<{ event: string; data: string }> {
  let buffer = '';
  const decoder = new StringDecoder('utf8');
  for await (const chunk of stream) {
    buffer += decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = 'message';
      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) yield { event, data: dataLines.join('\n') };
    }
  }
  buffer += decoder.end();
}

/**
 * Parse Anthropic /messages streaming events into StreamEvents.
 */
export async function* parseAnthropicEvents(stream: Readable): AsyncGenerator<StreamEvent> {
  let promptTokens = 0;
  let completionTokens = 0;
  let finishReason: string | undefined;
  // tool_use accumulation, keyed by content block index
  const toolAcc = new Map<number, { id: string; name: string; json: string }>();

  for await (const { data } of readSSEFrames(stream)) {
    if (data === '[DONE]') break;
    let evt: any;
    try {
      evt = JSON.parse(data);
    } catch {
      continue;
    }
    switch (evt.type) {
      case 'message_start':
        promptTokens = evt.message?.usage?.input_tokens ?? 0;
        break;
      case 'content_block_start':
        if (evt.content_block?.type === 'tool_use') {
          toolAcc.set(evt.index, { id: evt.content_block.id, name: evt.content_block.name, json: '' });
        }
        break;
      case 'content_block_delta':
        if (evt.delta?.type === 'text_delta' && evt.delta.text) {
          yield { type: 'text', delta: evt.delta.text };
        } else if (evt.delta?.type === 'input_json_delta') {
          const acc = toolAcc.get(evt.index);
          if (acc) acc.json += evt.delta.partial_json ?? '';
        }
        break;
      case 'message_delta':
        finishReason = evt.delta?.stop_reason ?? finishReason;
        completionTokens = evt.usage?.output_tokens ?? completionTokens;
        break;
      case 'message_stop':
        // handled after the loop
        break;
    }
  }

  if (toolAcc.size > 0) {
    const toolCalls: ToolCall[] = [...toolAcc.values()].map((t) => ({
      id: t.id,
      type: 'function',
      function: { name: t.name, arguments: t.json || '{}' },
    }));
    yield { type: 'tool_calls', toolCalls };
  }
  yield {
    type: 'done',
    finishReason,
    usage: { promptTokens, completionTokens, totalTokens: promptTokens + completionTokens },
  };
}

/** Parse OpenAI-compatible /chat/completions streaming events into StreamEvents. */
export async function* parseOpenAIEvents(stream: Readable): AsyncGenerator<StreamEvent> {
  let finishReason: string | undefined;
  let usage: LLMResponse['usage'];
  const toolAcc = new Map<number, { id: string; name: string; args: string }>();

  for await (const { data } of readSSEFrames(stream)) {
    if (data === '[DONE]') break;
    let evt: any;
    try { evt = JSON.parse(data); } catch { continue; }
    const choice = evt.choices?.[0];
    if (evt.usage) {
      usage = { promptTokens: evt.usage.prompt_tokens, completionTokens: evt.usage.completion_tokens, totalTokens: evt.usage.total_tokens };
    }
    if (!choice) continue;
    if (choice.finish_reason) finishReason = choice.finish_reason;
    const delta = choice.delta || {};
    if (delta.content) yield { type: 'text', delta: delta.content };
    if (Array.isArray(delta.tool_calls)) {
      for (const tc of delta.tool_calls) {
        const idx = tc.index ?? 0;
        const acc = toolAcc.get(idx) || { id: '', name: '', args: '' };
        if (tc.id) acc.id = tc.id;
        if (tc.function?.name) acc.name = tc.function.name;
        if (tc.function?.arguments) acc.args += tc.function.arguments;
        toolAcc.set(idx, acc);
      }
    }
  }

  if (toolAcc.size > 0) {
    const toolCalls: ToolCall[] = [...toolAcc.values()].map((t) => ({
      id: t.id, type: 'function', function: { name: t.name, arguments: t.args || '{}' },
    }));
    yield { type: 'tool_calls', toolCalls };
  }
  yield { type: 'done', finishReason, usage };
}

/**
 * Base class for LLM providers
 */
export abstract class BaseProvider {
  protected client: AxiosInstance;
  protected apiKey: string;
  protected apiBase: string;

  constructor(apiKey: string, apiBase?: string) {
    this.apiKey = apiKey;
    this.apiBase = apiBase || this.getDefaultApiBase();
    this.client = axios.create({
      baseURL: this.apiBase,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      timeout: 60000, // 60 seconds
    });
  }

  /**
   * Get the default API base URL for this provider
   */
  protected abstract getDefaultApiBase(): string;

  /**
   * Complete a chat conversation
   */
  abstract complete(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): Promise<LLMResponse>;

  /**
   * Stream a completion. Default: call complete() once and yield it whole.
   * Providers with native streaming override this.
   */
  async *completeStream(
    messages: Message[],
    model: string,
    temperature?: number,
    maxTokens?: number,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const res = await this.complete(messages, model, temperature, maxTokens, tools);
    if (res.content) yield { type: 'text', delta: res.content };
    if (res.toolCalls && res.toolCalls.length > 0) {
      yield { type: 'tool_calls', toolCalls: res.toolCalls };
    }
    yield {
      type: 'done',
      finishReason: res.finishReason,
      usage: res.usage,
    };
  }

  /**
   * Format model name for provider
   */
  protected formatModelName(model: string): string {
    return model;
  }
}

/**
 * OpenRouter provider
 */
export class OpenRouterProvider extends BaseProvider {
  protected getDefaultApiBase(): string {
    return 'https://openrouter.ai/api/v1';
  }

  async complete(
    messages: Message[],
    model: string,
    temperature = 0.7,
    maxTokens = 4096,
    tools?: ToolDefinition[]
  ): Promise<LLMResponse> {
    try {
      const requestData: Record<string, unknown> = {
        model: this.formatModelName(model),
        messages: messages.map((m) => ({
          role: m.role,
          content: m.content,
          ...(m.name && { name: m.name }),
          ...(m.tool_calls && { tool_calls: m.tool_calls }),
          ...(m.tool_call_id && { tool_call_id: m.tool_call_id }),
        })),
        temperature,
        max_tokens: maxTokens,
      };

      if (tools && tools.length > 0) {
        requestData.tools = tools;
      }

      const response = await this.client.post('/chat/completions', requestData);

      const choice = response.data.choices[0];
      const message = choice.message;

      return {
        content: message.content || '',
        toolCalls: message.tool_calls as ToolCall[] | undefined,
        finishReason: choice.finish_reason,
        usage: response.data.usage
          ? {
              promptTokens: response.data.usage.prompt_tokens,
              completionTokens: response.data.usage.completion_tokens,
              totalTokens: response.data.usage.total_tokens,
            }
          : undefined,
      };
    } catch (error) {
      logger.error({ error }, 'OpenRouter API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(
          `OpenRouter API error: ${error.response?.data?.error?.message || error.message}`
        );
      }
      throw new ProviderError(`OpenRouter API error: ${(error as Error).message}`);
    }
  }
}

/**
 * Anthropic provider
 */
export class AnthropicProvider extends BaseProvider {
  protected getDefaultApiBase(): string {
    return 'https://api.anthropic.com/v1';
  }

  protected formatModelName(model: string): string {
    // Remove anthropic/ prefix if present
    if (model.startsWith('anthropic/')) {
      return model.substring(10);
    }
    return model;
  }

  /**
   * Convert internal messages to Anthropic's expected format.
   * Anthropic requires:
   * - assistant tool calls as content blocks [{type:"tool_use",...}]
   * - tool results as user messages with [{type:"tool_result",...}]
   */
  private formatAnthropicMessages(messages: Message[]): Record<string, unknown>[] {
    const result: Record<string, unknown>[] = [];

    for (const m of messages) {
      if (m.role === 'assistant' && m.tool_calls && m.tool_calls.length > 0) {
        // Assistant message with tool calls → content blocks
        const content: Record<string, unknown>[] = [];
        if (m.content) {
          content.push({ type: 'text', text: m.content });
        }
        for (const tc of m.tool_calls) {
          // Handle both OpenAI-compatible format ({function:{name,arguments}})
          // and Anthropic-raw format ({name,input}) that may exist in persisted memory
          const tcAny = tc as unknown as Record<string, unknown>;
          const fn = tc.function || tcAny as { name: string; arguments: string };
          const toolName = fn.name || (tcAny.name as string) || 'unknown';
          let toolInput: unknown;
          if (fn.arguments) {
            try { toolInput = JSON.parse(fn.arguments); } catch { toolInput = {}; }
          } else if (tcAny.input) {
            toolInput = tcAny.input;
          } else {
            toolInput = {};
          }
          content.push({
            type: 'tool_use',
            id: tc.id,
            name: toolName,
            input: toolInput,
          });
        }
        result.push({ role: 'assistant', content });
      } else if (m.role === 'tool') {
        // Tool result → user message with tool_result content block
        // Anthropic groups consecutive tool results into one user message
        const lastMsg = result[result.length - 1];
        const toolResultBlock = {
          type: 'tool_result',
          tool_use_id: m.tool_call_id,
          content: m.content,
        };
        if (lastMsg && lastMsg.role === 'user' && Array.isArray(lastMsg.content)
            && (lastMsg.content as Record<string, unknown>[]).every(
              (b: Record<string, unknown>) => b.type === 'tool_result')) {
          // Merge into existing tool_result user message
          (lastMsg.content as Record<string, unknown>[]).push(toolResultBlock);
        } else {
          result.push({ role: 'user', content: [toolResultBlock] });
        }
      } else if (m.role === 'assistant') {
        result.push({ role: 'assistant', content: m.content });
      } else {
        // user messages
        result.push({ role: 'user', content: m.content });
      }
    }

    return result;
  }

  async complete(
    messages: Message[],
    model: string,
    temperature = 0.7,
    maxTokens = 4096,
    tools?: ToolDefinition[]
  ): Promise<LLMResponse> {
    try {
      // Extract system message
      const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
      const nonSystemMessages = messages.filter((m) => m.role !== 'system');

      // Format messages for Anthropic's API
      const anthropicMessages = this.formatAnthropicMessages(nonSystemMessages);

      const requestData: Record<string, unknown> = {
        model: this.formatModelName(model),
        messages: anthropicMessages,
        temperature,
        max_tokens: maxTokens,
      };

      if (systemMessage) {
        requestData.system = systemMessage;
      }

      if (tools && tools.length > 0) {
        requestData.tools = tools.map((t) => ({
          name: t.function.name,
          description: t.function.description,
          input_schema: t.function.parameters,
        }));
      }

      const response = await this.client.post('/messages', requestData, {
        headers: {
          'anthropic-version': '2023-06-01',
          'x-api-key': this.apiKey,
        },
      });

      // Extract text and tool_use blocks from response content
      const contentBlocks = response.data.content as Array<Record<string, unknown>>;
      const textParts: string[] = [];
      const toolUseBlocks: ToolCall[] = [];

      for (const block of contentBlocks) {
        if (block.type === 'text') {
          textParts.push(block.text as string);
        } else if (block.type === 'tool_use') {
          // Convert Anthropic tool_use to OpenAI-compatible ToolCall format
          toolUseBlocks.push({
            id: block.id as string,
            type: 'function',
            function: {
              name: block.name as string,
              arguments: JSON.stringify(block.input),
            },
          });
        }
      }

      return {
        content: textParts.join('\n'),
        toolCalls: toolUseBlocks.length > 0 ? toolUseBlocks : undefined,
        finishReason: response.data.stop_reason,
        usage: response.data.usage
          ? {
              promptTokens: response.data.usage.input_tokens,
              completionTokens: response.data.usage.output_tokens,
              totalTokens: response.data.usage.input_tokens + response.data.usage.output_tokens,
            }
          : undefined,
      };
    } catch (error) {
      logger.error({ error }, 'Anthropic API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(
          `Anthropic API error: ${error.response?.data?.error?.message || error.message}`
        );
      }
      throw new ProviderError(`Anthropic API error: ${(error as Error).message}`);
    }
  }

  async *completeStream(
    messages: Message[],
    model: string,
    temperature = 0.7,
    maxTokens = 4096,
    tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
    const nonSystemMessages = messages.filter((m) => m.role !== 'system');
    const anthropicMessages = this.formatAnthropicMessages(nonSystemMessages);

    const requestData: Record<string, unknown> = {
      model: this.formatModelName(model),
      messages: anthropicMessages,
      temperature,
      max_tokens: maxTokens,
      stream: true,
    };
    if (systemMessage) requestData.system = systemMessage;
    if (tools && tools.length > 0) {
      requestData.tools = tools.map((t) => ({
        name: t.function.name,
        description: t.function.description,
        input_schema: t.function.parameters,
      }));
    }

    let response;
    try {
      response = await this.client.post('/messages', requestData, {
        responseType: 'stream',
        headers: { 'anthropic-version': '2023-06-01', 'x-api-key': this.apiKey },
      });
    } catch (error) {
      logger.error({ error }, 'Anthropic API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(
          `Anthropic API error: ${error.response?.data?.error?.message || error.message}`
        );
      }
      throw new ProviderError(`Anthropic API error: ${(error as Error).message}`);
    }
    yield* parseAnthropicEvents(response.data as Readable);
  }
}

/**
 * OpenAI provider
 */
export class OpenAIProvider extends BaseProvider {
  protected getDefaultApiBase(): string {
    return 'https://api.openai.com/v1';
  }

  protected formatModelName(model: string): string {
    // Strip a leading "provider/" prefix (openai/, gemini/, groq/, deepseek/, dashscope/, …).
    const slash = model.indexOf('/');
    return slash === -1 ? model : model.slice(slash + 1);
  }

  async complete(
    messages: Message[],
    model: string,
    temperature = 0.7,
    maxTokens = 4096,
    tools?: ToolDefinition[]
  ): Promise<LLMResponse> {
    try {
      const requestData: Record<string, unknown> = {
        model: this.formatModelName(model),
        messages: messages.map((m) => ({
          role: m.role,
          content: m.content,
          ...(m.name && { name: m.name }),
          ...(m.tool_calls && { tool_calls: m.tool_calls }),
          ...(m.tool_call_id && { tool_call_id: m.tool_call_id }),
        })),
        temperature,
        max_tokens: maxTokens,
      };

      if (tools && tools.length > 0) {
        requestData.tools = tools;
      }

      const response = await this.client.post('/chat/completions', requestData);

      const choice = response.data.choices[0];
      const message = choice.message;

      return {
        content: message.content || '',
        toolCalls: message.tool_calls as ToolCall[] | undefined,
        finishReason: choice.finish_reason,
        usage: response.data.usage
          ? {
              promptTokens: response.data.usage.prompt_tokens,
              completionTokens: response.data.usage.completion_tokens,
              totalTokens: response.data.usage.total_tokens,
            }
          : undefined,
      };
    } catch (error) {
      logger.error({ error }, 'OpenAI API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(
          `OpenAI API error: ${error.response?.data?.error?.message || error.message}`
        );
      }
      throw new ProviderError(`OpenAI API error: ${(error as Error).message}`);
    }
  }

  async *completeStream(
    messages: Message[], model: string, temperature = 0.7, maxTokens = 4096, tools?: ToolDefinition[]
  ): AsyncGenerator<StreamEvent> {
    const requestData: Record<string, unknown> = {
      model: this.formatModelName(model),
      messages: messages.map((m) => ({
        role: m.role, content: m.content,
        ...(m.name && { name: m.name }),
        ...(m.tool_calls && { tool_calls: m.tool_calls }),
        ...(m.tool_call_id && { tool_call_id: m.tool_call_id }),
      })),
      temperature, max_tokens: maxTokens, stream: true,
      stream_options: { include_usage: true },
    };
    if (tools && tools.length > 0) requestData.tools = tools;
    let response;
    try {
      response = await this.client.post('/chat/completions', requestData, { responseType: 'stream' });
    } catch (error) {
      logger.error({ error }, 'OpenAI API error');
      if (axios.isAxiosError(error)) {
        throw new ProviderError(`OpenAI API error: ${error.response?.data?.error?.message || error.message}`);
      }
      throw new ProviderError(`OpenAI API error: ${(error as Error).message}`);
    }
    yield* parseOpenAIEvents(response.data as Readable);
  }
}
