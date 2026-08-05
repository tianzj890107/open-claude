/** REST surface of oc_chat_server.py. Note what is absent: no paths, no file
 *  tree, no project — the workspace behind a conversation is not addressable. */

import type { LogEvent } from "../api";

export interface ChatSummary {
  id: string;
  title: string;
  status: "idle" | "working" | "error";
  created: number;
  updated: number;
}

export interface Artifact {
  id: string;
  name: string;
  kind: "image" | "pdf" | "markdown" | "html" | "table" | "data" | "document" | "code" | "text" | "file";
  size: number;
  created: number;
}

export interface ChatDetail extends ChatSummary {
  log: LogEvent[];
  artifacts: Artifact[];
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
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body as { error?: string }).error || `HTTP ${res.status}`);
  return body as T;
}

function post<T>(url: string, data?: unknown): Promise<T> {
  return req<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data ?? {}),
  });
}

export const chatApi = {
  meta: () => req<Meta>("/api/meta"),
  chats: () => req<{ chats: ChatSummary[] }>("/api/chats").then((r) => r.chats),
  chat: (id: string) => req<ChatDetail>(`/api/chats/${id}`),
  create: () => post<ChatSummary>("/api/chats"),
  upload: (chat: string, name: string, data: string) =>
    post<{ ok: true; name: string; id: string; url: string }>("/api/upload", {
      chat,
      name,
      data,
    }),
  setModel: (model: string) => post<{ model: string }>("/api/model", { model }),
  setParams: (p: Partial<Params>) => post<Params>("/api/params", p),
};

/** Artifacts are reachable only through their opaque id. */
export const artifactURL = (chat: string, id: string, download = false) =>
  `/api/artifact/${chat}/${id}${download ? "?download=1" : ""}`;

export function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
