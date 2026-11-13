"""Outbox：引擎的 Outbox 端口，也是唯一写回执的地方，是所有“找人”的消息的唯一出口。

send 在一个事务里留回执并排 reply 副作用；副作用的处理方通过来源的渠道把它发出去。
同一张工单发往同一个会话的消息按排队顺序发；来源关着时消息等着，会话没了才丢弃。
打扰人的渠道（钉钉）还要过三道闸：静默时段只发 P0、每天最多 N 张问题卡片、暂停期间不发。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from ..core.model import to_iso
from .effects import Later


def parse_quiet(spec: str | None) -> tuple[int, int] | None:
    if not spec or "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    h1, m1 = (int(x) for x in a.split(":"))
    h2, m2 = (int(x) for x in b.split(":"))
    return h1 * 60 + m1, h2 * 60 + m2


def quiet_until(local_now: datetime, spec: str | None) -> datetime | None:
    """现在是否在静默时段；是的话返回静默结束的本地时间。"""
    window = parse_quiet(spec)
    if not window:
        return None
    start, end = window
    minute = local_now.hour * 60 + local_now.minute
    inside = start <= minute < end if start < end else (minute >= start or minute < end)
    if not inside:
        return None
    until = local_now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    return until if until > local_now else until + timedelta(days=1)


class Outbox:
    def __init__(self, store, effects, sources, *, paused: Callable[[], bool], config: Callable[[], dict],
                 local_now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.store, self.effects, self.sources = store, effects, sources
        self.paused, self.config, self.local_now = paused, config, local_now
        effects.handle("reply", lambda _payload, mid: self.deliver(mid), limit=lambda: 0 if paused() else 1, retry=(5.0, 300.0))

    # ---- 引擎端口

    def send(self, message: dict) -> None:
        def work():
            if self.store.receipts.add(message):
                self.effects.enqueue("reply", message["id"], {})
        self.store.atomic(work)

    def status(self, message_id: str) -> str | None:
        receipt = self.store.receipts.get(message_id)
        return receipt["status"] if receipt else None

    def withdraw(self, message_id: str) -> None:
        def work():
            if self.store.receipts.withdraw(message_id):
                self.effects.cancel("reply", message_id)
        self.store.atomic(work)

    # ---- 发送

    def deliver(self, message_id: str) -> None:
        receipt = self.store.receipts.get(message_id)
        if not receipt or receipt["status"] != "pending":
            return
        if self.store.receipts.behind(receipt):
            raise Later("同一工单更早的消息还没发出去")
        channel = receipt["channel"]
        if not self.sources.enabled(channel["source"]):
            raise Later("来源关着，消息先等着", 30)
        priority = receipt.get("priority", 2)
        if self.sources.interrupts(channel["source"]):
            cfg = self.config()
            now = self.local_now()
            until = quiet_until(now, cfg.get("quiet_hours"))
            if until and priority > 0:
                raise Later("静默时段只发 P0", max(30.0, min((until - now).total_seconds(), 3600)))
            cap = cfg.get("max_cards_per_day")
            if receipt["kind"] == "attention" and cap and priority > 0:
                day_start = to_iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
                if self.store.receipts.sent_today("attention", day_start) >= int(cap):
                    raise Later("今天的卡片已到上限，顺延到明天", 1800)
        result = self.sources.send(receipt)
        if result == "gone":
            self.store.receipts.settle(message_id, "dropped", "channelGone")
        elif result == "ignored":
            self.store.receipts.settle(message_id, "dropped", "channelIgnored")
        else:
            self.store.receipts.settle(message_id, "sent")

    def notice(self, channel: dict, text: str, *, key: str, priority: int = 2, ticket_id: str | None = None, kind: str = "notice") -> None:
        """宿主自己要说的话（告警、发布结果、文件等）也走同一个出口。key 相同只发一次。"""
        message = {"id": key, "channel": channel, "text": text, "kind": kind, "priority": priority}
        if ticket_id:
            message["ticketId"] = ticket_id
        self.send(message)
