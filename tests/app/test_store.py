"""Store（superops/app/store.py）与内存仓库（superops/core/memory.py）。

前半部分的断言对两种仓库各跑一遍（参数化），确认领域核心在测试里用的内存仓库和线上用的 SQLite 行为一致；
后半部分是 Store 独有的：after_commit、键值、编号、计数器、回执、运行记录、只读查询、数据保留。
场景部分移植自原型的 tests/store.spec.ts。
"""
from __future__ import annotations

import pytest

from superops.app.store import SCHEMA_VERSION, Store
from superops.core.memory import MemoryRepositories


def stamp(n: int) -> str:
    return f"2026-11-11T00:00:{n:02d}.000Z"


def trigger(id_: str, *, thread: str = "x", source: str = "chat", at: str = stamp(1), human: bool = True, text: str = "hi",
            threads: list | None = None, meta: dict | None = None) -> dict:
    return {"id": id_, "source": source, "threads": threads or [{"source": source, "id": thread}], "actor": {"source": source, "id": "u"},
            "human": human, "at": at, "text": text, "meta": meta or {}}


def thread(id_: str, source: str = "git", **fields) -> dict:
    return {"key": f"{source}:{id_}", "ref": {"source": source, "id": id_}, "createdAt": "", "subscriptions": [], "correlated": [],
            "pending": [], "meta": {}, **fields}


def ticket(id_: str, *, ended: bool = False, states: tuple = (), undelivered: tuple = (), n: int = 1, no: int | None = None,
           rules: list | None = None, **fields) -> dict:
    out = {"id": id_, "title": id_, "decisionId": "", "channels": [], "inputs": [], "undelivered": list(undelivered),
           "mandate": {"strength": "explicit", "policyVersion": "", "basis": {"via": "addressed", **({"rules": rules} if rules else {})},
                       "actor": {"source": "x", "id": ""}, "scope": ""},
           "assignments": [{"id": f"{id_}-{i}", "state": s, "createdAt": ""} for i, s in enumerate(states)],
           "createdAt": stamp(n), "updatedAt": stamp(n), "origin": {"triggerId": "", "triggerIds": []}, **fields}
    if ended:
        out["ended"] = {"at": stamp(n), "by": "executor", "outcome": "closed"}
    if no is not None:
        out["no"] = no
    return out


def decision(id_: str, *, thread_key: str = "git:1", at: str | None = None, kind: str = "ignore", **fields) -> dict:
    return {"id": id_, "evalId": "", "threadKey": thread_key, "triggerIds": [], "policyVersion": "", "at": at or stamp(1), "kind": kind,
            "reason": [], "strength": "explicit", **fields}


def ids(items: list[dict]) -> list[str]:
    return [i.get("id") or i.get("key") for i in items]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    yield s
    s.close()


@pytest.fixture(params=["sqlite", "memory"])
def repo(request, tmp_path):
    """同一组断言各跑一遍：Store 和 MemoryRepositories。"""
    if request.param == "memory":
        yield MemoryRepositories()
        return
    s = Store(tmp_path / "db.sqlite")
    yield s
    s.close()


# ================================================================ 两种仓库共同的行为


def test_触发的存取往返_读出来的是副本(repo):
    assert repo.triggers.get("a") is None and repo.triggers.disposition("a") is None
    saved = trigger("a", meta={"type": "x.y", "data": {"n": 1}}, text="你好")
    repo.triggers.save(saved)
    assert repo.triggers.get("a") == saved
    got = repo.triggers.get("a")
    got["text"] = "被改了"
    got["meta"]["data"]["n"] = 2
    assert repo.triggers.get("a") == saved                         # 改读出来的不影响库里的
    saved["text"] = "调用方之后又改"
    assert repo.triggers.get("a")["text"] == "你好"                 # 存进去的也是副本
    repo.triggers.save({**repo.triggers.get("a"), "text": "新的"})
    assert repo.triggers.get("a")["text"] == "新的"                 # 同一个 id 再存是覆盖


def test_触发的处置单独存取(repo):
    repo.triggers.save(trigger("a"))
    repo.triggers.save_disposition("a", {"fanout": ["t1"], "at": stamp(1)})
    assert repo.triggers.disposition("a") == {"fanout": ["t1"], "at": stamp(1)}
    got = repo.triggers.disposition("a")
    got["fanout"].append("t2")
    assert repo.triggers.disposition("a")["fanout"] == ["t1"]
    repo.triggers.save_disposition("a", {"fanout": [], "filtered": {"code": "needs_approval"}, "at": stamp(2)})
    assert repo.triggers.disposition("a")["filtered"] == {"code": "needs_approval"}
    assert repo.triggers.get("a")["id"] == "a"                     # 处置不动触发本身


