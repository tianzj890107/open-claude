import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Collapse, Spin, Tag, Tooltip, Typography, message } from "antd";
import {
  DatabaseOutlined,
  EditOutlined,
  FileTextOutlined,
  ThunderboltFilled,
  UnorderedListOutlined,
} from "@ant-design/icons";
import {
  CopilotChat,
  CopilotKitProvider,
  HttpAgent,
  defineToolCallRenderer,
  useAgent,
} from "@copilotkit/react-core/v2";
import { readAsBase64 } from "../api";
import { logToMessages } from "../replay";
import { chatApi, type Artifact, type ChatSummary, type Meta } from "./api";
import ActivityCard from "./ActivityCard";
import ArtifactPanel, { KIND_ICON } from "./ArtifactPanel";
import SettingsModal, { PANELS } from "../shared/SettingsModal";

const LABELS = {
  chatInputPlaceholder: "给助手发送消息…",
  chatDisclaimerText: "助手可能会出错,请核实重要信息。",
  welcomeMessageText: "有什么可以帮你的?",
  assistantMessageToolbarCopyMessageLabel: "复制",
  assistantMessageToolbarCopyCodeLabel: "复制代码",
  assistantMessageToolbarCopyCodeCopiedLabel: "已复制",
  assistantMessageToolbarRegenerateLabel: "重新生成",
  userMessageToolbarCopyMessageLabel: "复制",
  userMessageToolbarEditMessageLabel: "编辑",
};

const TOOL_RENDERERS = [
  defineToolCallRenderer({
    name: "*",
    render: (props) => (
      <ActivityCard
        name={props.name}
        args={(props.args ?? {}) as Record<string, unknown>}
        status={props.status}
        result={typeof props.result === "string" ? props.result : undefined}
      />
    ),
  }),
];

/** Watches the run for finished turns and for the artifacts a turn produced. */
function RunBridge({
  onArtifacts,
  onEnd,
}: {
  onArtifacts: (a: Artifact[]) => void;
  onEnd: () => void;
}) {
  const { agent } = useAgent();
  useEffect(() => {
    const sub = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name === "artifacts") onArtifacts(event.value as Artifact[]);
      },
      onRunFinalized: () => onEnd(),
      onRunFailed: () => onEnd(),
    });
    return () => sub.unsubscribe();
  }, [agent, onArtifacts, onEnd]);
  return null;
}

interface ThreadProps {
  chat: ChatSummary;
  onArtifacts: (a: Artifact[]) => void;
  onEnd: () => void;
}

function Thread({ chat, onArtifacts, onEnd }: ThreadProps) {
  const agent = useMemo(
    () => new HttpAgent({ url: `/api/agui?chat=${encodeURIComponent(chat.id)}` }),
    [chat.id],
  );
  const [ready, setReady] = useState(false);

  // Wires the composer's “+” to our upload route: the bytes go into the hidden
  // workspace, and the chip the user sees points at an opaque id, never a path.
  const attachments = useMemo(
    () => ({
      enabled: true,
      maxSize: 20 * 1024 * 1024,
      onUpload: async (file: File) => {
        const r = await chatApi.upload(chat.id, file.name, await readAsBase64(file));
        return {
          type: "url" as const,
          value: r.url,
          mimeType: file.type || undefined,
          metadata: { filename: r.name },
        };
      },
      onUploadFailed: ({ message: m }: { message: string }) => message.error(m),
    }),
    [chat.id],
  );

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const detail = await chatApi.chat(chat.id);
        if (alive) agent.messages = logToMessages(detail.log);
      } finally {
        if (alive) setReady(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [agent, chat.id]);

  if (!ready) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
        <Spin />
      </div>
    );
  }

  return (
    <CopilotKitProvider agents__unsafe_dev_only={{ default: agent }} renderToolCalls={TOOL_RENDERERS}>
      <div className="cc-chat">
        <CopilotChat labels={LABELS} attachments={attachments} />
      </div>
      <RunBridge onArtifacts={onArtifacts} onEnd={onEnd} />
    </CopilotKitProvider>
  );
}

