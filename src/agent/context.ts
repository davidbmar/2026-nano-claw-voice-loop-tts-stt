import { Message, Skill, ToolDefinition, AgentConfig, SYSTEM_CACHE_MARKER } from '../types';
import { formatDate } from '../utils/helpers';
import { loadKnowledge } from './knowledge';

/**
 * Context builder for constructing prompts
 */
export class ContextBuilder {
  private config: AgentConfig;

  constructor(config: AgentConfig) {
    this.config = config;
  }

  /**
   * Build system prompt with skills and tools
   */
  buildSystemPrompt(skills: Skill[], tools: ToolDefinition[]): string {
    const parts: string[] = [];

    // Add base system prompt
    if (this.config.systemPrompt) {
      parts.push(this.config.systemPrompt);
    } else {
      parts.push(this.getDefaultSystemPrompt());
    }

    // Grounding knowledge (site digests built by scripts/build_knowledge.py).
    // Kept adjacent to the persona and AHEAD of the per-turn timestamp so the
    // stable prefix (persona + knowledge) can be covered by a prompt-cache
    // breakpoint; anything after the timestamp is uncacheable.
    if (this.config.knowledgeFiles?.length) {
      const knowledge = loadKnowledge(this.config.knowledgeFiles);
      if (knowledge) {
        parts.push('\n## Knowledge');
        parts.push(
          'Ground answers on the topics below ONLY in this knowledge section. ' +
            'For schedules, launches, news, and events, anything you remember from ' +
            'training is stale by definition — do not use it, and do not invent ' +
            'items that are not listed here. This is a point-in-time snapshot; ' +
            'each section states when its data was captured. When speaking, keep ' +
            'list answers to the top two or three items and offer to continue. ' +
            'Mention data age only when the data is volatile, clearly old, or the ' +
            "user asks. If a question on these topics isn't covered below, say " +
            'what you do know and be clear the snapshot does not include it.\n'
        );
        parts.push(knowledge);
      } else {
        parts.push(
          '\nNote: a site knowledge base is configured but currently unavailable. ' +
            'If asked about it, say the knowledge base is unavailable right now ' +
            'rather than answering from memory.'
        );
      }
    }

    // Everything above this marker (persona + knowledge) is stable across
    // turns; cache-capable providers mark it as a cacheable prefix, others
    // strip the marker. The timestamp and anything below churn per turn.
    parts.push(SYSTEM_CACHE_MARKER);

    // Add current time (minute precision — finer would churn the cacheable
    // prompt prefix every turn for no benefit)
    const now = new Date();
    now.setSeconds(0, 0);
    parts.push(`\nCurrent time: ${formatDate(now)}`);

    // Add skills information
    if (skills.length > 0) {
      parts.push('\n## Available Skills');
      parts.push(
        'You have access to the following skills that provide additional context and capabilities:\n'
      );
      for (const skill of skills) {
        parts.push(`### ${skill.name}`);
        parts.push(skill.description);
        parts.push('');
      }
    }

    // Add tools information
    if (tools.length > 0) {
      parts.push('\n## Available Tools');
      parts.push('You can use the following tools to perform actions:\n');
      for (const tool of tools) {
        parts.push(`- **${tool.function.name}**: ${tool.function.description}`);
      }
      parts.push('');
    } else {
      // Knowledge-only mode: the base persona may mention tools — override
      // that so the model never promises actions it cannot take.
      parts.push(
        '\nNo tools are available in this session. Answer directly from this ' +
          'prompt and the conversation; never say you will run, check, or look ' +
          'something up with a tool.'
      );
    }

    return parts.join('\n');
  }

  /**
   * Get default system prompt
   */
  private getDefaultSystemPrompt(): string {
    return `You are a helpful AI assistant powered by nano-claw. You are knowledgeable, precise, and aim to be helpful.

Your capabilities:
- Answer questions accurately and concisely
- Execute tasks using available tools
- Remember context from the conversation
- Use skills to enhance your knowledge and capabilities

Guidelines:
- Be honest if you don't know something
- Use tools when they can help accomplish the task
- Keep responses clear and well-structured
- Respect user privacy and security`;
  }

  /**
   * Build context messages for LLM
   */
  buildContextMessages(
    conversationMessages: Message[],
    skills: Skill[],
    tools: ToolDefinition[]
  ): Message[] {
    const messages: Message[] = [];

    // Add system message with full context
    const systemPrompt = this.buildSystemPrompt(skills, tools);
    messages.push({
      role: 'system',
      content: systemPrompt,
    });

    // Add conversation history
    messages.push(...conversationMessages);

    return messages;
  }

  /**
   * Format tool result for display
   */
  formatToolResult(toolName: string, result: string): string {
    return `[Tool: ${toolName}]\n${result}`;
  }

  /**
   * Truncate context if too long
   */
  truncateContext(messages: Message[], maxLength: number): Message[] {
    // Always keep system message
    const systemMessages = messages.filter((m) => m.role === 'system');
    const otherMessages = messages.filter((m) => m.role !== 'system');

    // Calculate total length
    let totalLength = 0;
    for (const msg of messages) {
      totalLength += msg.content.length;
    }

    if (totalLength <= maxLength) {
      return messages;
    }

    // Keep recent messages that fit within limit
    const recentMessages: Message[] = [];
    let currentLength = systemMessages.reduce((sum, m) => sum + m.content.length, 0);

    for (let i = otherMessages.length - 1; i >= 0; i--) {
      const msg = otherMessages[i];
      if (currentLength + msg.content.length > maxLength) {
        break;
      }
      recentMessages.unshift(msg);
      currentLength += msg.content.length;
    }

    return [...systemMessages, ...recentMessages];
  }
}
