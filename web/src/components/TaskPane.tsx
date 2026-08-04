import { useEffect, useMemo, useState } from "react";
import { Spin } from "antd";
import {
  CopilotChat,
  CopilotKitProvider,
  HttpAgent,
  defineToolCallRenderer,
  useAgent,
  useAgentContext,
} from "@copilotkit/react-core/v2";
import { api, type TaskSummary } from "../api";
import { logToMessages } from "../replay";
import ToolCallCard from "./ToolCallCard";
import ApprovalBridge from "./ApprovalBridge";

const LABELS = {
  chatInputPlaceholder: "描述你要做的事,智能体会在项目目录内执行…",
  chatDisclaimerText: "智能体的所有文件与命令操作都被限制在沙箱项目目录内。",
  welcomeMessageText: "开始新的一轮工作",
  assistantMessageToolbarCopyMessageLabel: "复制",
  assistantMessageToolbarCopyCodeLabel: "复制代码",
  assistantMessageToolbarCopyCodeCopiedLabel: "已复制",
  assistantMessageToolbarRegenerateLabel: "重新生成",
  userMessageToolbarCopyMessageLabel: "复制",
  userMessageToolbarEditMessageLabel: "编辑",
};

// One wildcard renderer covers every tool — including MCP tools and skills the
// backend gains later, which need no frontend change to render sensibly.
const TOOL_RENDERERS = [
  defineToolCallRenderer({
    name: "*",
    render: (props) => (
      <ToolCallCard
        name={props.name}
        args={(props.args ?? {}) as Record<string, unknown>}
        status={props.status}
        result={typeof props.result === "string" ? props.result : undefined}
      />
    ),
  }),
];

/** Tells the agent which file the user is looking at, as first-class context. */
function PreviewContextBridge({ file }: { file: string }) {
  useAgentContext({ description: "用户当前在右侧预览的文件", value: file });
  return null;
}

/** Notifies the shell when a turn ends so the file tree can refresh. */
function RunWatcher({ onEnd }: { onEnd: () => void }) {
  const { agent } = useAgent();
  useEffect(() => {
    const sub = agent.subscribe({
      onRunFinalized: () => onEnd(),
      onRunFailed: () => onEnd(),
    });
    return () => sub.unsubscribe();
  }, [agent, onEnd]);
  return null;
}

interface Props {
  task: TaskSummary;
  previewFile: string | null;
  onTurnEnd: () => void;
}

export default function TaskPane({ task, previewFile, onTurnEnd }: Props) {
  // The agent posts straight to the Python server's AG-UI endpoint — CopilotKit
  // consumes the protocol directly, so there is no Node runtime in the loop.
  const agent = useMemo(
    () => new HttpAgent({ url: `/api/agui?task=${encodeURIComponent(task.id)}` }),
    [task.id],
  );
  const [ready, setReady] = useState(false);

  // Replay the server-side transcript so reopening a task shows its history.
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const detail = await api.task(task.id);
        if (alive) agent.messages = logToMessages(detail.log);
      } finally {
        if (alive) setReady(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [agent, task.id]);

  if (!ready) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
        <Spin />
      </div>
    );
  }

  return (
    <CopilotKitProvider
      agents__unsafe_dev_only={{ default: agent }}
      renderToolCalls={TOOL_RENDERERS}
    >
      <div className="oc-chat">
        <CopilotChat labels={LABELS} />
      </div>
      <ApprovalBridge taskId={task.id} />
      <RunWatcher onEnd={onTurnEnd} />
      {previewFile && <PreviewContextBridge file={previewFile} />}
    </CopilotKitProvider>
  );
}
