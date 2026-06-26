"""
Bridge server for the generic_claude_gpt_style_chat front-end.

This is a *front-end adapter*: it does NOT modify the open_claude package at all.
It imports open-claude's existing engine (Conversation, stream_message) and
exposes it over a tiny HTTP + SSE API that the HTML chat UI can talk to. The
command-line agent keeps working exactly as before with full tool access.

The web surface is a PURE CONVERSATIONAL AGENT: it has no tools at all. It cannot
read or write files, run shell commands, search the repo, call skills, or spawn
sub-agents — it only streams chat replies. All local-filesystem capability is
removed here (and OC_READONLY_FS is set as a belt-and-braces guard). Use the CLI
for anything that needs to operate on the project.

Wired through:
  - streaming assistant text
  - model switching across providers
  - conversation memory / auto-compaction

Run:
    python oc_web_server.py [--cwd DIR] [--profile NAME] [--port 47291]
then open http://127.0.0.1:47291/ in a browser.
"""

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- import open-claude's engine (unmodified) ------------------------------
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
from open_claude.profile import load_profile
from open_claude.sessions import SessionStore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(SCRIPT_DIR, "generic_claude_gpt_style_chat.html")


def _stringify(content) -> str:
    """tool_result content is normally a string; be defensive about other shapes."""
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


class Bridge:
    """Owns one open-claude Conversation and drives it for the web UI."""

    def __init__(self, cwd: str, profile_name=None):
        self.cwd = cwd
        profile = load_profile(profile_name, cwd) if profile_name else None
        if profile_name and profile is None:
            print(f"[bridge] profile '{profile_name}' not found; using defaults")
        # Non-interactive: auto-approve (no terminal for permission prompts).
        self.conv = Conversation(cwd, permission_mode="always_allow", profile=profile)
        # Belt-and-braces: never block on an interactive prompt even if ask-rules exist.
        self.conv.permissions._prompt_user = lambda *a, **k: (True, "")

        # Pure conversational agent: the web surface exposes NO tools at all. It
        # cannot read or write files, run shell commands, call skills, or spawn
        # sub-agents — it just talks. (OC_READONLY_FS, set in main(), stays as a
        # belt-and-braces guard.) The CLI is unaffected and keeps every tool.
        self.conv.tool_schemas = []

        self.lock = threading.Lock()

    # -- introspection -------------------------------------------------------

    def meta(self) -> dict:
        conv = self.conv
        # A pure chat agent: expose only what the UI needs to talk, switch
        # models, and tune inference params — no tools or skills are surfaced.
        return {
            "model": conv.model,
            "profile": conv.profile.name,
            "models": [{"id": m["id"], "label": m["label"]} for m in AVAILABLE_MODELS],
            "params": self.params(),
        }

    def params(self) -> dict:
        """Current inference parameters (read from the active profile)."""
        p = self.conv.profile
        return {
            "temperature": p.temperature,
            "max_tokens": p.max_tokens,
            "default_max_tokens": get_max_tokens(),
            "thinking": p.thinking,
            "thinking_budget": p.thinking_budget,
        }

    def set_params(self, data: dict) -> dict:
        """Patch inference parameters; only provided keys are changed."""
        with self.lock:
            p = self.conv.profile
            if "temperature" in data:
                v = data["temperature"]
                p.temperature = None if v in (None, "") else max(0.0, min(2.0, float(v)))
            if "max_tokens" in data:
                v = data["max_tokens"]
                p.max_tokens = None if v in (None, "") else max(1, int(v))
            if "thinking" in data:
                p.thinking = bool(data["thinking"])
            if "thinking_budget" in data:
                v = data["thinking_budget"]
                if v not in (None, ""):
                    p.thinking_budget = max(1024, int(v))
        return self.params()

    def reset(self):
        with self.lock:
            self.conv.messages.clear()
            self.conv.session = SessionStore(self.cwd)
            self.conv.cost_tracker.__init__()

    def set_model(self, model_id: str):
        with self.lock:
            self.conv.model = model_id
            os.environ["CLAUDE_MODEL"] = model_id

    # -- the turn loop (mirrors Conversation.run_turn, but emits SSE) ---------

    def stream_turn(self, text: str, emit):
        """Run one full turn, calling emit(dict) for each event."""
        with self.lock:
            conv = self.conv
            conv.add_user_message(text)
            try:
                for _ in range(max(1, conv.profile.max_iterations)):
                    conv._maybe_compact()
                    stop_reason = self._stream_once(conv, emit)

                    if stop_reason == "tool_use":
                        # Reuse open-claude's exact execution path (permission,
                        # hooks, Agent/MCP/execute_tool dispatch). It appends a
                        # tool_result message we then surface to the UI.
                        conv._execute_pending_tools()
                        last = conv.messages[-1] if conv.messages else None
                        if last and last.get("role") == "user" and isinstance(last.get("content"), list):
                            for blk in last["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                    emit({
                                        "type": "tool_result",
                                        "tool_use_id": blk.get("tool_use_id", ""),
                                        "content": _stringify(blk.get("content", "")),
                                        "is_error": bool(blk.get("is_error", False)),
                                    })
                        continue
                    break
            except Exception as e:
                traceback.print_exc()
                emit({"type": "error", "error": str(e)})
            finally:
                cost = getattr(conv.cost_tracker, "total_cost_usd", 0.0)
                emit({"type": "done", "model": conv.model, "cost": round(cost, 5)})

    def _stream_once(self, conv, emit) -> str:
        """Stream one assistant response; mirror _stream_assistant_response side effects."""
        text_buf = []
        tool_uses = []
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
                text_buf.append(ev["text"])
                emit({"type": "text", "text": ev["text"]})
            elif t == "tool_use_end":
                tool_uses.append({
                    "type": "tool_use", "id": ev["id"],
                    "name": ev["name"], "input": ev["input"],
                })
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
                emit({"type": "error", "error": ev["error"]})
                stop_reason = "error"
                break

        # Persist the assistant message exactly like the REPL does.
        content = []
        full = "".join(text_buf)
        if full:
            content.append({"type": "text", "text": full})
        content.extend(tool_uses)
        if content:
            msg = {"role": "assistant", "content": content}
            conv.messages.append(msg)
            conv.session.append_message(msg)

        return stop_reason


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

