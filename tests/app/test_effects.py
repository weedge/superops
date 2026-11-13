"""副作用队列（superops/app/effects.py）：合并、并发上限、有序、退避重试、Later、取消、事务、健康板、停机续跑。

场景移植自原型的 tests/effects.spec.ts。除了两条真并发的测试，都用同步模式和可注入的时钟。
"""
from __future__ import annotations

import threading
import time

import pytest

from superops.app.effects import Effects, Later
from superops.app.health import HealthBoard
from superops.app.store import Store


class Clock:
    def __init__(self, at: float = 1_000_000.0):
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


class Calls:
    """记下每次调用的 (payload, key)；fail 里的 key 会抛对应的异常。"""

    def __init__(self):
        self.seen: list[tuple[dict, str]] = []
        self.fail: dict[str, Exception] = {}

    def __call__(self, payload: dict, key: str) -> None:
        self.seen.append((payload, key))
        if key in self.fail:
            raise self.fail[key]

    @property
    def keys(self) -> list[str]:
        return [k for _, k in self.seen]


@pytest.fixture
def q(tmp_path):
    class Q:
        pass
    x = Q()
    x.store = Store(tmp_path / "db.sqlite")
    x.clock = Clock()
    x.health = HealthBoard()
    x.effects = Effects(x.store, x.health, now=x.clock, sync=True)
    x.effects.start()
    x.row = lambda kind, key: (x.store.all("SELECT attempts, error, due, generation, seq, value FROM effect WHERE kind=? AND key=?", (kind, key)) or [None])[0]
    yield x
    x.store.close()


# ---------------------------------------------------------------- 事务


def test_事务提交之后才执行_回滚的事务里入队的不执行(q):
    run = Calls()
    q.effects.handle("relay", run)
    kicks = []
    q.effects.kick = lambda: kicks.append(1)

    def committed():
        q.effects.enqueue("relay", "kept", {"text": "a"})
        assert kicks == []                               # 提交之前不唤醒调度

    q.store.atomic(committed)
    assert kicks == [1]

    def rolled_back():
        q.effects.enqueue("relay", "dropped", {"text": "b"})
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError, match="rolled back"):
        q.store.atomic(rolled_back)
    assert kicks == [1]                                  # 回滚的不唤醒
    assert q.effects.pending("relay", "kept") and not q.effects.pending("relay", "dropped")
    assert q.effects.run_until_idle() == 1
    assert run.seen == [({"text": "a"}, "kept")]
    assert q.effects.pending("relay") is False


def test_不在事务里入队也会执行_没有载荷时处理方拿到空字典(q):
    run = Calls()
    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "k")
    q.effects.run_until_idle()
    assert run.seen == [({}, "k")]


def test_没start之前什么都不调度(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    effects = Effects(store, now=Clock(), sync=True)
    run = Calls()
    effects.handle("relay", run)
    effects.enqueue("relay", "k", {})
    assert effects.dispatch() == 0 and run.seen == []
    effects.start()
    assert effects.dispatch() == 1 and run.keys == ["k"]
    store.close()


def test_一个种类只能有一个处理方(q):
    q.effects.handle("relay", Calls())
    with pytest.raises(RuntimeError, match="relay"):
        q.effects.handle("relay", Calls())


# ---------------------------------------------------------------- 合并与排队位置


def test_同一key再入队会合并成一行_用最新的载荷_保持排队位置(q):
    run, room = Calls(), [0]
    q.effects.handle("launch", run, limit=lambda: room[0])

    def work():
        q.effects.enqueue("launch", "first", {"n": 1})
        q.effects.enqueue("launch", "second", {"n": 2})
        q.effects.enqueue("launch", "first", {"n": 3})

    q.store.atomic(work)
    assert q.effects.run_until_idle() == 0               # 上限是 0：都压着
    assert q.store.one("SELECT COUNT(*) FROM effect WHERE kind='launch'") == 2
    assert q.effects.waiting()["launch"]["waiting"] == 2
    room[0] = 1
    q.effects.run_until_idle()
    assert run.seen == [({"n": 3}, "first"), ({"n": 2}, "second")]
    assert q.effects.pending("launch") is False


def test_再入队让等待中的行立刻到期_并清掉失败次数(q):
    run = Calls()
    run.fail["k"] = RuntimeError("boom")
    q.effects.handle("relay", run, retry=(100.0, 1000.0))
    q.effects.enqueue("relay", "k", {"v": 1})
    q.effects.run_until_idle()
    assert q.row("relay", "k")[0] == 1                   # 失败过一次，100 秒后才重试
    run.fail.clear()
    q.effects.enqueue("relay", "k", {"v": 2})
    attempts, error, due, *_ = q.row("relay", "k")
    assert (attempts, error, due) == (0, "", q.clock())
    assert q.effects.run_until_idle() == 1 and run.seen[-1] == ({"v": 2}, "k")


def test_带delay入队的到点才执行(q):
    run = Calls()
    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "later", {}, delay=30)
    q.clock.advance(29.5)
    assert q.effects.run_until_idle() == 0
    q.clock.advance(0.5)
    assert q.effects.run_until_idle() == 1 and run.keys == ["later"]


