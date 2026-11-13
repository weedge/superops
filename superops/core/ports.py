"""核心依赖的端口。来源、策略、研判、执行器、渠道都属于部署，核心只认这些接口。"""
from __future__ import annotations

from typing import Any, Callable, Protocol


class SourceScope(Protocol):
    def scope(self, source: str, meta: dict) -> dict:
        """来源是否收这条触发：{'listened': True, 'observed': bool, 'row'?: str} 或 {'listened': False, 'reason': str}。
        observed = 这个范围里没 @ 机器人的消息也收；row = 准入表里的接收范围。"""


class Policy(Protocol):
    """带版本的规则；gate 决定谁可以发起工作。来源收什么不属于策略。"""
    version: str
    paused: bool
    eval_timeout_ms: int

    def gate(self, trigger: dict, thread: dict, tickets: list[dict]) -> dict:
        """{'admissible': False, 'reason': code} 或 {'admissible': True, 'strength': ..., 'basis': {...}}"""

    def revalidate(self, mandate: dict) -> bool:
        """按当前策略复核一份授权的准入依据，例如已结束的工单恢复之前。"""

    def admits_inferred(self, rules: list[str]) -> bool:
        """推断候选能否凭研判命中的这些规则接单：模式是 auto，且每条规则都启用。"""

    def may_answer(self, trigger: dict, ticket: dict) -> bool:
        """这条人的消息能否批这张工单的问题（批红）：全权审批人，或被限定可批该规则的人。"""


class Assessor(Protocol):
    """语义研判。request 只排队（按 evalId 幂等），结果通过 Engine.decide 回来。"""

    def request(self, candidate: dict) -> None: ...

    def release(self, eval_id: str) -> None:
        """评估结束时调用（不论有没有决定），让研判方丢掉它为这次评估留的东西。"""


class Executor(Protocol):
    """执行派工；并发和工作目录是它自己的事。引擎在命令内调用它，所以每个方法只排队，结果通过引擎的回调报回。"""

    def start(self, ticket: dict, assignment: dict) -> None: ...

    def deliver(self, ticket: dict, assignment: dict) -> None: ...

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None: ...


class Outbox(Protocol):
    """发往渠道的消息；引擎在命令内排队，之后由发送方送出。"""

    def send(self, message: dict) -> None:
        """幂等排队：{id, channel, text, kind: acknowledgement|reply|notice|attention, ticketId?, priority?}"""

    def status(self, message_id: str) -> str | None:
        """pending | sent | dropped；没排过队返回 None。"""

    def withdraw(self, message_id: str) -> None: ...


class Repositories(Protocol):
    """持久状态。所有方法同步，atomic 可以把一个工作单元包成事务。get 返回的是副本。"""
    triggers: Any    # get(id) / save(trigger) / disposition(id) / save_disposition(id, value)
    threads: Any     # get(key) / save(thread) / list() / active()
    tickets: Any     # get(id) / save(ticket) / list() / open() / active()
    decisions: Any   # get(id) / save(decision) / by_thread(key)

    def atomic(self, work: Callable[[], Any]) -> Any: ...


# 引擎在变更后发出的领域事件（type → data 的键）
DOMAIN_EVENTS = {
    "trigger.received": ("triggerId", "threads"),
    "eval.started": ("evalId", "threadKey", "triggerIds", "strength"),
    "decision.recorded": ("decisionId", "kind", "threadKey"),
    "ticket.created": ("ticketId", "decisionId", "strength"),
    "ticket.resumed": ("ticketId", "triggerId"),
    "ticket.ended": ("ticketId", "by", "outcome", "reason"),
    "assignment.started": ("ticketId", "assignmentId"),
    "assignment.ended": ("ticketId", "assignmentId", "reason"),
    "thread.subscribed": ("threadKey", "ticketId"),
    "thread.unsubscribed": ("threadKey", "ticketId", "by", "reason"),
    "trigger.requeued": ("triggerId",),
    "trigger.followup": ("triggerId",),
    "policy.changed": ("from", "to"),
    "attention.asked": ("ticketId", "attentionId"),
    "attention.answered": ("ticketId", "attentionId", "option", "by", "reason", "triggerId", "expired"),
}