def test_线程的存取往返和工作集(repo):
    assert repo.threads.get("git:1") is None
    evaluating = {"id": "e", "threadKey": "git:2", "triggerIds": ["b"], "strength": "explicit", "actor": {"source": "git", "id": "alice"},
                  "basis": {"via": "followup"}, "policyVersion": "", "startedAt": "", "deadline": ""}
    repo.threads.save(thread("1", pending=["a"], lastAt=stamp(1)))
    repo.threads.save(thread("2", subscriptions=["running"], evaluating=evaluating, lastAt=stamp(2)))
    repo.threads.save(thread("3", lastAt=stamp(3)))
    repo.threads.save(thread("oc:om", "chat", subscriptions=["done"], lastAt=stamp(4)))
    assert repo.threads.get("git:2") == thread("2", subscriptions=["running"], evaluating=evaluating, lastAt=stamp(2))
    got = repo.threads.get("git:1")
    got["pending"].append("z")
    assert repo.threads.get("git:1")["pending"] == ["a"]
    assert sorted(ids(repo.threads.list())) == ["chat:oc:om", "git:1", "git:2", "git:3"]
    assert ids(repo.threads.active()) == ["git:1", "git:2"]        # 有排队的触发或有评估在进行
    repo.threads.save(thread("1", lastAt=stamp(1)))                # 队列空了就离开工作集
    repo.threads.save(thread("3", pending=["c"], lastAt=stamp(3)))
    assert ids(repo.threads.active()) == ["git:2", "git:3"]


def test_工单的存取往返_读出来的是副本(repo):
    assert repo.tickets.get("t") is None
    saved = ticket("t", states=("running",), no=7, rules=["collect"], inputs=[{"id": "i1", "kind": "text", "text": "x", "at": stamp(1)}])
    repo.tickets.save(saved)
    assert repo.tickets.get("t") == saved
    got = repo.tickets.get("t")
    got["assignments"][0]["state"] = "ended"
    got["title"] = "被改了"
    assert repo.tickets.get("t") == saved


def test_工单的open和active工作集_跟着每次保存变(repo):
    repo.tickets.save(ticket("running", states=("running",), n=1))
    repo.tickets.save(ticket("queued", states=("ended", "pending"), n=2))       # 重试 / 恢复：结束的派工后面跟一个待启动的
    repo.tickets.save(ticket("waking", ended=True, states=("ended",), undelivered=("input",), n=3))   # 已结束、人的消息在等它
    repo.tickets.save(ticket("done", ended=True, states=("ended",), n=4))
    repo.tickets.save(ticket("idle", states=("ended",), n=5))                   # 没结束，但没有在跑的派工也没有待送的输入
    repo.tickets.save(ticket("fresh", n=6))                                     # 还没有派工
    assert ids(repo.tickets.open()) == ["running", "queued", "idle", "fresh"]
    assert ids(repo.tickets.active()) == ["running", "queued", "waking"]
    repo.tickets.save(ticket("running", ended=True, states=("ended",), n=1))
    repo.tickets.save(ticket("idle", states=("ended",), undelivered=("i",), n=5))
    repo.tickets.save(ticket("waking", ended=True, states=("ended",), n=3))
    assert ids(repo.tickets.open()) == ["queued", "idle", "fresh"]
    assert ids(repo.tickets.active()) == ["queued", "idle"]


def test_工单列表新的在前_再存一次不改变位置(repo):
    for n, name in enumerate(("first", "second", "third"), start=1):
        repo.tickets.save(ticket(name, ended=name == "second", states=("running",) if name != "second" else ("ended",), n=n))
    assert ids(repo.tickets.list()) == ["third", "second", "first"]
    repo.tickets.save({**repo.tickets.get("first"), "title": "again", "updatedAt": stamp(30)})
    assert ids(repo.tickets.list()) == ["third", "second", "first"]
    assert repo.tickets.list()[-1]["title"] == "again"
    assert ids(repo.tickets.open()) == ["first", "third"]                       # 工作集是先接的在前


def test_决定的存取往返_按线程取_时间早的在前(repo):
    assert repo.decisions.get("d1") is None
    repo.decisions.save(decision("d1", at=stamp(1)))
    repo.decisions.save(decision("other", thread_key="git:2", at=stamp(2)))
    repo.decisions.save(decision("d2", at=stamp(3), kind="unavailable"))
    assert repo.decisions.get("d2") == decision("d2", at=stamp(3), kind="unavailable")
    assert ids(repo.decisions.by_thread("git:1")) == ["d1", "d2"]
    assert ids(repo.decisions.by_thread("git:2")) == ["other"] and repo.decisions.by_thread("git:9") == []
    repo.decisions.save({**repo.decisions.get("d1"), "dismissed": True})        # 再存是覆盖，位置不变
    assert ids(repo.decisions.by_thread("git:1")) == ["d1", "d2"] and repo.decisions.get("d1")["dismissed"] is True
    got = repo.decisions.get("d2")
    got["reason"].append({"code": "x"})
    assert repo.decisions.get("d2")["reason"] == []


