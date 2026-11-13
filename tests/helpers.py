"""领域测试共用：假的策略、研判方、执行器、发件箱，和一个装好的引擎。"""
from __future__ import annotations

from superops.core import Engine, EngineOptions, MemoryRepositories, shift


class FakePolicy:
    def __init__(self):
        self.version = "v1"
        self.paused = False
        self.eval_timeout_ms = 120_000
        self.allowed = {"alice"}
        self.mode = "auto"
        self.rules = {"r1"}
        self.revoked = False

    def gate(self, trigger, thread, tickets):
        if not trigger["human"]:
            if self.mode == "off":
                return {"admissible": False, "reason": "admission_ignored"}
            return {"admissible": True, "strength": "inferred", "basis": {"via": "admission", "cell": "unlistedUnaddressed",
                                                                       "source": trigger["source"], "observation": {"mode": self.mode}}}
        allowed = trigger["actor"]["id"] in self.allowed
        participating = any(i not in (thread.get("context") or []) for i in [*thread["subscriptions"], *[t["id"] for t in tickets]])
        if allowed and trigger.get("addressed"):
            return {"admissible": True, "strength": "explicit", "basis": {"via": "addressed", "addressed": trigger["addressed"]}}
        if participating and (allowed or self.mode != "off"):
            return {"admissible": True, "strength": "explicit" if allowed else "inferred", "basis": {"via": "followup"}}
        return {"admissible": False, "reason": "not_a_member" if not allowed else "no_mention"}

    def revalidate(self, mandate):
        return not self.revoked

    def admits_inferred(self, rules):
        return self.mode == "auto" and bool(rules) and all(r in self.rules for r in rules)

    def may_answer(self, trigger, ticket):
        return trigger["actor"]["id"] in self.allowed


class Recorder:
    """同时充当研判方、执行器和发件箱，把被调用的情况都记下来。"""

    def __init__(self):
        self.requests, self.released = [], []
        self.started, self.delivered, self.stopped = [], [], []
        self.messages: dict[str, dict] = {}
        self.events = []

    # assessor
    def request(self, candidate):
        self.requests.append(candidate)

    def release(self, eval_id):
        self.released.append(eval_id)

    # executor
    def start(self, ticket, assignment):
        self.started.append((ticket["id"], assignment["id"]))

    def deliver(self, ticket, assignment):
        self.delivered.append((ticket["id"], assignment["id"], list(ticket["undelivered"])))

    def stop(self, ticket, assignment, reason):
        self.stopped.append((ticket["id"], assignment["id"], reason))

    # outbox
    def send(self, message):
        self.messages.setdefault(message["id"], {**message, "status": "pending"})

    def status(self, message_id):
        return (self.messages.get(message_id) or {}).get("status")

    def withdraw(self, message_id):
        if self.status(message_id) == "pending":
            self.messages[message_id]["status"] = "withdrawn"

    def texts(self, kind=None):
        return [m["text"] for m in self.messages.values() if m["status"] != "withdrawn" and (kind is None or m["kind"] == kind)]


class World:
    def __init__(self):
        self.repo = MemoryRepositories()
        self.policy = FakePolicy()
        self.rec = Recorder()
        self.clock = "2026-11-11T00:00:00.000Z"
        self.n = 0
        self.engine = Engine(EngineOptions(
            repo=self.repo, policy=lambda: self.policy, scope=self, assessor=self.rec, executor=self.rec, outbox=self.rec,
            now=self.now, emit=self.rec.events.append))

    def scope(self, source, meta):
        return {"listened": False, "reason": "not_listened"} if meta.get("ignored") else {"listened": True, "observed": True, "row": "main"}

    def now(self):
        return self.clock

    def tick(self, seconds=1.0):
        self.clock = shift(self.clock, seconds)
        return self.clock

    def trigger(self, text="做一下", *, thread="t1", actor="alice", human=True, addressed="mention", source="chat", **extra):
        self.n += 1
        self.tick()
        trig = {"id": extra.pop("id", f"trg{self.n}"), "source": source, "threads": [{"source": source, "id": thread}],
                "actor": {"source": source, "id": actor}, "human": human, "at": self.clock, "text": text, "meta": {},
                "channel": {"source": source, "thread": thread, "address": {"chat": "c1"}}, **extra}
        if addressed and human:
            trig["addressed"] = addressed
        return trig

    def send(self, *args, **kwargs):
        trig = self.trigger(*args, **kwargs)
        self.engine.receive(trig)
        return trig

    def accept(self, trig, **outcome):
        """收一条触发、开评估并接单，返回工单。"""
        self.engine.evaluate_due()
        decision = self.engine.decide(trig["id"], {"kind": "accept", "title": "工单", "scope": "范围", "reason": "要做", **outcome})
        return self.repo.tickets.get(decision["ticketId"])

    def open_ticket(self, **kwargs):
        """名单内的人 @ 机器人 → 接单 → 派工开始运行。"""
        trig = self.send(**kwargs)
        ticket = self.accept(trig)
        self.engine.assignment_started(ticket["id"], ticket["assignments"][0]["id"])
        return trig, self.repo.tickets.get(ticket["id"])

    def ticket(self, ticket_id):
        return self.repo.tickets.get(ticket_id)


