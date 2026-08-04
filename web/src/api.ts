/** Thin typed wrapper over the Python server's REST surface. */

export interface Project {
  name: string;
  mtime: number;
}

export interface TaskSummary {
  id: string;
  project: string;
  title: string;
  status: "idle" | "working" | "error";
  created: number;
  updated: number;
}

/** One entry of a task's replayable event log (see Task.log server-side). */
export type LogEvent =
  | { type: "user"; text: string }
  | { type: "assistant"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; content: string; is_error: boolean }
  | { type: "error"; error: string }
  | { type: string; [k: string]: unknown };

export interface TaskDetail extends TaskSummary {
  log: LogEvent[];
}

export interface Params {
  temperature: number | null;
  max_tokens: number | null;
  thinking: boolean;
  thinking_budget: number;
  default_max_tokens: number;
}

export interface Meta {
  model: string;
  models: { id: string; label: string }[];
  params: Params;
  sandbox: string;
  projects: Project[];
}

export interface ProjectFile {
  path: string;
  size: number;
  mtime: number;
}

/** Payload of the CUSTOM `approval_request` event the agent emits mid-turn. */
export interface ApprovalRequest {
  id: string;
  tool: string;
  summary: string;
  detail: string;
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body as { error?: string }).error || `HTTP ${res.status}`);
  return body as T;
}

function post<T>(url: string, data: unknown): Promise<T> {
  return req<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export const api = {
  meta: () => req<Meta>("/api/meta"),
  projects: () => req<{ projects: Project[] }>("/api/projects").then((r) => r.projects),
  createProject: (name: string) => post<{ ok: true; name: string }>("/api/projects", { name }),

  tasks: () => req<{ tasks: TaskSummary[] }>("/api/tasks").then((r) => r.tasks),
  task: (id: string) => req<TaskDetail>(`/api/tasks/${id}`),
  createTask: (project: string) => post<TaskSummary>("/api/tasks", { project }),
  approve: (taskId: string, id: string, approved: boolean) =>
    post<{ ok: true }>(`/api/tasks/${taskId}/approve`, { id, approved }),

  files: (project: string) =>
    req<{ files: ProjectFile[] }>(`/api/files?project=${encodeURIComponent(project)}`).then(
      (r) => r.files,
    ),
  upload: (project: string, name: string, data: string) =>
    post<{ ok: true; name: string; replaced?: boolean; unchanged?: boolean }>("/api/upload", {
      project,
      name,
      data,
    }),

  setModel: (model: string) => post<{ model: string }>("/api/model", { model }),
  setParams: (p: Partial<Params>) => post<Params>("/api/params", p),
};

/** URL of a file inside a project, as served by the sandbox-confined /p/ route. */
export function fileURL(project: string, rel: string): string {
  const parts = rel.split(/[\\/]/).filter(Boolean).map(encodeURIComponent);
  return `/p/${encodeURIComponent(project)}/${parts.join("/")}`;
}

/** Read a browser File as bare base64 (no data: prefix) for /api/upload. */
export function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("读取文件失败"));
    reader.onload = () => {
      const s = String(reader.result);
      resolve(s.slice(s.indexOf(",") + 1));
    };
    reader.readAsDataURL(file);
  });
}