def test_事务给出work的返回值_出错时整体回滚并把异常抛出来(repo):
    assert repo.atomic(lambda: 42) == 42

    def work():
        repo.tickets.save(ticket("t"))
        repo.threads.save(thread("1", pending=["a"]))
        repo.triggers.save(trigger("a"))
        repo.triggers.save_disposition("a", {"fanout": [], "at": ""})
        repo.decisions.save(decision("d"))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        repo.atomic(work)
    assert repo.tickets.get("t") is None and repo.threads.get("git:1") is None and repo.triggers.get("a") is None
    assert repo.triggers.disposition("a") is None and repo.decisions.get("d") is None
    assert repo.tickets.list() == [] and repo.threads.active() == []


def test_嵌套事务_内层回滚只撤销内层的_外层已做的和之后做的都留下(repo):
    def outer():
        repo.tickets.save(ticket("before", n=1))

        def inner():
            repo.tickets.save(ticket("inner", n=2))
            repo.tickets.save({**repo.tickets.get("before"), "title": "内层改过"})
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            repo.atomic(inner)
        assert repo.tickets.get("inner") is None and repo.tickets.get("before")["title"] == "before"   # 外层事务里立刻看得到
        repo.tickets.save(ticket("after", n=3))
        return "done"

    assert repo.atomic(outer) == "done"
    assert ids(repo.tickets.list()) == ["after", "before"] and repo.tickets.get("before")["title"] == "before"


def test_嵌套事务_内层提交了_外层回滚时全部撤销(repo):
    repo.tickets.save(ticket("kept", n=1))

    def outer():
        repo.tickets.save(ticket("outer", n=2))
        repo.atomic(lambda: repo.tickets.save(ticket("inner", n=3)))
        repo.atomic(lambda: repo.atomic(lambda: repo.threads.save(thread("deep", pending=["a"]))))
        repo.tickets.save({**repo.tickets.get("kept"), "title": "改过"})
        assert repo.tickets.get("inner") is not None
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        repo.atomic(outer)
    assert ids(repo.tickets.list()) == ["kept"] and repo.tickets.get("kept")["title"] == "kept"
    assert repo.threads.get("git:deep") is None
    repo.atomic(lambda: repo.tickets.save(ticket("next", n=4)))    # 回滚之后还能正常开新事务
    assert ids(repo.tickets.list()) == ["next", "kept"]


def test_嵌套事务都成功时全部提交(repo):
    def outer():
        repo.tickets.save(ticket("outer", n=1))
        return repo.atomic(lambda: repo.tickets.save(ticket("inner", n=2)) or "inner-result")

    assert repo.atomic(outer) == "inner-result"
    assert ids(repo.tickets.list()) == ["inner", "outer"]


def listing(repo) -> dict:
    """按时间和键的顺序存进去的一组记录，在两种仓库里列出来的顺序。"""
    repo.tickets.save(ticket("first", states=("running",), n=1))
    repo.tickets.save(ticket("second", ended=True, states=("ended",), n=2))
    repo.tickets.save(ticket("third", states=("pending",), n=3))
    repo.tickets.save({**repo.tickets.get("first"), "title": "again"})
    repo.threads.save(thread("1", pending=["a"], lastAt=stamp(3)))
    repo.threads.save(thread("2", lastAt=stamp(2)))
    repo.threads.save(thread("3", pending=["b"], lastAt=stamp(1)))
    repo.decisions.save(decision("d1", at=stamp(1)))
    repo.decisions.save(decision("d2", at=stamp(2)))

    def work():
        repo.tickets.save(ticket("outer", n=4))
        with pytest.raises(RuntimeError):
            repo.atomic(lambda: (repo.tickets.save(ticket("inner", n=5)), (_ for _ in ()).throw(RuntimeError("boom"))))

    repo.atomic(work)
    return {"tickets": ids(repo.tickets.list()), "open": ids(repo.tickets.open()), "active": ids(repo.tickets.active()),
            "threads": ids(repo.threads.list()), "active_threads": ids(repo.threads.active()), "decisions": ids(repo.decisions.by_thread("git:1"))}


def test_两种仓库列出来的顺序一致(store):
    memory = listing(MemoryRepositories())
    assert memory == {"tickets": ["outer", "third", "second", "first"], "open": ["first", "third", "outer"], "active": ["first", "third"],
                      "threads": ["git:1", "git:2", "git:3"], "active_threads": ["git:1", "git:3"], "decisions": ["d1", "d2"]}
    assert listing(store) == memory


def test_创建时间相同的工单_再存一次后两种仓库的顺序仍一致(store):
    def run(repo):
        for name in ("first", "second", "third"):
            repo.tickets.save(ticket(name, states=("running",), n=1))
        repo.tickets.save({**repo.tickets.get("first"), "title": "again"})
        return ids(repo.tickets.list())

    memory = run(MemoryRepositories())
    assert memory == ["third", "second", "first"] and run(store) == memory


