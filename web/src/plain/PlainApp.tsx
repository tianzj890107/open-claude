import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Collapse, Spin, Tag, Tooltip, message } from "antd";
import {
  DatabaseOutlined,
  EditOutlined,
  LockOutlined,
  ThunderboltFilled,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { CopilotChat, CopilotKitProvider, HttpAgent } from "@copilotkit/react-core/v2";
import { logToMessages } from "../replay";
import type { LogEvent } from "../api";
import type { Meta } from "../chat/api";
import SettingsModal, { PANELS } from "../shared/SettingsModal";

const LABELS = {
  chatInputPlaceholder: "给对话助手发送消息…",
  chatDisclaimerText: "这是纯对话助手,不会读写本地文件或执行命令。请核实重要信息。",
  welcomeMessageText: "在这里和模型自由对话",
  assistantMessageToolbarCopyMessageLabel: "复制",
  assistantMessageToolbarCopyCodeLabel: "复制代码",
  assistantMessageToolbarCopyCodeCopiedLabel: "已复制",
  assistantMessageToolbarRegenerateLabel: "重新生成",
  userMessageToolbarCopyMessageLabel: "复制",
  userMessageToolbarEditMessageLabel: "编辑",
};

/**
 * The pure-conversation surface: one bridge, one conversation, no tools.
 *
 * Deliberately absent compared with the other two apps — there are no tool-call
 * renderers, no attachments and no output panel, because this backend hands the
 * model an empty tool list and runs with OC_READONLY_FS. Nothing can arrive
 * that would need them.
 */
function Thread({ epoch }: { epoch: number }) {
  const agent = useMemo(() => new HttpAgent({ url: "/api/agui" }), []);
  const [ready, setReady] = useState(false);

  // Restore the transcript so a reload doesn't lose the thread (the bridge is
  // the authority; `epoch` bumps on 新对话 to force a clean remount).
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await fetch("/api/history");
        const { log } = (await r.json()) as { log: LogEvent[] };
        if (alive) agent.messages = logToMessages(log ?? []);
      } finally {
        if (alive) setReady(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [agent, epoch]);

  if (!ready) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
        <Spin />
      </div>
    );
  }

  return (
    <CopilotKitProvider agents__unsafe_dev_only={{ default: agent }}>
      <div className="cc-chat">
        <CopilotChat labels={LABELS} />
      </div>
    </CopilotKitProvider>
  );
}

export default function PlainApp() {
  const [meta, setMeta] = useState<(Meta & { profile?: string }) | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [settings, setSettings] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setMeta(await fetch("/api/meta").then((r) => r.json()));
      } catch (e) {
        message.error(`无法连接后端:${(e as Error).message}`);
      }
    })();
  }, []);

  const newChat = useCallback(async () => {
    try {
      await fetch("/api/new", { method: "POST" });
      setEpoch((e) => e + 1);
    } catch (e) {
      message.error((e as Error).message);
    }
  }, []);

  if (!meta) {
    return (
      <div style={{ height: "100vh", display: "grid", placeItems: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  const groups = [
    {
      key: "set",
      label: "设置",
      icon: <DatabaseOutlined />,
      items: Object.keys(PANELS).filter((k) => PANELS[k].cat === "设置"),
    },
    {
      key: "log",
      label: "日志",
      icon: <UnorderedListOutlined />,
      items: Object.keys(PANELS).filter((k) => PANELS[k].cat === "日志"),
    },
  ];

  return (
    <div className="cc-app">
      <aside className="cc-side">
        <div className="cc-brand">
          <div className="cc-mark">
            <ThunderboltFilled style={{ fontSize: 15 }} />
          </div>
          <span style={{ fontSize: 15, fontWeight: 500, flex: 1 }}>Open Claude</span>
        </div>

        <Button icon={<EditOutlined />} onClick={newChat} style={{ borderRadius: 11 }}>
          新对话
        </Button>

        <div className="cc-nav">
          <Collapse
            ghost
            size="small"
            items={groups.map((g) => ({
              key: g.key,
              label: (
                <span style={{ fontSize: 13 }}>
                  {g.icon} <span style={{ marginLeft: 6 }}>{g.label}</span>
                </span>
              ),
              children: (
                <div style={{ display: "flex", flexDirection: "column" }}>
                  {g.items.map((k) => (
                    <button key={k} className="cc-item" onClick={() => setSettings(k)}>
                      {PANELS[k].title}
                    </button>
                  ))}
                </div>
              ),
            }))}
          />

          <div className="cc-seclabel">会话</div>
          <button className="cc-item on">当前会话</button>
        </div>

        <div className="cc-userrow">
          <div className="cc-avatar">OC</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13 }}>对话助手</div>
            <div style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
              profile: {meta.profile ?? "default"}
            </div>
          </div>
        </div>
      </aside>

      <main className="cc-main">
        <div className="cc-topbar">
          <div style={{ flex: 1 }} />
          <Tooltip title="此界面没有任何工具,无法读写文件或执行命令">
            <Tag icon={<LockOutlined />} style={{ borderRadius: 8 }}>
              纯对话
            </Tag>
          </Tooltip>
        </div>

        <Thread key={epoch} epoch={epoch} />
      </main>

      <SettingsModal
        panel={settings}
        meta={meta}
        onMeta={setMeta}
        onClose={() => setSettings(null)}
      />
    </div>
  );
}
