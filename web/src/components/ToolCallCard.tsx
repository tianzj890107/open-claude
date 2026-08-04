import { useState } from "react";
import { Spin, Tag, Tooltip } from "antd";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  CodeOutlined,
  DownOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  RightOutlined,
  RobotOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { ToolCallStatus } from "@copilotkit/react-core/v2";
import { usePreview } from "../preview";

type Args = Record<string, unknown>;

interface Props {
  name: string;
  args: Args;
  status: ToolCallStatus;
  result?: string;
}

const ICONS: Record<string, React.ReactNode> = {
  Bash: <CodeOutlined />,
  Read: <FileTextOutlined />,
  Write: <EditOutlined />,
  Edit: <EditOutlined />,
  Glob: <FolderOpenOutlined />,
  Grep: <SearchOutlined />,
  Skill: <ThunderboltOutlined />,
  Agent: <RobotOutlined />,
  Task: <RobotOutlined />,
};

/** The one-line summary shown next to the tool name, Codex-style. */
function headline(name: string, args: Args): string {
  const s = (k: string) => (args[k] == null ? "" : String(args[k]));
  switch (name) {
    case "Bash":
      return s("command");
    case "Read":
    case "Write":
    case "Edit":
      return s("file_path");
    case "Glob":
      return [s("pattern"), s("path")].filter(Boolean).join("  ·  ");
    case "Grep":
      return [s("pattern"), s("path"), s("glob")].filter(Boolean).join("  ·  ");
    case "Skill":
      return [s("skill"), s("args")].filter(Boolean).join(" ");
    default: {
      const j = JSON.stringify(args ?? {});
      return j === "{}" ? "" : j;
    }
  }
}

/** file_path arguments double as preview links. */
const PATH_TOOLS = new Set(["Read", "Write", "Edit"]);

function DiffView({ args }: { args: Args }) {
  const del = String(args.old_string ?? "");
  const add = String(args.new_string ?? args.content ?? "");
  return (
    <pre className="oc-pre">
      {del
        .split("\n")
        .filter((l, i, a) => !(i === a.length - 1 && l === ""))
        .map((l, i) => (
          <div key={`d${i}`} className="oc-diff-del">
            - {l}
          </div>
        ))}
      {add
        .split("\n")
        .filter((l, i, a) => !(i === a.length - 1 && l === ""))
        .map((l, i) => (
          <div key={`a${i}`} className="oc-diff-add">
            + {l}
          </div>
        ))}
    </pre>
  );
}

/**
 * Wildcard renderer for every tool the agent calls. Registered once on the
 * provider, so new tools (MCP servers, skills, sub-agents) get a sane card
 * without any frontend change.
 */
export default function ToolCallCard({ name, args, status, result }: Props) {
  const [open, setOpen] = useState(false);
  const preview = usePreview();

  const running = status !== ToolCallStatus.Complete;
  const isError = !running && /^Error:|^Tool error/i.test(result ?? "");
  const summary = headline(name, args ?? {});
  const filePath = PATH_TOOLS.has(name) ? String(args?.file_path ?? "") : "";
  const showDiff = (name === "Edit" || name === "Write") && (args?.new_string ?? args?.content);

  return (
    <div className="oc-tool">
      <div className="oc-tool-head" onClick={() => setOpen((v) => !v)}>
        {open ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
        <span style={{ color: "#a8adb7" }}>{ICONS[name] ?? <ToolOutlined />}</span>
        <span className="oc-tool-name">{name}</span>
        {filePath ? (
          <Tooltip title="在右侧预览">
            <span
              className="oc-tool-arg link"
              onClick={(e) => {
                e.stopPropagation();
                preview.open(filePath);
              }}
            >
              {summary}
            </span>
          </Tooltip>
        ) : (
          <span className="oc-tool-arg">{summary}</span>
        )}
        {running ? (
          <Spin size="small" />
        ) : isError ? (
          <CloseCircleFilled style={{ color: "#ff7875" }} />
        ) : (
          <CheckCircleFilled style={{ color: "#52c41a", opacity: 0.75 }} />
        )}
      </div>

      {open && (
        <div className="oc-tool-body">
          {showDiff ? (
            <DiffView args={args ?? {}} />
          ) : (
            <pre className="oc-pre">{JSON.stringify(args ?? {}, null, 2)}</pre>
          )}
          {!running && (
            <>
              <Tag style={{ margin: "10px 0 6px" }} color={isError ? "error" : "default"}>
                输出
              </Tag>
              <pre className={isError ? "oc-pre err" : "oc-pre"}>{result || "(无输出)"}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
