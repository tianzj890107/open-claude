import { useState } from "react";
import { Alert, Drawer, Form, InputNumber, Select, Switch, Typography, message } from "antd";
import { api, type Meta } from "../api";
import { MONO } from "../theme";

interface Props {
  open: boolean;
  meta: Meta;
  onClose: () => void;
  onMeta: (m: Meta) => void;
}

/** Model + inference parameters, applied to every live task immediately. */
export default function SettingsDrawer({ open, meta, onClose, onMeta }: Props) {
  const [saving, setSaving] = useState(false);

  const patch = async (fn: () => Promise<unknown>, label: string) => {
    setSaving(true);
    try {
      await fn();
      onMeta(await api.meta());
      message.success(`${label}已更新`);
    } catch (e) {
      message.error(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer title="模型参数" open={open} onClose={onClose} width={380}>
      <Form layout="vertical" disabled={saving}>
        <Form.Item label="模型">
          <Select
            value={meta.model}
            options={meta.models.map((m) => ({ value: m.id, label: m.label }))}
            onChange={(v) => void patch(() => api.setModel(v), "模型")}
          />
        </Form.Item>

        <Form.Item
          label="temperature"
          extra="留空使用模型默认值。开启思考时该值会被忽略。"
        >
          <InputNumber
            style={{ width: "100%" }}
            min={0}
            max={2}
            step={0.1}
            value={meta.params.temperature ?? undefined}
            onChange={(v) => void patch(() => api.setParams({ temperature: v ?? null }), "temperature")}
          />
        </Form.Item>

        <Form.Item
          label="max_tokens"
          extra={`留空使用默认值 ${meta.params.default_max_tokens}`}
        >
          <InputNumber
            style={{ width: "100%" }}
            min={1}
            step={1024}
            value={meta.params.max_tokens ?? undefined}
            onChange={(v) => void patch(() => api.setParams({ max_tokens: v ?? null }), "max_tokens")}
          />
        </Form.Item>

        <Form.Item label="扩展思考">
          <Switch
            checked={meta.params.thinking}
            onChange={(v) => void patch(() => api.setParams({ thinking: v }), "扩展思考")}
          />
        </Form.Item>

        {meta.params.thinking && (
          <Form.Item label="thinking_budget" extra="最小 1024">
            <InputNumber
              style={{ width: "100%" }}
              min={1024}
              step={1024}
              value={meta.params.thinking_budget}
              onChange={(v) =>
                void patch(() => api.setParams({ thinking_budget: v ?? 8000 }), "思考预算")
              }
            />
          </Form.Item>
        )}
      </Form>

      <Alert
        type="info"
        showIcon
        message="沙箱目录"
        description={
          <Typography.Text style={{ fontFamily: MONO, fontSize: 11, wordBreak: "break-all" }}>
            {meta.sandbox}
          </Typography.Text>
        }
      />
    </Drawer>
  );
}
