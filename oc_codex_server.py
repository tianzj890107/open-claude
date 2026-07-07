"""
Codex-style web server for open-claude.

This serves `codex_web.html` — a Codex-like coding-agent UI — and drives the
UNMODIFIED open_claude engine underneath. Unlike the pure-chat bridge
(oc_web_server.py), this surface has the agent's FULL tool set (Bash, Read,
Write, Edit, Glob, Grep, Skill, Tasks, sub-agents, MCP), but every session is
confined to a project folder inside the sandbox directory:

    <repo>/sandbox/<project-name>/

Confinement is enforced at the tool-dispatch layer via OC_SANDBOX_ROOT (see
open_claude/tools.py). The CLI is unaffected: it never sets that variable and
keeps operating on whatever folder it was launched in.

Concepts:
  - project = a folder under sandbox/ (create new ones from the UI)
  - task    = one conversation bound to a project (its own Conversation,
              session recording under <project>/.open-claude via SessionStore)

Run:
    python oc_codex_server.py [--port 47313]
then open http://127.0.0.1:47313/ in a browser.
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
from urllib.parse import parse_qs, unquote, urlparse

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
HTML_PATH = os.path.join(SCRIPT_DIR, "codex_web.html")
SANDBOX_DIR = os.path.join(SCRIPT_DIR, "sandbox")

# Project names: letters/digits/CJK plus - _ . (no separators, no traversal).
_PROJECT_NAME_RE = re.compile(r"^[\w\-.一-鿿]{1,64}$")

# Inference-parameter defaults applied to every new task (patched via /api/params).
PARAM_DEFAULTS = {"temperature": None, "max_tokens": None,
                  "thinking": False, "thinking_budget": 8000}


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
# Projects (folders under sandbox/)
# ---------------------------------------------------------------------------

def list_projects() -> list[dict]:
    out = []
    try:
        for name in sorted(os.listdir(SANDBOX_DIR)):
            p = os.path.join(SANDBOX_DIR, name)
            if os.path.isdir(p) and not name.startswith("."):
                out.append({"name": name, "mtime": os.path.getmtime(p)})
    except OSError:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def create_project(name: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not _PROJECT_NAME_RE.match(name) or ".." in name:
        return False, "项目名只能包含中英文、数字、- _ .(1–64 字符)"
    path = os.path.join(SANDBOX_DIR, name)
    if os.path.exists(path):
        return False, "同名项目已存在"
    os.makedirs(path)
    return True, name


def project_path(name: str) -> str | None:
    """Resolve a project name to its folder, refusing anything outside sandbox."""
    if not name or not _PROJECT_NAME_RE.match(name):
        return None
    p = os.path.realpath(os.path.join(SANDBOX_DIR, name))
    root = os.path.realpath(SANDBOX_DIR)
    if not (p == root or p.startswith(root + os.sep)) or p == root:
        return None
    return p if os.path.isdir(p) else None


_SKIP_DIRS = {".git", ".open-claude", "node_modules", "__pycache__", ".venv", "venv"}


def list_project_files(base: str) -> list[dict]:
    """Flat file listing of a project (for the preview panel's tree)."""
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append({"path": os.path.relpath(fp, base).replace("\\", "/"),
                        "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= 2000:
                return out
    return out


def resolve_project_file(project: str, rel: str) -> str | None:
    """Resolve a (possibly absolute) file path against a project, confined to it.

    os.path.join ignores `base` when `rel` is absolute, so tool-card clicks that
    carry an absolute path also work — the realpath containment check below is
    what actually enforces the boundary.
    """
    base = project_path(project)
    if not base or not rel:
        return None
    p = os.path.realpath(os.path.join(base, rel))
    if not (p == base or p.startswith(base + os.sep)):
        return None
    return p


# ---------------------------------------------------------------------------
# Tasks — one Conversation per task, bound to a project folder
# ---------------------------------------------------------------------------

class Task:
    def __init__(self, project: str, cwd: str):
        self.id = uuid.uuid4().hex[:12]
        self.project = project
        self.cwd = cwd
        self.title = "新任务"
        self.created = time.time()
        self.updated = self.created
        self.status = "idle"          # idle | working | error
        self.log: list[dict] = []     # replayable UI events
        self.lock = threading.Lock()

        # 网页确认流:危险操作暂停执行,推送 approval_request 事件,等待用户点击
        self.pending_approval: dict | None = None
        self._approval_event: threading.Event | None = None
        self._approval_answer = False
        self._rec = None              # 当前回合的 record+emit,供确认流推送事件

        # Full-capability agent, confined to the project dir by OC_SANDBOX_ROOT.
        # default mode: dangerous tools (Bash/Write/Edit) route to _prompt_user,
        # which we redirect to the web approval flow below.
        # Pin the model via profile (highest precedence) so a Claude Code-only id
        # in ~/.claude/settings.json (e.g. "claude-fable-5[1m]") can't leak in.
        self.conv = Conversation(cwd, permission_mode="default",
                                 profile=AgentProfile(
                                     model=get_model(),
                                     style="始终使用简体中文回复用户;代码、命令、文件名等技术标识除外。"))
        self.conv.permissions._prompt_user = self._web_prompt_user
        p = self.conv.profile
        p.temperature = PARAM_DEFAULTS["temperature"]
        p.max_tokens = PARAM_DEFAULTS["max_tokens"]
        p.thinking = PARAM_DEFAULTS["thinking"]
        p.thinking_budget = PARAM_DEFAULTS["thinking_budget"]

    # -- web approval flow -----------------------------------------------------

    def _web_prompt_user(self, tool_name: str, tool_input: dict,
                         forced_by: str = "") -> tuple[bool, str]:
        """Replace the CLI permission prompt: push a confirm card to the web UI
        and block this turn until the user clicks 允许/拒绝 (or times out)."""
        # 新建文件的 Write 自动放行;只有覆盖已有文件才要求确认
        if tool_name == "Write":
            fp = str(tool_input.get("file_path") or "")
            ap = fp if os.path.isabs(fp) else os.path.join(self.cwd, fp)
            if not os.path.exists(ap):
                return True, ""
        rec = self._rec
        if rec is None:               # 不在流式回合中(理论上不会发生),避免死锁
            return True, ""
        if tool_name == "Bash":
            summary, detail = "执行命令", str(tool_input.get("command") or "")
        elif tool_name == "Write":
            summary, detail = "覆盖已有文件", str(tool_input.get("file_path") or "")
        elif tool_name == "Edit":
            summary, detail = "修改文件", str(tool_input.get("file_path") or "")
        else:
            summary = "执行 " + tool_name
            detail = json.dumps(tool_input, ensure_ascii=False)[:400]
        req_id = uuid.uuid4().hex[:8]
        self._approval_event = threading.Event()
        self._approval_answer = False
        req = {"type": "approval_request", "id": req_id, "tool": tool_name,
               "summary": summary, "detail": detail[:2000]}
        self.pending_approval = req
        rec(req)
        answered = self._approval_event.wait(timeout=900)   # 最长等 15 分钟
        self.pending_approval = None
        self._approval_event = None
        approved = bool(self._approval_answer) if answered else False
        rec({"type": "approval_result", "id": req_id, "approved": approved,
             "timeout": (not answered)})
        if approved:
            return True, ""
        return False, ("等待用户确认超时,已跳过该操作" if not answered else "用户拒绝执行该操作")

    def resolve_approval(self, req_id: str, approved: bool) -> bool:
        """Called from the /approve endpoint thread."""
        pa, ev = self.pending_approval, self._approval_event
        if not pa or not ev or (req_id and req_id != pa.get("id")):
            return False
        self._approval_answer = bool(approved)
        ev.set()
        return True

    def summary(self) -> dict:
        return {"id": self.id, "project": self.project, "title": self.title,
                "status": self.status, "created": self.created, "updated": self.updated}

    # -- one full agentic turn, streamed --------------------------------------

    def stream_turn(self, text: str, emit):
        """Run one turn; emit(dict) per event. Also records events for replay."""
        def rec(ev):
            self.log.append(ev)
            try:
                emit(ev)                    # 客户端断开时继续后台执行,不中断回合
            except OSError:
                pass

        with self.lock:
            self._rec = rec
            conv = self.conv
            self.status = "working"
            self.updated = time.time()
            if self.title == "新任务" and text:
                self.title = text[:48]
            self.log.append({"type": "user", "text": text})
            conv.add_user_message(text)

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
                        # Reuse open-claude's exact execution path (permissions,
                        # hooks, Agent/MCP/execute_tool dispatch + sandbox guard).
                        conv._execute_pending_tools()
                        last = conv.messages[-1] if conv.messages else None
                        if last and last.get("role") == "user" and isinstance(last.get("content"), list):
                            for blk in last["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    rec({
                                        "type": "tool_result",
                                        "tool_use_id": blk.get("tool_use_id", ""),
                                        "content": _stringify(blk.get("content", "")),
                                        "is_error": bool(blk.get("is_error", False)),
                                    })
                        continue
                    break
                self.status = "idle"
            except Exception as e:
                traceback.print_exc()
                self.status = "error"
                rec({"type": "error", "error": str(e)})
            finally:
                self._rec = None
                flush_text()
                self.updated = time.time()
                cost = getattr(conv.cost_tracker, "total_cost_usd", 0.0)
                try:
                    emit({"type": "done", "model": conv.model, "cost": round(cost, 5),
                          "status": self.status})
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

        # Persist the assistant message exactly like the REPL does.
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


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()


def create_task(project: str) -> Task | None:
    cwd = project_path(project)
    if not cwd:
        return None
    task = Task(project, cwd)
    with TASKS_LOCK:
        TASKS[task.id] = task
    return task


# ---------------------------------------------------------------------------
# Global model / params (apply to all live tasks + defaults for new ones)
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
    with TASKS_LOCK:
        for task in TASKS.values():
            p = task.conv.profile
            p.temperature = PARAM_DEFAULTS["temperature"]
            p.max_tokens = PARAM_DEFAULTS["max_tokens"]
            p.thinking = PARAM_DEFAULTS["thinking"]
            p.thinking_budget = PARAM_DEFAULTS["thinking_budget"]
    return current_params()


def set_model(model_id: str):
    os.environ["CLAUDE_MODEL"] = model_id
    with TASKS_LOCK:
        for task in TASKS.values():
            task.conv.model = model_id


# ---------------------------------------------------------------------------
# HTTP layer
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
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes ----------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._serve_html()
        elif path == "/api/files":
            base = project_path((qs.get("project") or [""])[0])
            if not base:
                self._send_json({"error": "项目不存在"}, status=404)
            else:
                self._send_json({"files": list_project_files(base)})
        elif path.startswith("/p/"):
            self._serve_project_file(path)
        elif path == "/api/meta":
            self._send_json({
                "model": get_model(),
                "models": [{"id": m["id"], "label": m["label"]} for m in AVAILABLE_MODELS],
                "params": current_params(),
                "sandbox": SANDBOX_DIR,
                "projects": list_projects(),
            })
        elif path == "/api/projects":
            self._send_json({"projects": list_projects()})
        elif path == "/api/tasks":
            with TASKS_LOCK:
                items = sorted(TASKS.values(), key=lambda t: t.updated, reverse=True)
                self._send_json({"tasks": [t.summary() for t in items]})
        else:
            m = re.match(r"^/api/tasks/([0-9a-f]+)$", path)
            if m:
                task = TASKS.get(m.group(1))
                if not task:
                    self._send_json({"error": "task not found"}, status=404)
                    return
                self._send_json({**task.summary(), "log": task.log})
                return
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/projects":
            data = self._read_body()
            ok, msg = create_project(data.get("name", ""))
            if ok:
                self._send_json({"ok": True, "name": msg, "projects": list_projects()})
            else:
                self._send_json({"error": msg}, status=400)
        elif path == "/api/tasks":
            data = self._read_body()
            task = create_task(data.get("project", ""))
            if not task:
                self._send_json({"error": "项目不存在或不在沙箱内"}, status=400)
                return
            self._send_json(task.summary())
        elif path == "/api/model":
            data = self._read_body()
            mid = data.get("model", "")
            if mid:
                set_model(mid)
            self._send_json({"ok": True, "model": get_model()})
        elif path == "/api/params":
            try:
                self._send_json(set_params(self._read_body()))
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, status=400)
        elif path == "/api/upload":
            self._handle_upload()
        else:
            m = re.match(r"^/api/tasks/([0-9a-f]+)/send$", path)
            if m:
                self._handle_send(m.group(1))
                return
            m = re.match(r"^/api/tasks/([0-9a-f]+)/approve$", path)
            if m:
                task = TASKS.get(m.group(1))
                data = self._read_body()
                if not task:
                    self._send_json({"error": "任务不存在"}, status=404)
                elif task.resolve_approval(str(data.get("id") or ""), bool(data.get("approved"))):
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "没有待确认的操作或请求已过期"}, status=400)
                return
            self.send_error(404)

    def _serve_project_file(self, path: str):
        """GET /p/<project>/<path> — raw file from a sandbox project.

        Served under a real URL path (not a query param) so that relative
        resources inside a previewed HTML page resolve correctly in the iframe.
        """
        m = re.match(r"^/p/([^/]+)/(.+)$", path)
        f = resolve_project_file(m.group(1), m.group(2)) if m else None
        if not f or not os.path.isfile(f):
            self.send_error(404)
            return
        ctype, _ = mimetypes.guess_type(f)
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
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
        self.end_headers()
        self.wfile.write(body)

    def _handle_upload(self):
        """POST /api/upload {project, name, data(base64)} — save into project root."""
        data = self._read_body()
        base = project_path(data.get("project", ""))
        name = os.path.basename(str(data.get("name") or "")).strip()
        if not base:
            self._send_json({"error": "项目不存在"}, status=400)
            return
        if not name or name.startswith("."):
            self._send_json({"error": "文件名无效"}, status=400)
            return
        try:
            blob = base64.b64decode(data.get("data", ""), validate=True)
        except Exception:
            self._send_json({"error": "文件数据无效"}, status=400)
            return
        if len(blob) > 20 * 1024 * 1024:
            self._send_json({"error": "文件过大(上限 20MB)"}, status=400)
            return
        # 同名文件:内容相同直接复用,内容不同则覆盖 —— 反复上传不再堆积 (1)(2)(3) 副本
        target = os.path.join(base, name)
        replaced = os.path.exists(target)
        if replaced:
            try:
                with open(target, "rb") as fh:
                    if fh.read() == blob:
                        self._send_json({"ok": True, "name": name, "unchanged": True})
                        return
            except OSError:
                pass
        try:
            with open(target, "wb") as fh:
                fh.write(blob)
        except OSError as e:
            self._send_json({"error": f"写入失败: {e}"}, status=500)
            return
        self._send_json({"ok": True, "name": name, "replaced": replaced})

    def _serve_html(self):
        try:
            with open(HTML_PATH, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(500, "frontend html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_send(self, task_id: str):
        task = TASKS.get(task_id)
        data = self._read_body()
        text = (data.get("message") or "").strip()

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()

        if not task or not text:
            try:
                emit({"type": "error", "error": "任务不存在" if not task else "空消息"})
                emit({"type": "done"})
            except OSError:
                pass
            return
        try:
            task.stream_turn(text, emit)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected mid-stream; the turn state is already saved.
            pass


def main():
    parser = argparse.ArgumentParser(description="Codex-style web server for open-claude")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47313)
    args = parser.parse_args()

    os.makedirs(SANDBOX_DIR, exist_ok=True)
    # Confine every tool call in this process to the sandbox tree. The pure-chat
    # bridge uses OC_READONLY_FS instead; here the agent keeps full tools but
    # can only touch sandbox/ (see execute_tool in open_claude/tools.py).
    os.environ["OC_SANDBOX_ROOT"] = SANDBOX_DIR

    provider = get_model_provider(get_model())
    if not get_api_key_for(provider):
        spec = PROVIDERS.get(provider, {})
        envs = " or ".join(spec.get("env", [])) or "the provider API key"
        print(f"Error: no API key for {spec.get('label', provider)}. "
              f"Set {envs} or add it to ~/.claude/config.json", file=sys.stderr)
        sys.exit(1)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[codex] sandbox: {SANDBOX_DIR}")
    print(f"[codex] model={get_model()}")
    print(f"[codex] open {url}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[codex] shutting down")
    finally:
        with TASKS_LOCK:
            for task in TASKS.values():
                try:
                    task.conv.mcp.shutdown()
                except Exception:
                    pass
        server.server_close()


if __name__ == "__main__":
    main()
