import { useState } from "react";
import { Spin } from "antd";
import {
  CheckOutlined,
  CodeOutlined,
  FileAddOutlined,
  FileSearchOutlined,
  GlobalOutlined,
  ReadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { ToolCallStatus } from "@copilotkit/react-core/v2";

interface Props {
  name: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  result?: string;
}

/**
 * What a tool call looks like to a non-developer: an activity, not a command.
 *
 * The detail is still one click away — people trust an assistant more when they
 * can see what it did — but nothing here exposes the workspace, because the
 * server has already scrubbed paths out of both arguments and output.
 */
const ACTIVITIES: Record<string, { running: string; done: string; icon: React.ReactNode }> = {
  Bash: { running: "正在运行代码", done: "已运行代码", icon: <CodeOutlined /> },
  Write: { running: "正在生成文件", done: "已生成文件", icon: <FileAddOutlined /> },
  Edit: { running: "正在修改文件", done: "已修改文件", icon: <FileAddOutlined /> },
  Read: { running: "正在阅读文件", done: "已阅读文件", icon: <ReadOutlined /> },
  Glob: { running: "正在查找资料", done: "已查找资料", icon: <FileSearchOutlined /> },
  Grep: { running: "正在检索内容", done: "已检索内容", icon: <FileSearchOutlined /> },
  WebSearch: { running: "正在联网搜索", done: "已联网搜索", icon: <GlobalOutlined /> },
  WebFetch: { running: "正在读取网页", done: "已读取网页", icon: <GlobalOutlined /> },
};

const FALLBACK = { running: "正在处理", done: "已完成", icon: <ThunderboltOutlined /> };

function subtitle(name: string, args: Record<string, unknown>): string {
  const s = (k: string) => (args[k] == null ? "" : String(args[k]));
  if (name === "Bash") return s("description") || s("command");
  if (name === "Read" || name === "Write" || name === "Edit") return s("file_path");
  if (name === "Glob" || name === "Grep") return s("pattern");
  if (name === "WebSearch" || name === "WebFetch") return s("query") || s("url");
  return "";
}

export default function ActivityCard({ name, args, status, result }: Props) {
  const [open, setOpen] = useState(false);
  const running = status !== ToolCallStatus.Complete;
  const a = ACTIVITIES[name] ?? FALLBACK;
  const sub = subtitle(name, args ?? {});

  const detail =
    name === "Bash"
      ? String(args?.command ?? "")
      : JSON.stringify(args ?? {}, null, 2);

  return (
    <div className="cc-act">
      <div className="cc-act-head" onClick={() => setOpen((v) => !v)}>
        <div className="cc-act-tile">{a.icon}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="cc-act-title">{running ? a.running : a.done}</div>
          {sub && <div className="cc-act-sub">{sub}</div>}
        </div>
        {running ? (
          <Spin size="small" />
        ) : (
          <CheckOutlined style={{ color: "#10b981", fontSize: 13 }} />
        )}
      </div>

      {open && (
        <div className="cc-act-body">
          <pre>{detail}</pre>
          {!running && result && (
            <pre style={{ marginTop: 8, opacity: 0.85 }}>{result}</pre>
          )}
        </div>
      )}
    </div>
  );
}
