"""Effects：持久化的副作用队列。命令在自己的事务里 enqueue，提交之后才开始执行。

每个 (kind, key) 一行：同一个等待中的 key 再入队会替换载荷并立刻到期，但不改变排队位置。
失败按指数退避重试；抛 Later 表示“现在做不了”（暂停、没有空位），不算失败。
同一类连续失败 3 次才在健康板上报错（quiet 的类别不报，它们的失败已经显示在工单上）。

    kind         处理方           做什么
    assessment   Assessor         研判一次评估的候选并作决定
    launch       Executor         启动一次待启动的派工
    delivery     Executor         把工单等待的输入交给它运行中的派工（跑一轮）
    reply        Outbox           通过来源把消息发到渠道
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from ..core.model import now_iso

log = logging.getLogger("superops.effects")
FAILURES_BEFORE_ERROR = 3


class Later(Exception):
    """这个副作用现在做不了；稍后再试，不计失败。可带 delay（秒）。"""

    def __init__(self, message: str = "", delay: float | None = None):
        super().__init__(message)
        self.delay = delay


@dataclass
class Handler:
    run: Callable[[dict, str], None]
    limit: Callable[[], int]
    retry: tuple[float, float]
    ordered: bool
    quiet: bool


class Effects:
    def __init__(self, store, health=None, *, now: Callable[[], float] = time.time, sync: bool = False, workers: int = 16):
        self.store, self.health, self.now, self.sync = store, health, now, sync
        self.handlers: dict[str, Handler] = {}
        self.running: dict[tuple[str, str], int] = {}
        self.broken: set[str] = set()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.started = self.stopped = False
        self.pool = None if sync else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="effect")
        self.thread: threading.Thread | None = None
        self.seq = int(store.one("SELECT IFNULL(MAX(seq), 0) FROM effect") or 0)

    def handle(self, kind: str, run: Callable[[dict, str], None], *, limit: Callable[[], int] = lambda: 1,
               retry: tuple[float, float] = (2.0, 60.0), ordered: bool = False, quiet: bool = False) -> None:
        if kind in self.handlers:
            raise RuntimeError(f"{kind} 已经有处理方了")
        self.handlers[kind] = Handler(run, (lambda: min(1, limit())) if ordered else limit, retry, ordered, quiet)
        self._report(kind)

    def enqueue(self, kind: str, key: str, payload: dict | None = None, delay: float = 0) -> None:
        """在调用方的事务里写入；提交后唤醒调度。"""
        with self.lock:
            self.seq += 1
            seq = self.seq
        self.store.run(
            "INSERT INTO effect(kind, key, value, seq, due, since) VALUES (?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
            "value = excluded.value, generation = generation + 1, attempts = 0, error = '', due = excluded.due",
            (kind, key, json.dumps(payload or {}, ensure_ascii=False), seq, self.now() + delay, now_iso()))
        self.store.after_commit(self.kick)

    def cancel(self, kind: str, key: str) -> None:
        self.store.run("DELETE FROM effect WHERE kind=? AND key=?", (kind, key))

    def pending(self, kind: str, key: str | None = None) -> bool:
        if key is None:
            return bool(self.store.one("SELECT 1 FROM effect WHERE kind=? LIMIT 1", (kind,)))
        return bool(self.store.one("SELECT 1 FROM effect WHERE kind=? AND key=?", (kind, key)))

    def waiting(self) -> dict[str, dict]:
        out = {}
        for kind, n, since, attempts in self.store.all("SELECT kind, COUNT(*), MIN(since), MAX(attempts) FROM effect GROUP BY kind"):
            out[kind] = {"waiting": n, "since": since, "attempts": attempts}
        return out

    # ---- 运行

    def start(self) -> None:
        self.started = True
        if not self.sync and not self.thread:
            self.thread = threading.Thread(target=self._loop, name="effects", daemon=True)
            self.thread.start()
        self.kick()

    def kick(self) -> None:
        self.wake.set()

    def stop(self, wait: float = 10.0) -> None:
        """不再启动新的；等正在跑的收尾。等待中的行留在库里，下次启动继续。"""
        self.stopped = True
        self.wake.set()
        if self.pool:
            deadline = time.time() + wait
            while self.running and time.time() < deadline:
                time.sleep(0.1)
            self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self.stopped:
            self.wake.wait(0.5)
            self.wake.clear()
            try:
                self.dispatch()
            except Exception:  # noqa: BLE001
                log.exception("副作用调度出错")

    def dispatch(self) -> int:
        """把到期且有空位的行交给处理方；返回启动了多少个。"""
        if not self.started or self.stopped:
            return 0
        launched = 0
        for kind, handler in list(self.handlers.items()):
            try:
                launched += self._dispatch_kind(kind, handler)
                if kind in self.broken:
                    self.broken.discard(kind)
                    self._report(kind)
            except Exception as error:  # noqa: BLE001  一个种类的调度出错：记在它的健康项上，不拖住别的种类
                self.broken.add(kind)
                if self.health:
                    self.health.set(f"effect:{kind}", "error", f"队列调度出错：{error}")
        return launched

    def _dispatch_kind(self, kind: str, handler: Handler) -> int:
        launched = 0
        with self.lock:
            running = {k for (kd, k) in self.running if kd == kind}
        if handler.ordered:
            rows = self.store.all("SELECT key, value, seq, generation, attempts, due FROM effect WHERE kind=? ORDER BY seq LIMIT 1", (kind,))
            ready = [] if running else [r for r in rows if r[5] <= self.now()]
        else:
            rows = self.store.all("SELECT key, value, seq, generation, attempts, due FROM effect WHERE kind=? AND due<=? ORDER BY seq LIMIT ?",
                                  (kind, self.now(), 32 + len(running)))
            ready = [r for r in rows if r[0] not in running]
        room = handler.limit() - len(running) if ready else 0
        for row in ready:
            if room <= 0:
                break
            room -= 1
            launched += 1
            with self.lock:
                self.running[(kind, row[0])] = row[3]
            if self.sync:
                self._attempt(kind, handler, row)
            else:
                self.pool.submit(self._attempt, kind, handler, row)
        return launched

    def run_until_idle(self, max_rounds: int = 200) -> int:
        """同步模式（测试、一次性命令）：反复调度，直到没有到期可做的。返回一共跑了多少个。"""
        total = 0
        for _ in range(max_rounds):
            n = self.dispatch()
            if not n:
                break
            total += n
        return total

    def _attempt(self, kind: str, handler: Handler, row: tuple) -> None:
        key, value, seq, generation, attempts, _due = row
        failure: BaseException | None = None
        try:
            handler.run(json.loads(value), key)
        except Exception as error:  # noqa: BLE001
            failure = error
        own, args = "kind=? AND key=? AND seq=? AND generation=?", (kind, key, seq, generation)
        try:
            if failure is None:
                self.store.run(f"DELETE FROM effect WHERE {own}", args)
            elif self.stopped:
                pass  # 停机时不动它，下次启动重试
            elif isinstance(failure, Later):
                self.store.run(f"UPDATE effect SET due=? WHERE {own}", (self.now() + (failure.delay or handler.retry[0]), *args))
            else:
                log.warning("副作用 %s/%s 失败（第 %d 次）：%s", kind, key[:12], attempts + 1, failure)
                delay = min(handler.retry[1], handler.retry[0] * 2 ** min(attempts, 16))
                self.store.run(f"UPDATE effect SET attempts = attempts + 1, error=?, due=? WHERE {own}",
                               (str(failure)[:1000], self.now() + delay, *args))
            if not self.stopped:
                self._report(kind)
        finally:
            with self.lock:
                self.running.pop((kind, key), None)
            self.kick()

    def _report(self, kind: str) -> None:
        if not self.health:
            return
        row = self.store.all("SELECT COUNT(*), MIN(since), MAX(attempts), (SELECT error FROM effect WHERE kind=? AND attempts>0 "
                             "ORDER BY seq DESC LIMIT 1) FROM effect WHERE kind=?", (kind, kind))[0]
        waiting, since, attempts, error = row
        failing = (attempts or 0) >= FAILURES_BEFORE_ERROR and not self.handlers[kind].quiet
        detail = f"{waiting} 个在等，最早 {since}" if waiting else "没有在等的"
        if failing:
            self.health.set(f"effect:{kind}", "error", f"{detail}；最近一次失败：{error}")
        else:
            self.health.set(f"effect:{kind}", "ready", detail)
