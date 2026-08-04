import { useEffect, useState } from "react";
import { Alert, Button, Modal, Typography } from "antd";
import { useAgent } from "@copilotkit/react-core/v2";
import { api, type ApprovalRequest } from "../api";
import { MONO } from "../theme";

const TITLES: Record<string, string> = {
  Bash: "执行命令",
  Write: "覆盖已有文件",
  Edit: "修改文件",
};

/**
 * Dangerous tools pause server-side and wait for a human. The agent announces
 * that as an AG-UI CUSTOM event; we surface it as a modal and answer through
 * /api/tasks/<id>/approve, which unblocks the waiting turn.
 */
export default function ApprovalBridge({ taskId }: { taskId: string }) {
  const { agent } = useAgent();
  const [req, setReq] = useState<ApprovalRequest | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const sub = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name === "approval_request") {
          setReq(event.value as ApprovalRequest);
        } else if (event.name === "approval_result") {
          setReq(null); // resolved elsewhere, or timed out server-side
        }
      },
    });
    return () => sub.unsubscribe();
  }, [agent]);

  const answer = async (approved: boolean) => {
    if (!req) return;
    setBusy(true);
    try {
      await api.approve(taskId, req.id, approved);
      setReq(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={!!req}
      title={req ? `需要确认:${TITLES[req.tool] ?? req.summary}` : ""}
      closable={false}
      maskClosable={false}
      footer={[
        <Button key="deny" danger disabled={busy} onClick={() => answer(false)}>
          拒绝
        </Button>,
        <Button key="ok" type="primary" loading={busy} onClick={() => answer(true)}>
          允许
        </Button>,
      ]}
    >
      {req && (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`智能体请求${req.summary},操作仅限沙箱项目目录内。`}
          />
          <Typography.Paragraph
            style={{ fontFamily: MONO, fontSize: 12, whiteSpace: "pre-wrap", marginBottom: 0 }}
          >
            {req.detail}
          </Typography.Paragraph>
        </>
      )}
    </Modal>
  );
}
