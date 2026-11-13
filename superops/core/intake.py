"""接入：存触发、记线程关联、按订阅扇出、排队评估。"""
from __future__ import annotations

from .model import iso_time, live_ticket, thread_key
from .part import EnginePart
from .reasons import key


class Intake(EnginePart):
    """来源的触发只存一次；来源不收的记 filtered；其余扇出给订阅了它线程的工单，
    没有工单的执行器来决定它时（见 followup_owners）排到主线程等评估。"""

    def __init__(self, options, tickets):
        super().__init__(options)
        self.tickets = tickets

    def receive(self, received: dict) -> str:
        trigger = {**received, "at": iso_time(received["at"])}
        trigger.setdefault("meta", {})
        if not trigger.get("threads"):
            raise ValueError("触发至少要属于一个线程")
        if self.repo.triggers.get(trigger["id"]):
            return "duplicate"
        at = self.now()
        scope = self.options.scope.scope(trigger["source"], trigger["meta"])
        self.repo.triggers.save(trigger)
        if not scope["listened"]:
            self.repo.triggers.save_disposition(trigger["id"], {"filtered": {"code": scope["reason"]}, "fanout": [], "at": at})
            return "filtered"
        keys = [thread_key(ref) for ref in trigger["threads"]]
        # 到达的工单，以及是否只通过“为了解上下文而关注”的线程到达（工单自己的线程会让 context 变 False）
        reach: dict[str, dict] = {}
        for ref in trigger["threads"]:
            thread = self.thread(ref)
            if not thread.get("lastAt") or trigger["at"] > thread["lastAt"]:
                thread["lastAt"] = trigger["at"]
            thread["correlated"] = list(dict.fromkeys([*thread["correlated"], *[k for k in keys if k != thread["key"]]]))
            for ticket_id in thread["subscriptions"]:
                if ticket_id == trigger.get("byTicket"):
                    continue  # 工单自己产出的事件不回送给它
                ticket = self.repo.tickets.get(ticket_id)
                followed = ticket_id in (thread.get("context") or [])
                # 只有人的消息、且在非上下文线程上，才唤醒已结束的工单；被人停掉的工单什么都不收
                resumes = trigger["human"] and bool(ticket) and (ticket.get("ended") or {}).get("by") != "human" and not followed
                if not ticket or (not live_ticket(ticket) and not resumes):
                    continue
                prev = reach.get(ticket_id)
                reach[ticket_id] = {"ticket": ticket, "context": followed and (prev["context"] if prev else True)}
            self.repo.threads.save(thread)
        for ticket_id, item in reach.items():
            entry = {"id": key("input", ticket_id, "trigger", trigger["id"]), "kind": "trigger", "triggerId": trigger["id"], "at": at}
            if trigger["human"]:
                entry["human"] = True
            if item["context"]:
                entry["context"] = True
            self.tickets.add_input(self.repo.tickets.get(ticket_id), entry)
        fanout = list(reach.keys())
        context = [i for i in fanout if reach[i]["context"]]
        disposition = {"fanout": fanout, "at": at}
        if context:
            disposition["context"] = context
        self.repo.triggers.save_disposition(trigger["id"], disposition)
        # 没有工单来决定的人的消息、以及所有机器的消息，进评估
        if not self.owners(trigger):
            self.queue_for_eval(trigger)
        for ticket_id in fanout:
            self.tickets.wake(ticket_id)
        self.emit("trigger.received", triggerId=trigger["id"], threads=keys)
        return "stored"