export default function ChatApp() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [active, setActive] = useState<ChatSummary | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [settings, setSettings] = useState<string | null>(null);

  const newChat = useCallback(async () => {
    const c = await chatApi.create();
    setActive(c);
    setArtifacts([]);
    setSelected(null);
    setPanelOpen(false);
    setChats(await chatApi.chats());
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        setMeta(await chatApi.meta());
        await newChat();
      } catch (e) {
        message.error(`无法连接后端:${(e as Error).message}`);
      }
    })();
  }, [newChat]);

  const openChat = async (c: ChatSummary) => {
    const detail = await chatApi.chat(c.id);
    setActive(c);
    setArtifacts(detail.artifacts);
    setSelected(null);
  };

  // New files pop the panel open the first time — that is the moment the user
  // needs to know something was produced.
  const onArtifacts = useCallback((fresh: Artifact[]) => {
    setArtifacts((prev) => {
      const byId = new Map(prev.map((a) => [a.id, a]));
      for (const a of fresh) byId.set(a.id, a);
      return [...byId.values()].sort((a, b) => b.created - a.created);
    });
    setPanelOpen(true);
  }, []);

  const onEnd = useCallback(() => {
    void chatApi.chats().then(setChats);
  }, []);

  if (!meta || !active) {
    return (
      <div style={{ height: "100vh", display: "grid", placeItems: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  const groups = [
    { key: "set", label: "设置", icon: <DatabaseOutlined />, items: Object.keys(PANELS).filter((k) => PANELS[k].cat === "设置") },
    { key: "log", label: "日志", icon: <UnorderedListOutlined />, items: Object.keys(PANELS).filter((k) => PANELS[k].cat === "日志") },
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
          {chats.map((c) => (
            <button
              key={c.id}
              className={`cc-item${c.id === active.id ? " on" : ""}`}
              onClick={() => void openChat(c)}
              title={c.title}
            >
              {c.title}
            </button>
          ))}
        </div>

        <div className="cc-userrow">
          <div className="cc-avatar">OC</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13 }}>对话助手</div>
            <div style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
              {artifacts.length > 0 ? `${artifacts.length} 个生成文件` : "随时可用"}
            </div>
          </div>
        </div>
      </aside>

      <main className="cc-main">
        {/* Only one control here: everything else lives in the left sidebar,
            and attaching files belongs in the composer's “+”. */}
        <div className="cc-topbar">
          <div style={{ flex: 1 }} />
          <Tooltip title="生成的内容">
            <Button
              type="text"
              icon={<FileTextOutlined />}
              onClick={() => {
                setSelected(null);
                setPanelOpen((v) => !v);
              }}
            />
          </Tooltip>
        </div>

        <Thread key={active.id} chat={active} onArtifacts={onArtifacts} onEnd={onEnd} />

        {artifacts.length > 0 && (
          <div className="cc-outputs">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              生成的文件
            </Typography.Text>
            {artifacts.slice(0, 6).map((a) => (
              <Tag
                key={a.id}
                icon={KIND_ICON[a.kind]}
                style={{ cursor: "pointer", borderRadius: 8, padding: "3px 9px" }}
                onClick={() => {
                  setSelected(a);
                  setPanelOpen(true);
                }}
              >
                {a.name}
              </Tag>
            ))}
          </div>
        )}
      </main>

      {panelOpen && (
        <ArtifactPanel
          chatId={active.id}
          artifacts={artifacts}
          selected={selected}
          onSelect={setSelected}
          onClose={() => setPanelOpen(false)}
        />
      )}

      <SettingsModal panel={settings} meta={meta} onMeta={setMeta} onClose={() => setSettings(null)} />
    </div>
  );
}
