import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Empty, Segmented, Space, Spin, Tooltip, Tree, Typography, Upload } from "antd";
import type { DataNode } from "antd/es/tree";
import {
  ArrowLeftOutlined,
  CloseOutlined,
  ExportOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import { api, fileURL, readAsBase64, type ProjectFile } from "../api";

const IMG = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;
const MAX_TEXT = 1_500_000;

function ext(p: string) {
  const i = p.lastIndexOf(".");
  return i < 0 ? "" : p.slice(i).toLowerCase();
}

function toTree(files: ProjectFile[]): DataNode[] {
  const root: DataNode[] = [];
  const dirs = new Map<string, DataNode>();

  const dirNode = (path: string): DataNode | null => {
    if (!path) return null;
    const hit = dirs.get(path);
    if (hit) return hit;
    const idx = path.lastIndexOf("/");
    const node: DataNode = {
      key: `dir:${path}`,
      title: path.slice(idx + 1),
      selectable: false,
      children: [],
    };
    dirs.set(path, node);
    const parent = dirNode(idx < 0 ? "" : path.slice(0, idx));
    (parent ? (parent.children as DataNode[]) : root).push(node);
    return node;
  };

  for (const f of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const idx = f.path.lastIndexOf("/");
    const parent = dirNode(idx < 0 ? "" : f.path.slice(0, idx));
    const node: DataNode = { key: f.path, title: f.path.slice(idx + 1), isLeaf: true };
    (parent ? (parent.children as DataNode[]) : root).push(node);
  }
  return root;
}

interface Props {
  project: string;
  file: string | null;
  onPick: (path: string | null) => void;
  onClose: () => void;
  /** Bumped by the task pane after each turn so new files show up. */
  refreshKey: number;
}

/**
 * Right-hand panel: the project's file tree, and an inline viewer that renders
 * HTML, images, PDF and Markdown rather than dumping bytes.
 */
export default function FilePanel({ project, file, onPick, onClose, refreshKey }: Props) {
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [binary, setBinary] = useState(false);
  const [mode, setMode] = useState<"预览" | "源码">("预览");

  const load = useCallback(async () => {
    if (!project) return;
    setLoading(true);
    try {
      setFiles(await api.files(project));
    } catch {
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }, [project]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const e = file ? ext(file) : "";
  const isImg = IMG.test(file ?? "");
  const isPdf = e === ".pdf";
  const isHtml = e === ".html" || e === ".htm";
  const isMd = e === ".md" || e === ".markdown";

  // Text-ish files are fetched so we can show them with our own chrome; images,
  // PDFs and HTML are handed straight to the browser via the /p/ route.
  useEffect(() => {
    setMode("预览");
    if (!file || isImg || isPdf) {
      setText(null);
      setBinary(false);
      return;
    }
    let alive = true;
    setText(null);
    setBinary(false);
    void (async () => {
      try {
        const res = await fetch(fileURL(project, file));
        const buf = await res.arrayBuffer();
        if (!alive) return;
        if (buf.byteLength > MAX_TEXT) {
          setBinary(true);
          return;
        }
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
  }, [file, project, isImg, isPdf]);

  const treeData = useMemo(() => toTree(files), [files]);

  const upload = async (f: File) => {
    const data = await readAsBase64(f);
    await api.upload(project, f.name, data);
    await load();
    return false;
  };

  const url = file ? fileURL(project, file) : "";

  return (
    <div className="oc-preview">
      <div className="oc-preview-head">
        {file ? (
          <Button size="small" type="text" icon={<ArrowLeftOutlined />} onClick={() => onPick(null)}>
            文件
          </Button>
        ) : (
          <Typography.Text strong style={{ fontSize: 13, paddingLeft: 4 }}>
            项目文件
          </Typography.Text>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          {file && (
            <Typography.Text
              ellipsis
              style={{ fontSize: 12, color: "#a8adb7", display: "block" }}
              title={file}
            >
              {file}
            </Typography.Text>
          )}
        </div>
        <Space size={4}>
          {file && (isHtml || isMd) && (
            <Segmented
              size="small"
              value={mode}
              options={["预览", "源码"]}
              onChange={(v) => setMode(v as "预览" | "源码")}
            />
          )}
          {file && (
            <Tooltip title="新标签页打开">
              <Button
                size="small"
                type="text"
                icon={<ExportOutlined />}
                href={url}
                target="_blank"
              />
            </Tooltip>
          )}
          {!file && (
            <>
              <Upload beforeUpload={upload} showUploadList={false} multiple>
                <Tooltip title="上传文件到项目根目录">
                  <Button size="small" type="text" icon={<UploadOutlined />} />
                </Tooltip>
              </Upload>
              <Tooltip title="刷新">
                <Button size="small" type="text" icon={<ReloadOutlined />} onClick={load} />
              </Tooltip>
            </>
          )}
          <Button size="small" type="text" icon={<CloseOutlined />} onClick={onClose} />
        </Space>
      </div>

      <div className="oc-preview-body">
        {!file ? (
          loading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : files.length === 0 ? (
            <Empty style={{ marginTop: 48 }} description="项目还没有文件" />
          ) : (
            <Tree
              treeData={treeData}
              defaultExpandAll
              blockNode
              style={{ padding: 8, background: "transparent" }}
              onSelect={(keys) => {
                const k = String(keys[0] ?? "");
                if (k && !k.startsWith("dir:")) onPick(k);
              }}
            />
          )
        ) : isImg ? (
          <div style={{ padding: 12, textAlign: "center" }}>
            <img src={url} alt={file} style={{ maxWidth: "100%" }} />
          </div>
        ) : isPdf ? (
          <iframe className="oc-preview-frame" src={url} title={file} />
        ) : isHtml && mode === "预览" ? (
          // Sandboxed: generated pages may run scripts but cannot touch this app.
          <iframe className="oc-preview-frame" src={url} title={file} sandbox="allow-scripts" />
        ) : binary ? (
          <Empty style={{ marginTop: 48 }} description="二进制文件或体积过大,无法内联预览" />
        ) : text === null ? (
          <div style={{ padding: 24, textAlign: "center" }}>
            <Spin />
          </div>
        ) : isMd && mode === "预览" ? (
          <div className="oc-md">
            <ReactMarkdown>{text}</ReactMarkdown>
          </div>
        ) : (
          <pre className="oc-code">{text}</pre>
        )}
      </div>
    </div>
  );
}
