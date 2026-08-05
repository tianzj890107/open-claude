"""
Consumer chat server for open-claude.

A ChatGPT/Claude-style assistant for non-developer users. It drives the same
UNMODIFIED open_claude engine as the other surfaces, and the agent keeps its
full tool set so it can actually solve things — run code, crunch a spreadsheet,
draft a document — but everything happens inside a private per-conversation
workspace that the user never sees:

    <repo>/chat_workspaces/<chat-id>/

Two properties distinguish this surface from the developer-facing one
(oc_codex_server.py):

  - The sandbox is *hidden*. There is no file tree, no paths in the UI, and
    every string that reaches the browser is scrubbed of workspace paths.
  - Files the assistant produces are surfaced as **artifacts**: opaque ids with
    a display name, opened in a right-hand viewer. The filesystem behind them
    is never addressable from the client.

Confinement is enforced at the tool-dispatch layer via OC_SANDBOX_ROOT (see
open_claude/tools.py); the CLI never sets it and is unaffected.

Run:
    python oc_chat_server.py [--port 47292]
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from oc_agui import AGUIStream, new_id
from open_claude.repl import Conversation
from open_claude.profile import AgentProfile
from open_claude.api import stream_message
from open_claude.config import (
    AVAILABLE_MODELS,
    PROVIDERS,
    get_api_key_for,
    get_max_tokens,
    get_model,
    get_model_provider,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIST = os.path.join(SCRIPT_DIR, "web", "dist")
CHAT_HTML = os.path.join(WEB_DIST, "chat.html")
WORKSPACES = os.path.join(SCRIPT_DIR, "chat_workspaces")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

PARAM_DEFAULTS = {"temperature": None, "max_tokens": None,
                  "thinking": False, "thinking_budget": 8000}

MAX_UPLOAD = 20 * 1024 * 1024
_SKIP_DIRS = {".git", ".open-claude", "node_modules", "__pycache__", ".venv", "venv"}

# The assistant is a general helper, not a coding agent. It may use the sandbox
# freely, but the user is never shown it — so it must not narrate paths.
STYLE = (
    "你是一个面向普通用户的智能助手,始终使用简体中文回复。"
    "你有一个私有的工作区可以运行代码、处理数据和生成文件,用户看不到它:"
    "不要提及文件路径、目录、沙箱或工作区,也不要让用户去某个路径找文件。"
    "需要计算、分析数据、处理表格或生成文档时,直接用工具做出来,"
    "生成的文件会自动出现在界面右侧供用户查看和下载,你只需按文件名提到它。"
    "回答要简洁、结论先行,不要复述你的内部步骤。"
)


def _stringify(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(blk.get("text") or json.dumps(blk, ensure_ascii=False))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Artifacts — files the assistant produced, exposed by opaque id only
# ---------------------------------------------------------------------------

_KINDS = [
    ("image", {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}),
    ("pdf", {".pdf"}),
    ("markdown", {".md", ".markdown"}),
    ("html", {".html", ".htm"}),
    ("table", {".csv", ".tsv"}),
    ("data", {".json", ".xml", ".yaml", ".yml"}),
    ("document", {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}),
    ("code", {".py", ".js", ".ts", ".sh", ".sql", ".r", ".java", ".c", ".cpp", ".go"}),
    ("text", {".txt", ".log", ".text"}),
]


def _kind_of(name: str) -> str:
    e = os.path.splitext(name)[1].lower()
    for kind, exts in _KINDS:
        if e in exts:
            return kind
    return "file"


def _snapshot(base: str) -> dict[str, float]:
    """Map of relative path -> mtime, used to spot what a turn produced."""
    out: dict[str, float] = {}
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.startswith("."):
                continue
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out[os.path.relpath(fp, base).replace("\\", "/")] = st.st_mtime_ns + st.st_size
    return out


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

class Chat:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.title = "新对话"
        self.created = time.time()
        self.updated = self.created
        self.status = "idle"              # idle | working | error
        self.log: list[dict] = []
        self.lock = threading.Lock()

        self.cwd = os.path.join(WORKSPACES, self.id)
        os.makedirs(self.cwd, exist_ok=True)

        # id -> {"id", "name", "kind", "size", "created"}; path kept server-side
        self.artifacts: dict[str, dict] = {}
        self._paths: dict[str, str] = {}

        self.scrub = _make_scrub(self.cwd)

        # always_allow: a consumer app has nobody to answer permission prompts,
        # and the sandbox — not the prompt — is what keeps this safe.
        self.conv = Conversation(self.cwd, permission_mode="always_allow",
                                 profile=AgentProfile(model=get_model(), style=STYLE))
        self.conv.permissions._prompt_user = lambda *a, **k: (True, "")
        p = self.conv.profile
        p.temperature = PARAM_DEFAULTS["temperature"]
        p.max_tokens = PARAM_DEFAULTS["max_tokens"]
        p.thinking = PARAM_DEFAULTS["thinking"]
        p.thinking_budget = PARAM_DEFAULTS["thinking_budget"]

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status,
                "created": self.created, "updated": self.updated}

    # -- artifacts -------------------------------------------------------------

    def register(self, rel: str) -> dict:
        """Give a produced file an opaque id. Re-registering keeps the same id."""
        for art in self.artifacts.values():
            if art["_rel"] == rel:
                path = self._paths[art["id"]]
                try:
                    art["size"] = os.path.getsize(path)
                except OSError:
                    pass
                art["created"] = time.time()
                return art
        aid = uuid.uuid4().hex[:16]
        path = os.path.join(self.cwd, rel.replace("/", os.sep))
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        art = {"id": aid, "name": os.path.basename(rel), "kind": _kind_of(rel),
               "size": size, "created": time.time(), "_rel": rel}
        self.artifacts[aid] = art
        self._paths[aid] = path
        return art

    def artifact_path(self, aid: str) -> str | None:
        """Resolve an artifact id, re-checking containment before serving it."""
        path = self._paths.get(aid)
        if not path:
            return None
        real = os.path.realpath(path)
        root = os.path.realpath(self.cwd)
        if not real.startswith(root + os.sep) or not os.path.isfile(real):
            return None
        return real

    def public_artifacts(self) -> list[dict]:
        items = [{k: v for k, v in a.items() if not k.startswith("_")}
                 for a in self.artifacts.values()]
        items.sort(key=lambda a: a["created"], reverse=True)
        return items

    # -- one turn --------------------------------------------------------------

    def stream_turn(self, text: str, emit, on_artifacts):
        def rec(ev):
            self.log.append(ev)
            try:
                emit(ev)
            except OSError:
                pass

        with self.lock:
            conv = self.conv
            self.status = "working"
            self.updated = time.time()
            if self.title == "新对话" and text:
                self.title = text[:40]
            self.log.append({"type": "user", "text": text})
            conv.add_user_message(text)

            before = _snapshot(self.cwd)
            text_buf: list[str] = []

            def flush_text():
                if text_buf:
                    self.log.append({"type": "assistant", "text": "".join(text_buf)})
                    text_buf.clear()

            try:
                for _ in range(max(1, conv.profile.max_iterations)):
                    conv._maybe_compact()
                    stop_reason = self._stream_once(conv, emit, text_buf, flush_text)
                    if stop_reason == "tool_use":
                        conv._execute_pending_tools()
                        last = conv.messages[-1] if conv.messages else None
                        if last and last.get("role") == "user" and isinstance(last.get("content"), list):
                            for blk in last["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    rec({"type": "tool_result",
                                         "tool_use_id": blk.get("tool_use_id", ""),
                                         "content": _stringify(blk.get("content", "")),
                                         "is_error": bool(blk.get("is_error", False))})
                        continue
                    break
                self.status = "idle"
            except Exception as e:
                traceback.print_exc()
                self.status = "error"
                rec({"type": "error", "error": str(e)})
            finally:
                flush_text()
                self.updated = time.time()

                # Anything created or changed this turn becomes an artifact.
                after = _snapshot(self.cwd)
                fresh = [rel for rel, stamp in after.items() if before.get(rel) != stamp]
                if fresh:
                    arts = [self.register(rel) for rel in sorted(fresh)]
                    payload = [{k: v for k, v in a.items() if not k.startswith("_")}
                               for a in arts]
                    self.log.append({"type": "artifacts", "artifacts": payload})
                    try:
                        on_artifacts(payload)
                    except OSError:
                        pass

    def _stream_once(self, conv, emit, text_buf, flush_text) -> str:
        tool_uses = []
        turn_text: list[str] = []
        stop_reason = "end_turn"

        gen = stream_message(
            conv.client, conv.messages, conv.system_prompt,
            model=conv.model, tools=conv.tool_schemas,
            max_tokens=conv.profile.max_tokens,
            temperature=conv.profile.temperature,
            thinking_budget=conv.profile.thinking_budget if conv.profile.thinking else None,
        )
        for ev in gen:
            t = ev["type"]
            if t == "text_delta":
                turn_text.append(ev["text"])
                text_buf.append(ev["text"])
                emit({"type": "text", "text": ev["text"]})
            elif t == "thinking_delta":
                emit({"type": "thinking", "text": ev["text"]})
            elif t == "tool_use_end":
                tool_uses.append({"type": "tool_use", "id": ev["id"],
                                  "name": ev["name"], "input": ev["input"]})
                flush_text()
                self.log.append({"type": "tool_use", "id": ev["id"],
                                 "name": ev["name"], "input": ev["input"]})
                emit({"type": "tool_use", "id": ev["id"],
                      "name": ev["name"], "input": ev["input"]})
            elif t == "message_end":
                stop_reason = ev.get("stop_reason", "end_turn")
                u = ev.get("usage", {})
                conv.cost_tracker.add_usage(
                    conv.model,
                    input_tokens=u.get("input_tokens", 0),
                    output_tokens=u.get("output_tokens", 0),
                    cache_read=u.get("cache_read_input_tokens", 0),
                    cache_creation=u.get("cache_creation_input_tokens", 0),
                )
            elif t == "error":
                flush_text()
                self.log.append({"type": "error", "error": ev["error"]})
                emit({"type": "error", "error": ev["error"]})
                stop_reason = "error"
                break

        content = []
        full = "".join(turn_text)
        if full:
            content.append({"type": "text", "text": full})
        content.extend(tool_uses)
        if content:
            msg = {"role": "assistant", "content": content}
            conv.messages.append(msg)
            conv.session.append_message(msg)
        return stop_reason


def _make_scrub(workspace: str):
    """Strip workspace/​sandbox paths from anything shown to the user.

    Both the workspace itself and its parent are matched, in either slash style,
    together with one trailing separator — so an absolute path collapses to the
    bare relative name the user recognises.
    """
    variants = {workspace, workspace.replace("\\", "/"),
                WORKSPACES, WORKSPACES.replace("\\", "/")}
    pattern = "(?:" + "|".join(re.escape(v) for v in
                               sorted(variants, key=len, reverse=True)) + r")[\\/]?"
    rx = re.compile(pattern, re.IGNORECASE)

    def scrub(s: str) -> str:
        return rx.sub("", s) if s else s

    return scrub


def _scrub_deep(value, scrub):
    """Apply the path scrubber to every string in a replayable log.

    Live turns are scrubbed as they stream (AGUIStream); this covers the other
    door into the same data — reopening a conversation.
    """
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_deep(v, scrub) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_deep(v, scrub) for k, v in value.items()}
    return value


CHATS: dict[str, Chat] = {}
CHATS_LOCK = threading.Lock()


def create_chat() -> Chat:
    chat = Chat()
    with CHATS_LOCK:
        CHATS[chat.id] = chat
    return chat


# ---------------------------------------------------------------------------
# Model / params
# ---------------------------------------------------------------------------

def current_params() -> dict:
    return {**PARAM_DEFAULTS, "default_max_tokens": get_max_tokens()}


def set_params(data: dict) -> dict:
    if "temperature" in data:
        v = data["temperature"]
        PARAM_DEFAULTS["temperature"] = None if v in (None, "") else max(0.0, min(2.0, float(v)))
    if "max_tokens" in data:
        v = data["max_tokens"]
        PARAM_DEFAULTS["max_tokens"] = None if v in (None, "") else max(1, int(v))
    if "thinking" in data:
        PARAM_DEFAULTS["thinking"] = bool(data["thinking"])
    if "thinking_budget" in data:
        v = data["thinking_budget"]
        if v not in (None, ""):
            PARAM_DEFAULTS["thinking_budget"] = max(1024, int(v))
    with CHATS_LOCK:
        for chat in CHATS.values():
            p = chat.conv.profile
            p.temperature = PARAM_DEFAULTS["temperature"]
            p.max_tokens = PARAM_DEFAULTS["max_tokens"]
            p.thinking = PARAM_DEFAULTS["thinking"]
            p.thinking_budget = PARAM_DEFAULTS["thinking_budget"]
    return current_params()


def set_model(model_id: str):
    os.environ["CLAUDE_MODEL"] = model_id
    with CHATS_LOCK:
        for chat in CHATS.values():
            chat.conv.model = model_id


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        """Read the whole request body — always, even for routes that ignore it.

        On a keep-alive connection any byte left unread is parsed as the start
        of the next request line, which desynchronises the connection (the
        browser then sees a spurious 501 on whatever it asks for next).
        """
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- GET -------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html", "/chat.html"):
            self._serve_file(CHAT_HTML, "text/html; charset=utf-8")
        elif path.startswith("/assets/"):
            self._serve_static(path)
        elif path == "/api/meta":
            self._send_json({
                "model": get_model(),
                "models": [{"id": m["id"], "label": m["label"]} for m in AVAILABLE_MODELS],
                "params": current_params(),
            })
        elif path == "/api/chats":
            with CHATS_LOCK:
                items = sorted(CHATS.values(), key=lambda c: c.updated, reverse=True)
                self._send_json({"chats": [c.summary() for c in items]})
        elif path.startswith("/api/artifact/"):
            self._serve_artifact(path, qs)
        else:
            m = re.match(r"^/api/chats/([0-9a-f]+)$", path)
            if m:
                chat = CHATS.get(m.group(1))
                if not chat:
                    self._send_json({"error": "对话不存在"}, status=404)
                else:
                    self._send_json({**chat.summary(),
                                     "log": _scrub_deep(chat.log, chat.scrub),
                                     "artifacts": chat.public_artifacts()})
                return
            self.send_error(404)

    # -- POST ------------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        data = self._read_body()      # drained here so no route can leave bytes

        if path == "/api/chats":
            self._send_json(create_chat().summary())
        elif path == "/api/model":
            mid = data.get("model", "")
            if mid:
                set_model(mid)
            self._send_json({"ok": True, "model": get_model()})
        elif path == "/api/params":
            try:
                self._send_json(set_params(data))
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, status=400)
        elif path == "/api/upload":
            self._handle_upload(data)
        elif path == "/api/agui":
            self._handle_agui(parse_qs(parsed.query), data)
        else:
            self.send_error(404)

    # -- handlers --------------------------------------------------------------

    def _serve_file(self, filepath: str, ctype: str):
        try:
            with open(filepath, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(500, "frontend not built — run: cd web && npm run build")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str):
        rel = path.lstrip("/").replace("/", os.sep)
        f = os.path.realpath(os.path.join(WEB_DIST, rel))
        root = os.path.realpath(WEB_DIST)
        if not f.startswith(root + os.sep) or not os.path.isfile(f):
            self.send_error(404)
            return
        ctype, _ = mimetypes.guess_type(f)
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json",
                                                  "application/javascript"):
            ctype += "; charset=utf-8"
        self._serve_file(f, ctype)

    def _serve_artifact(self, path: str, qs: dict):
        """GET /api/artifact/<chat>/<artifact>[?download=1] — opaque ids only."""
        m = re.match(r"^/api/artifact/([0-9a-f]+)/([0-9a-f]+)$", path)
        chat = CHATS.get(m.group(1)) if m else None
        f = chat.artifact_path(m.group(2)) if chat and m else None
        if not f:
            self.send_error(404)
            return
        art = chat.artifacts[m.group(2)]
        ctype, _ = mimetypes.guess_type(art["name"])
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json",
                                                  "application/javascript"):
            ctype += "; charset=utf-8"
        try:
            with open(f, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if qs.get("download"):
            name = art["name"].encode("utf-8").decode("latin-1", "ignore")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(body)

    def _handle_upload(self, data: dict):
        """POST /api/upload {chat, name, data(base64)} — into the hidden workspace."""
        chat = CHATS.get(str(data.get("chat") or ""))
        name = os.path.basename(str(data.get("name") or "")).strip()
        if not chat:
            self._send_json({"error": "对话不存在"}, status=400)
            return
        if not name or name.startswith("."):
            self._send_json({"error": "文件名无效"}, status=400)
            return
        try:
            blob = base64.b64decode(data.get("data", ""), validate=True)
        except Exception:
            self._send_json({"error": "文件数据无效"}, status=400)
            return
        if len(blob) > MAX_UPLOAD:
            self._send_json({"error": "文件过大(上限 20MB)"}, status=400)
            return
        try:
            with open(os.path.join(chat.cwd, name), "wb") as fh:
                fh.write(blob)
        except OSError as e:
            self._send_json({"error": f"保存失败: {e}"}, status=500)
            return
        self._send_json({"ok": True, "name": name})

    def _handle_agui(self, qs: dict, data: dict):
        """POST /api/agui?chat=<id> — AG-UI run endpoint."""
        thread_id = str(data.get("threadId") or "")
        run_id = str(data.get("runId") or new_id())
        chat = CHATS.get((qs.get("chat") or [""])[0])

        text = ""
        for msg in reversed(data.get("messages") or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = _stringify(msg.get("content") or "").strip()
                break

        ctx = [c for c in (data.get("context") or []) if isinstance(c, dict)]
        if text and ctx:
            lines = [f"- {c.get('description', '')}: {c.get('value', '')}" for c in ctx]
            text += "\n\n[界面上下文]\n" + "\n".join(lines)

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False)
                              + "\n\n").encode("utf-8"))
            self.wfile.flush()

        stream = AGUIStream(send, scrub=chat.scrub if chat else None)
        try:
            stream.run_started(thread_id, run_id)
            if not chat:
                stream.run_error("对话不存在,请新建对话")
                return
            if not text:
                stream.run_error("空消息")
                return
            chat.stream_turn(text, stream.feed,
                             lambda arts: stream.custom("artifacts", arts))
            stream.run_finished(thread_id, run_id)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser(description="Consumer chat server for open-claude")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47292)
    args = parser.parse_args()

    os.makedirs(WORKSPACES, exist_ok=True)
    # Full tools, but every call is confined to the workspaces tree.
    os.environ["OC_SANDBOX_ROOT"] = WORKSPACES

    provider = get_model_provider(get_model())
    if not get_api_key_for(provider):
        spec = PROVIDERS.get(provider, {})
        envs = " or ".join(spec.get("env", [])) or "the provider API key"
        print(f"Error: no API key for {spec.get('label', provider)}. "
              f"Set {envs} or add it to ~/.claude/config.json", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(CHAT_HTML):
        print("[chat] web/dist/chat.html 未构建。请先执行:cd web && npm install && npm run build",
              file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[chat] workspaces: {WORKSPACES}")
    print(f"[chat] model={get_model()}")
    print(f"[chat] open http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chat] shutting down")
    finally:
        with CHATS_LOCK:
            for chat in CHATS.values():
                try:
                    chat.conv.mcp.shutdown()
                except Exception:
                    pass
        server.server_close()


if __name__ == "__main__":
    main()
