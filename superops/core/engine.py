"""领域引擎：核心唯一的入口。触发、线程、决定、工单、派工只有它能写。

每个命令在一个事务里改它的聚合；端口在事务内只排队，真正的外部动作在提交之后由适配器去做。
命令用祈使命名（stop_ticket），适配器报告的事实用过去时（assignment_started）。
"""
from __future__ import annotations

from .assignments import Assignments
from .errors import UserError
from .evals import Evals
from .intake import Intake
from .model import iso_time, live_ticket
from .part import EngineOptions
from .replies import Replies
from .tickets import Tickets


class Engine:
    def __init__(self, options: EngineOptions):
        self.options = options
        self.repo = options.repo
        self.replies = Replies(options)
        self.tickets = Tickets(options, self.replies)
        self.assignments = Assignments(options, self.tickets, self.replies)
        self.evals = Evals(options, self.tickets)
        self.intake = Intake(options, self.tickets)

    # ---- 来源

    def receive(self, trigger: dict) -> str:
        """存一次触发（stored / duplicate / filtered），扇出给订阅了它线程的工单，没有工单决定它时排队评估。"""
        return self.repo.atomic(lambda: self.intake.receive(trigger))

    def bind_thread(self, ticket_id: str, thread: dict, own: bool) -> dict:
        return self.repo.atomic(lambda: self.tickets.bind(ticket_id, thread, own))

    def thread_closed(self, thread: dict, outcome: str, at: str) -> None:
        self.repo.atomic(lambda: self.tickets.thread_closed(thread, outcome, iso_time(at)))

    # ---- 时钟

    def evaluate_due(self) -> None:
        """过期的评估重新排队（两次之后记未完成），给有排队触发的线程开评估，
        恢复暂停期间等来了人的消息的已结束工单。来源声明了 notBefore 的线程到点才评估。"""
        policy = self.options.policy()
        for thread in self.repo.threads.active():
            if thread.get("evaluating"):
                self.repo.atomic(lambda t=thread: self.evals.due(t, policy))
        if policy.paused:
            return
        for ticket in self.repo.tickets.active():
            if not live_ticket(ticket) and ticket["undelivered"]:
                self.repo.atomic(lambda t=ticket: self.tickets.wake(t["id"]))
        now = self.options.now() if self.options.now else None
        for thread in self.repo.threads.active():
            if thread["pending"] and not thread.get("evaluating"):
                if thread.get("holdUntil") and thread["holdUntil"] > (now or self.evals.now()):
                    continue
                self.repo.atomic(lambda t=thread: self.evals.start(t["key"], policy))

    # ---- 主 Session / 研判方

    def candidate(self, trigger_id: str) -> dict | None:
        return self.evals.candidate(trigger_id)

    def candidate_delivered(self, eval_id: str) -> None:
        self.repo.atomic(lambda: self.evals.delivered(eval_id, self.options.policy()))

    def decide(self, trigger_id: str, outcome: dict) -> dict:
        return self.repo.atomic(lambda: self.evals.decide(trigger_id, outcome))

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> dict:
        """把一条明确指向某张工单的触发转给它；已结束的工单在原授权下恢复。"""
        def work():
            disposition = self.repo.triggers.disposition(trigger_id) or {}
            if any(h["ticketId"] == ticket_id for h in disposition.get("handbacks") or []):
                raise UserError("handedBackHere")
            trigger = self.tickets.attachable(ticket_id, trigger_id)
            self.evals.withdraw(trigger)
            self.tickets.attach(ticket_id, trigger, note)
            self.evals.routed(trigger_id, ticket_id)
            self.tickets.wake(ticket_id)
            return self.repo.tickets.get(ticket_id)
        return self.repo.atomic(work)

    def request_status(self, ticket_id: str) -> bool:
        return self.repo.atomic(lambda: self.tickets.request_status(ticket_id))

    def note(self, ticket_id: str, text: str) -> dict:
        return self.repo.atomic(lambda: self.tickets.note(ticket_id, text))

    def reply(self, request: dict) -> dict:
        return self.repo.atomic(lambda: self.replies.reply(request))

    # ---- 人（面板、终端，或经主 Session）

    def reevaluate(self, trigger_id: str) -> bool:
        return self.repo.atomic(lambda: self.evals.reevaluate(trigger_id))

    def dismiss(self, decision_id: str) -> dict:
        return self.repo.atomic(lambda: self.evals.dismiss(decision_id))

    def unsubscribe(self, thread: str) -> bool:
        return self.repo.atomic(lambda: self.tickets.drop_subscriptions(thread, "person"))

    def unsubscribe_ticket(self, ticket_id: str, thread: str, reason: str | None = None) -> bool:
        """宿主让一张工单不再订阅某个线程（例如已结束的工单不再听共用的定时线程）。"""
        return self.repo.atomic(lambda: self.tickets.drop_subscriptions(thread, "executor", ticket_id, reason))

    def stop_ticket(self, ticket_id: str, reason: str | None = None) -> dict:
        """人停掉工单，授权随之撤回：它结束，之后什么都不收。"""
        return self.repo.atomic(lambda: self.tickets.stop(ticket_id, reason))

    def retry_ticket(self, ticket_id: str) -> dict:
        return self.repo.atomic(lambda: self.tickets.retry(ticket_id))

    def drop_ticket(self, ticket_id: str, reason: str) -> None:
        """宿主的事实：工单没有等到后续事件（闲置超期），安静结束；人之后在它的线程上发消息仍会恢复它。"""
        self.repo.atomic(lambda: self.tickets.end(ticket_id, "executor", "dropped", None, reason))

    def answer(self, ticket_id: str, option, *, trigger_id: str | None = None, by: str | None = None,
               reason: str | None = None, expired: bool = False) -> dict:
        """人回答工单当前的问题（批红）。返回被选中的选项。"""
        return self.repo.atomic(lambda: self.assignments.answer(ticket_id, option, trigger_id=trigger_id, by=by,
                                                                reason=reason, expired=expired))

    # ---- 执行器

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.started(ticket_id, assignment_id))

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        self.repo.atomic(lambda: self.assignments.delivered(ticket_id, assignment_id, input_ids))

    def inputs_read(self, ticket_id: str, input_ids: list[str], at: str | None = None) -> None:
        self.repo.atomic(lambda: self.assignments.read(ticket_id, input_ids, at))

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.turn_ended(ticket_id, assignment_id))

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        self.repo.atomic(lambda: self.assignments.delivery_failed(ticket_id, assignment_id, error))

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        self.repo.atomic(lambda: self.tickets.mark_unrelated(ticket_id, thread, reason))

    def follow_for_context(self, ticket_id: str, thread: dict) -> bool:
        return self.repo.atomic(lambda: self.tickets.follow_for_context(ticket_id, thread))

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        return self.repo.atomic(lambda: self.evals.handback(ticket_id, trigger_id, reason))

    def report(self, ticket_id: str, report: dict) -> dict:
        return self.repo.atomic(lambda: self.assignments.report(ticket_id, report))

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.interrupted(ticket_id, assignment_id))

    # ---- 设置

    def policy_changed(self, from_: str, to: str, effects: dict | None = None) -> None:
        """策略变了：受影响的未结束工单各自被告知，进行中的评估在新版本下重来。"""
        def work():
            self.tickets.policy_changed(from_, to, effects or {})
            self.evals.policy_changed(from_, to)
        self.repo.atomic(work)
