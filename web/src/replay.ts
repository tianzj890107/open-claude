import type { Message } from "@ag-ui/client";
import type { LogEvent } from "./api";

let seq = 0;
const nextId = () => `replay-${Date.now().toString(36)}-${seq++}`;

/**
 * Rebuild a task's transcript as AG-UI messages.
 *
 * The server keeps the authoritative history (each task owns a real open-claude
 * Conversation); this only reconstructs what the chat should *look* like when a
 * task is reopened. Consecutive tool calls fold into the assistant message that
 * introduced them so a turn renders as one bubble, exactly as it streamed live.
 */
export function logToMessages(log: LogEvent[]): Message[] {
  const out: Message[] = [];
  let assistant: (Message & { role: "assistant" }) | null = null;

  for (const ev of log) {
    switch (ev.type) {
      case "user":
        assistant = null;
        out.push({ id: nextId(), role: "user", content: String(ev.text ?? "") });
        break;

      case "assistant": {
        const text = String(ev.text ?? "");
        if (!text) break;
        // A turn's text always precedes its tool calls, so an assistant entry
        // arriving after them belongs to the next bubble.
        if (assistant && !assistant.toolCalls?.length) {
          assistant.content = (assistant.content ?? "") + text;
        } else {
          assistant = { id: nextId(), role: "assistant", content: text };
          out.push(assistant);
        }
        break;
      }

      case "tool_use": {
        if (!assistant) {
          assistant = { id: nextId(), role: "assistant", content: "" };
          out.push(assistant);
        }
        assistant.toolCalls = [
          ...(assistant.toolCalls ?? []),
          {
            id: String(ev.id ?? nextId()),
            type: "function",
            function: {
              name: String(ev.name ?? ""),
              arguments: JSON.stringify(ev.input ?? {}),
            },
          },
        ];
        break;
      }

      case "tool_result":
        out.push({
          id: nextId(),
          role: "tool",
          toolCallId: String(ev.tool_use_id ?? ""),
          content: String(ev.content ?? ""),
        });
        assistant = null;
        break;

      case "error":
        assistant = null;
        out.push({
          id: nextId(),
          role: "assistant",
          content: `⚠️ ${String(ev.error ?? "运行出错")}`,
        });
        break;

      default:
        break; // approval_request / approval_result are transient UI signals
    }
  }
  return out;
}
