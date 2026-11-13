"""工单阶段和能否重试（docs/003）。按表从上往下，第一条命中的算数。"""
from __future__ import annotations

from .model import latest_assignment, waits_for_person


def ticket_stage(ticket: dict) -> dict:
    """{'stage': 'person', 'action': delivery|answer|review|blocked|failed} | {'stage': 'working', 'action'?: 'queued'}
    | {'stage': 'idle'} | {'stage': 'ended'}"""
    if ticket.get("ended"):
        return {"stage": "ended"}
    if ticket.get("delivery"):
        return {"stage": "person", "action": "delivery"}
    if ticket.get("attention"):
        return {"stage": "person", "action": "answer"}
    assignment = latest_assignment(ticket)
    report = ((assignment or {}).get("report") or {}).get("state")
    if report == "ready":
        return {"stage": "person", "action": "review"}
    if report in ("blocked", "failed") and waits_for_person(ticket):
        return {"stage": "person", "action": report}
    if not assignment or assignment["state"] == "pending":
        return {"stage": "working", "action": "queued"}
    if report == "waiting" or assignment["state"] == "ended":
        return {"stage": "idle"}
    return {"stage": "working"}


def can_retry(ticket: dict) -> bool:
    """未结束，且卡在 blocked / failed（之后没有人的消息）或输入送达失败。"""
    return not ticket.get("ended") and (waits_for_person(ticket) or bool(ticket.get("delivery")))
