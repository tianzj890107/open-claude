"""AG-UI protocol adapter shared by open-claude's web surfaces.

The React frontends talk the AG-UI protocol (@ag-ui/client's HttpAgent), which
CopilotKit consumes directly — no Node runtime in the loop. This translates the
turn events open-claude already emits into AG-UI's event vocabulary.

Wire rules verified against @ag-ui/client 0.0.57:
  - TOOL_CALL_START may reference a parentMessageId that no TEXT_MESSAGE_START
    ever opened; the client materialises the assistant message for it.
  - A message closed with TEXT_MESSAGE_END can still gain tool calls afterwards,
    so text and tool calls from one assistant turn land in one bubble.
  - REASONING_MESSAGE_START requires role="reasoning" (schema-validated).
"""

import json
import uuid
from typing import Callable, Optional

# Tool output can be megabytes (a big Read, a chatty build); cap what crosses the
# wire. The agent still sees the full result — this only trims the UI copy.
MAX_RESULT_CHARS = 20000


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class AGUIStream:
    """Turns open-claude turn events into AG-UI events on an SSE writer.

    `scrub` is applied to every string that reaches the browser. The consumer
    chat surface uses it to strip workspace paths, which must never leak.
    """

    def __init__(self, send: Callable[[dict], None],
                 scrub: Optional[Callable[[str], str]] = None):
        self._send = send
        self._scrub = scrub or (lambda s: s)
        self.msg_id: Optional[str] = None   # assistant message currently building
        self.text_open = False
        self.reason_id: Optional[str] = None
        self.errored = False

    def _ev(self, **kw):
        self._send(kw)

    # -- message framing -------------------------------------------------------

    def _close_text(self):
        if self.text_open:
            self._ev(type="TEXT_MESSAGE_END", messageId=self.msg_id)
            self.text_open = False

    def _close_reasoning(self):
        if self.reason_id:
            self._ev(type="REASONING_MESSAGE_END", messageId=self.reason_id)
            self.reason_id = None

    def run_started(self, thread_id: str, run_id: str):
        self._ev(type="RUN_STARTED", threadId=thread_id, runId=run_id)

    def run_finished(self, thread_id: str, run_id: str):
        if self.errored:      # RUN_ERROR already terminated the run
            return
        self._close_text()
        self._close_reasoning()
        self._ev(type="RUN_FINISHED", threadId=thread_id, runId=run_id)

    def run_error(self, message: str):
        if self.errored:
            return
        self._close_text()
        self._close_reasoning()
        self.errored = True
        self._ev(type="RUN_ERROR", message=self._scrub(message))

    def custom(self, name: str, value):
        """Out-of-band UI signal (approvals, generated artifacts, …)."""
        self._ev(type="CUSTOM", name=name, value=value)

    # -- open-claude event -> AG-UI --------------------------------------------

    def feed(self, ev: dict):
        t = ev.get("type")

        if t == "text":
            self._close_reasoning()
            if not self.text_open:
                self.msg_id = self.msg_id or new_id()
                self._ev(type="TEXT_MESSAGE_START", messageId=self.msg_id,
                         role="assistant")
                self.text_open = True
            self._ev(type="TEXT_MESSAGE_CONTENT", messageId=self.msg_id,
                     delta=self._scrub(ev.get("text", "")))

        elif t == "thinking":
            # Reasoning is its own message kind; end the text bubble and start a
            # fresh one afterwards so the two never interleave in a single id.
            if self.text_open:
                self._close_text()
                self.msg_id = None
            if not self.reason_id:
                self.reason_id = new_id()
                self._ev(type="REASONING_MESSAGE_START",
                         messageId=self.reason_id, role="reasoning")
            self._ev(type="REASONING_MESSAGE_CONTENT",
                     messageId=self.reason_id, delta=self._scrub(ev.get("text", "")))

        elif t == "tool_use":
            self._close_reasoning()
            self._close_text()          # keep msg_id: tool calls join that bubble
            self.msg_id = self.msg_id or new_id()
            args = json.dumps(ev.get("input") or {}, ensure_ascii=False)
            self._ev(type="TOOL_CALL_START", toolCallId=ev.get("id", ""),
                     toolCallName=ev.get("name", ""), parentMessageId=self.msg_id)
            self._ev(type="TOOL_CALL_ARGS", toolCallId=ev.get("id", ""),
                     delta=self._scrub(args))
            self._ev(type="TOOL_CALL_END", toolCallId=ev.get("id", ""))

        elif t == "tool_result":
            content = self._scrub(ev.get("content", ""))
            if len(content) > MAX_RESULT_CHARS:
                content = content[:MAX_RESULT_CHARS] + "\n…(输出已截断)"
            self._ev(type="TOOL_CALL_RESULT", messageId=new_id(),
                     toolCallId=ev.get("tool_use_id", ""), role="tool",
                     content=content)
            # Whatever the model says next belongs to a new assistant message.
            self.msg_id = None
            self.text_open = False

        elif t in ("approval_request", "approval_result"):
            # The frontend pops a confirm dialog and answers over its own route.
            self.custom(t, ev)

        elif t == "error":
            self.run_error(str(ev.get("error", "")))

        # "done" is emitted by stream_turn after the loop; RUN_FINISHED is sent
        # by the request handler instead, so it carries thread/run ids.
