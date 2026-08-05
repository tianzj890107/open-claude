import { useEffect, useMemo, useState } from "react";
import { Button, Empty, List, Segmented, Space, Spin, Table, Tooltip, Typography } from "antd";
import {
  ArrowLeftOutlined,
  CloseOutlined,
  DownloadOutlined,
  FileExcelOutlined,
  FileImageOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FileUnknownOutlined,
  Html5Outlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import { artifactURL, humanSize, type Artifact } from "./api";

const MAX_TEXT = 2_000_000;

export const KIND_ICON: Record<Artifact["kind"], React.ReactNode> = {
  image: <FileImageOutlined />,
  pdf: <FilePdfOutlined />,
  markdown: <FileMarkdownOutlined />,
  html: <Html5Outlined />,
  table: <FileExcelOutlined />,
  data: <FileTextOutlined />,
  document: <FileExcelOutlined />,
  code: <FileTextOutlined />,
  text: <FileTextOutlined />,
  file: <FileUnknownOutlined />,
};

/** Minimal RFC4180-ish parser — enough for the CSVs an assistant produces. */
function parseCSV(text: string, sep: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else quoted = false;
      } else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === sep) {
      row.push(cell);
      cell = "";
    } else if (c === "\n") {
      row.push(cell.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      cell = "";
    } else cell += c;
  }
  if (cell || row.length) {
    row.push(cell.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows.filter((r) => r.some((v) => v !== ""));
}

function TableView({ text, name }: { text: string; name: string }) {
  const rows = useMemo(
    () => parseCSV(text, name.toLowerCase().endsWith(".tsv") ? "\t" : ","),
    [text, name],
  );
  if (rows.length === 0) return <Empty style={{ marginTop: 48 }} description="表格为空" />;
  const [header, ...body] = rows;
  return (
    <Table
      size="small"
      scroll={{ x: "max-content" }}
      pagination={body.length > 50 ? { pageSize: 50, size: "small" } : false}
      style={{ padding: 12 }}
      columns={header.map((h, i) => ({ title: h || `列 ${i + 1}`, dataIndex: String(i), key: String(i) }))}
      dataSource={body.map((r, ri) => {
        const o: Record<string, string> = { key: String(ri) };
        r.forEach((v, ci) => (o[String(ci)] = v));
        return o;
      })}
    />
  );
}

interface Props {
  chatId: string;
  artifacts: Artifact[];
  selected: Artifact | null;
  onSelect: (a: Artifact | null) => void;
  onClose: () => void;
}

/**
 * Right-hand viewer for what the assistant produced. It shows *documents*, not
 * a filesystem: every artifact is addressed by an opaque id, and there is no
 * way from here to browse or name anything else in the workspace.
 */
export default function ArtifactPanel({ chatId, artifacts, selected, onSelect, onClose }: Props) {
  const [text, setText] = useState<string | null>(null);
  const [binary, setBinary] = useState(false);
  const [mode, setMode] = useState<"预览" | "源码">("预览");

  const url = selected ? artifactURL(chatId, selected.id) : "";
  const kind = selected?.kind;
  const inlineByBrowser = kind === "image" || kind === "pdf";

  useEffect(() => {
    setMode("预览");
    if (!selected || inlineByBrowser) {
      setText(null);
      setBinary(false);
      return;
    }
    let alive = true;
    setText(null);
    setBinary(false);
    void (async () => {
      try {
        const buf = await fetch(url).then((r) => r.arrayBuffer());
        if (!alive) return;
        if (buf.byteLength > MAX_TEXT) return setBinary(true);
        try {
          setText(new TextDecoder("utf-8", { fatal: true }).decode(buf));
        } catch {
          setBinary(true);
        }
      } catch {
        if (alive) setBinary(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [selected, url, inlineByBrowser]);

  const toggleable = kind === "markdown" || kind === "html" || kind === "table";

  return (
    <div className="cc-panel">
      <div className="cc-panel-head">
        {selected ? (
          <Button size="small" type="text" icon={<ArrowLeftOutlined />} onClick={() => onSelect(null)} />
        ) : null}
        <Typography.Text strong ellipsis style={{ flex: 1, fontSize: 13 }}>
          {selected ? selected.name : "生成的内容"}
        </Typography.Text>
        <Space size={4}>
          {selected && toggleable && (
            <Segmented
              size="small"
              value={mode}
              options={["预览", "源码"]}
              onChange={(v) => setMode(v as "预览" | "源码")}
            />
          )}
          {selected && (
            <Tooltip title="下载">
              <Button
                size="small"
                type="text"
                icon={<DownloadOutlined />}
                href={artifactURL(chatId, selected.id, true)}
              />
            </Tooltip>
          )}
          <Button size="small" type="text" icon={<CloseOutlined />} onClick={onClose} />
        </Space>
      </div>

      <div className="cc-panel-body">
        {!selected ? (
          artifacts.length === 0 ? (
            <Empty style={{ marginTop: 56 }} description="助手还没有生成文件" />
          ) : (
            <List
              dataSource={artifacts}
              renderItem={(a) => (
                <List.Item style={{ padding: "10px 14px", cursor: "pointer" }} onClick={() => onSelect(a)}>
                  <List.Item.Meta
                    avatar={<span style={{ fontSize: 18, opacity: 0.7 }}>{KIND_ICON[a.kind]}</span>}
                    title={<span style={{ fontSize: 13 }}>{a.name}</span>}
                    description={<span style={{ fontSize: 11 }}>{humanSize(a.size)}</span>}
                  />
                </List.Item>
              )}
            />
          )
        ) : kind === "image" ? (
          <div style={{ padding: 12, textAlign: "center" }}>
            <img src={url} alt={selected.name} style={{ maxWidth: "100%" }} />
          </div>
        ) : kind === "pdf" ? (
          <iframe className="cc-frame" src={url} title={selected.name} />
        ) : kind === "html" && mode === "预览" ? (
          // Sandboxed: a generated page may run scripts but cannot reach this app.
          <iframe className="cc-frame" src={url} title={selected.name} sandbox="allow-scripts" />
        ) : binary ? (
          <Empty
            style={{ marginTop: 56 }}
            description={
              <Space direction="vertical">
                <span>这种文件无法在页面内预览</span>
                <Button
                  type="primary"
                  icon={<DownloadOutlined />}
                  href={artifactURL(chatId, selected.id, true)}
                >
                  下载
                </Button>
              </Space>
            }
          />
        ) : text === null ? (
          <div style={{ padding: 32, textAlign: "center" }}>
            <Spin />
          </div>
        ) : kind === "markdown" && mode === "预览" ? (
          <div className="cc-doc">
            <ReactMarkdown>{text}</ReactMarkdown>
          </div>
        ) : kind === "table" && mode === "预览" ? (
          <TableView text={text} name={selected.name} />
        ) : (
          <pre className="cc-code">{text}</pre>
        )}
      </div>
    </div>
  );
}
