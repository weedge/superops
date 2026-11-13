"""引擎各部分共用的基类和配置。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import UserError
from .model import followup_owners, now_iso, reached, thread_key


@dataclass
class EngineOptions:
    repo: Any
    policy: Callable[[], Any]
    scope: Any                 # SourceScope：来源不收的触发只记 filtered
    assessor: Any
    executor: Any
    outbox: Any
    prepare_ticket: Callable[[dict, dict], dict] | None = None
    now: Callable[[], str] | None = None
    emit: Callable[[dict], None] | None = None


class EnginePart:
    """Intake / Evals / Tickets / Assignments / Replies 的基类：共用仓库、策略、时钟和事件。
    每个子类只改自己的聚合，跨聚合的变更交给兄弟部分，事务由 Engine 打开。"""

    def __init__(self, options: EngineOptions):
        self.options = options
        self.repo = options.repo

    def now(self) -> str:
        return self.options.now() if self.options.now else now_iso()

    def policy(self):
        return self.options.policy()

    def owners(self, trigger: dict) -> list[str]:
        return followup_owners(trigger, self.repo)

    def left_to_workers(self, trigger: dict) -> None:
        """工单执行器还在决定的跟进，主 Session 不能路由。"""
        owners = self.owners(trigger)
        if owners:
            raise UserError("workerDecides", {"tickets": ", ".join(owners)})

    def hand_back(self, ticket_id: str, trigger: dict, reason: str) -> bool:
        """记下工单的执行器交还了一条到过它的触发；它原本由这张工单决定、且没有别的工单还在决定时，排队等评估。"""
        decided = ticket_id in self.owners(trigger)
        disposition = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": self.now()}
        handbacks = [*(disposition.get("handbacks") or []), {"ticketId": ticket_id, "reason": reason, "at": self.now()}]
        self.repo.triggers.save_disposition(trigger["id"], {**disposition, "handbacks": handbacks})
        return decided and not self.owners(trigger) and self.queue_for_eval(trigger)

    def take_up(self, ticket_id: str, trigger_id: str) -> None:
        """记下工单的执行器接下了一条人的消息（确认或答复了它）。"""
        disposition = self.repo.triggers.disposition(trigger_id)
        if disposition and ticket_id not in (disposition.get("acked") or []):
            self.repo.triggers.save_disposition(trigger_id, {**disposition, "acked": [*(disposition.get("acked") or []), ticket_id]})

    def queue_for_eval(self, trigger: dict) -> bool:
        """把触发排到它主线程的下一次评估；已在排队或被评估持有时不重复排。"""
        primary = self.thread(trigger["threads"][0])
        if trigger["id"] in primary["pending"] or trigger["id"] in ((primary.get("evaluating") or {}).get("triggerIds") or []):
            return False
        primary["pending"].append(trigger["id"])
        hold = trigger.get("notBefore")
        if hold and hold > (primary.get("holdUntil") or ""):
            primary["holdUntil"] = hold
        self.repo.threads.save(primary)
        return True

    def emit(self, type_: str, **data) -> None:
        if self.options.emit:
            self.options.emit({"type": type_, "at": self.now(), "data": data})

    def require(self, ticket_id: str) -> dict:
        ticket = self.repo.tickets.get(ticket_id)
        if not ticket:
            raise UserError("unknownTicket")
        return ticket

    def thread(self, ref: dict) -> dict:
        """已存的线程，或一个还没存的空线程。"""
        k = thread_key(ref)
        return self.repo.threads.get(k) or {"key": k, "ref": {"source": ref["source"], "id": ref["id"]}, "createdAt": self.now(),
                                            "subscriptions": [], "correlated": [], "pending": [], "meta": {}}

    def tickets_on_thread(self, thread: dict) -> list[dict]:
        return [t for t in (self.repo.tickets.get(i) for i in thread["subscriptions"]) if t]

    def source(self, ticket: dict) -> dict | None:
        return self.repo.triggers.get(ticket["origin"]["triggerId"])

    def reaches(self, ticket: dict, trigger: dict, context: bool = True) -> bool:
        return reached(ticket, trigger, self.repo.threads.get, context)
