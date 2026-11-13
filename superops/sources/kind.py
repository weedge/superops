"""来源：事件流的一种可复用实现（kind）+ 一份配置（instance）。

来源自己保存读到哪了、自己的机器人身份；定义什么算一条事件（包括要等一段时间才成立的条件）；
把自己发的消息和不收的范围在进入领域之前丢掉；把每条触发翻译成它所属的线程和回复用的渠道地址。
新增一种来源见 docs/008。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("superops.sources")


class SourceKind:
    id = ""
    title = ""
    #: 系统来源：事件没有发送人（定时、流水线），默认作为推断候选进研判
    system = False
    #: 渠道会打扰人（要过静默时段和每日卡片上限）
    interrupts = False

    def __init__(self, host: Any):
        self.host = host            # Runtime 的窄接口：receive / store / settings / cfg / health / engine 等

    def config(self) -> dict:
        return ((self.host.settings.current()["sources"].get(self.id) or {}).get("config")) or {}

    def scope(self, meta: dict) -> dict:
        """这条触发收不收。返回 {'listened': True, 'observed': bool, 'row'?, 'system'?} 或 {'listened': False, 'reason': code}。"""
        return {"listened": True, "observed": True, "row": "events", "system": self.system}

    def send(self, message: dict) -> str:
        """把一条消息写到渠道：返回 'sent'、'gone'（会话永远收不到了）或 'ignored'；暂时失败就抛异常，稍后重试。"""
        raise NotImplementedError

    def tick(self) -> None:
        """每一轮 pass 调一次：轮询型来源在这里取新事件。"""

    def start(self) -> None:
        """长连接型来源在这里起自己的线程。"""

    def stop(self) -> None:
        pass

    def report(self, state: str, detail: str = "") -> None:
        self.host.health.set(f"source:{self.id}", state, detail)


class SourceRegistry:
    """登记了哪些来源，以及每个来源开没开；同时是引擎的 SourceScope。"""

    def __init__(self, settings_getter):
        self.kinds: dict[str, SourceKind] = {}
        self._settings = settings_getter

    def register(self, kind: SourceKind) -> None:
        if kind.id in self.kinds:
            raise RuntimeError(f"来源 {kind.id} 重复登记")
        self.kinds[kind.id] = kind

    def kind(self, source: str) -> SourceKind | None:
        return self.kinds.get(source)

    def enabled(self, source: str) -> bool:
        if source not in self.kinds:
            return False
        instance = self._settings()["sources"].get(source)
        return bool(instance["enabled"]) if instance else True

    def interrupts(self, source: str) -> bool:
        kind = self.kinds.get(source)
        return bool(kind and kind.interrupts)

    def scope(self, source: str, meta: dict) -> dict:
        if not self.enabled(source):
            return {"listened": False, "reason": "source_off"}
        return self.kinds[source].scope(meta or {})

    def send(self, message: dict) -> str:
        kind = self.kinds.get(message["channel"]["source"])
        if not kind:
            return "gone"
        return kind.send(message)

    def tick(self) -> None:
        for kind in list(self.kinds.values()):
            if not self.enabled(kind.id):
                kind.report("off")
                continue
            try:
                kind.tick()
            except Exception as error:  # noqa: BLE001
                log.exception("来源 %s 轮询出错", kind.id)
                kind.report("error", str(error)[:300])

    def start(self) -> None:
        for kind in self.kinds.values():
            if self.enabled(kind.id):
                kind.start()

    def stop(self) -> None:
        for kind in self.kinds.values():
            try:
                kind.stop()
            except Exception:  # noqa: BLE001
                log.exception("来源 %s 停止出错", kind.id)
