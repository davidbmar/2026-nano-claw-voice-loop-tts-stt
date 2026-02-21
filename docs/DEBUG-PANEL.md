# Debug Panel — Loop Observability

The voice interface includes a built-in debug panel that gives you real-time visibility into what the agent is doing on every loop iteration. No more guessing — see exactly which model was called, how many tokens were used, how long it took, and why the LLM stopped.

## Opening the Debug Panel

Click the **DEBUG** button below the status bar. It toggles open to reveal a scrolling log of every agent loop iteration.

![Debug panel expanded](debug-panel-expanded.png)

Each row is one LLM call. Multiple rows per voice interaction means the agent looped (typically because it called tools).

## Reading a Debug Row

```
iter 1  msgs 13  model anthropic/claude-sonnet-4-5  tok 1676/129/1805  dur 2635ms  finish tool_use
```

| Field | Meaning |
|-------|---------|
| **iter** | Which pass through the agent loop (1 = first call, 2 = after first tool result, etc.) |
| **msgs** | Number of messages in the conversation history sent to the LLM. Grows each iteration as tool calls and results are appended. |
| **model** | The LLM model used for this call. |
| **tok** | Token counts: **prompt / completion / total**. Prompt tokens grow each iteration because the conversation history gets longer. |
| **dur** | Wall-clock time for the LLM API call (network + inference). Does not include tool execution time. |
| **finish** | Why the LLM stopped generating. See [Finish Reasons](#finish-reasons) below. |

## Iteration Detail View

Click any row to open a detailed breakdown with explanations for every field.

![Debug detail modal](debug-detail-modal.png)

The modal shows each metric with its value and a plain-English description of what it means and why it matters.

## Finish Reasons

| Value | Meaning |
|-------|---------|
| `end_turn` | The LLM finished its response — this is the final answer. |
| `tool_use` | The LLM wants to call a tool. The loop will pause for approval, then continue. |
| `max_tokens` | The LLM hit the token limit before finishing. The response may be truncated. |

## Server Logs

The same debug information is logged in the Docker terminal with structured fields:

```
2026-02-21 20:16:19 voice-server INFO  iter=1 msgs=1 model=anthropic/claude-sonnet-4-5
    tokens={'prompt': 897, 'completion': 68, 'total': 965} duration=2131ms finish=tool_use
```

The nano-claw API server also logs each iteration:

```
[20:16:19] INFO (nano-claw): Agent loop iteration complete
    iteration: 1
    messageCount: 1
    model: "anthropic/claude-sonnet-4-5"
    tokenUsage: { "prompt": 897, "completion": 68, "total": 965 }
    durationMs: 2131
    finishReason: "tool_use"
    hasToolCalls: true
```

Tool executions are logged separately with their own timing:

```
[20:16:19] INFO (nano-claw): Tool execution complete
    tool: "shell"
    success: true
    durationMs: 342
```

## Understanding Multi-Iteration Loops

A typical tool-calling interaction looks like this:

```
iter 1  msgs 13  tok 1676/129/1805   dur 2635ms  finish tool_use    <- LLM decides to call a tool
iter 2  msgs 16  tok 1925/80/2005    dur 2197ms  finish tool_use    <- got result, calls another tool
iter 3  msgs 18  tok 2062/176/2238   dur 4612ms  finish end_turn    <- got result, gives final answer
```

Notice:
- **msgs grows** (13 -> 16 -> 18): each iteration adds the assistant's tool call message + the tool result message
- **prompt tokens grow** (1676 -> 1925 -> 2062): the LLM re-reads the full conversation each time
- **iter 3 took longest** (4612ms): it generated the most completion tokens (176) for the final answer
- **Total cost**: 6,048 tokens across 3 calls in ~9.4 seconds of LLM time

## Architecture

```
Browser                    Voice Server (Python)         API Server (TypeScript)
   |                              |                              |
   |  <-- debug WebSocket msg --- | <-- debug in JSON response - |
   |       { type: "debug",      |      { type: "final",        |  stepLoop() records
   |         iteration: 1,       |        response: "...",      |  Date.now() before/after
   |         tokenUsage: {...},  |        debug: {              |  each providerManager.complete()
   |         durationMs: 2635 }  |          iteration: 1,       |
   |                              |          durationMs: 2635    |
   v                              v          ...                 v
Debug Panel                  log.info(...)              logger.info(...)
```
