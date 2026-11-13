"""引擎发往渠道的消息，每种都按允许它的规则来。"""
from __future__ import annotations

from .errors import UserError
from .model import latest_assignment, live_ticket, ticket_outcome
from .part import EnginePart
from .reasons import key


class Replies(EnginePart):
    """通过 Outbox 端口写渠道：确认、对人提问的答复、工单的结果、工单要人决定的问题。
    每条消息用确定的 id，只会发出一次。"""

    def reply(self, request: dict) -> dict:
        """request: {purpose: acknowledgement|answer|result|attention, ticketId?, triggerId?, question?, text, priority?}
        确认和答复发到那条人的消息所在的地方；问题发到引出它的消息所在的地方（否则工单开始的会话）；
        结果发到工单开始的会话（只有 ready / completed / merged / dropped 的工单可以发）。
        返回 {id, status, reused}。"""
        return self._acknowledge(request) if request["purpose"] == "acknowledgement" else self._answer(request)

    def acknowledge_accepted(self, ticket_id: str, trigger: dict, text: str) -> None:
        """接单时带的确认：发到开工那条消息所在的地方（它有渠道、且是人写的才发）。"""
        if trigger.get("channel") and trigger["human"]:
            self.options.outbox.send({"id": key("ack", trigger["id"]), "channel": trigger["channel"], "text": text,
                                      "kind": "acknowledgement", "ticketId": ticket_id})

    def withdraw_attention(self, attention_id: str) -> None:
        """撤回还没发出去的问题。"""
        self.options.outbox.withdraw(key("attention-reply", attention_id))

    def source_channel(self, ticket: dict) -> dict | None:
        """工单开始的会话：起点触发的渠道，否则它的第一个渠道。"""
        return (self.source(ticket) or {}).get("channel") or (ticket["channels"][0] if ticket["channels"] else None)

    def _acknowledge(self, request: dict) -> dict:
        ticket = self.require(request["ticketId"])
        if not live_ticket(ticket):
            raise UserError("ackEnded")
        trigger = self.repo.triggers.get(request["triggerId"])
        if not trigger:
            raise UserError("unknownTrigger")
        if not self.reaches(ticket, trigger, False):  # 只为上下文关注的线程只读，不能在那里确认
            raise UserError("notReached")
        if not trigger.get("channel"):
            raise UserError("ackNoChannel")
        mid = key("ack", trigger["id"])
        existing = self.options.outbox.status(mid)
        if not existing and not trigger["human"]:
            raise UserError("ackNotPerson")
        if not existing:  # 保留自动发的或最先排队的那条确认
            self.options.outbox.send({"id": mid, "channel": trigger["channel"], "text": request["text"],
                                      "kind": "acknowledgement", "ticketId": ticket["id"]})
        self.take_up(ticket["id"], trigger["id"])
        return {"id": mid, "status": self.options.outbox.status(mid) or "pending", "reused": existing is not None}

    def _answer(self, request: dict) -> dict:
        purpose = request["purpose"]
        trigger = self.repo.triggers.get(request["triggerId"]) if request.get("triggerId") else None
        if request.get("triggerId") and not trigger:
            raise UserError("unknownTrigger")
        if request.get("ticketId"):
            ticket = self.require(request["ticketId"])
        else:
            ticket = next((t for t in self.repo.tickets.list() if self.reaches(t, trigger)), None) if trigger else None
        if purpose == "result":
            outcome = ticket_outcome(ticket) if ticket else None
            reported = bool(ticket) and live_ticket(ticket) and ((latest_assignment(ticket) or {}).get("report") or {}).get("state") == "ready"
            if not ticket or not (outcome in ("merged", "completed", "dropped") or reported):
                raise UserError("notReady")
        attention = (ticket or {}).get("attention") if purpose == "attention" else None
        if purpose == "attention" and (not attention or attention["question"] != request.get("question")):
            raise UserError("notAttention")
        if purpose == "answer" and not (trigger and trigger["human"]):
            raise UserError("notQuestion")
        mid = key("attention-reply", attention["id"]) if attention else key(
            "reply", (trigger or {}).get("id") or (ticket or {}).get("id") or "", request["text"])
        existing = self.options.outbox.status(mid)
        ready = not ticket or not trigger or trigger["id"] in (ticket.get("answers") or [])
        if purpose == "answer" and not ready and not existing:
            raise UserError("noAnswer")
        asker = self.repo.triggers.get(attention["triggerId"]) if attention and attention.get("triggerId") else None
        if purpose == "answer":
            channel = trigger.get("channel")
        else:
            channel = (asker or {}).get("channel") or (self.source_channel(ticket) if ticket else None)
        if not channel:
            raise UserError("noChannel")
        message = {"id": mid, "channel": channel, "text": request["text"], "kind": "attention" if attention else "reply"}
        if ticket:
            message["ticketId"] = ticket["id"]
        if request.get("priority") is not None:
            message["priority"] = request["priority"]
        self.options.outbox.send(message)
        if purpose == "answer" and ticket and trigger and trigger["id"] in (ticket.get("answers") or []):
            ticket["answers"] = [i for i in ticket["answers"] if i != trigger["id"]]
            self.repo.tickets.save(ticket)
        return {"id": mid, "status": self.options.outbox.status(mid) or "pending", "reused": existing is not None}
