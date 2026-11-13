"""健康板：每个部件一条，由认领它的那一方写。健康的状态不占地方，面板只列 error。"""
from __future__ import annotations

import threading

from ..core.model import now_iso


class HealthBoard:
    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.lock = threading.Lock()

    def set(self, name: str, state: str, detail: str = "") -> None:
        """state: ready | error | off | waiting"""
        with self.lock:
            self.entries[name] = {"name": name, "state": state, "detail": detail, "at": now_iso()}

    def get(self, name: str) -> dict | None:
        return self.entries.get(name)

    def all(self) -> list[dict]:
        with self.lock:
            return sorted(self.entries.values(), key=lambda e: e["name"])

    def problems(self) -> list[dict]:
        return [e for e in self.all() if e["state"] == "error"]