bridge: Bridge = None  # set in main()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet

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

    # -- routes --------------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/api/meta":
            self._send_json(bridge.meta())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/send":
            self._handle_send()
        elif self.path == "/api/new":
            bridge.reset()
            self._send_json({"ok": True})
        elif self.path == "/api/model":
            data = self._read_body()
            mid = data.get("model", "")
            if mid:
                bridge.set_model(mid)
            self._send_json({"ok": True, "model": bridge.conv.model})
        elif self.path == "/api/params":
            data = self._read_body()
            try:
                self._send_json(bridge.set_params(data))
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, status=400)
        else:
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

    def _handle_send(self):
        data = self._read_body()
        text = (data.get("message") or "").strip()
        # One turn per request: close the connection when the stream ends so the
        # client gets a clean EOF (no Content-Length is known up front for SSE).
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            try:
                payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise

        if not text:
            try:
                emit({"type": "done"})
            except OSError:
                pass
            return

        try:
            bridge.stream_turn(text, emit)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected mid-stream


def main():
    parser = argparse.ArgumentParser(description="Web bridge for open-claude")
    parser.add_argument("--cwd", default=os.getcwd(), help="Directory the agent operates in")
    parser.add_argument("--profile", default=os.environ.get("OC_PROFILE"), help="Agent profile name")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47291)
    args = parser.parse_args()

    # Enforce read-only filesystem for the whole web process (covers sub-agents).
    os.environ["OC_READONLY_FS"] = "1"

    provider = get_model_provider(get_model())
    if not get_api_key_for(provider):
        spec = PROVIDERS.get(provider, {})
        envs = " or ".join(spec.get("env", [])) or "the provider API key"
        print(f"Error: no API key for {spec.get('label', provider)}. "
              f"Set {envs} or add it to ~/.claude/config.json", file=sys.stderr)
        sys.exit(1)

    cwd = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd):
        print(f"Error: not a directory: {cwd}", file=sys.stderr)
        sys.exit(1)

    global bridge
    print(f"[bridge] starting open-claude agent in {cwd}")
    bridge = Bridge(cwd, profile_name=args.profile)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[bridge] model={bridge.conv.model}  profile={bridge.conv.profile.name}")
    print(f"[bridge] open {url}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down")
    finally:
        try:
            bridge.conv.mcp.shutdown()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