# ---------------------------------------------------------------------------
# 以下是移植原型 tests/core-engine.spec.ts 用的夹具：和原型 spec 里的 harness 一一对应，
# 这样两边的用例可以逐条对照。上面的 World / FakePolicy / Recorder 不受影响。
# ---------------------------------------------------------------------------
import copy  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from superops.core import UserError, running_assignment, to_iso  # noqa: E402

MEMBER = {"source": "chat", "id": "alice"}
GUEST = {"source": "chat", "id": "guest"}
# 原型 harness 的准入给名单内的人的依据是旧数据才有的 via: 'listed'；核心不解释依据，这里换成现行的写法。
EXPLICIT_BASIS = {"via": "addressed", "addressed": "mention"}
INFERRED_BASIS = {"via": "followup"}


def chat(thread_id: str) -> dict:
    return {"source": "chat", "id": thread_id}


def gh(thread_id: str) -> dict:
    return {"source": "gh", "id": thread_id}


def trig(trigger_id: str, **overrides) -> dict:
    """原型 spec 的 trigger()：名单内的 alice 在 chat:t1 上写的一条消息，at 由 id 决定。"""
    return {"id": trigger_id, "source": "chat", "threads": [chat("t1")], "actor": dict(MEMBER), "human": True,
            "at": f"2026-09-23T00:00:{trigger_id.zfill(2)}Z", "text": f"message {trigger_id}",
            "channel": {"source": "chat", "thread": "t1", "address": {"message": trigger_id}}, "meta": {}, **overrides}


class _Any:
    """对应 expect.anything()：存在且不是 None 就相等。"""

    def __eq__(self, other):
        return other is not None

    def __repr__(self):
        return "ANY"


class _AnyStr:
    """对应 expect.any(String)。"""

    def __eq__(self, other):
        return isinstance(other, str)

    def __repr__(self):
        return "ANY_STR"


ANY = _Any()
ANY_STR = _AnyStr()


def _subset(expected, actual) -> bool:
    if isinstance(expected, Partial):
        return expected == actual
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, (list, tuple)):
        return (isinstance(actual, (list, tuple)) and len(expected) == len(actual)
                and all(_subset(e, a) for e, a in zip(expected, actual)))
    return expected == actual


class Partial:
    """对应 expect.objectContaining：只比给出的键；嵌套的 dict 也按子集比，list 要求等长、逐项比。"""

    def __init__(self, spec: dict | None = None, **keys):
        self.spec = {**(spec or {}), **keys}

    def __eq__(self, other):
        return isinstance(other, dict) and _subset(self.spec, other)

    def __repr__(self):
        return f"Partial({self.spec!r})"


def like(actual, expected) -> bool:
    """对应 toMatchObject：expected 给出的键都在 actual 里且相符。"""
    return _subset(expected, actual)


@contextmanager
def refuses(code: str, **values):
    """对应 toThrow(new UserError(code, values))：看错误码，给了 values 就连同取值一起看。"""
    with pytest.raises(UserError) as info:
        yield
    assert info.value.code == code, f"期望 {code}，实际 {info.value.code}：{info.value}"
    if values:
        assert info.value.values == values


def _spec_gate(trigger: dict, thread: dict, tickets: list) -> dict:
    """原型 harness 的准入：机器的不收；alice 明确；别人只有在线程已有工单订阅时才作为推断收下。"""
    if not trigger["human"]:
        return {"admissible": False, "reason": "not human"}
    if trigger["actor"]["id"] == "alice":
        return {"admissible": True, "strength": "explicit", "basis": dict(EXPLICIT_BASIS)}
    if thread["subscriptions"]:
        return {"admissible": True, "strength": "inferred", "basis": dict(INFERRED_BASIS)}
    return {"admissible": False, "reason": "not a member"}


class _SpecPolicy:
    eval_timeout_ms = 60_000

    def __init__(self, harness, gate, admits_inferred):
        self._h = harness
        self._gate = gate or _spec_gate
        self._admits = admits_inferred or (lambda rules: len(rules) > 0)

    @property
    def version(self):
        return self._h.version

    @property
    def paused(self):
        return self._h.paused

    def gate(self, trigger, thread, tickets):
        return self._gate(trigger, thread, tickets)

    def revalidate(self, mandate):
        return self._h.version != "revoked"

    def admits_inferred(self, rules):
        return self._admits(rules)

    def may_answer(self, trigger, ticket):
        return trigger["actor"]["id"] == "alice"


