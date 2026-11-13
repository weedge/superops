"""内存里的仓库：顺序和嵌套回滚与 Store 一致，给领域测试用。get 返回副本，和从 SQLite 读出来一样。"""
from __future__ import annotations

import copy

from .model import live_ticket


class _Table:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        row = self.rows.get(key)
        return copy.deepcopy(row) if row is not None else None

    def all(self) -> list[dict]:
        return [copy.deepcopy(r) for r in self.rows.values()]


class _Triggers(_Table):
    def __init__(self):
        super().__init__()
        self.dispositions: dict[str, dict] = {}

    def save(self, trigger: dict) -> None:
        self.rows[trigger["id"]] = copy.deepcopy(trigger)

    def disposition(self, key: str) -> dict | None:
        row = self.dispositions.get(key)
        return copy.deepcopy(row) if row is not None else None

    def save_disposition(self, key: str, value: dict) -> None:
        self.dispositions[key] = copy.deepcopy(value)


class _Threads(_Table):
    def save(self, thread: dict) -> None:
        self.rows[thread["key"]] = copy.deepcopy(thread)

    def list(self) -> list[dict]:
        """最近有触发的在前（稳定排序），和 Store 一致。"""
        return sorted(self.all(), key=lambda t: t.get("lastAt") or "", reverse=True)

    def active(self) -> list[dict]:
        return [t for t in self.all() if t["pending"] or t.get("evaluating")]


class _Tickets(_Table):
    def save(self, ticket: dict) -> None:
        self.rows[ticket["id"]] = copy.deepcopy(ticket)

    def list(self) -> list[dict]:
        """新的在前，和 Store 一致。"""
        return self.all()[::-1]

    def open(self) -> list[dict]:
        return [t for t in self.all() if live_ticket(t)]

    def active(self) -> list[dict]:
        return [t for t in self.all() if t["undelivered"] or (t["assignments"] and t["assignments"][-1]["state"] in ("pending", "running"))]


class _Decisions(_Table):
    def save(self, decision: dict) -> None:
        self.rows[decision["id"]] = copy.deepcopy(decision)

    def by_thread(self, key: str) -> list[dict]:
        return [d for d in self.all() if d["threadKey"] == key]


class MemoryRepositories:
    def __init__(self):
        self.triggers = _Triggers()
        self.threads = _Threads()
        self.tickets = _Tickets()
        self.decisions = _Decisions()

    def _tables(self):
        return [self.triggers.rows, self.triggers.dispositions, self.threads.rows, self.tickets.rows, self.decisions.rows]

    def atomic(self, work):
        """出错时整体回滚到进入前的状态；可以嵌套。"""
        snapshot = [copy.deepcopy(t) for t in self._tables()]
        try:
            return work()
        except BaseException:
            for table, saved in zip(self._tables(), snapshot):
                table.clear()
                table.update(saved)
            raise
