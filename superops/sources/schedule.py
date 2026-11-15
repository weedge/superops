"""定时来源：schedules.yaml 里的每一项到点产生一条触发。

“到某个时间”是一个要等才成立的条件，所以它是一个普通的来源，评估从不自己等。
机器睡着错过的，醒来后补发最近一次（超过 max_late 就不补）。每个定时类型一个线程：schedule:<type>。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..infra import config as cfgmod
from ..biz import scheduler
from ..infra.timeutil import iso, now as biz_now
from ..core.reasons import key
from .kind import SourceKind


class ScheduleSource(SourceKind):
    id = "schedule"
    title = "定时"
    system = True

    def __init__(self, host, *, now: Callable[[], datetime] = biz_now):
        super().__init__(host)
        self.now = now
        self._specs: list[dict] | None = None

    def specs(self) -> list[dict]:
        if self._specs is None:
            self._specs = cfgmod.load_schedules(self.host.home) or []
        return self._specs

    def reload(self) -> None:
        self._specs = None

    def scope(self, meta: dict) -> dict:
        return {"listened": True, "observed": True, "row": "schedule", "system": True}

    def _required(self, dotted: str) -> bool:
        node = self.host.cfg
        for part in dotted.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return bool(node)

    def tick(self) -> None:
        at = self.now()
        fired = []
        for spec in self.specs():
            type_ = spec["type"]
            if spec.get("requires") and not self._required(spec["requires"]):
                continue
            due = scheduler.last_due(spec, at)
            state = self.host.store.get(f"schedule:{type_}", {})
            if state.get("last_due") == iso(due):
                continue
            if not state:  # 第一次见到这个定时：从下一次开始，不补发装机之前的
                self.host.store.set(f"schedule:{type_}", {"last_due": iso(due), "fired_at": None})
                continue
            self.host.store.set(f"schedule:{type_}", {"last_due": iso(due), "fired_at": iso(at)})
            if at - due > scheduler.max_late(spec):
                continue
            self.fire(type_, due, spec.get("data"))
            fired.append(type_)
        self.report("ready", f"{len(self.specs())} 个定时" + (f"，刚触发 {', '.join(fired)}" if fired else ""))

    def fire(self, type_: str, due: datetime | None = None, data: dict | None = None) -> dict:
        """产生一条定时触发（也给 `superops fire` 手动补发用）。"""
        due = due or self.now()
        trigger = {
            "id": key("schedule", type_, iso(due)), "source": self.id, "threads": [{"source": self.id, "id": type_}],
            "actor": {"source": self.id, "id": ""}, "human": False, "at": due.isoformat(), "text": f"定时触发 {type_}",
            "meta": {"type": type_, "origin": "cron", "subject": type_, "due": iso(due), **({"data": data} if data else {})}}
        return {"id": trigger["id"], "result": self.host.receive(trigger)}

    def send(self, message: dict) -> str:
        return "ignored"  # 定时没有可回复的会话；这类工单的消息发到 home 渠道
