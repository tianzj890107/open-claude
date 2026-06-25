"""
MCP (Model Context Protocol) client - stdio transport.

Server configuration follows the Claude Code format:

Project: .mcp.json            (checked into the repo)
User:    ~/.claude/mcp.json   (personal, all projects)

{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {"FOO": "bar"}
    }
  }
}

Each server's tools are exposed to the model as `mcp__<server>__<tool>`.
Only the stdio transport is supported; sse/http entries are skipped.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from . import __version__

PROTOCOL_VERSION = "2024-11-05"
INIT_TIMEOUT = 15      # seconds for initialize + tools/list
CALL_TIMEOUT = 120     # seconds for tools/call

# Anthropic API tool names must match [a-zA-Z0-9_-]{1,64}
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    return _NAME_SAFE.sub("_", name)


class MCPError(Exception):
    pass


class MCPServer:
    """One running MCP server (stdio subprocess + JSON-RPC client)."""

    def __init__(self, name: str, command: str, args: list[str],
                 env: Optional[dict[str, str]] = None, cwd: str = "."):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self.cwd = cwd

        self.proc: Optional[subprocess.Popen] = None
        self.tools: list[dict[str, Any]] = []  # raw MCP tool definitions
        self.status = "stopped"  # stopped | running | failed
        self.error: str = ""

        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._response_event = threading.Condition()
        self._reader_thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Spawn the server process and perform the MCP handshake."""
        try:
            argv = self._resolve_argv()
            env = {**os.environ, **self.env}
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
                env=env,
            )
        except Exception as e:
            self.status = "failed"
            self.error = f"failed to spawn: {e}"
            return False

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "open-claude", "version": __version__},
            }, timeout=INIT_TIMEOUT)
            self._notify("notifications/initialized")

            result = self._request("tools/list", {}, timeout=INIT_TIMEOUT)
            self.tools = result.get("tools", [])
            self.status = "running"
            return True
        except Exception as e:
            self.status = "failed"
            self.error = str(e)
            self.stop()
            return False

    def _resolve_argv(self) -> list[str]:
        """Resolve the command to an executable path (handles npx etc. on Windows)."""
        exe = shutil.which(self.command)
        if exe is None and os.name == "nt":
            for suffix in (".cmd", ".bat", ".exe"):
                exe = shutil.which(self.command + suffix)
                if exe:
                    break
        if exe is None:
            exe = self.command  # let Popen raise a clear error
        return [exe, *self.args]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.status == "running":
            self.status = "stopped"

    # -- JSON-RPC plumbing ---------------------------------------------------

    def _read_loop(self):
        """Background thread: read newline-delimited JSON-RPC messages."""
        try:
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    with self._response_event:
                        self._responses[msg["id"]] = msg
                        self._response_event.notify_all()
        except Exception:
            pass

    def _send(self, msg: dict[str, Any]):
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise MCPError(f"server '{self.name}' is not running")
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: Optional[dict] = None):
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        import time
        deadline = time.monotonic() + timeout
        with self._response_event:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"timeout waiting for {method} from '{self.name}'")
                self._response_event.wait(timeout=min(remaining, 1.0))
            msg = self._responses.pop(req_id)

        if "error" in msg:
            err = msg["error"]
            raise MCPError(f"{err.get('message', 'unknown error')} (code {err.get('code')})")
        return msg.get("result", {})

    # -- tool invocation -----------------------------------------------------

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=CALL_TIMEOUT,
        )

        parts = []
        for block in result.get("content", []):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        text = "\n".join(parts) if parts else "(no output)"

        if result.get("isError"):
            return f"Error from MCP tool: {text}"
        return text


class MCPManager:
    """Discovers, starts, and routes calls to all configured MCP servers."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.servers: dict[str, MCPServer] = {}
        # api tool name -> (server_name, real_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}

    # -- config discovery ----------------------------------------------------

    def load_config(self) -> dict[str, dict[str, Any]]:
        """Merge user (~/.claude/mcp.json) and project (.mcp.json) server configs."""
        merged: dict[str, dict[str, Any]] = {}
        for path in (Path.home() / ".claude" / "mcp.json", Path(self.cwd) / ".mcp.json"):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            servers = data.get("mcpServers", {})
            if isinstance(servers, dict):
                merged.update(servers)
        return merged

    def start_all(self) -> list[tuple[str, bool, str]]:
        """Start all configured servers. Returns [(name, ok, detail)]."""
        results = []
        config = self.load_config()

        for name, spec in config.items():
            if not isinstance(spec, dict):
                continue
            transport = spec.get("type", "stdio")
            if transport != "stdio":
                results.append((name, False, f"unsupported transport: {transport}"))
                continue
            command = spec.get("command")
            if not command:
                results.append((name, False, "missing 'command'"))
                continue

            server = MCPServer(
                name=name,
                command=command,
                args=spec.get("args", []) or [],
                env=spec.get("env", {}) or {},
                cwd=self.cwd,
            )
            ok = server.start()
            self.servers[name] = server
            if ok:
                self._register_tools(server)
                results.append((name, True, f"{len(server.tools)} tools"))
            else:
                results.append((name, False, server.error))

        return results

    def _register_tools(self, server: MCPServer):
        for tool in server.tools:
            api_name = f"mcp__{_sanitize(server.name)}__{_sanitize(tool.get('name', ''))}"
            api_name = api_name[:64]
            self._tool_map[api_name] = (server.name, tool.get("name", ""))

    # -- integration with the agent loop -------------------------------------

    def get_tool_schemas(self, exclude_servers: Optional[set] = None) -> list[dict[str, Any]]:
        """Anthropic-format tool schemas for all MCP tools.

        Servers named in `exclude_servers` are hidden (used by agent profiles).
        """
        exclude = exclude_servers or set()
        schemas = []
        for api_name, (server_name, tool_name) in self._tool_map.items():
            if server_name in exclude:
                continue
            server = self.servers.get(server_name)
            if not server or server.status != "running":
                continue
            raw = next((t for t in server.tools if t.get("name") == tool_name), None)
            if not raw:
                continue
            schemas.append({
                "name": api_name,
                "description": (raw.get("description") or f"MCP tool {tool_name} from {server_name}")[:1024],
                "input_schema": raw.get("inputSchema") or {"type": "object", "properties": {}},
            })
        return schemas

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_map

    def call(self, api_name: str, arguments: dict[str, Any]) -> str:
        entry = self._tool_map.get(api_name)
        if not entry:
            return f"Unknown MCP tool: {api_name}"
        server_name, tool_name = entry
        server = self.servers.get(server_name)
        if not server or server.status != "running":
            return f"MCP server '{server_name}' is not running"
        try:
            return server.call_tool(tool_name, arguments)
        except MCPError as e:
            return f"MCP error: {e}"
        except Exception as e:
            return f"MCP call failed: {e}"

    def shutdown(self):
        for server in self.servers.values():
            server.stop()

    def describe(self) -> list[str]:
        """Human-readable server listing for /mcp."""
        lines = []
        for name, server in self.servers.items():
            if server.status == "running":
                tool_names = ", ".join(t.get("name", "?") for t in server.tools) or "(none)"
                lines.append(f"{name}: running - tools: {tool_names}")
            else:
                lines.append(f"{name}: {server.status}" + (f" - {server.error}" if server.error else ""))
        return lines