def test_活跃线程的顺序两种仓库一致(store):
    def run(repo):
        repo.threads.save(thread("b", pending=["1"], lastAt=stamp(1)))
        repo.threads.save(thread("a", pending=["2"], lastAt=stamp(2)))
        return ids(repo.threads.active())

    assert run(store) == run(MemoryRepositories())


def test_线程列表的顺序两种仓库一致(store):
    def run(repo):
        repo.threads.save(thread("1", lastAt=stamp(1)))
        repo.threads.save(thread("2", lastAt=stamp(2)))
        return ids(repo.threads.list())

    assert run(store) == run(MemoryRepositories())


def test_时间不单调时按线程取决定的顺序两种仓库一致(store):
    def run(repo):
        repo.decisions.save(decision("d2", at=stamp(2)))
        repo.decisions.save(decision("d1", at=stamp(1)))
        return ids(repo.decisions.by_thread("git:1"))

    memory = run(MemoryRepositories())
    assert memory == ["d2", "d1"] and run(store) == memory


# ================================================================ Store 独有


def test_after_commit只在最外层提交后执行_回滚的那一层登记的被丢弃(store):
    ran: list[str] = []

    def outer():
        store.after_commit(lambda: ran.append("outer"))
        store.atomic(lambda: store.after_commit(lambda: ran.append("inner")))

        def failing():
            store.after_commit(lambda: ran.append("rolled back"))
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            store.atomic(failing)
        store.after_commit(lambda: ran.append("outer again"))
        assert ran == []                                           # 最外层提交之前什么都不跑

    store.atomic(outer)
    assert ran == ["outer", "inner", "outer again"]


def test_after_commit_最外层回滚则全部丢弃_不在事务里时立刻执行(store):
    ran: list[str] = []

    def failing():
        store.after_commit(lambda: ran.append("failed"))
        store.atomic(lambda: store.after_commit(lambda: ran.append("inner of failed")))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.atomic(failing)
    assert ran == []
    store.after_commit(lambda: ran.append("outside"))
    assert ran == ["outside"]
    store.atomic(lambda: store.after_commit(lambda: ran.append("next")))     # 上一次回滚丢掉的不会混进下一次
    assert ran == ["outside", "next"]


def test_after_commit的动作执行时事务已经提交_动作里可以再开事务(store):
    seen = []

    def action():
        seen.append(store.get("a"))
        store.atomic(lambda: store.set("b", 2))

    def work():
        store.set("a", 1)
        store.after_commit(action)

    store.atomic(work)
    assert seen == [1] and store.get("b") == 2


def test_提交后的动作出错不影响其余动作和已提交的数据(store):
    ran: list[str] = []

    def work():
        store.set("a", "m")

        def bad():
            raise RuntimeError("cancel failed")

        store.after_commit(bad)
        store.after_commit(lambda: ran.append("next"))
        return "done"

    try:
        value = store.atomic(work)
    except RuntimeError:
        value = None
    assert store.get("a") == "m"                                   # 已提交的保住了（这一点现在是对的）
    assert ran == ["next"] and value == "done"


