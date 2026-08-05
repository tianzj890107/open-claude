import { Alert, Form, InputNumber, Modal, Radio, Slider, Switch, Typography, message } from "antd";
import type { Meta } from "../chat/api";

/** All three servers expose the same /api/meta, /api/model and /api/params
 *  shapes, so this panel is shared verbatim across the surfaces. */
const post = async (url: string, data: unknown) => {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body as { error?: string }).error || `HTTP ${res.status}`);
  return body;
};

const settingsApi = {
  meta: () => fetch("/api/meta").then((r) => r.json() as Promise<Meta>),
  setModel: (model: string) => post("/api/model", { model }),
  setParams: (p: Record<string, unknown>) => post("/api/params", p),
};

/** Sidebar panels carried over from the original UI. Only 模型参数 is wired up;
 *  the rest describe planned capabilities and say so plainly. */
export const PANELS: Record<string, { cat: string; title: string; desc: string }> = {
  datasource: {
    cat: "设置",
    title: "数据源",
    desc: "配置并管理外部数据源连接,例如数据库、文件、API 与第三方服务,作为知识库与检索的输入。",
  },
  knowledge: {
    cat: "设置",
    title: "知识库",
    desc: "管理用于检索增强(RAG)的文档集合与索引:上传、分块、向量化与命中调试。",
  },
  ontology: {
    cat: "设置",
    title: "本体",
    desc: "维护领域本体 / 概念模型:实体、关系、属性与推理规则的定义和编辑。",
  },
  modelparams: {
    cat: "设置",
    title: "模型参数",
    desc: "调整推理参数:温度、最大输出 token、思考预算等,切换立即生效。",
  },
  memory: {
    cat: "设置",
    title: "记忆管理",
    desc: "查看与编辑长期记忆条目、记忆写入模式与清理 / 归档策略。",
  },
  roles: {
    cat: "设置",
    title: "角色管理",
    desc: "管理助手角色 / 人设(profile):系统提示、工具范围、权限与默认模型。",
  },
  prompts: {
    cat: "设置",
    title: "提示词管理",
    desc: "维护系统提示词与可复用的提示词模板库,支持版本与变量。",
  },
  "log-ontology": {
    cat: "日志",
    title: "本体日志",
    desc: "记录本体的变更历史与推理过程:谁在何时改动了哪些实体 / 关系 / 规则。",
  },
  "log-llm": {
    cat: "日志",
    title: "大模型日志",
    desc: "记录每次模型调用的请求 / 响应、token 用量、耗时与成本,便于审计与排错。",
  },
  "log-tool": {
    cat: "日志",
    title: "工具日志",
    desc: "记录工具 / 子代理的调用入参、执行结果与错误,用于追踪助手行为。",
  },
};

interface Props {
  panel: string | null;
  meta: Meta;
  onMeta: (m: Meta) => void;
  onClose: () => void;
}

export default function SettingsModal({ panel, meta, onMeta, onClose }: Props) {
  const p = panel ? PANELS[panel] : null;

  const patch = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      onMeta(await settingsApi.meta());
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  return (
    <Modal
      open={!!p}
      title={p?.title}
      onCancel={onClose}
      footer={null}
      width={560}
      destroyOnClose
    >
      {p && panel !== "modelparams" && (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
            {p.desc}
          </Typography.Paragraph>
          <Alert type="info" message="该面板为前端占位,功能正在开发中(等待后端接入)。" />
        </>
      )}

      {panel === "modelparams" && (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
            选择对话使用的模型(切换立即生效):
          </Typography.Paragraph>
          <Radio.Group
            value={meta.model}
            style={{ display: "flex", flexDirection: "column", gap: 6, width: "100%" }}
            onChange={(e) => void patch(() => settingsApi.setModel(e.target.value))}
          >
            {meta.models.map((m) => (
              <Radio.Button
                key={m.id}
                value={m.id}
                style={{ height: "auto", padding: "8px 11px", borderRadius: 10, width: "100%" }}
              >
                {m.label}
                <span style={{ float: "right", fontSize: 11, opacity: 0.55 }}>{m.id}</span>
              </Radio.Button>
            ))}
          </Radio.Group>

          <Typography.Title level={5} style={{ marginTop: 24 }}>
            推理参数
          </Typography.Title>
          <Form layout="vertical">
            <Form.Item label="最大输出 token" extra={`留空 = 使用默认 ${meta.params.default_max_tokens}`}>
              <InputNumber
                style={{ width: 160 }}
                min={1}
                step={256}
                placeholder={String(meta.params.default_max_tokens)}
                value={meta.params.max_tokens ?? undefined}
                onChange={(v) => void patch(() => settingsApi.setParams({ max_tokens: v ?? null }))}
              />
            </Form.Item>

            <Form.Item label="温度 (temperature)" extra="越高越发散,范围 0–2;开启思考后由模型固定为 1">
              <Slider
                min={0}
                max={2}
                step={0.1}
                disabled={meta.params.thinking}
                value={meta.params.temperature ?? 1}
                onChangeComplete={(v) => void patch(() => settingsApi.setParams({ temperature: v }))}
              />
            </Form.Item>

            <Form.Item label="扩展思考">
              <Switch
                checked={meta.params.thinking}
                onChange={(v) => void patch(() => settingsApi.setParams({ thinking: v }))}
              />
            </Form.Item>

            {meta.params.thinking && (
              <Form.Item label="思考预算 token" extra="≥ 1024">
                <InputNumber
                  style={{ width: 160 }}
                  min={1024}
                  step={512}
                  value={meta.params.thinking_budget}
                  onChange={(v) =>
                    void patch(() => settingsApi.setParams({ thinking_budget: v ?? 8000 }))
                  }
                />
              </Form.Item>
            )}
          </Form>
        </>
      )}
    </Modal>
  );
}
