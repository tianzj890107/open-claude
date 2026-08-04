import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { FileSearchOutlined, FolderAddOutlined, PlusOutlined } from "@ant-design/icons";
import { api, type Meta, type Project, type TaskSummary } from "./api";
import { PreviewProvider } from "./preview";
import Sidebar from "./components/Sidebar";
import TaskPane from "./components/TaskPane";
import FilePanel from "./components/FilePanel";
import SettingsDrawer from "./components/SettingsDrawer";

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [active, setActive] = useState<TaskSummary | null>(null);

  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewFile, setPreviewFile] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const [settings, setSettings] = useState(false);
  const [newProject, setNewProject] = useState(false);
  const [newName, setNewName] = useState("");

  const reloadTasks = useCallback(async () => {
    try {
      setTasks(await api.tasks());
    } catch {
      /* transient — the sidebar keeps showing the previous list */
    }
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const m = await api.meta();
        setMeta(m);
        setProjects(m.projects);
        setProject((p) => p || m.projects[0]?.name || "");
        await reloadTasks();
      } catch (e) {
        message.error(`无法连接后端:${(e as Error).message}`);
      }
    })();
  }, [reloadTasks]);

  const createProject = async () => {
    try {
      const r = await api.createProject(newName.trim());
      setProjects(await api.projects());
      setProject(r.name);
      setNewProject(false);
      setNewName("");
      message.success(`已创建项目 ${r.name}`);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const createTask = async () => {
    if (!project) return;
    try {
      const t = await api.createTask(project);
      setActive(t);
      setPreviewFile(null);
      await reloadTasks();
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  // After every turn: refresh the task list (title/status) and the file tree.
  const onTurnEnd = useCallback(() => {
    void reloadTasks();
    setRefreshKey((k) => k + 1);
  }, [reloadTasks]);

  // Tool cards may carry an absolute path; the /p/ route resolves it against
  // the project and refuses anything that escapes it.
  const openFile = useCallback((path: string) => {
    setPreviewFile(path);
    setPreviewOpen(true);
  }, []);

  const openTree = useCallback(() => {
    setPreviewFile(null);
    setPreviewOpen(true);
  }, []);

  if (!meta) {
    return (
      <div style={{ height: "100vh", display: "grid", placeItems: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  const activeProject = active?.project ?? project;

  return (
    <PreviewProvider value={{ project: activeProject, open: openFile, openTree }}>
      <div className="oc-shell" style={{ display: "flex" }}>
        <div style={{ width: 264, flex: "0 0 264px" }}>
          <Sidebar
            projects={projects}
            project={project}
            tasks={tasks}
            activeId={active?.id ?? null}
            onProject={(p) => {
              setProject(p);
              setActive(null);
            }}
            onNewProject={() => setNewProject(true)}
            onNewTask={createTask}
            onOpenTask={(t) => {
              setActive(t);
              setPreviewFile(null);
            }}
            onSettings={() => setSettings(true)}
          />
        </div>

        <div className="oc-main">
          <div className="oc-center">
            <div className="oc-topbar">
              <Typography.Text strong style={{ flex: 1, minWidth: 0 }} ellipsis>
                {active ? active.title : "Open Claude Agent"}
              </Typography.Text>
              {active && <Tag>{active.project}</Tag>}
              <Tag>{meta.model}</Tag>
              <Tooltip title="项目文件">
                <Button
                  size="small"
                  icon={<FileSearchOutlined />}
                  disabled={!activeProject}
                  onClick={() => (previewOpen ? setPreviewOpen(false) : openTree())}
                />
              </Tooltip>
            </div>

            {active ? (
              <TaskPane
                key={active.id}
                task={active}
                previewFile={previewFile}
                onTurnEnd={onTurnEnd}
              />
            ) : (
              <div className="oc-home">
                <div className="oc-home-inner" style={{ textAlign: "center" }}>
                  <Typography.Title level={3} style={{ marginBottom: 8 }}>
                    接下来要做什么?
                  </Typography.Title>
                  <Typography.Paragraph type="secondary">
                    每个任务都是一次完整能力的智能体会话:读写文件、执行命令、调用技能与子智能体,
                    全部限制在沙箱项目目录内。
                  </Typography.Paragraph>
                  {projects.length === 0 ? (
                    <Empty description="沙箱里还没有项目">
                      <Button
                        type="primary"
                        icon={<FolderAddOutlined />}
                        onClick={() => setNewProject(true)}
                      >
                        新建项目
                      </Button>
                    </Empty>
                  ) : (
                    <Space>
                      <Button
                        type="primary"
                        size="large"
                        icon={<PlusOutlined />}
                        disabled={!project}
                        onClick={createTask}
                      >
                        在 {project || "项目"} 中开始新任务
                      </Button>
                      <Button size="large" onClick={() => setNewProject(true)}>
                        新建项目
                      </Button>
                    </Space>
                  )}
                </div>
              </div>
            )}
          </div>

          {previewOpen && (
            <FilePanel
              project={activeProject}
              file={previewFile}
              onPick={setPreviewFile}
              onClose={() => setPreviewOpen(false)}
              refreshKey={refreshKey}
            />
          )}
        </div>
      </div>

      <Modal
        title="新建项目"
        open={newProject}
        onOk={createProject}
        onCancel={() => setNewProject(false)}
        okText="创建"
        cancelText="取消"
      >
        <Form layout="vertical">
          <Form.Item
            label="项目名"
            extra="将在沙箱目录下创建同名文件夹,只允许中英文、数字与 - _ ."
          >
            <Input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onPressEnter={createProject}
              placeholder="my-project"
            />
          </Form.Item>
        </Form>
      </Modal>

      <SettingsDrawer
        open={settings}
        meta={meta}
        onClose={() => setSettings(false)}
        onMeta={setMeta}
      />
    </PreviewProvider>
  );
}