class Harness:
    """原型 core-engine.spec.ts 的 harness()：内存仓库 + 记录调用的研判方 / 执行器 / 发件箱 + 可拨动的时钟。

    - requests / released：研判方收到的候选、被释放的评估；
    - starts / delivers / stops：执行器每次被调用时参数的快照（对应 vi.fn 的调用记录，clear() 即 mockClear）；
    - delivered：settle() 模拟执行器把输入交给运行中的派工，记 (工单 id, 输入 id)；
    - outbox / withdrawn：排队的消息和被撤回的消息 id；events：发出的领域事件。
    时钟从 2026-09-23T00:00:00.000Z 起，tick(秒) 往前拨。"""

    def __init__(self, *, gate=None, admits_inferred=None, paused=False, prepare_ticket=None):
        self.repo = MemoryRepositories()
        self.outbox: list[dict] = []
        self.withdrawn: list[str] = []
        self.requests: list[dict] = []
        self.released: list[str] = []
        self.starts: list[tuple] = []
        self.delivers: list[tuple] = []
        self.stops: list[tuple] = []
        self.delivered: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self._launches: list[tuple[str, str]] = []
        self._deliveries: list[str] = []
        self.version, self.paused, self.clock = "v1", paused, 0
        self.policy = _SpecPolicy(self, gate, admits_inferred)
        snap = copy.deepcopy

        def start(ticket, assignment):
            self.starts.append((snap(ticket), snap(assignment)))
            self._launches.append((ticket["id"], assignment["id"]))

        def deliver(ticket, assignment):
            self.delivers.append((snap(ticket), snap(assignment)))
            if ticket["id"] not in self._deliveries:
                self._deliveries.append(ticket["id"])

        def send(message):
            if not any(m["id"] == message["id"] for m in self.outbox):
                self.outbox.append(message)

        self.engine = Engine(EngineOptions(
            repo=self.repo, policy=lambda: self.policy,
            scope=SimpleNamespace(scope=lambda source, meta: {"listened": False, "reason": "chat not allowed"} if meta.get("blocked")
                                  else {"listened": True, "observed": False}),
            assessor=SimpleNamespace(request=self.requests.append, release=self.released.append),
            executor=SimpleNamespace(start=start, deliver=deliver,
                                     stop=lambda ticket, assignment, reason: self.stops.append((snap(ticket), snap(assignment), reason))),
            outbox=SimpleNamespace(send=send, withdraw=self.withdrawn.append,
                                   status=lambda mid: "pending" if any(m["id"] == mid for m in self.outbox) else None),
            prepare_ticket=prepare_ticket, emit=self.events.append, now=self.now))

    def now(self) -> str:
        return to_iso(datetime(2026, 9, 23, tzinfo=timezone.utc) + timedelta(seconds=self.clock))

    def tick(self, seconds: int) -> None:
        self.clock += seconds

    def settle(self, launch: bool = True) -> None:
        """模拟执行器：把排队的派工启动起来（launch=False 时不启动），再把等着的输入逐条送达。"""
        while True:
            if launch and self._launches:
                ticket_id, assignment_id = self._launches.pop(0)
                self.engine.assignment_started(ticket_id, assignment_id)
                continue
            if not self._deliveries:
                return
            ticket_id = self._deliveries.pop(0)
            ticket = self.repo.tickets.get(ticket_id)
            assignment = running_assignment(ticket)
            if ticket.get("ended") or not assignment:
                continue
            for input_id in ticket["undelivered"]:
                self.delivered.append((ticket_id, input_id))
                self.engine.inputs_delivered(ticket_id, assignment["id"], [input_id])

    def pump(self) -> None:
        self.engine.evaluate_due()
        self.settle()

    def read(self, ticket_id: str) -> None:
        """模拟 worker 的会话把已送达、还没读的触发输入取进一步。"""
        self.engine.inputs_read(ticket_id, [i["id"] for i in self.ticket(ticket_id)["inputs"]
                                            if i["kind"] == "trigger" and "readAt" in i and i["readAt"] is None])

    def decide(self, candidate: dict, outcome: dict) -> dict:
        """按候选所属的评估作决定（带 evalId）。"""
        return self.engine.decide(candidate["triggers"][0]["id"], {**outcome, "evalId": candidate["evalId"]})

    def accepted(self, trigger_id: str = "1", thread: str = "t1") -> dict:
        """收一条 alice 的消息、开评估、接单（带确认 ok）并让派工跑起来，返回接单的决定。"""
        self.engine.receive(trig(trigger_id, threads=[chat(thread)]))
        self.pump()
        decision = self.decide(self.requests[-1], {"kind": "accept", "title": "Fix", "scope": "small", "reason": "requested",
                                                   "acknowledgement": "ok"})
        self.settle()
        return decision

    # 读仓库的简写
    def ticket(self, ticket_id: str) -> dict:
        return self.repo.tickets.get(ticket_id)

    def thread(self, key_: str) -> dict:
        return self.repo.threads.get(key_)

    def pending(self, key_: str = "chat:t1") -> list[str]:
        return self.repo.threads.get(key_)["pending"]

    def decisions(self, key_: str = "chat:t1") -> list[dict]:
        return self.repo.decisions.by_thread(key_)

    def disposition(self, trigger_id: str) -> dict:
        return self.repo.triggers.disposition(trigger_id)

    def running(self, ticket_id: str) -> dict | None:
        return running_assignment(self.ticket(ticket_id))
