import { Badge, Button, Empty, Select, Space, Tooltip, Typography } from "antd";
import { FolderAddOutlined, PlusOutlined, SettingOutlined } from "@ant-design/icons";
import type { Project, TaskSummary } from "../api";

const DOT: Record<TaskSummary["status"], "processing" | "default" | "error"> = {
  working: "processing",
  idle: "default",
  error: "error",
};

function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

interface Props {
  projects: Project[];
  project: string;
  tasks: TaskSummary[];
  activeId: string | null;
  onProject: (name: string) => void;
  onNewProject: () => void;
  onNewTask: () => void;
  onOpenTask: (t: TaskSummary) => void;
  onSettings: () => void;
}

export default function Sidebar({
  projects,
  project,
  tasks,
  activeId,
  onProject,
  onNewProject,
  onNewTask,
  onOpenTask,
  onSettings,
}: Props) {
  return (
    <div className="oc-sider">
      <div className="oc-brand">
        <div className="oc-brand-mark">OC</div>
        <div style={{ lineHeight: 1.2 }}>
          <div style={{ fontWeight: 600, fontSize: 14 }}>Open Claude</div>
          <div style={{ fontSize: 11, color: "#7c828d", letterSpacing: "0.08em" }}>AGENT</div>
        </div>
      </div>

      <div style={{ padding: "0 12px 10px" }}>
        <Space.Compact style={{ width: "100%" }}>
          <Select
            style={{ flex: 1 }}
            value={project || undefined}
            placeholder="选择项目"
            options={projects.map((p) => ({ value: p.name, label: p.name }))}
            onChange={onProject}
            notFoundContent={<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无项目" />}
          />
          <Tooltip title="新建项目">
            <Button icon={<FolderAddOutlined />} onClick={onNewProject} />
          </Tooltip>
        </Space.Compact>

        <Button
          type="primary"
          block
          icon={<PlusOutlined />}
          style={{ marginTop: 8 }}
          disabled={!project}
          onClick={onNewTask}
        >
          新任务
        </Button>
      </div>

      <div className="oc-tasklist">
        {tasks.length === 0 ? (
          <Typography.Text style={{ fontSize: 12, color: "#7c828d", padding: "8px 10px", display: "block" }}>
            还没有任务
          </Typography.Text>
        ) : (
          tasks.map((t) => (
            <div
              key={t.id}
              className={`oc-task-row${t.id === activeId ? " active" : ""}`}
              onClick={() => onOpenTask(t)}
            >
              <Space size={6} style={{ width: "100%" }}>
                <Badge status={DOT[t.status]} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="oc-task-title">{t.title}</div>
                  <div style={{ fontSize: 11, color: "#7c828d" }}>
                    {t.project} · {ago(t.updated)}
                  </div>
                </div>
              </Space>
            </div>
          ))
        )}
      </div>

      <div style={{ borderTop: "1px solid #2a2d34", padding: 10 }}>
        <Button block type="text" icon={<SettingOutlined />} onClick={onSettings}>
          模型参数
        </Button>
      </div>
    </div>
  );
}
