"""流水线来源：业务事件（content.* / social.* / demand.* / gitlab.* / lead.* …）作为触发进入领域。

事件从三处来：确定性脚本（采集器、执行器、CI）通过管理 API 发；工单的执行器在报告里发；人批红后宿主按选项发。
每个业务对象一个线程：pipeline:<subject>（如 content:c_20261110_01、mr:popto/frontend!12）。
订阅把同一对象后续的事件送回负责它的工单——复核结论回到写稿的工单，流水线结果回到开发的工单，不需要再写一条路由。

两条守门规则在这里执行：
- 只有人批过的选项才能发出 requires_approval 规则监听的事件类型（发布、回复、合并、清理…）；Agent 自己发的记为 filtered；
- 被暂停的事件来源（`暂停 douyin`）不收。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from ..biz import governance
from ..biz.timeutil import parse_duration
from ..core.model import now_iso, shift
from ..core.reasons import key
from ..app.settings import matching_rules
from .kind import SourceKind

INTERNAL_TYPES = {"decision.made", "decision.note", "agent.unavailable"}
DEFAULT_CLOSES = {"gitlab.mr.merged": "merged", "gitlab.mr.closed": "closed", "content.closed": "closed"}


class PipelineSource(SourceKind):
    id = "pipeline"
    title = "流水线事件"
    system = True

    def scope(self, meta: dict) -> dict:
        if meta.get("unapproved"):
            return {"listened": False, "reason": "needs_approval"}
        if meta.get("origin") and self.host.settings.is_paused(f"origin:{meta['origin']}"):
            return {"listened": False, "reason": "origin_paused"}
        return {"listened": True, "observed": True, "row": "events", "system": True}

    def send(self, message: dict) -> str:
        return "ignored"

    def emit(self, event: dict, *, by_ticket: str | None = None, producer: str | None = None, approved: dict | None = None,
             trusted: bool = False) -> dict:
        """event: {type, source?, subject?, summary?, link?, payload_ref?, data?, dedup_key?, occurred_at?}
        trusted = 确定性脚本或宿主自己发的；approved = 人批红的凭据 {ticketId, attentionId, option, by, at}。"""
        if not event.get("type"):
            raise ValueError("事件必须有 type")
        type_ = str(event["type"])
        if type_ in INTERNAL_TYPES:
            return {"routed": "internal", **self._internal(event)}
        settings = self.host.settings.current()
        origin = str(event.get("source") or "pipeline")
        subject = event.get("subject")
        data = event.get("data") if isinstance(event.get("data"), dict) else ({"value": event["data"]} if event.get("data") is not None else {})
        data = dict(data)
        if producer:
            data.setdefault("producer", producer)
        meta = {"type": type_, "origin": origin, **({"subject": subject} if subject else {}), "data": data}
        for field in ("link", "payload_ref"):
            if event.get(field):
                meta[field] = event[field]
        if approved:
            meta["approved"] = approved
        trigger = {
            "id": key("pipeline", event["dedup_key"]) if event.get("dedup_key") else key("pipeline", uuid.uuid4().hex),
            "source": self.id, "threads": [{"source": self.id, "id": str(subject or type_)}],
            "actor": {"source": self.id, "id": ""}, "human": False, "at": event.get("occurred_at") or now_iso(),
            "text": str(event.get("summary") or type_)[:500], "meta": meta}
        if event.get("link"):
            trigger["url"] = event["link"]
        if by_ticket:
            trigger["byTicket"] = by_ticket
        gated = [r for r in matching_rules(settings, trigger, enabled_only=False) if r["requires_approval"]]
        if gated and not approved and not trusted:
            meta["unapproved"] = True
        debounce = max([parse_duration(r["debounce"]) for r in matching_rules(settings, trigger) if r.get("debounce")] or [timedelta(0)])
        if debounce:
            trigger["notBefore"] = shift(now_iso(), debounce.total_seconds())
        result = self.host.receive(trigger)
        closes = {**DEFAULT_CLOSES, **(self.config().get("closes") or {})}
        if result == "stored" and type_ in closes and subject:
            self.host.engine.thread_closed({"source": self.id, "id": str(subject)}, closes[type_], trigger["at"])
        return {"id": trigger["id"], "routed": {"stored": "trigger"}.get(result, result)}

    def _internal(self, event: dict) -> dict:
        data = event.get("data") or {}
        if event["type"] == "decision.made":
            return self.host.answer_by_code(str(data.get("card") or event.get("subject") or ""), data.get("choice"),
                                            by=str(data.get("by") or event.get("source") or "operator"), reason=data.get("reason"))
        if event["type"] == "decision.note":
            text = data.get("text") or event.get("summary") or ""
            if not text.strip():
                return {"ok": False, "error": "空内容"}
            return {"ok": True, "entry": governance.append_decision(text, source=f"{event.get('source') or 'pipeline'}", home=self.host.home)}
        if event["type"] == "agent.unavailable" and data.get("agent"):
            self.host.agents.open(data["agent"], "network", event.get("summary"), cooldown=data.get("open_for", "10m"))
            return {"ok": True}
        return {"ok": False}
