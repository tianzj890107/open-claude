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
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from open_claude.repl import Conversation
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

        # Full-capability agent, confined to the project dir by OC_SANDBOX_ROOT.
        # always_allow: the web UI has no terminal for permission prompts.
        self.conv = Conversation(cwd, permission_mode="always_allow")
        self.conv.permissions._prompt_user = lambda *a, **k: (True, "")
        p = self.conv.profile
        p.temperature = PARAM_DEFAULTS["temperature"]
        p.max_tokens = PARAM_DEFAULTS["max_tokens"]
        p.thinking = PARAM_DEFAULTS["thinking"]
        p.thinking_budget = PARAM_DEFAULTS["thinking_budget"]

    def summary(self) -> dict:
        return {"id": self.id, "project": self.project, "title": self.title,
                "status": self.status, "created": self.created, "updated": self.updated}

    # -- one full agentic turn, streamed --------------------------------------

    def stream_turn(self, text: str, emit):
        """Run one turn; emit(dict) per event. Also records events for replay."""
        def rec(ev):
            self.log.append(ev)
            emit(ev)

        with self.lock:
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
                flush_text()
                self.updated = time.time()
                cost = getattr(conv.cost_tracker, "total_cost_usd", 0.0)
                emit({"type": "done", "model": conv.model, "cost": round(cost, 5),
                      "status": self.status})

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
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_html()
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
        else:
            m = re.match(r"^/api/tasks/([0-9a-f]+)/send$", path)
            if m:
                self._handle_send(m.group(1))
                return
            self.send_error(404)

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