def test_回滚失败的事务里键值也撤销(store):
    def work():
        store.set("a", 1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.atomic(work)
    assert store.get("a") is None


def test_数据落盘_重新打开还在_比程序新的库拒绝打开(tmp_path):
    path = tmp_path / "sub" / "db.sqlite"
    first = Store(path)
    first.tickets.save(ticket("t", states=("running",), no=3))
    first.set("k", {"v": 1})
    first.close()
    again = Store(path)
    assert again.tickets.get("t")["no"] == 3 and ids(again.tickets.open()) == ["t"] and again.get("k") == {"v": 1}
    assert again.get("schema") == SCHEMA_VERSION
    again.set("schema", SCHEMA_VERSION + 1)
    again.close()
    with pytest.raises(RuntimeError, match="schema"):
        Store(path)


def test_键值的存取_默认值_删除_按前缀列出(store):
    assert store.get("missing") is None and store.get("missing", {"d": 1}) == {"d": 1}
    store.set("agent:claude", {"state": "open"})
    store.set("agent:codex", {"state": "closed"})
    store.set("schedule:cron.x", [1, 2])
    store.set("zero", 0)
    assert store.get("agent:claude") == {"state": "open"} and store.get("zero", 5) == 0      # 存了 0 不当成没有
    assert store.keys("agent:") == ["agent:claude", "agent:codex"]
    store.set("agent:claude", {"state": "closed"})
    assert store.get("agent:claude") == {"state": "closed"}
    store.delete("agent:claude")
    store.delete("never-set")
    assert store.keys("agent:") == ["agent:codex"] and store.get("agent:claude") is None


def test_工单编号从1递增_重新打开接着编_回滚的不占号(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    assert [store.next_no(), store.next_no(), store.next_no()] == [1, 2, 3]

    def work():
        assert store.next_no() == 4
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.atomic(work)
    assert store.next_no() == 4
    store.close()
    again = Store(path)
    assert again.next_no() == 5
    again.close()


def test_按编号找工单(store):
    store.tickets.save(ticket("a", no=1, n=1))
    store.tickets.save(ticket("b", no=12, n=2))
    store.tickets.save(ticket("no-number", n=3))
    assert store.tickets.by_no(12)["id"] == "b" and store.tickets.by_no(1)["id"] == "a"
    assert store.tickets.by_no(2) is None
    store.tickets.save({**store.tickets.get("b"), "title": "改过"})
    assert store.tickets.by_no(12)["title"] == "改过"


def test_计数器按天按名字累加(store):
    assert store.counter("popto_credits", "2026-11-11") == 0
    store.add_counter("popto_credits", 24, "2026-11-11")
    store.add_counter("popto_credits", 12.5, "2026-11-11")
    store.add_counter("popto_credits", 100, "2026-11-12")
    store.add_counter("tikhub_requests", 3, "2026-11-11")
    assert store.counter("popto_credits", "2026-11-11") == 36.5
    assert store.counter("popto_credits", "2026-11-12") == 100
    assert store.counter("tikhub_requests", "2026-11-11") == 3 and store.counter("tikhub_requests", "2026-11-12") == 0

    def work():
        store.add_counter("popto_credits", 1000, "2026-11-11")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.atomic(work)
    assert store.counter("popto_credits", "2026-11-11") == 36.5


def test_工单的最近列表_按更新时间倒序_可按是否结束和起始时间筛(store):
    store.tickets.save(ticket("old", ended=True, n=1))
    store.tickets.save(ticket("mid", n=2))
    store.tickets.save(ticket("new", ended=True, n=3))
    store.tickets.save({**ticket("touched", n=0), "updatedAt": stamp(9)})
    assert ids(store.tickets.recent()) == ["touched", "new", "mid", "old"]
    assert ids(store.tickets.recent(2)) == ["touched", "new"]
    assert ids(store.tickets.recent(open_=False)) == ["new", "old"] and ids(store.tickets.recent(open_=True)) == ["touched", "mid"]
    assert ids(store.tickets.recent(since=stamp(3))) == ["touched", "new"]


def test_按规则找工单_可限定在某个线程上(store):
    store.tickets.save(ticket("a", rules=["collect"], n=1))
    store.tickets.save(ticket("b", rules=["collect", "other"], ended=True, n=2))
    store.tickets.save(ticket("c", rules=["brief"], n=3))
    store.tickets.save(ticket("explicit", n=4))
    store.threads.save(thread("x", "pipeline", subscriptions=["b", "c"]))
    assert ids(store.tickets_by_rule("collect")) == ["a", "b"] and ids(store.tickets_by_rule("brief")) == ["c"]
    assert store.tickets_by_rule("nothing") == []
    assert ids(store.tickets_by_rule("collect", "pipeline:x")) == ["b"]
    assert store.tickets_by_rule("collect", "pipeline:unknown") == []


def test_决定的最近列表_可按种类和起始时间筛(store):
    store.decisions.save(decision("d1", at=stamp(1), kind="ignore"))
    store.decisions.save(decision("d2", at=stamp(2), kind="unavailable", thread_key="git:2"))
    store.decisions.save(decision("d3", at=stamp(3), kind="ignore"))
    assert ids(store.decisions.recent()) == ["d3", "d2", "d1"]
    assert ids(store.decisions.recent(kind="ignore")) == ["d3", "d1"] and ids(store.decisions.recent(1, kind="ignore")) == ["d3"]
    assert ids(store.decisions.recent(since=stamp(2))) == ["d3", "d2"]
    assert ids(store.decisions.recent(kind="unavailable", since=stamp(3))) == []


def test_按线程找触发_新的在前_一条触发可以在多个线程上(store):
    store.triggers.save(trigger("a", thread="oc_1:om_%_x", at=stamp(2)))
    store.triggers.save(trigger("b", at=stamp(1), threads=[{"source": "chat", "id": "oc_1:om_%_x"}, {"source": "git", "id": "3"}]))
    store.triggers.save(trigger("c", thread="oc_1:om_y_x", at=stamp(3)))
    assert ids(store.thread_triggers("chat:oc_1:om_%_x")) == ["a", "b"]
    assert ids(store.thread_triggers("git:3")) == ["b"]
    assert ids(store.thread_triggers("chat:oc_1:om_%_x", limit=1)) == ["a"]
    assert store.thread_triggers("chat:nothing") == []


def test_最近的触发和各来源的数量(store):
    store.triggers.save(trigger("old", at=stamp(1)))
    store.triggers.save(trigger("kept", at=stamp(5)))
    store.triggers.save(trigger("sys", source="pipeline", at=stamp(6), human=False))
    assert ids(store.recent_triggers()) == ["sys", "kept", "old"]
    assert ids(store.recent_triggers(since=stamp(5))) == ["sys", "kept"]
    assert ids(store.recent_triggers(source="chat")) == ["kept", "old"] and ids(store.recent_triggers(1)) == ["sys"]
    assert store.trigger_stats(stamp(2)) == {"chat": 1, "pipeline": 1} and store.trigger_stats(stamp(1)) == {"chat": 2, "pipeline": 1}


def test_运行记录的存取_按工单和起始时间筛_新的在前(store):
    def run(id_: str, ticket_id: str, started: str, **extra) -> dict:
        return {"id": id_, "ticket": ticket_id, "assignment": "a1", "role": "write", "agent": "claude", "is_fallback": 0, "started_at": started,
                "finished_at": started, "status": "ok", **extra}

    store.save_run(run("r1", "t1", "2026-11-11T08:00:00+08:00", session_id="s1", cost_usd=0.12, duration_s=3.5, dir="/tmp/x"))
    store.save_run(run("r2", "t2", "2026-11-11T09:00:00+08:00", status="timeout", error_kind="timeout", error_detail="超时", is_fallback=1))
    store.save_run(run("r3", "t1", "2026-11-11T10:00:00+08:00"))
    assert [r["id"] for r in store.runs()] == ["r3", "r2", "r1"]
    assert [r["id"] for r in store.runs("t1")] == ["r3", "r1"] and [r["id"] for r in store.runs(limit=1)] == ["r3"]
    assert [r["id"] for r in store.runs(since="2026-11-11T09:00:00+08:00")] == ["r3", "r2"]
    first = store.runs("t1")[-1]
    assert first == {"id": "r1", "ticket": "t1", "assignment": "a1", "role": "write", "agent": "claude", "is_fallback": 0,
                     "started_at": "2026-11-11T08:00:00+08:00", "finished_at": "2026-11-11T08:00:00+08:00", "status": "ok", "error_kind": None,
                     "error_detail": None, "session_id": "s1", "cost_usd": 0.12, "duration_s": 3.5, "dir": "/tmp/x"}
    store.save_run(run("r1", "t1", "2026-11-11T08:00:00+08:00", status="error"))       # 同一个 id 再存是覆盖
    assert store.runs("t1")[-1]["status"] == "error" and len(store.runs()) == 3


# ---------------------------------------------------------------- 回执


CHAT = {"source": "chat", "thread": "t1", "address": {"chat": "c1"}}


def message(id_: str, *, ticket_id: str | None = "T1", channel: dict = CHAT, kind: str = "reply", text: str = "正文") -> dict:
    out = {"id": id_, "channel": channel, "text": text, "kind": kind}
    if ticket_id:
        out["ticketId"] = ticket_id
    return out


@pytest.fixture
def clocked(store, monkeypatch):
    """回执的时间戳每次往后 1 秒：先排队的一定更早。"""
    n = [0]

    def tick() -> str:
        n[0] += 1
        return stamp(n[0])

    monkeypatch.setattr("superops.app.store.now_iso", tick)
    return store


def test_回执_id排过队就不再排_结清和撤回(clocked):
    r = clocked.receipts
    assert r.get("m1") is None
    assert r.add(message("m1")) is True and r.add(message("m1", text="第二次")) is False
    assert r.get("m1") == {**message("m1"), "status": "pending", "at": stamp(1), "settledAt": None, "reason": None}
    r.settle("m1", "sent")
    assert (r.get("m1")["status"], r.get("m1")["settledAt"], r.get("m1")["reason"]) == ("sent", stamp(2), None)
    assert r.withdraw("m1") is False and r.get("m1")["status"] == "sent"               # 发出去的撤不回
    r.add(message("m2"))
    assert r.withdraw("m2") is True
    assert (r.get("m2")["status"], r.get("m2")["reason"]) == ("dropped", "withdrawn")
    assert r.withdraw("m2") is False and r.withdraw("unknown") is False
    assert r.add(message("m2")) is False                                               # 撤回过的也不再排
    r.add(message("m3"))
    r.settle("m3", "dropped", "channelGone")
    assert (r.get("m3")["status"], r.get("m3")["reason"]) == ("dropped", "channelGone")


def test_回执列表_新的在前_可按状态_种类_起始时间筛(clocked):
    r = clocked.receipts
    r.add(message("a", kind="acknowledgement"))
    r.add(message("b", kind="attention"))
    r.add(message("c", kind="attention"))
    r.settle("a", "sent")
    r.settle("b", "sent")
    assert ids(r.list()) == ["c", "b", "a"] and ids(r.list(limit=2)) == ["c", "b"]
    assert ids(r.list(status="sent")) == ["b", "a"] and ids(r.list(status="pending")) == ["c"]
    assert ids(r.list(kind="attention")) == ["c", "b"] and ids(r.list(status="sent", kind="attention")) == ["b"]
    assert ids(r.list(since=stamp(2))) == ["c", "b"]
    assert r.list()[0]["status"] == "pending" and r.list()[0]["channel"] == CHAT


def test_回执_sent_today只数某个时间之后发出的那一种(clocked):
    r = clocked.receipts
    for id_, kind in (("card1", "attention"), ("card2", "attention"), ("reply", "reply"), ("pending", "attention"), ("dropped", "attention")):
        r.add(message(id_, kind=kind))                                                  # at = 1..5 秒
    r.settle("card1", "sent")                                                           # 6
    r.settle("reply", "sent")                                                           # 7
    r.settle("card2", "sent")                                                           # 8
    r.settle("dropped", "dropped", "channelGone")                                       # 9
    assert r.sent_today("attention", stamp(0)) == 2 and r.sent_today("reply", stamp(0)) == 1
    assert r.sent_today("attention", stamp(7)) == 1 and r.sent_today("attention", stamp(9)) == 0


def test_回执_behind_同一工单发往同一会话的更早的消息还没发出去(clocked):
    r = clocked.receipts
    other = {**CHAT, "thread": "t2"}
    r.add(message("first"))
    r.add(message("second"))
    r.add(message("other-ticket", ticket_id="T2"))
    r.add(message("no-ticket", ticket_id=None))
    r.add(message("other-channel", channel=other))
    behind = lambda id_: r.behind(r.get(id_))
    assert behind("first") is False                                # 它自己是最早的
    assert behind("second") is True
    assert behind("other-ticket") is False and behind("no-ticket") is False and behind("other-channel") is False
    r.settle("first", "sent")
    assert behind("second") is False                               # 前一条发出去了
    r.add(message("third"))
    assert behind("third") is True                                 # 还有 second 在前面
    r.withdraw("second")
    assert behind("third") is False                                # 撤回 / 丢弃的不再挡着


def test_回执_behind_前一条连续失败3次之后不再挡着后面的(clocked):
    r = clocked.receipts
    r.add(message("first"))
    r.add(message("second"))
    second = r.get("second")
    clocked.run("INSERT INTO effect(kind, key, value, seq, due, since, attempts) VALUES ('reply', 'first', '{}', 1, 0, '', 2)")
    assert r.behind(second) is True
    clocked.run("UPDATE effect SET attempts = 3 WHERE key = 'first'")
    assert r.behind(second) is False
    clocked.run("UPDATE effect SET kind = 'launch' WHERE key = 'first'")               # 别的种类的副作用不算它的失败次数
    assert r.behind(second) is True


# ---------------------------------------------------------------- 数据保留


def seed_retention(store) -> None:
    store.triggers.save(trigger("old", at=stamp(1), text="旧的正文", meta={"type": "x.y", "origin": "cron", "subject": "s", "data": {"phone": "138"}}))
    store.triggers.save(trigger("old-data-only", at=stamp(2), text="", meta={"type": "x.y", "data": {"k": "v"}}))
    store.triggers.save(trigger("old-empty", at=stamp(3), text="", meta={"type": "x.y"}))
    store.triggers.save(trigger("new", at=stamp(30), text="新的正文", meta={"data": {"k": "v"}}))
    inputs = [{"id": "i1", "kind": "text", "text": "人写的话", "at": stamp(1)},
              {"id": "i2", "kind": "trigger", "triggerId": "old", "note": "路由备注", "at": stamp(1)},
              {"id": "i3", "kind": "answer", "option": "approve", "by": "u1", "at": stamp(1)}]
    report = {"state": "ready", "summary": "做完了，结论是……", "at": stamp(2)}
    full = lambda id_, n, ended: {**ticket(id_, ended=ended, states=("ended",), n=n, no=n), "title": f"标题 {id_}", "inputs": [dict(i) for i in inputs],
                                  "assignments": [{"id": f"{id_}-0", "state": "ended", "createdAt": stamp(n), "report": dict(report)},
                                                  {"id": f"{id_}-1", "state": "ended", "createdAt": stamp(n)}]}
    store.tickets.save(full("ended-old", 1, True))
    store.tickets.save(full("open-old", 2, False))
    store.tickets.save(full("ended-new", 30, True))
    store.run("INSERT INTO outbox(id, ticket, kind, status, at, settled_at, reason, value) VALUES (?,?,?,?,?,?,?,?)",
              ("sent-old", "T1", "reply", "sent", stamp(1), stamp(2), None, '{"id":"sent-old","channel":{"source":"chat"},"text":"发出去的","kind":"reply","ticketId":"T1"}'))
    store.run("INSERT INTO outbox(id, ticket, kind, status, at, settled_at, reason, value) VALUES (?,?,?,?,?,?,?,?)",
              ("dropped-old", None, "notice", "dropped", stamp(2), stamp(3), "channelGone", '{"id":"dropped-old","channel":{"source":"chat"},"text":"丢弃的","kind":"notice"}'))
    store.run("INSERT INTO outbox(id, ticket, kind, status, at, value) VALUES (?,?,?,?,?,?)",
              ("pending-old", None, "notice", "pending", stamp(3), '{"id":"pending-old","channel":{"source":"chat"},"text":"还没发的","kind":"notice"}'))
    store.run("INSERT INTO outbox(id, ticket, kind, status, at, settled_at, value) VALUES (?,?,?,?,?,?,?)",
              ("sent-new", None, "reply", "sent", stamp(30), stamp(31), '{"id":"sent-new","channel":{"source":"chat"},"text":"新发的","kind":"reply"}'))


def snapshot(store) -> dict:
    return {table: store.all(f"SELECT * FROM {table} ORDER BY 1") for table in ("trigger", "ticket", "outbox", "disposition", "thread", "decision")}


def test_blank_text的dry_run只数数_不动数据(store):
    seed_retention(store)
    before = snapshot(store)
    assert store.blank_text(stamp(10), dry_run=True) == {"triggers": 2, "tickets": 1, "messages": 2}
    assert snapshot(store) == before


def test_blank_text只清正文_保留元数据_不动期限之后的和没结束的(store):
    seed_retention(store)
    assert store.blank_text(stamp(10)) == {"triggers": 2, "tickets": 1, "messages": 2}
    old = store.triggers.get("old")
    assert old["text"] == "" and old["meta"] == {"type": "x.y", "origin": "cron", "subject": "s"}        # 类型、来源、对象还在
    assert (old["id"], old["at"], old["actor"], old["threads"]) == ("old", stamp(1), {"source": "chat", "id": "u"}, [{"source": "chat", "id": "x"}])
    assert store.triggers.get("old-data-only")["meta"] == {"type": "x.y"}
    assert store.triggers.get("new")["text"] == "新的正文" and store.triggers.get("new")["meta"] == {"data": {"k": "v"}}
    assert ids(store.thread_triggers("chat:x")) == ["new", "old-empty", "old-data-only", "old"]         # 线程上的记录还找得到
    blanked = store.tickets.get("ended-old")
    assert [i.get("text") for i in blanked["inputs"]] == ["", None, None] and blanked["inputs"][1]["note"] == ""
    assert [(i["id"], i["kind"], i["at"]) for i in blanked["inputs"]] == [("i1", "text", stamp(1)), ("i2", "trigger", stamp(1)), ("i3", "answer", stamp(1))]
    assert blanked["inputs"][2]["option"] == "approve" and blanked["inputs"][1]["triggerId"] == "old"
    assert blanked["assignments"][0]["report"] == {"state": "ready", "summary": "", "at": stamp(2)}
    assert (blanked["title"], blanked["no"], blanked["ended"]["outcome"], blanked["blanked"]) == ("标题 ended-old", 1, "closed", True)
    assert store.tickets.by_no(1)["id"] == "ended-old" and ids(store.tickets.recent(open_=False)) == ["ended-new", "ended-old"]
    for untouched in ("open-old", "ended-new"):
        kept = store.tickets.get(untouched)
        assert kept["inputs"][0]["text"] == "人写的话" and kept["assignments"][0]["report"]["summary"] == "做完了，结论是……" and "blanked" not in kept
    receipts = store.receipts
    assert receipts.get("sent-old")["text"] == "" and receipts.get("dropped-old")["text"] == ""
    assert (receipts.get("sent-old")["status"], receipts.get("sent-old")["kind"], receipts.get("sent-old")["ticketId"]) == ("sent", "reply", "T1")
    assert receipts.get("dropped-old")["reason"] == "channelGone"
    assert receipts.get("pending-old")["text"] == "还没发的" and receipts.get("sent-new")["text"] == "新发的"   # 没发的不清
    # 再跑一次：清过的不重复数
    assert store.blank_text(stamp(10)) == {"triggers": 0, "tickets": 0, "messages": 0}
    assert store.blank_text(stamp(10), dry_run=True) == {"triggers": 0, "tickets": 0, "messages": 0}


def test_blank_text的期限往后挪时_新进入期限的才被清(store):
    seed_retention(store)
    store.blank_text(stamp(10))
    assert store.blank_text(stamp(40)) == {"triggers": 1, "tickets": 1, "messages": 1}
    assert store.triggers.get("new")["text"] == "" and store.tickets.get("ended-new")["blanked"] is True
    assert store.tickets.get("open-old")["inputs"][0]["text"] == "人写的话"            # 没结束的工单一直不清
    assert store.receipts.get("pending-old")["text"] == "还没发的"
