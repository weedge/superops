"""终端来源：操作者在本机直接对 Agent 说话（`superops ask`），回复写到本机的收件箱。

它也是默认的 home 渠道：没配钉钉时，主动工单的问题和结果都落在这里（notify/console.log，`superops inbox` 查看）。
"""
from __future__ import annotations

import json
import sys
import uuid

from ..biz import paths
from ..core.model import now_iso
from ..core.reasons import key
from .kind import SourceKind


class CliSource(SourceKind):
    id = "cli"
    title = "终端"

    def scope(self, meta: dict) -> dict:
        return {"listened": True, "observed": False, "row": "terminal"}

    def ask(self, text: str, *, thread: str | None = None, actor: str = "operator", meta: dict | None = None) -> dict:
        """操作者的一句话：名单内的人直接对 Agent 说的，是明确请求。thread 给了就落在那个线程上（例如回某张工单）。"""
        thread_id = thread or f"ask-{uuid.uuid4().hex[:10]}"
        at = now_iso()
        trigger = {"id": key("cli", thread_id, at, text), "source": self.id, "threads": [{"source": self.id, "id": thread_id}],
                   "actor": {"source": self.id, "id": actor}, "human": True, "addressed": "direct", "at": at, "text": text,
                   "channel": {"source": self.id, "thread": thread_id, "address": {}}, "meta": {"row": "terminal", **(meta or {})}}
        return {"id": trigger["id"], "thread": f"{self.id}:{thread_id}", "result": self.host.receive(trigger)}

    def send(self, message: dict) -> str:
        line = {"at": now_iso(), "kind": message["kind"], "priority": message.get("priority", 2), "ticket": message.get("ticketId"),
                "thread": message["channel"].get("thread"), "text": message["text"]}
        log = paths.ops_data() / "notify" / "console.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        if getattr(self.host, "echo", False):
            print(f"[{line['kind']}] {message['text']}\n", file=sys.stderr)
        return "sent"