def test_执行期间同一key又入队_这次跑完后用新的载荷再跑一次(q):
    seen = []

    def run(payload, key):
        seen.append(payload["text"])
        if payload["text"] == "first":
            q.effects.enqueue("relay", "k", {"text": "second"})
            q.effects.enqueue("relay", "k", {"text": "third"})

    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "k", {"text": "first"})
    q.effects.run_until_idle()
    assert seen == ["first", "third"] and q.effects.pending("relay") is False


def test_执行期间被取消又重新入队的_第一次跑完不会把它带走(q):
    seen = []

    def run(payload, key):
        seen.append(payload["text"])
        if len(seen) == 1:
            q.effects.cancel("relay", "k")
            q.effects.enqueue("relay", "k", {"text": "second"})

    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "k", {"text": "first"})
    q.effects.run_until_idle()
    assert seen == ["first", "second"] and q.effects.pending("relay") is False


def test_cancel把等待中的拿掉_之后不再执行(q):
    run = Calls()
    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "a", {})
    q.effects.enqueue("relay", "b", {})
    assert q.effects.pending("relay", "a") and q.effects.pending("relay", "b")
    q.effects.cancel("relay", "a")
    q.effects.cancel("relay", "never-queued")           # 取消不存在的不出错
    assert not q.effects.pending("relay", "a") and q.effects.pending("relay")
    q.effects.run_until_idle()
    assert run.keys == ["b"]


def test_cancel只管自己那个种类下的key(q):
    relay, launch = Calls(), Calls()
    q.effects.handle("relay", relay)
    q.effects.handle("launch", launch)
    q.effects.enqueue("relay", "same", {})
    q.effects.enqueue("launch", "same", {})
    q.effects.cancel("relay", "same")
    q.effects.run_until_idle()
    assert relay.keys == [] and launch.keys == ["same"]


# ---------------------------------------------------------------- 并发上限


def test_每个种类按入队先后执行_一轮调度不超过它的上限_上限为0时压住(q):
    run, limit = Calls(), [2]
    q.effects.handle("launch", run, limit=lambda: limit[0])
    for key in ("c", "a", "b"):
        q.effects.enqueue("launch", key, {})
    assert q.effects.dispatch() == 2 and run.keys == ["c", "a"]       # 先入队的先跑，不按 key 排
    limit[0] = 0
    assert q.effects.dispatch() == 0 and run.keys == ["c", "a"]
    assert q.effects.pending("launch", "b")
    limit[0] = 2
    assert q.effects.dispatch() == 1 and run.keys == ["c", "a", "b"]


def test_没有到期的东西时不问上限(q):
    asked = []
    q.effects.handle("delivery", Calls(), limit=lambda: asked.append(1) or 4)
    q.effects.dispatch()
    q.effects.enqueue("delivery", "t", {}, delay=60)
    q.effects.dispatch()
    assert asked == []
    q.clock.advance(60)
    q.effects.dispatch()
    assert asked == [1]


def test_各种类的上限互不影响(q):
    relay, launch = Calls(), Calls()
    q.effects.handle("relay", relay, limit=lambda: 0)
    q.effects.handle("launch", launch, limit=lambda: 5)
    for n in range(3):
        q.effects.enqueue("relay", f"r{n}", {})
        q.effects.enqueue("launch", f"l{n}", {})
    q.effects.run_until_idle()
    assert relay.keys == [] and launch.keys == ["l0", "l1", "l2"]


def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not condition():
        if time.time() > deadline:
            raise AssertionError("等待超时")
        time.sleep(0.01)


def test_线程模式下同时在跑的不超过上限(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    effects = Effects(store, HealthBoard())
    lock, state = threading.Lock(), {"now": 0, "peak": 0, "done": []}

    def run(payload, key):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
            state["done"].append(key)

    effects.handle("launch", run, limit=lambda: 2)
    effects.start()
    try:
        for n in range(6):
            effects.enqueue("launch", f"k{n}", {})
        wait_for(lambda: len(state["done"]) == 6)
        assert state["peak"] == 2 and sorted(state["done"]) == [f"k{n}" for n in range(6)]
        wait_for(lambda: not effects.pending("launch"))
    finally:
        effects.stop(wait=2)
        store.close()


# ---------------------------------------------------------------- 有序的种类


def test_有序的种类一次一个_按入队顺序_前面失败的挡住后面的(q):
    run = Calls()
    run.fail["first"] = RuntimeError("offline")
    q.effects.handle("relay", run, ordered=True, limit=lambda: 4, retry=(1.0, 1.0))
    q.effects.enqueue("relay", "first", {})
    q.effects.enqueue("relay", "second", {})
    assert q.effects.dispatch() == 1                     # 上限写的是 4，有序的也只起一个
    q.effects.run_until_idle()
    q.clock.advance(1)
    q.effects.run_until_idle()
    assert run.keys == ["first", "first"]                # second 一直等着
    run.fail.clear()
    q.clock.advance(1)
    q.effects.run_until_idle()
    assert run.keys == ["first", "first", "first", "second"]
    assert q.effects.pending("relay") is False


def test_有序的种类里_前面那个被Later推迟时后面的也等着(q):
    run = Calls()
    run.fail["first"] = Later("来源关着", 30)
    q.effects.handle("relay", run, ordered=True)
    q.effects.enqueue("relay", "first", {})
    q.effects.enqueue("relay", "second", {})
    q.effects.run_until_idle()
    assert run.keys == ["first"]
    run.fail.clear()
    q.clock.advance(30)
    q.effects.run_until_idle()
    assert run.keys == ["first", "first", "second"]


def test_有序的种类上限为0时也压住(q):
    run, limit = Calls(), [0]
    q.effects.handle("relay", run, ordered=True, limit=lambda: limit[0])
    q.effects.enqueue("relay", "a", {})
    assert q.effects.run_until_idle() == 0
    limit[0] = 7
    assert q.effects.run_until_idle() == 1


# ---------------------------------------------------------------- 失败重试、Later、健康板


def test_失败按指数退避重试到上限_次数累计_连续3次后健康板报错_成功后恢复(q):
    run = Calls()
    run.fail["prompts"] = RuntimeError("session busy")
    q.effects.handle("notice", run, retry=(1.0, 3.0))
    assert q.health.get("effect:notice")["state"] == "ready" and q.health.get("effect:notice")["detail"] == "没有在等的"
    q.effects.enqueue("notice", "prompts", {"summary": "x"})
    q.effects.run_until_idle()
    assert len(run.seen) == 1 and q.row("notice", "prompts")[:2] == (1, "session busy")
    entry = q.health.get("effect:notice")
    assert entry["state"] == "ready" and entry["detail"].startswith("1 个在等，最早 ")
    for nth, wait in enumerate((1.0, 2.0, 3.0, 3.0), start=2):
        q.clock.advance(wait - 0.5)
        q.effects.run_until_idle()
        assert len(run.seen) == nth - 1                  # 还没到点
        q.clock.advance(0.5)
        q.effects.run_until_idle()
        assert len(run.seen) == nth and q.row("notice", "prompts")[0] == nth
        state = q.health.get("effect:notice")["state"]
        assert state == ("error" if nth >= 3 else "ready")
    assert "最近一次失败：session busy" in q.health.get("effect:notice")["detail"]
    assert q.effects.waiting()["notice"] == {"waiting": 1, "since": q.effects.waiting()["notice"]["since"], "attempts": 5}
    run.fail.clear()
    q.clock.advance(3)
    q.effects.run_until_idle()
    assert q.effects.pending("notice") is False
    assert q.health.get("effect:notice")["state"] == "ready" and q.health.get("effect:notice")["detail"] == "没有在等的"


def test_默认的重试间隔从2秒起翻倍到60秒(q):
    run = Calls()
    run.fail["k"] = RuntimeError("x")
    q.effects.handle("assessment", run)
    q.effects.enqueue("assessment", "k", {})
    delays = []
    for _ in range(7):
        q.effects.run_until_idle()
        due = q.row("assessment", "k")[2]
        delays.append(due - q.clock())
        q.clock.advance(due - q.clock())
    assert delays == [2, 4, 8, 16, 32, 60, 60]


def test_Later不计失败_到它说的时间或第一档重试间隔之后再做(q):
    run = Calls()
    run.fail["a"] = Later("paused")
    run.fail["b"] = Later("没有空位", 20)
    q.effects.handle("launch", run, limit=lambda: 5, retry=(5.0, 60.0))
    q.effects.enqueue("launch", "a", {})
    q.effects.enqueue("launch", "b", {})
    q.effects.run_until_idle()
    assert q.row("launch", "a")[:3] == (0, "", q.clock() + 5) and q.row("launch", "b")[:3] == (0, "", q.clock() + 20)
    assert q.health.get("effect:launch")["state"] == "ready" and q.health.get("effect:launch")["detail"].startswith("2 个在等")
    q.clock.advance(4.5)
    assert q.effects.run_until_idle() == 0
    run.fail.pop("a")
    q.clock.advance(0.5)
    q.effects.run_until_idle()
    assert run.keys == ["a", "b", "a"] and not q.effects.pending("launch", "a")
    for _ in range(5):                                   # Later 多少次都不算失败
        q.clock.advance(20)
        q.effects.run_until_idle()
    assert q.row("launch", "b")[0] == 0 and q.health.get("effect:launch")["state"] == "ready"


def test_Later不清掉之前累计的失败次数(q):
    run = Calls()
    run.fail["k"] = RuntimeError("boom")
    q.effects.handle("relay", run, retry=(1.0, 1.0))
    q.effects.enqueue("relay", "k", {})
    for _ in range(2):
        q.effects.run_until_idle()
        q.clock.advance(1)
    run.fail["k"] = Later("稍后")
    q.effects.run_until_idle()
    assert q.row("relay", "k")[0] == 2


def test_quiet的种类失败再多也不上健康板(q):
    run = Calls()
    run.fail["t"] = RuntimeError("Session gone")
    q.effects.handle("delivery", run, quiet=True, retry=(1.0, 1.0))
    q.effects.enqueue("delivery", "t", {})
    for _ in range(4):
        q.effects.run_until_idle()
        q.clock.advance(1)
    assert len(run.seen) == 4 and q.row("delivery", "t")[0] == 4
    entry = q.health.get("effect:delivery")
    assert entry["state"] == "ready" and entry["detail"].startswith("1 个在等")
    assert q.health.problems() == []


def test_一个种类里只要有一项连续失败到3次就报错_别的种类不受牵连(q):
    bad, good = Calls(), Calls()
    bad.fail["x"] = RuntimeError("坏了")
    q.effects.handle("relay", bad, limit=lambda: 5, retry=(1.0, 1.0))
    q.effects.handle("launch", good)
    q.effects.enqueue("relay", "x", {})
    for n in range(3):
        q.effects.enqueue("relay", f"ok{n}", {})
        q.effects.enqueue("launch", f"l{n}", {})
        q.effects.run_until_idle()
        q.clock.advance(1)
    assert [p["name"] for p in q.health.problems()] == ["effect:relay"]
    assert q.health.get("effect:launch")["state"] == "ready"
    assert good.keys == ["l0", "l1", "l2"] and [k for k in bad.keys if k != "x"] == ["ok0", "ok1", "ok2"]


def test_没有健康板也能跑(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    effects = Effects(store, now=Clock(), sync=True)
    run = Calls()
    run.fail["k"] = RuntimeError("x")
    effects.handle("relay", run)
    effects.start()
    effects.enqueue("relay", "k", {})
    assert effects.run_until_idle() == 1 and effects.pending("relay", "k")
    store.close()


def test_某个种类的调度自身出错_记在它的健康项上_不拖住别的种类(q):
    broken, later = [True], Calls()

    def limit():
        if broken[0]:
            raise RuntimeError("database is locked")
        return 1

    q.effects.handle("relay", Calls(), limit=limit)
    q.effects.handle("launch", later)
    q.effects.enqueue("relay", "k", {})
    q.effects.enqueue("launch", "l", {})
    try:
        q.effects.run_until_idle()
    except RuntimeError:
        pass
    assert later.keys == ["l"]
    assert q.health.get("effect:relay")["state"] == "error" and "database is locked" in q.health.get("effect:relay")["detail"]
    broken[0] = False
    q.effects.run_until_idle()
    assert q.health.get("effect:relay")["state"] == "ready"


# ---------------------------------------------------------------- 停机与续跑


def test_stop之后不再启动新的_等待中的行留在库里_新的Effects按原来的顺序接着跑(q):
    q.effects.handle("relay", Calls(), limit=lambda: 0)
    q.effects.enqueue("relay", "first", {"text": "a"})
    q.effects.enqueue("relay", "second", {"text": "b"})
    q.effects.stop()
    q.effects.enqueue("relay", "late", {"text": "c"})    # 停机之后入队的也留着
    assert q.effects.dispatch() == 0 and q.effects.run_until_idle() == 0
    assert q.store.one("SELECT COUNT(*) FROM effect") == 3
    following = Effects(q.store, HealthBoard(), now=q.clock, sync=True)
    following.enqueue("relay", "third", {"text": "d"})   # 重启后入队的排在原来等着的后面
    run = Calls()
    following.handle("relay", run, ordered=True)
    following.start()
    following.run_until_idle()
    assert run.seen == [({"text": "a"}, "first"), ({"text": "b"}, "second"), ({"text": "c"}, "late"), ({"text": "d"}, "third")]
    assert following.pending("relay") is False


def test_停机时被打断的那一次不算失败_下次启动重试(q):
    def run(payload, key):
        q.effects.stop()
        raise RuntimeError("aborted")

    q.effects.handle("relay", run)
    q.effects.enqueue("relay", "cut", {"text": "a"})
    q.effects.dispatch()
    assert q.row("relay", "cut")[:2] == (0, "")
    following = Effects(q.store, now=q.clock, sync=True)
    again = Calls()
    following.handle("relay", again)
    following.start()
    following.run_until_idle()
    assert again.seen == [({"text": "a"}, "cut")]


def test_线程模式_stop等正在跑的收尾_被打断的和没轮到的都留给下一次(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    effects = Effects(store, HealthBoard())
    started, release = threading.Event(), threading.Event()

    def run(payload, key):
        started.set()
        release.wait(5)
        raise RuntimeError("aborted")

    effects.handle("relay", run)
    effects.start()
    effects.enqueue("relay", "cut", {"text": "a"})
    assert started.wait(5)
    stopper = threading.Thread(target=lambda: effects.stop(wait=5))
    stopper.start()
    wait_for(lambda: effects.stopped)
    effects.enqueue("relay", "later", {"text": "b"})
    release.set()
    stopper.join(5)
    assert not stopper.is_alive() and not effects.running
    assert store.all("SELECT key, attempts FROM effect ORDER BY seq") == [("cut", 0), ("later", 0)]
    following = Effects(store, now=time.time, sync=True)
    run_again = Calls()
    following.handle("relay", run_again)
    following.start()
    following.run_until_idle()
    assert run_again.keys == ["cut", "later"]
    store.close()


def test_waiting按种类汇总等待的数量_最早的时间和最多的失败次数(q):
    assert q.effects.waiting() == {}
    run = Calls()
    run.fail["bad"] = RuntimeError("x")
    q.effects.handle("relay", run, limit=lambda: 5)
    q.effects.handle("launch", Calls(), limit=lambda: 0)
    q.effects.enqueue("relay", "bad", {})
    q.effects.enqueue("launch", "a", {})
    q.effects.enqueue("launch", "b", {})
    q.effects.run_until_idle()
    waiting = q.effects.waiting()
    assert waiting["relay"]["waiting"] == 1 and waiting["relay"]["attempts"] == 1
    assert waiting["launch"]["waiting"] == 2 and waiting["launch"]["attempts"] == 0 and waiting["launch"]["since"].endswith("Z")
