"""原型 tests/core-engine.spec.ts 的逐条移植：对 superops/core 这次移植是否忠实的独立检查。

每条用例上方的「spec:行号」指原型 core-engine.spec.ts 里对应的 it，方便两边对照。
夹具在 helpers.py（Harness / trig / like / Partial / refuses），和原型 spec 的 harness 一一对应。
与原型不同、按 Python 版写的地方：时间是 UTC 毫秒；错误看 UserError.code；文案是中文；
带问题的回复 kind 是 attention；工单结束时会撤回还没发出的问题。
"""
from __future__ import annotations

import pytest

from helpers import ANY, ANY_STR, EXPLICIT_BASIS, GUEST, INFERRED_BASIS, MEMBER, Harness, Partial, chat, gh, like, refuses, trig
from superops.core import UserError, followup_owners, key, ms, said, ticket_outcome, ticket_stage, waits_for_person
from superops.core.assignments import RECOVERY_NOTE
from superops.core.tickets import STATUS_REQUEST

ACCEPT = {"kind": "accept", "title": "Fix", "scope": "small", "reason": "requested"}
READY = {"state": "ready", "summary": "done"}
DROPPED = {"state": "dropped", "summary": "nothing to do"}
PROGRESS = {"state": "progress", "summary": "working"}


def ids(candidate: dict) -> list[str]:
    return [t["id"] for t in candidate["triggers"]]


def ack(ticket_id: str, trigger_id: str, text: str = "On it") -> dict:
    return {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": trigger_id, "text": text}


# ---------------------------------------------------------------- 接收（intake）

# spec:75
def test_触发只存一次_来源不收的记为过滤():
    h = Harness()
    assert h.engine.receive(trig("1")) == "stored"
    assert h.engine.receive(trig("1")) == "duplicate"
    assert h.engine.receive(trig("2", meta={"blocked": True})) == "filtered"
    assert h.disposition("2")["filtered"] == {"code": "chat not allowed"}
    assert h.pending() == ["1"]


# spec:83
def test_跨多个线程的触发记下线程关联_只在主线程上评估():
    h = Harness()
    h.engine.receive(trig("1", threads=[gh("pr-7"), gh("run-9")]))
    assert like(h.thread("gh:pr-7"), {"pending": ["1"], "correlated": ["gh:run-9"]})
    assert like(h.thread("gh:run-9"), {"pending": [], "correlated": ["gh:pr-7"]})


# ---------------------------------------------------------------- 评估（eval）

# spec:91
def test_没有可准入的触发时_在准入处忽略_不找研判方():
    h = Harness()
    h.engine.receive(trig("1", human=False))
    h.pump()
    assert h.requests == []
    assert h.decisions() == [Partial(kind="ignore", triggerIds=["1"])]
    assert h.disposition("1").get("decisionId")


# spec:98
def test_评估结束时释放研判方为它留的状态():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    h.decide(h.requests[0], {"kind": "ignore", "reason": "chatter"})
    assert h.released == [h.requests[0]["evalId"]]


# spec:104
def test_同一线程的评估串行_期间到达的触发攒到下一次():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    h.engine.receive(trig("2"))
    h.engine.receive(trig("3"))
    h.pump()
    assert len(h.requests) == 1
    assert h.pending() == ["2", "3"]
    h.decide(h.requests[0], {"kind": "ignore", "reason": "chatter"})
    h.pump()
    assert ids(h.requests[1]) == ["2", "3"]
    assert h.requests[1]["strength"] == "explicit"


# spec:115
def test_接单创建带授权的工单_订阅线程_排队确认_启动派工():
    h = Harness()
    decision = h.accepted()
    ticket = h.ticket(decision["ticketId"])
    assert like(ticket, {"title": "Fix",
                         "mandate": {"strength": "explicit", "policyVersion": "v1", "basis": EXPLICIT_BASIS, "actor": MEMBER, "scope": "small"},
                         "channels": [trig("1")["channel"]], "origin": {"triggerId": "1", "triggerIds": ["1"]}})
    assert "ended" not in ticket
    assert ticket["assignments"] == [Partial(state="running", startedAt=ANY_STR)]
    assert h.thread("chat:t1")["subscriptions"] == [ticket["id"]]
    assert h.outbox == [{"id": key("ack", "1"), "channel": trig("1")["channel"], "text": "ok", "kind": "acknowledgement",
                         "ticketId": ticket["id"]}]
    assert len(h.starts) == 1


# spec:127
def test_被后来的评估取代的评估不能再决定_后来的可以():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    first = h.requests[0]
    h.version = "v2"
    h.engine.policy_changed("v1", "v2")
    h.pump()
    second = h.requests[1]
    with refuses("evalFinished", eval=first["evalId"]):
        h.decide(first, {"kind": "ignore", "reason": "chatter"})
    assert like(h.decide(second, {"kind": "ignore", "reason": "chatter"}), {"kind": "ignore", "evalId": second["evalId"]})


# spec:136
def test_接单的决定_工单_订阅和确认要么一起记下_要么都没有():
    def no_directory(ticket, outcome):
        raise RuntimeError("没有给工单的目录")

    h = Harness(prepare_ticket=no_directory)
    h.engine.receive(trig("1"))
    h.pump()
    eval_id = h.requests[0]["evalId"]
    with pytest.raises(RuntimeError, match="没有给工单的目录"):
        h.decide(h.requests[0], {**ACCEPT, "acknowledgement": "ok"})
    assert h.decisions() == []
    assert h.repo.tickets.list() == []
    assert like(h.thread("chat:t1"), {"evaluating": {"id": eval_id}, "subscriptions": []})
    assert h.outbox == []


# spec:146
def test_研判期间策略版本变了_记为未完成():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    h.version = "v2"
    decision = h.decide(h.requests[0], {"kind": "accept", "title": "x", "scope": "x", "reason": "x"})
    assert decision["kind"] == "unavailable"
    assert h.repo.tickets.list() == []


# spec:154
def test_一步启动所有等着的评估_再跑一遍不会重复启动():
    h = Harness()
    for trigger_id, thread in (("1", "A"), ("2", "B")):
        h.engine.receive(trig(trigger_id, threads=[chat(thread)]))
    assert h.engine.evaluate_due() is None
    h.engine.evaluate_due()
    assert [c["thread"]["key"] for c in h.requests] == ["chat:A", "chat:B"]


# spec:161
def test_评估期限从候选送达算起_过期重排队两次_第三次记为未完成():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    # 候选还没送到研判方的会话，不会过期
    h.tick(600)
    h.pump()
    assert h.engine.candidate("1")["evalId"] == h.requests[0]["evalId"]

    def deliver():
        h.engine.candidate_delivered(h.requests[-1]["evalId"])

    deliver()
    h.tick(59)
    h.pump()
    assert len(h.requests) == 1
    # 过期了：不记决定，把触发重新排队等新的评估，这样来两次
    h.tick(2)
    h.pump()
    assert h.decisions() == []
    assert h.requests[0]["evalId"] in h.released
    assert [ids(r) for r in h.requests] == [["1"], ["1"]]
    deliver()
    h.tick(61)
    h.pump()
    assert len(h.requests) == 3
    # 第三次过期记为未完成
    deliver()
    h.tick(61)
    h.pump()
    assert h.decisions() == [Partial(kind="unavailable", reason=[{"code": "timeout"}])]
    thread = h.thread("chat:t1")
    assert thread["pending"] == []
    assert "evaluating" not in thread
    assert "expired" not in thread


# ---------------------------------------------------------------- 订阅扇出

# spec:187
def test_订阅线程上人后来的消息直接到进行中的工单_不进评估():
    h = Harness()
    decision = h.accepted()
    requests = len(h.requests)
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    ticket = h.ticket(decision["ticketId"])
    assert ticket["inputs"] == [Partial(kind="trigger", triggerId="2")]
    assert ticket["undelivered"] == []
    assert h.delivered == [(ticket["id"], ticket["inputs"][0]["id"])]
    assert h.disposition("2") == {"fanout": [ticket["id"]], "at": ANY_STR}
    # 没有评估，所以没有决定、也没有超时，线程的队列空着等新的请求
    assert (len(h.requests), h.pending(), len(h.decisions())) == (requests, [], 1)
    h.tick(600)
    h.engine.evaluate_due()
    assert len(h.decisions()) == 1
    # 机器在那里发的消息仍然进评估，准入不收
    h.engine.receive(trig("3", human=False))
    h.pump()
    assert like(h.decisions()[-1], {"kind": "ignore", "triggerIds": ["3"]})


# spec:206
def test_派工没跑起来之前输入先不送达_并再请执行器启动上次没启动成的派工():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    decision = h.decide(h.requests[0], ACCEPT)
    h.settle(launch=False)
    h.engine.receive(trig("2"))
    h.engine.evaluate_due()
    h.settle(launch=False)
    ticket = h.ticket(decision["ticketId"])
    assert len(ticket["undelivered"]) == 1
    assert h.delivers == []
    assert len(h.starts) == 2
    h.engine.assignment_started(ticket["id"], ticket["assignments"][0]["id"])
    h.settle()
    assert h.delivered == [(ticket["id"], ticket["undelivered"][0])]


# spec:219
def test_机器的消息不交给已结束的工单_但仍然评估():
    h = Harness()
    decision = h.accepted()
    h.engine.report(decision["ticketId"], READY)
    h.engine.receive(trig("2", human=False))
    h.pump()
    # 执行器恢复时会自己重读线程，所以机器和 CI 既不唤醒也不排到已结束的工单上
    assert like(h.ticket(decision["ticketId"]), {"ended": ANY, "undelivered": []})
    assert len(h.starts) == 1
    assert like(h.decisions()[-1], {"kind": "ignore", "triggerIds": ["2"]})


# spec:228
def test_不管跑多少遍只恢复一次_策略不再支持授权时放掉等着的消息():
    h = Harness()
    decision = h.accepted()
    h.engine.report(decision["ticketId"], READY)
    h.version = "revoked"
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    assert like(h.ticket(decision["ticketId"]), {"ended": ANY, "undelivered": []})
    h.version = "v1"
    h.pump()
    assert "ended" in h.ticket(decision["ticketId"])
    h.engine.receive(trig("3", actor=GUEST))
    h.pump()
    h.pump()
    ticket = h.ticket(decision["ticketId"])
    assert len(ticket["assignments"]) == 2
    assert len([i for i in ticket["inputs"] if i["kind"] == "resumed"]) == 1


# spec:243
def test_送达报告只认仍在执行未结束工单的那次派工():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    assignment = h.ticket(ticket_id)["assignments"][0]["id"]
    h.engine.receive(trig("2"))
    input_id = key("input", ticket_id, "trigger", "2")
    h.engine.delivery_failed(ticket_id, assignment, "Session busy")
    assert like(h.ticket(ticket_id)["delivery"], {"error": "Session busy"})
    h.engine.delivery_failed(ticket_id, "another", "other")
    h.engine.inputs_delivered(ticket_id, "another", [input_id])
    assert like(h.ticket(ticket_id), {"undelivered": [input_id], "delivery": {"error": "Session busy"}})
    h.engine.inputs_delivered(ticket_id, assignment, [input_id])
    assert h.ticket(ticket_id)["undelivered"] == []
    assert "delivery" not in h.ticket(ticket_id)
    h.engine.receive(trig("3"))
    h.engine.stop_ticket(ticket_id)
    h.engine.delivery_failed(ticket_id, assignment, "late")
    assert like(h.ticket(ticket_id), {"ended": ANY, "undelivered": []})
    assert "delivery" not in h.ticket(ticket_id)


# spec:262
def test_取消线程订阅后不再扇出_直到新的决定再订阅():
    h = Harness()
    decision = h.accepted()
    assert h.engine.unsubscribe("chat:t1") is True
    h.engine.receive(trig("2"))
    h.pump()
    assert h.ticket(decision["ticketId"])["inputs"] == []


# spec:269
def test_执行器可以放掉转给它的线程_不影响线程上别的工单_但放不掉自己的线程():
    h = Harness()
    first = h.accepted()["ticketId"]
    second = h.accepted("2", "t2")["ticketId"]
    h.engine.receive(trig("3", threads=[chat("t2")]))
    # 第二张工单的跟进，要等它的执行器交还之后才能转给第一张
    h.engine.handback(second, "3", "It is about the first fix")
    h.engine.attach(first, "3")
    assert h.thread("chat:t2")["subscriptions"] == [second, first]
    h.engine.mark_unrelated(first, "chat:t2", "about another fix")
    assert h.thread("chat:t2")["subscriptions"] == [second]
    assert [e["data"] for e in h.events if e["type"] == "thread.unsubscribed"] == [
        {"threadKey": "chat:t2", "ticketId": first, "by": "executor", "reason": "about another fix"}]
    with refuses("notFollowing"):
        h.engine.mark_unrelated(first, "chat:t2", "again")
    with refuses("sourceThread"):
        h.engine.mark_unrelated(first, "chat:t1", "its own")
    h.engine.bind_thread(first, gh("pr-9"), True)
    with refuses("followedThread"):
        h.engine.mark_unrelated(first, "gh:pr-9", "its PR")


# ---------------------------------------------------------------- 恢复与停止

def ended(h: Harness, by: str) -> str:
    decision = h.accepted()
    if by == "executor":
        h.engine.report(decision["ticketId"], READY)
    else:
        h.engine.stop_ticket(decision["ticketId"])
    return decision["ticketId"]


# spec:296
def test_已结束的工单在它线程上任何人的消息到来时恢复_授权不变_不产生决定():
    h = Harness()
    ticket_id = ended(h, "executor")
    decisions = len(h.decisions())
    # 消息一到就恢复，早于对它的任何评估
    h.engine.receive(trig("2", actor=GUEST))
    assert "ended" not in h.ticket(ticket_id)
    h.pump()
    ticket = h.ticket(ticket_id)
    assert len(ticket["assignments"]) == 2
    assert ticket["mandate"]["policyVersion"] == "v1"
    assert [i["kind"] for i in ticket["inputs"]] == ["trigger", "resumed"]
    assert len(h.starts) == 2
    assert len([d for d in h.decisions() if d["kind"] != "ignore"]) == decisions
    assert [e["data"] for e in h.events if e["type"] == "ticket.resumed"] == [{"ticketId": ticket_id, "triggerId": "2"}]


# spec:312
def test_已结束的工单上有人的消息在等时_下一轮把它恢复():
    h = Harness()
    ticket_id = ended(h, "executor")
    h.engine.receive(trig("2", actor=GUEST, threads=[chat("elsewhere")]))
    stored = h.ticket(ticket_id)
    waiting = {"id": "waiting", "kind": "trigger", "triggerId": "2", "at": stored["updatedAt"], "human": True}
    h.repo.tickets.save({**stored, "inputs": [*stored["inputs"], waiting], "undelivered": ["waiting"]})
    h.pump()
    assert any(i["kind"] == "resumed" for i in h.ticket(ticket_id)["inputs"])
    assert "ended" not in h.ticket(ticket_id)


# spec:323
def test_把消息转给已结束的工单时立即恢复它():
    h = Harness()
    ticket_id = ended(h, "executor")
    h.engine.receive(trig("2", actor=GUEST, threads=[chat("elsewhere")]))
    subject = h.engine.attach(ticket_id, "2")
    assert like(subject, {"assignments": [ANY, Partial(state="pending")]})
    assert "ended" not in subject


# spec:331
def test_被人停掉的工单保持结束_停止撤回了它的授权():
    h = Harness()
    ticket_id = ended(h, "human")
    assert len(h.stops) == 1
    h.engine.receive(trig("2"))
    h.pump()
    assert like(h.ticket(ticket_id), {"ended": Partial(by="human")})
    assert len(h.starts) == 1


# spec:339
def test_授权通不过当前策略的工单保持结束():
    h = Harness()
    ticket_id = ended(h, "executor")
    h.version = "revoked"
    h.engine.receive(trig("2"))
    h.pump()
    assert "ended" in h.ticket(ticket_id)


# spec:346
def test_只有明确的指示才能停掉进行中的工单_并记为人结束的():
    h = Harness()
    decision = h.accepted()
    # 到过工单的跟进：主 Session 对它动作时由准入判断，访客的只算推断
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    assert like(h.engine.decide("2", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "x"}),
                {"kind": "ignore", "strength": "inferred", "reason": [{"code": "stopNotExplicit"}]})
    h.engine.receive(trig("3"))
    h.pump()
    assert h.engine.decide("3", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "human said stop"})["kind"] == "stop"
    assert like(h.ticket(decision["ticketId"]), {"ended": Partial(by="human", outcome="stopped", reason="human said stop")})
    assert len(h.stops) == 1


# ---------------------------------------------------------------- 跟进（follow-ups）

# spec:360
def test_队列里由工单决定的跟进离开队列且不产生决定_被人重新送去评估的留下():
    h = Harness()
    h.accepted()
    h.engine.receive(trig("2", actor=GUEST))
    h.engine.receive(trig("3", actor=GUEST))
    # 两条都在评估队列里；第二条是人重新送去评估的
    thread = h.thread("chat:t1")
    h.repo.threads.save({**thread, "pending": ["2", "3"]})
    h.engine.reevaluate("3")
    h.pump()
    assert ids(h.requests[-1]) == ["3"]
    assert h.pending() == []
    assert [i for d in h.decisions() for i in d["triggerIds"]] == ["1"]
    assert [e["data"] for e in h.events if e["type"] == "trigger.followup"] == [{"triggerId": "2"}]


# spec:372
def test_跟进在只过准入的评估里决定_线程上进行中的评估照常继续():
    h = Harness()
    decision = h.accepted()
    h.engine.receive(trig("2", actor=GUEST))
    h.engine.reevaluate("2")
    h.pump()
    evaluating = h.thread("chat:t1")["evaluating"]
    h.engine.receive(trig("3"))
    assert like(h.engine.decide("3", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "stop it"}),
                {"kind": "stop", "triggerIds": ["3"]})
    assert h.thread("chat:t1")["evaluating"] == evaluating
    assert like(h.decide(h.requests[-1], {"kind": "ignore", "reason": "chat"}), {"kind": "ignore", "triggerIds": ["2"]})


# spec:383
def test_跟进不占评估_之后没有工单接的请求可以马上评估():
    h = Harness()
    decision = h.accepted()
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    h.engine.stop_ticket(decision["ticketId"], "not needed")
    # 跟进归工单决定期间没有占着评估，所以下一个请求立刻评估，连同停止时工单放掉的那条跟进一起
    h.engine.receive(trig("3"))
    h.pump()
    assert ids(h.requests[-1]) == ["2", "3"]


# ---------------------------------------------------------------- 交还（hand-back）

# spec:395
def test_交还的消息按准入的强度重新评估_每张工单只能交还一次_订阅保留():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2"))
    h.engine.receive(trig("3", actor=GUEST))
    h.pump()
    assert h.engine.handback(ticket_id, "2", "A new request") is True
    h.pump()
    assert like(h.requests[-1], {"strength": "explicit", "triggers": [{"id": "2"}]})
    assert h.disposition("2")["handbacks"] == [{"ticketId": ticket_id, "reason": "A new request", "at": ANY_STR}]
    h.decide(h.requests[-1], {"kind": "ignore", "reason": "chat"})
    with refuses("handedBack"):
        h.engine.handback(ticket_id, "2", "Again")
    assert h.engine.handback(ticket_id, "3", "A guest asks for more") is True
    h.pump()
    assert like(h.requests[-1], {"strength": "inferred", "triggers": [{"id": "3"}]})
    assert ticket_id in h.thread("chat:t1")["subscriptions"]
    # 只有到过这张工单的消息才能交还
    h.engine.receive(trig("4", threads=[chat("elsewhere")]))
    with refuses("notReached"):
        h.engine.handback(ticket_id, "4", "Not mine")


# spec:412
def test_不由工单决定的消息交还时不再排队_人送去评估的_机器的_只为上下文关注的线程上的():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.follow_for_context(ticket_id, chat("issue"))
    h.engine.receive(trig("2"))
    h.engine.reevaluate("2")
    h.engine.receive(trig("3", human=False))
    h.engine.receive(trig("4", threads=[chat("issue")]))
    h.pump()
    for candidate in h.requests[-2:]:
        h.decide(candidate, {"kind": "ignore", "reason": "chat"})
    # 它们各自没经过这张工单就进了评估，决定照旧有效
    for trigger_id in ("2", "3", "4"):
        assert h.engine.handback(ticket_id, trigger_id, "Not this ticket") is False
    assert (h.pending("chat:t1"), h.pending("chat:issue")) == ([], [])
    # 交还照样记下，所以主 Session 不能再把它转回来
    assert h.disposition("4")["handbacks"] == [{"ticketId": ticket_id, "reason": "Not this ticket", "at": ANY_STR}]
    with refuses("handedBackHere"):
        h.engine.attach(ticket_id, "4")


# spec:427
def test_主Session转给工单的消息归它的执行器决定_执行器交还后才进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", threads=[chat("x")]))
    h.pump()
    h.engine.attach(ticket_id, "2")
    h.settle()
    # 已经转给工单了，就不能同时又当新工作接
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.decide("2", {"kind": "accept", "title": "Y", "scope": "small", "reason": "requested"})
    assert h.engine.handback(ticket_id, "2", "Asks for another change") is True
    assert h.pending("chat:x") == ["2"]


# ---------------------------------------------------------------- 上下文线程与备注

# spec:440
def test_为上下文关注的线程上的消息扇出给进行中的工单_不唤醒已结束的_除非被转给它():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    assert h.engine.follow_for_context(ticket_id, chat("issue")) is True
    assert h.engine.follow_for_context(ticket_id, chat("issue")) is False
    h.engine.receive(trig("2", threads=[chat("issue")]))
    h.settle()
    assert [i["kind"] == "trigger" and i["triggerId"] for i in h.ticket(ticket_id)["inputs"]] == ["2"]
    h.engine.report(ticket_id, DROPPED)
    h.engine.receive(trig("3", threads=[chat("issue")]))
    assert "ended" in h.ticket(ticket_id)
    # 主 Session 把消息转给它之后，这个线程就是工单自己的了，之后那里人的消息会唤醒它
    h.engine.attach(ticket_id, "3")
    h.settle()
    assert like(h.thread("chat:issue"), {"context": []})
    h.engine.report(ticket_id, DROPPED)
    assert "ended" in h.ticket(ticket_id)
    h.engine.receive(trig("4", threads=[chat("issue")]))
    assert "ended" not in h.ticket(ticket_id)


# spec:458
def test_只为上下文关注的线程上的消息不算工单的跟进_也不解除它的等人状态():
    h = Harness()
    owner = h.accepted()["ticketId"]
    follower = h.accepted("2", "other")["ticketId"]
    h.engine.follow_for_context(follower, chat("t1"))
    h.engine.receive(trig("3", actor=GUEST))
    h.pump()
    assert like(h.disposition("3"), {"fanout": [owner, follower], "context": [follower]})
    assert like(h.ticket(follower)["inputs"][-1], {"triggerId": "3", "context": True})
    # 只到了关注者的消息进评估，连同被停掉的工单放掉的那条访客消息一起
    h.engine.stop_ticket(owner, "done")
    h.engine.report(follower, {"state": "blocked", "summary": "needs a decision"})
    # 原型这里没有拨时钟，4 号消息和受阻报告同一时刻，最后一条断言不论实现如何都成立；拨一秒让它真的晚于报告
    h.tick(1)
    h.engine.receive(trig("4"))
    h.pump()
    assert ids(h.requests[-1]) == ["3", "4"]
    assert like(h.ticket(follower)["inputs"][-1], {"triggerId": "4", "context": True, "human": True})
    assert waits_for_person(h.ticket(follower)) is True


# spec:474
def test_消息到的工单不能恢复_只剩为上下文关注线程的工单时_消息进评估():
    h = Harness()
    owner = h.accepted()["ticketId"]
    follower = h.accepted("2", "other")["ticketId"]
    h.engine.follow_for_context(follower, chat("t1"))
    h.engine.report(owner, READY)
    h.version = "revoked"
    h.engine.receive(trig("3"))
    h.version = "v1"
    h.pump()
    assert like(h.disposition("3"), {"fanout": [follower], "context": [follower]})
    assert ids(h.requests[-1]) == ["3"]


# spec:485
def test_备注作为单独的输入给未结束工单的执行器_等人时和自动输入一样压着_已结束的工单拒收():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    assert like(h.engine.note(ticket_id, "The other ticket renamed the API")["inputs"][-1],
                {"kind": "note", "text": "The other ticket renamed the API"})
    # 报告了受阻的执行器在等人：备注不唤醒它
    h.settle()
    h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
    note = h.engine.note(ticket_id, "Ticket B changed the schema")["inputs"][-1]
    h.settle()
    assert note["id"] not in [input_id for _, input_id in h.delivered]
    assert note in h.ticket(ticket_id)["inputs"]
    h.engine.stop_ticket(ticket_id, "done")
    with refuses("ticketEnded"):
        h.engine.note(ticket_id, "Too late")


# ---------------------------------------------------------------- 跟进归属（follow-up owners）

OTHER = {"kind": "accept", "title": "Y", "scope": "small", "reason": "requested"}


# spec:504
def test_主Session在这条跟进上停掉工单之后_可以把它当新工作接下():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    # “停掉这个，改做 Y”：停止结束了决定这条消息的工单，于是这个请求归主 Session
    h.engine.receive(trig("2"))
    h.settle()
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.decide("2", OTHER)
    assert h.engine.decide("2", {"kind": "stop", "ticketId": ticket_id, "reason": "asked to stop"})["kind"] == "stop"
    # 同一时刻，接单是停止之外的另一个决定
    assert like(h.engine.decide("2", OTHER), {"kind": "accept", "triggerIds": ["2"]})
    assert [d["kind"] for d in h.decisions() if "2" in d["triggerIds"]] == ["stop", "accept"]


# spec:516
@pytest.mark.parametrize("end", ["report", "stop"])
def test_工单结束时_执行器从没收到的跟进进评估(end):
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2"))
    assert len(h.ticket(ticket_id)["undelivered"]) == 1
    if end == "report":
        h.engine.report(ticket_id, DROPPED)
    else:
        h.engine.stop_ticket(ticket_id, "not needed")
    assert h.pending() == ["2"]
    h.pump()
    assert ids(h.requests[-1]) == ["2"]


# spec:529
def test_工单结束时_执行器上次报告之后才拿到的跟进进评估_它确认过的除外():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, PROGRESS)
    # 执行器确认了两条消息中的一条；它还没接另一条，最后的报告就已经在路上了
    h.tick(1)
    h.engine.receive(trig("2"))
    h.engine.receive(trig("3"))
    h.settle()
    h.engine.reply(ack(ticket_id, "2"))
    h.engine.report(ticket_id, DROPPED)
    assert h.pending() == ["3"]
    # 工单不再算作到过它，所以它的候选不会列出一张没给理由的工单
    assert (h.disposition("2")["fanout"], h.disposition("3")["fanout"]) == ([ticket_id], [])
    # 确认记在消息上（原型 Replies.acknowledge → takeUp），不只是发件箱里有那条确认
    assert h.disposition("2")["acked"] == [ticket_id]


# spec:541
@pytest.mark.parametrize("taken", [True, False])
def test_MR合并后_执行器在做它上次报告之后接下的人的消息时_工单保持打开(taken):
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    # 有人又要改一处，执行器接下了或还没接，这时 MR 合并了
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    if taken:
        h.engine.reply(ack(ticket_id, "2"))
    h.engine.thread_closed(gh("pr-9"), "merged", "2026-09-23T00:00:01Z")
    # 还没接的那条不拦着合并结束工单，它自己进评估
    outcome = (h.ticket(ticket_id).get("ended") or {}).get("outcome")
    assert (outcome, h.pending()) == ((None, []) if taken else ("merged", ["2"]))
    # 执行器的就绪报告让工单按 MR 的结果结束
    if taken:
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})


# spec:560
@pytest.mark.parametrize("resumed", [False, True])
def test_已结束工单放掉的跟进_主Session再转给它时_执行器重新拿到(resumed):
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    # 工单在执行器接下一条人的消息之前结束，消息进评估；之后的消息可能已经把它恢复
    h.engine.report(ticket_id, PROGRESS)
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.report(ticket_id, DROPPED)
    if resumed:
        h.tick(1)
        h.engine.receive(trig("3"))
    h.pump()
    h.tick(1)
    h.engine.attach(ticket_id, "2")
    h.settle()
    # 执行器重新拿到这条消息，已结束的工单为它恢复
    copies = [i for i in h.ticket(ticket_id)["inputs"] if i["kind"] == "trigger" and i["triggerId"] == "2"]
    assert len(copies) == 2
    assert (ticket_id, copies[1]["id"]) in h.delivered
    assert "ended" not in h.ticket(ticket_id)
    assert followup_owners(trig("2"), h.repo) == [ticket_id]


# spec:578
@pytest.mark.parametrize("read", ["context", "report"])
def test_转给已结束工单的消息_它只当上下文读过或在上次报告前读过_也会唤醒它(read):
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    if read == "context":
        h.engine.follow_for_context(ticket_id, chat("issue"))
    h.tick(1)
    h.engine.receive(trig("2", threads=[chat("issue" if read == "context" else "t1")]))
    h.settle()
    # 执行器读过之后报告并放弃了工单；人从面板把它手里的那条跟进送去评估
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "read it"})
    h.engine.report(ticket_id, DROPPED)
    if read == "report":
        h.engine.reevaluate("2")
    h.pump()
    h.tick(1)
    h.engine.attach(ticket_id, "2")
    h.settle()
    assert "ended" not in h.ticket(ticket_id)
    assert followup_owners(h.repo.triggers.get("2"), h.repo) == [ticket_id]


# spec:594
def test_最后的报告答复了的提问算已接下_不会在答复之外又作为候选回来():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.report(ticket_id, {"state": "ready", "summary": "It reads the config.", "answers": "2"})
    assert h.pending() == []
    assert like(h.disposition("2"), {"fanout": [ticket_id], "acked": [ticket_id]})


# spec:602
def test_评估未完成之后转给工单的消息_工单没接就结束时进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", threads=[chat("x")]))
    h.pump()
    h.decide(h.requests[-1], {"kind": "unavailable", "reason": "model down"})
    h.tick(1)
    h.engine.attach(ticket_id, "2")
    h.settle()
    h.engine.report(ticket_id, DROPPED)
    assert h.pending("chat:x") == ["2"]


# spec:611
def test_发件箱里已有的确认_即使没记在消息上_也算接下了这条消息():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, PROGRESS)
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.outbox.append({"id": key("ack", "2"), "channel": trig("2")["channel"], "text": "On it", "kind": "acknowledgement", "ticketId": ticket_id})
    h.engine.report(ticket_id, DROPPED)
    assert (h.pending(), h.disposition("2")["fanout"]) == ([], [ticket_id])


# spec:620
def test_唤醒工单的消息算被再次结束它的报告接下_之后才来的不算():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, READY)
    # 人的一句感谢唤醒了工单，派工启动片刻后执行器送达、worker 先把它取进一步；它在报告没事可做的时候又来了一个请求
    h.tick(1)
    h.engine.receive(trig("2"))
    h.tick(1)
    h.engine.assignment_started(ticket_id, h.ticket(ticket_id)["assignments"][-1]["id"])
    h.tick(1)
    h.settle()
    h.read(ticket_id)
    h.tick(1)
    h.engine.receive(trig("3"))
    h.settle()
    h.engine.report(ticket_id, DROPPED)
    assert h.pending() == ["3"]


# spec:633
@pytest.mark.parametrize("actor", [GUEST, MEMBER], ids=["访客的停止被拒", "名单内的人的停止生效"])
def test_停止被拒后工单放掉的跟进进评估_停止生效的那条不进(actor):
    # 访客的停止被拒，执行器之后放弃了工单；名单内的人的停止结束了工单，由停掉它的主 Session 自己决定这条消息剩下的部分
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", actor=actor))
    h.engine.decide("2", {"kind": "stop", "ticketId": ticket_id, "reason": "asked to stop"})
    if actor is GUEST:
        h.engine.report(ticket_id, DROPPED)
    assert h.pending() == (["2"] if actor is GUEST else [])


# spec:645
def test_停止所依据的消息还到过另一张工单_那张没接就结束时消息进评估():
    h = Harness()
    stopped = h.accepted()["ticketId"]
    other = h.accepted("5", "t5")["ticketId"]
    h.engine.bind_thread(other, chat("t1"), False)
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.decide("2", {"kind": "stop", "ticketId": stopped, "reason": "asked to stop"})
    assert h.pending() == []
    h.engine.report(other, DROPPED)
    assert h.pending() == ["2"]


# spec:655
def test_执行器认定线程无关时_那里的跟进带着它的理由交还去评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", threads=[chat("x")]))
    h.pump()
    h.engine.attach(ticket_id, "2")
    h.settle()
    h.engine.receive(trig("3", threads=[chat("x")]))
    h.settle()
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.decide("3", OTHER)
    # 执行器发现转来的线程不是它这张工单的，在那里什么都没答：主 Session 转来的那条和后来的那条一起交还，各自的候选都说明原因
    h.engine.mark_unrelated(ticket_id, "chat:x", "about something else")
    assert h.pending("chat:x") == ["2", "3"]
    assert h.disposition("3")["handbacks"] == [{"ticketId": ticket_id, "reason": "about something else", "at": ANY_STR}]
    h.pump()
    assert ids(h.requests[-1]) == ["2", "3"]


# spec:671
def test_到过两张工单的跟进留给没交还的那张_两张都不决定时才排队():
    h = Harness()
    first = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.handback(first, "2", "another change")
    h.pump()
    second = h.decide(h.requests[-1], OTHER)["ticketId"]
    h.settle()
    h.tick(1)
    h.engine.receive(trig("3"))
    h.settle()
    assert h.disposition("3")["fanout"] == [first, second]
    assert h.engine.handback(first, "3", "for the other ticket") is False
    assert h.pending() == []
    # 第二张工单的执行器还在决定它时，主 Session 既不能接也不能忽略
    with refuses("workerDecides", tickets=second):
        h.engine.decide("3", OTHER)
    with refuses("workerDecides", tickets=second):
        h.engine.decide("3", {"kind": "ignore", "reason": "chat"})
    assert h.engine.handback(second, "3", "neither ticket") is True
    assert h.pending() == ["3"]


# spec:688
def test_评估持有的跟进由那次评估决定_不归工单():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2"))
    h.settle()
    thread = h.thread("chat:t1")
    h.repo.threads.save({**thread, "evaluating": {
        "id": "e-held", "threadKey": "chat:t1", "triggerIds": ["2"], "primary": "2", "strength": "explicit", "actor": MEMBER,
        "basis": EXPLICIT_BASIS, "policyVersion": "v1", "startedAt": "2026-09-23T00:00:00.000Z", "deadline": "2026-09-24T00:00:00.000Z"}})
    assert followup_owners(trig("2"), h.repo) == []
    assert like(h.engine.decide("2", {"kind": "ignore", "reason": "chat", "evalId": "e-held"}), {"kind": "ignore", "triggerIds": ["2"]})
    assert "ended" not in h.ticket(ticket_id)


# ---------------------------------------------------------------- 被打断的派工

# spec:702
def test_执行中被打断的派工带恢复说明重新开始_在等的派工原样拿到等它的输入():
    h = Harness()
    interrupted = h.accepted()["ticketId"]
    first = h.ticket(interrupted)["assignments"][0]
    waiting = h.accepted("2", "t2")["ticketId"]
    h.engine.report(waiting, {"state": "waiting", "summary": "done for now"})
    h.engine.receive(trig("3", threads=[chat("t2")]))
    h.starts.clear()
    h.delivers.clear()
    # 执行器逐个报告当时在跑的派工；关于已不在跑的派工的事实被丢弃
    for ticket_id in (interrupted, waiting):
        h.engine.assignment_interrupted(ticket_id, h.running(ticket_id)["id"])
    h.engine.assignment_interrupted(interrupted, first["id"])
    ticket = h.ticket(interrupted)
    assert ticket["assignments"] == [Partial(id=first["id"], state="ended", endedReason="hostRestarted"), Partial(state="pending")]
    assert ticket["inputs"] == [Partial(kind="text", text=RECOVERY_NOTE)]
    assert ticket["undelivered"] == [ticket["inputs"][0]["id"]]
    assert h.starts == [(Partial(id=interrupted), ticket["assignments"][1])]
    # 在等的 worker 保留它的会话，拿到等着它的输入
    assert len(h.ticket(waiting)["assignments"]) == 1
    assert h.delivers == [(Partial(id=waiting), Partial(state="running"))]


# spec:723
def test_报告之后又收到人的消息并在处理的worker_被打断时重新开始():
    h = Harness()
    other = h.accepted("2", "t2")["ticketId"]
    h.engine.report(other, {"state": "waiting", "summary": "done for now"})
    h.tick(1)
    h.engine.receive(trig("3", actor=GUEST, threads=[chat("t2")]))
    h.settle()
    running = h.running(other)
    assert running["deliveredAt"] > running["report"]["at"]
    h.engine.assignment_interrupted(other, running["id"])
    assert [a["state"] for a in h.ticket(other)["assignments"]] == ["ended", "pending"]


# ---------------------------------------------------------------- 暂停

# spec:735
def test_暂停期间评估不过期_恢复后期限顺延暂停的时长():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    h.engine.candidate_delivered(h.requests[0]["evalId"])
    deadline = ms(h.thread("chat:t1")["evaluating"]["deadline"])
    h.tick(30)
    h.paused = True
    h.pump()
    # 暂停着过了期限，评估等着
    h.tick(300)
    h.pump()
    assert h.engine.candidate("1") is not None
    h.paused = False
    h.pump()
    assert ms(h.thread("chat:t1")["evaluating"]["deadline"]) == deadline + 300_000
    h.tick(29)
    h.pump()
    assert h.engine.candidate("1") is not None
    # 过期了：触发重新排队等新的评估
    h.tick(1)
    h.pump()
    assert h.engine.candidate("1")["evalId"] != h.requests[0]["evalId"]
    assert h.decisions() == []


# ---------------------------------------------------------------- 策略变更

# spec:755
def test_策略变更只告诉受影响的未结束工单_进行中的评估在新版本下重来():
    h = Harness()
    decision = h.accepted()
    other = h.accepted("3", "t3")["ticketId"]
    h.engine.receive(trig("2", threads=[chat("t2")]))
    h.pump()
    effect = {"rules": [{"id": "copy", "name": "Copy", "change": "changed", "instruction": "Fix typos only."}]}
    h.version = "v2"
    h.engine.policy_changed("v1", "v2", {decision["ticketId"]: effect})
    assert h.ticket(decision["ticketId"])["inputs"] == [Partial({"kind": "policy-changed", "from": "v1", "to": "v2", "effect": effect})]
    assert [i for i in h.ticket(other)["inputs"] if i["kind"] == "policy-changed"] == []
    assert h.decisions("chat:t2") == [Partial(kind="unavailable", reason=[{"code": "policyChanged"}])]
    assert h.pending("chat:t2") == ["2"]
    h.pump()
    assert like(h.requests[-1], {"policyVersion": "v2", "triggers": [Partial(id="2")]})
    told = next(i for i in h.ticket(decision["ticketId"])["inputs"] if i["kind"] == "policy-changed")
    assert h.delivered == [(decision["ticketId"], told["id"])]
    assert [e["data"] for e in h.events if e["type"] == "policy.changed"] == [{"from": "v1", "to": "v2"}]


# spec:771
def test_暂停时不开评估():
    h = Harness(paused=True)
    h.engine.receive(trig("1"))
    h.pump()
    assert h.requests == []


# ---------------------------------------------------------------- 执行器的报告

# spec:779
def test_问人的问题_提出_保持_替换_清掉_被替换的问题撤回():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    asked = h.engine.report(ticket_id, {"state": "progress", "summary": "need a call", "question": "Which color?", "askedBy": "1"})
    assert like(asked["attention"], {"question": "Which color?", "triggerId": "1"})
    assert h.engine.report(ticket_id, {"state": "blocked", "summary": "still waiting"})["attention"] == asked["attention"]
    assert h.withdrawn == []
    replaced = h.engine.report(ticket_id, {"state": "progress", "summary": "other question", "question": "Which size?"})
    assert like(replaced["attention"], {"question": "Which size?"})
    assert "triggerId" not in replaced["attention"]
    # 被替换的问题如果消息还没发出去，从发件箱撤回
    assert h.withdrawn == [key("attention-reply", asked["attention"]["id"])]
    cleared = h.engine.report(ticket_id, {"state": "progress", "summary": "answered", "question": None})
    assert "attention" not in cleared
    assert h.withdrawn == [key("attention-reply", asked["attention"]["id"]), key("attention-reply", replaced["attention"]["id"])]
    # Python 版新增：每提出一个新问题发一次 attention.asked
    assert [e["data"] for e in h.events if e["type"] == "attention.asked"] == [
        {"ticketId": ticket_id, "attentionId": asked["attention"]["id"]}, {"ticketId": ticket_id, "attentionId": replaced["attention"]["id"]}]


def _final_report(state: str, follows: bool, closed: bool = False) -> tuple[Harness, dict]:
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "progress", "summary": "asking", "question": "Merge it?"})
    if follows:
        h.engine.bind_thread(ticket_id, gh("pr-9"), False)
        h.engine.bind_thread(ticket_id, gh("pr-10"), True)
    # 两个线程都关了之后又恢复的工作，随它的就绪报告结束，结果取最晚的那次关闭
    if closed:
        h.engine.thread_closed(gh("pr-9"), "closed", "2026-09-22T00:00:00Z")
        h.engine.thread_closed(gh("pr-10"), "merged", "2026-09-22T00:00:01Z")
    h.engine.report(ticket_id, {"state": state, "summary": "done"})
    return h, h.ticket(ticket_id)


# spec:795
def test_最后的报告结束工单_dropped放弃_ready完成_除非它跟随的来源线程还开着():
    h, dropped = _final_report("dropped", True)
    assert like(dropped, {"ended": {"by": "executor", "outcome": "dropped"}})
    assert "reason" not in dropped["ended"]
    # Python 版：工单结束时撤回还没发出的问题（原型只清掉问题，不撤回）
    assert len(h.withdrawn) == 1 and "attention" not in dropped

    _, completed = _final_report("ready", False)
    assert like(completed, {"ended": {"outcome": "completed"}})
    assert "attention" not in completed

    _, following = _final_report("ready", True)
    assert like(following, {"follows": [{"thread": "gh:pr-9", "own": False}, {"thread": "gh:pr-10", "own": True}],
                            "assignments": [Partial(report=Partial(state="ready"))]})
    assert "ended" not in following
    assert "attention" not in following

    _, merged = _final_report("ready", True, True)
    assert like(merged, {"ended": {"outcome": "merged"}})

    _, failed = _final_report("failed", False)
    assert like(failed, {"attention": {"question": "Merge it?"}})
    assert "ended" not in failed


# spec:822
def test_问题不能和结果或答复一起报_问题和答复指的必须是到过工单的人的消息():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", human=False, threads=[chat("t1")]))
    h.engine.receive(trig("4", threads=[chat("elsewhere")]))

    def report(**extra):
        return h.engine.report(ticket_id, {"state": "progress", "summary": "x", **extra})

    with refuses("questionWithResult"):
        report(state="ready", question="Q?")
    with refuses("questionWithResult"):
        report(question="Q?", answers="1")
    with refuses("askerWithoutQuestion"):
        report(askedBy="1")
    with refuses("notAsker"):
        report(question="Q?", askedBy="2")
    with refuses("notAnswerable"):
        report(answers="4")
    assert like(report(answers="1"), {"answers": ["1"]})


# spec:835
def test_已结束的工单不收报告():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.stop_ticket(ticket_id)
    with refuses("ticketEnded"):
        h.engine.report(ticket_id, {"state": "progress", "summary": "x"})


# ---------------------------------------------------------------- 结果

# spec:843
def test_记下工单是怎么结束的():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.stop_ticket(ticket_id, "Human asked to stop")
    assert like(h.ticket(ticket_id)["ended"], {"by": "human", "outcome": "stopped", "reason": "Human asked to stop"})
    assert ticket_outcome(h.ticket(ticket_id)) == "stopped"
    assert ticket_outcome({}) is None
    assert [e["data"] for e in h.events if e["type"] == "ticket.ended"] == [
        {"ticketId": ticket_id, "by": "human", "outcome": "stopped", "reason": "Human asked to stop"}]


# ---------------------------------------------------------------- 转给工单（attach）

# spec:853
def test_执行器已有的消息只补送路由备注_只持有这条消息的评估随之结束_被人停掉或策略不再支持的工单拒收():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    assert [input_id for _, input_id in h.delivered] == [key("input", ticket_id, "trigger", "2")]
    # 跟进没经过评估就到了工单，归它的执行器判断；人再把它送去评估，才在那里判断
    assert h.engine.candidate("2") is None
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.attach(ticket_id, "2")
    assert h.engine.reevaluate("2") is True
    h.pump()
    assert h.engine.candidate("2") is not None
    attached = h.engine.attach(ticket_id, "2", "The guest asks for the same fix")
    h.settle()
    assert attached["inputs"] == [Partial(triggerId="2"), Partial(triggerId="2", note="The guest asks for the same fix")]
    assert [input_id for _, input_id in h.delivered] == [i["id"] for i in attached["inputs"]]
    assert h.engine.candidate("2") is None
    assert like(h.decisions()[-1], {"kind": "ignore", "reason": [{"code": "routed", "values": {"ticket": ticket_id}}]})
    # 消息还在等执行器时，备注随它一起送；人刚把它送去评估的也一样
    h.engine.receive(trig("5", actor=GUEST))
    h.engine.reevaluate("5")
    assert like(h.engine.attach(ticket_id, "5", "Same fix again")["inputs"][-1], {"triggerId": "5", "note": "Same fix again"})
    h.settle()
    h.engine.report(ticket_id, READY)
    h.version = "revoked"
    with refuses("mandateRevoked"):
        h.engine.attach(ticket_id, "2")
    h.version = "v1"
    stopped = h.accepted("3", "t3")["ticketId"]
    h.engine.stop_ticket(stopped)
    with refuses("ticketStopped"):
        h.engine.attach(stopped, "2")
    with refuses("unknownTicket"):
        h.engine.attach("missing", "2")


# spec:881
def test_批量评估里的一条消息转给工单后_评估结束_其余的重新评估_这条归那张工单():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    other = h.accepted("5", "t5")["ticketId"]
    h.engine.receive(trig("2", threads=[chat("x")]))
    h.engine.receive(trig("3", threads=[chat("x")]))
    h.pump()
    h.engine.attach(ticket_id, "3")
    h.pump()
    assert h.decisions("chat:x") == [Partial(kind="unavailable", triggerIds=["2", "3"],
                                             reason=[{"code": "routed", "triggerId": "3", "values": {"ticket": ticket_id}}])]
    assert ids(h.engine.candidate("2")) == ["2"]
    assert followup_owners(h.repo.triggers.get("3"), h.repo) == [ticket_id]
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.attach(other, "3")


# spec:893
def test_错误区分不存在和被拒绝_并把取值填进文案():
    assert [UserError("unknownTicket").missing, UserError("ticketEnded").missing] == [True, False]
    error = UserError("notAdmissible", {"reason": "not_a_member"})
    assert (error.code, error.values, str(error)) == ("notAdmissible", {"reason": "not_a_member"}, "这条触发不准入：not_a_member")


# ---------------------------------------------------------------- 直接决定

# spec:899
def test_没有评估持有的明确请求直接决定_没命中规则的主动接单被拒_一条触发只接一次():
    h = Harness()
    h.engine.receive(trig("1"))
    decision = h.engine.decide("1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "routed"})
    assert like(decision, {"kind": "accept", "triggerIds": ["1"], "strength": "explicit"})
    thread = h.thread("chat:t1")
    assert thread["pending"] == [] and "evaluating" not in thread
    assert h.engine.decide("1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "routed"}) == decision
    # 访客的跟进没经过评估就到了那张工单：归它的执行器决定，只有执行器交还的才能成为新工作，权限按准入算，是推断
    h.engine.receive(trig("2", actor=GUEST))
    h.pump()
    ticket_id = decision["ticketId"]
    with refuses("workerDecides", tickets=ticket_id):
        h.engine.decide("2", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["r1"]})
    h.engine.handback(ticket_id, "2", "A different fix")
    with refuses("notExplicit"):
        h.engine.decide("2", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "proactive"})
    assert like(h.engine.decide("2", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["r1"]}),
                {"kind": "accept", "strength": "inferred", "mandate": {"basis": {**INFERRED_BASIS, "rules": ["r1"]}}})
    with refuses("unknownTrigger"):
        h.engine.decide("missing", {"kind": "ignore", "reason": "x"})


# spec:919
def test_推断候选只能凭策略认可的规则接单_不管规则是谁报的():
    h = Harness(admits_inferred=lambda rules: "enabled" in rules)
    h.accepted()
    h.engine.receive(trig("2", actor=GUEST))
    h.engine.reevaluate("2")
    h.pump()
    candidate = h.requests[-1]
    with refuses("notExplicit"):
        h.decide(candidate, {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["disabled"]})
    assert like(h.decide(candidate, {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["enabled"]}),
                {"kind": "accept"})


# spec:927
def test_重放一条开过工单的触发_它的评估结束_返回那张工单的决定():
    h = Harness()
    decision = h.accepted()
    h.tick(1)
    assert h.engine.reevaluate("1") is True
    h.pump()
    assert h.engine.candidate("1") is None
    assert like(h.decisions()[-1], {"kind": "ignore", "triggerIds": ["1"],
                                    "reason": [{"code": "startedTicket", "triggerId": "1", "values": {"ticket": decision["ticketId"]}}]})
    assert h.engine.decide("1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "again"}) == decision
    assert len(h.repo.tickets.list()) == 1
    with refuses("unknownTrigger"):
        h.engine.reevaluate("missing")
    assert [e["data"] for e in h.events if e["type"] == "trigger.requeued"] == [{"triggerId": "1"}]


# spec:940
def test_重放的已开过工单的触发不进和它一起排队的触发的评估():
    h = Harness()
    decision = h.accepted()
    h.engine.receive(trig("2", actor=GUEST))
    h.engine.reevaluate("2")
    h.engine.reevaluate("1")
    h.pump()
    # 名单内的人重放的请求不给访客的消息带来权限
    assert h.engine.candidate("1") is None
    assert like(h.engine.candidate("2"), {"strength": "inferred", "triggers": [{"id": "2"}]})
    with refuses("notExplicit"):
        h.engine.decide("2", {"kind": "accept", "title": "Other", "scope": "small", "reason": "routed"})
    assert h.engine.decide("1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "again"}) == decision
    assert len(h.repo.tickets.list()) == 1


# spec:951
def test_和重放的请求一起被接的请求开自己的工单_有自己的起点和确认():
    h = Harness()
    first = h.accepted()
    h.tick(1)
    h.engine.receive(trig("2"))
    h.engine.reevaluate("2")
    h.engine.reevaluate("1")
    h.pump()
    second = h.decide(h.requests[-1], {"kind": "accept", "title": "Other", "scope": "small", "reason": "routed", "acknowledgement": "On it"})
    assert second["ticketId"] != first["ticketId"]
    assert h.ticket(second["ticketId"])["origin"] == {"triggerId": "2", "triggerIds": ["2"]}
    assert like(next(m for m in h.outbox if m["id"] == key("ack", "2")), {"text": "On it", "kind": "acknowledgement"})
    # 重放的请求仍然以它开的那张工单作答
    assert h.engine.decide("1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "again"}) == first


# ---------------------------------------------------------------- 自动输入

# spec:965
def test_执行器报告受阻或失败后_自动输入压着不送_直到有人的消息到来():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]

    def bot(trigger_id):
        return trig(trigger_id, human=False)

    h.engine.receive(bot("2"))
    h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
    # 已经在等的和之后来的自动输入都留在工单上，不唤醒执行器
    h.engine.receive(bot("3"))
    h.settle()
    assert like(h.ticket(ticket_id), {"undelivered": [], "inputs": [Partial(triggerId="2"), Partial(triggerId="3")]})
    assert h.delivered == []
    h.tick(1)
    h.engine.receive(trig("4", actor=GUEST))
    h.engine.receive(bot("5"))
    h.settle()
    assert [input_id for _, input_id in h.delivered] == [key("input", ticket_id, "trigger", "4"), key("input", ticket_id, "trigger", "5")]


# spec:979
def test_人的消息_之后既非受阻也非失败的报告_或重试_都解除等人状态():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
    assert waits_for_person(h.ticket(ticket_id)) is True
    assert ticket_stage(h.ticket(ticket_id)) == {"stage": "person", "action": "blocked"}
    # 执行器拿到了人的回复，过时的受阻报告不再卡住工单
    h.tick(1)
    h.engine.receive(trig("2", actor=GUEST))
    h.settle()
    assert waits_for_person(h.ticket(ticket_id)) is False
    assert ticket_stage(h.ticket(ticket_id)) == {"stage": "working"}
    h.tick(1)
    h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
    assert waits_for_person(h.ticket(ticket_id)) is True
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "found a way"})
    assert waits_for_person(h.ticket(ticket_id)) is False
    h.tick(1)
    h.engine.report(ticket_id, {"state": "blocked", "summary": "needs access"})
    h.engine.retry_ticket(ticket_id)
    assert waits_for_person(h.ticket(ticket_id)) is False


# spec:998
def test_启动失败的派工因人的消息重新启动_策略变更不会():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    ticket_id = h.decide(h.requests[-1], ACCEPT)["ticketId"]
    h.engine.report(ticket_id, {"state": "failed", "summary": "The prepare command failed"})
    h.starts.clear()
    h.version = "v2"
    h.engine.policy_changed("v1", "v2")
    assert h.starts == []
    # 原型到此为止：不带 effects 时没有工单收到输入，上面的断言不论实现如何都成立。
    # 带上对这张工单的影响，它收到 policy-changed 输入，但在等人，所以仍然不重新启动
    h.version = "v3"
    h.engine.policy_changed("v2", "v3", {ticket_id: {"requesterAllowed": False}})
    assert [i["kind"] for i in h.ticket(ticket_id)["inputs"]] == ["policy-changed"]
    assert h.starts == []
    h.tick(1)
    h.engine.receive(trig("2", actor=GUEST))
    assert h.starts == [(Partial(id=ticket_id), Partial(state="pending"))]


# ---------------------------------------------------------------- 回复

# spec:1011
def test_到过未结束工单的人的消息只确认一次_其余情况拒绝():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    # 接单时的确认保留；之后再要确认就复用它
    assert h.engine.reply(ack(ticket_id, "1")) == {"id": key("ack", "1"), "status": "pending", "reused": True}
    assert [m["text"] for m in h.outbox] == ["ok"]
    h.engine.receive(trig("2", actor=GUEST))
    assert h.engine.reply(ack(ticket_id, "2", "Looking")) == {"id": key("ack", "2"), "status": "pending", "reused": False}
    h.engine.receive(trig("3", human=False))
    with refuses("ackNotPerson"):
        h.engine.reply(ack(ticket_id, "3", "x"))
    h.engine.receive(trig("6", threads=[chat("elsewhere")]))
    with refuses("notReached"):
        h.engine.reply(ack(ticket_id, "6", "x"))
    h.engine.stop_ticket(ticket_id)
    with refuses("ackEnded"):
        h.engine.reply(ack(ticket_id, "2", "x"))


# spec:1026
def test_答复要等执行器报告了才发_结果只给就绪的工单_问题只发当前的那个():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    asked = trig("2", actor=GUEST, channel={"source": "chat", "thread": "t1", "address": {"message": "2"}})
    h.engine.receive(asked)
    with refuses("noAnswer"):
        h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Yes"})
    h.engine.report(ticket_id, {"state": "progress", "summary": "Yes", "answers": "2"})
    assert like(h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Yes"}), {"status": "pending", "reused": False})
    assert like(h.outbox[-1], {"channel": asked["channel"], "text": "Yes", "kind": "reply", "ticketId": ticket_id})
    assert h.ticket(ticket_id)["answers"] == []
    with refuses("notReady"):
        h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
    h.engine.report(ticket_id, {"state": "progress", "summary": "asking", "question": "Which color?", "askedBy": "2"})
    with refuses("notAttention"):
        h.engine.reply({"purpose": "attention", "ticketId": ticket_id, "question": "Which size?", "text": "x"})
    # 没有待决的问题就没什么可问的，不管有没有指明问题
    quiet = h.accepted("9", "t9")["ticketId"]
    with refuses("notAttention"):
        h.engine.reply({"purpose": "attention", "ticketId": quiet, "text": "Anything to send."})
    with refuses("notAttention"):
        h.engine.reply({"purpose": "attention", "ticketId": quiet, "question": "", "text": "Anything to send."})
    attention = h.ticket(ticket_id)["attention"]
    sent = h.engine.reply({"purpose": "attention", "ticketId": ticket_id, "question": "Which color?", "text": "Which color?"})
    assert sent["id"] == key("attention-reply", attention["id"])
    # 问题发到引出它的那条消息所在的地方；Python 版里带问题的回复 kind 是 attention（原型是 reply）
    assert h.outbox[-1]["channel"] == asked["channel"]
    assert h.outbox[-1]["kind"] == "attention"
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    assert like(h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"}), {"status": "pending"})
    assert h.outbox[-1]["channel"] == trig("1")["channel"]
    assert h.outbox[-1]["kind"] == "reply"


# ---------------------------------------------------------------- 工单命令

# spec:1053
def test_线程关闭和就绪报告按同一条规则结束工单_它开的MR都关了_按最晚那个的结果():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    # 它从别人的 MR 开始，又开了自己的
    h.engine.bind_thread(ticket_id, gh("pr-5"), False)
    h.engine.bind_thread(ticket_id, gh("pr-7"), True)
    h.engine.thread_closed(gh("pr-7"), "merged", "2026-09-23T00:00:00Z")
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})
    # 人的评论把它恢复；它的就绪报告让它再次按自己 MR 的结果结束，尽管它起步的那个 MR 还开着
    h.tick(10)
    h.engine.receive(trig("2", threads=[gh("pr-5")]))
    h.settle()
    assert "ended" not in h.ticket(ticket_id)
    h.engine.report(ticket_id, {"state": "ready", "summary": "Answered"})
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})
    # 它起步的那个 MR 之后关闭不改变什么
    h.engine.thread_closed(gh("pr-5"), "closed", "2026-09-23T00:00:30Z")
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})
    # 没开过 MR 的工单，在它跟随的那个关闭时结束，结果随它
    other = h.accepted("3", "t3")["ticketId"]
    h.engine.bind_thread(other, gh("pr-8"), False)
    h.engine.report(other, {"state": "ready", "summary": "Pushed to their branch"})
    assert "ended" not in h.ticket(other)
    h.engine.thread_closed(gh("pr-8"), "closed", "2026-09-23T00:01:00Z")
    assert like(h.ticket(other)["ended"], {"outcome": "closed"})


# spec:1077
def test_人取消订阅的线程_工单不再跟随():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-7"), True)
    assert h.engine.unsubscribe("gh:pr-7") is True
    assert "follows" not in h.ticket(ticket_id)
    # 它现在不跟随任何线程，就绪报告直接完成它
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "completed"})


# spec:1087
def test_工单跟随来源的线程_自己开的线程都关了就结束_除非恢复的工作还没做完():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]

    def close(thread_id, outcome, at):
        opened = [t["id"] for t in h.repo.tickets.open()]
        h.engine.thread_closed(gh(thread_id), outcome, at)
        return [i for i in opened if h.ticket(i).get("ended")]

    assert like(h.engine.bind_thread(ticket_id, gh("pr-9"), False), {"follows": [{"thread": "gh:pr-9", "own": False}]})
    h.engine.bind_thread(ticket_id, gh("pr-10"), True)
    assert h.thread("gh:pr-9")["subscriptions"] == [ticket_id]
    # 它起步的线程关了而它自己的还开着：记下这次关闭，继续工作
    assert close("pr-9", "closed", "2026-09-23T00:00:00Z") == []
    assert like(h.ticket(ticket_id), {"follows": [{"thread": "gh:pr-9", "closed": {"outcome": "closed"}}, {"thread": "gh:pr-10"}]})
    assert "ended" not in h.ticket(ticket_id)
    # 它自己的也关了：按那一个的结果、在那个时间结束，只结束一次
    assert close("pr-10", "merged", "2026-09-23T00:00:01Z") == [ticket_id]
    assert like(h.ticket(ticket_id), {"ended": {"by": "executor", "outcome": "merged", "at": "2026-09-23T00:00:01.000Z"}})
    assert close("pr-10", "merged", "2026-09-23T00:00:01Z") == []
    with refuses("ticketEnded"):
        h.engine.bind_thread(ticket_id, gh("pr-11"), True)
    # MR 关了、但关闭还没报上来之前被重试的工单，一直工作到它报告就绪
    other = h.accepted("2", "t2")["ticketId"]
    h.engine.bind_thread(other, gh("pr-12"), True)
    h.engine.report(other, {"state": "failed", "summary": "tests fail"})
    h.tick(10)
    h.engine.retry_ticket(other)
    assert close("pr-12", "closed", "2026-09-23T00:00:05Z") == []
    assert "ended" not in h.ticket(other)
    h.engine.report(other, READY)
    assert like(h.ticket(other), {"ended": {"outcome": "closed"}})


# spec:1118
def test_报告失败的工单重试时换一次新派工_只向在等的worker询问状态():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    with refuses("notRetryable"):
        h.engine.retry_ticket(ticket_id)
    assert h.engine.request_status(ticket_id) is False
    assert h.ticket(ticket_id)["inputs"] == []
    h.engine.report(ticket_id, {"state": "waiting", "summary": "idle"})
    assert h.engine.request_status(ticket_id) is True
    assert h.ticket(ticket_id)["inputs"] == [Partial(kind="text", text=STATUS_REQUEST)]
    h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
    retried = h.engine.retry_ticket(ticket_id)
    assert retried["assignments"] == [Partial(state="ended", endedReason="retried"), Partial(state="pending")]
    assert len(h.stops) == 1
    assert h.starts[-1] == (Partial(id=ticket_id), retried["assignments"][1])
    assert [e["data"] for e in h.events if e["type"] == "assignment.ended"] == [
        {"ticketId": ticket_id, "assignmentId": retried["assignments"][0]["id"], "reason": "retried"}]


# ---------------------------------------------------------------- 送达失败

# spec:1135
def test_新派工接手或工单结束后_不再记着之前的送达失败():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]

    def failed():
        h.engine.delivery_failed(ticket_id, h.running(ticket_id)["id"], "Session busy")
        assert like(h.ticket(ticket_id)["delivery"], {"error": "Session busy"})

    failed()
    h.engine.report(ticket_id, {"state": "blocked", "summary": "needs access"})
    h.engine.retry_ticket(ticket_id)
    h.settle()
    assert "delivery" not in h.ticket(ticket_id)
    failed()
    h.engine.stop_ticket(ticket_id)
    assert "delivery" not in h.ticket(ticket_id)


# ---------------------------------------------------------------- 问题（dismiss）

# spec:1152
def test_只能忽略未完成的决定所显示的问题():
    h = Harness()
    decision = h.accepted()
    with refuses("unknownProblem"):
        h.engine.dismiss(decision["id"])
    h.engine.receive(trig("2", threads=[chat("t2")]))
    h.pump()
    unavailable = h.decide(h.requests[-1], {"kind": "unavailable", "reason": "model down"})
    assert like(h.engine.dismiss(unavailable["id"]), {"kind": "unavailable", "dismissed": True})
    assert like(h.repo.decisions.get(unavailable["id"]), {"dismissed": True})


# ---------------------------------------------------------------- 送达失败后的重试

# spec:1163
def test_输入一直送不到的工单重试时开新派工_由它接走那条输入():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "failed", "summary": "tests fail"})
    # 人的回答发给失败的 worker，送达失败
    h.tick(5)
    h.engine.receive(trig("2"))
    answered = h.ticket(ticket_id)
    assert len(answered["undelivered"]) == 1
    h.engine.delivery_failed(ticket_id, h.running(ticket_id)["id"], "Session gone")
    assert ticket_stage(h.ticket(ticket_id)) == {"stage": "person", "action": "delivery"}
    retried = h.engine.retry_ticket(ticket_id)
    assert [a["state"] for a in retried["assignments"]] == ["ended", "pending"]
    assert "delivery" not in retried
    assert len(retried["undelivered"]) == 1


# ---------------------------------------------------------------- 跨策略版本的评估、被撤回的授权、混合批次

# spec:1180
def test_在旧策略下作出决定的评估_整批重新排队():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    candidate = h.requests[-1]
    # 升级改了版本，但没有报告策略变更
    h.version = "v2"
    assert like(h.decide(candidate, {"kind": "accept", "title": "Fix", "scope": "small", "reason": "r"}),
                {"kind": "unavailable", "reason": [{"code": "policyChanged"}]})
    assert h.pending() == ["1"]
    h.pump()
    assert like(h.requests[-1], {"policyVersion": "v2", "triggers": [Partial(id="1")]})


# spec:1191
def test_人的消息到的唯一工单在当前策略下不能恢复时_消息进评估_且不算到过任何工单():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    h.settle()
    h.version = "revoked"
    h.tick(5)
    h.engine.receive(trig("2"))
    assert like(h.ticket(ticket_id), {"undelivered": [], "ended": ANY})
    assert h.pending() == ["2"]
    assert followup_owners(trig("2"), h.repo) == []
    assert h.disposition("2")["fanout"] == []


# spec:1201
def test_暂停期间等着的人的消息_暂停解除后工单不能恢复时进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    h.settle()
    h.paused = True
    h.tick(5)
    h.engine.receive(trig("2"))
    h.pump()
    assert len(h.ticket(ticket_id)["undelivered"]) == 1
    h.version = "revoked"
    h.paused = False
    h.engine.evaluate_due()
    assert like(h.ticket(ticket_id), {"undelivered": [], "ended": ANY})
    assert "2" in h.pending() or h.engine.candidate("2") is not None


# spec:1212
def test_新工单的起点和确认取自给批次带来强度的那条可准入触发():
    h = Harness()
    h.engine.receive(trig("1"))
    h.tick(1)
    h.engine.receive(trig("2", human=False, actor={"source": "chat", "id": "ci-bot"},
                          channel={"source": "chat", "thread": "chat:t1", "address": {"message": "bot"}}))
    h.pump()
    assert ids(h.requests[-1]) == ["1", "2"]
    decision = h.decide(h.requests[-1], {"kind": "accept", "title": "Fix", "scope": "small", "reason": "r", "acknowledgement": "ok"})
    assert h.ticket(decision["ticketId"])["origin"] == {"triggerId": "1", "triggerIds": ["1", "2"]}
    assert h.outbox == [Partial(id=key("ack", "1"), channel=trig("1")["channel"])]


# ---------------------------------------------------------------- 补漏（gaps）

# spec:1224
def test_混合批次的强度_发起人和依据取自最强的可准入触发_同强度取最晚的():
    # 访客的消息作为推断收下，就像在有工单跟随的线程上
    def gate(trigger, thread, tickets):
        if trigger["actor"]["id"] == "alice":
            return {"admissible": True, "strength": "explicit", "basis": dict(EXPLICIT_BASIS)}
        return {"admissible": True, "strength": "inferred", "basis": dict(INFERRED_BASIS)}

    for order in (["guest", "alice"], ["alice", "guest"]):
        h = Harness(gate=gate)
        for index, who in enumerate(order):
            h.tick(1)
            h.engine.receive(trig(str(10 + index), actor=MEMBER if who == "alice" else GUEST))
        h.pump()
        alice = str(10 + order.index("alice"))
        assert like(h.thread("chat:t1")["evaluating"], {"strength": "explicit", "actor": MEMBER, "basis": EXPLICIT_BASIS, "primary": alice})

    named = Harness(gate=lambda trigger, thread, tickets: {
        "admissible": True, "strength": "explicit", "basis": {**EXPLICIT_BASIS, "sender": trigger["actor"]["id"]}})
    named.engine.receive(trig("1", actor={"source": "chat", "id": "bob"}))
    named.tick(1)
    named.engine.receive(trig("2", actor={"source": "chat", "id": "carol"}))
    named.pump()
    assert like(named.thread("chat:t1")["evaluating"], {"actor": {"id": "carol"}, "basis": {"sender": "carol"}, "primary": "2"})


# spec:1241
def test_被重试替换掉的派工迟到的启动_什么都不改也什么都不送():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    first = h.ticket(ticket_id)["assignments"][0]["id"]
    h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
    h.engine.retry_ticket(ticket_id)
    before = h.ticket(ticket_id)
    delivered = len(h.delivered)
    h.engine.assignment_started(ticket_id, first)
    h.settle(launch=False)
    assert h.ticket(ticket_id)["assignments"] == before["assignments"]
    assert len(h.delivered) == delivered


# spec:1253
def test_停止指向不存在或已结束的工单时记为忽略():
    h = Harness()
    done = h.accepted()["ticketId"]
    h.engine.report(done, {"state": "ready", "summary": "Done"})
    for trigger_id, ticket_id in (("2", "missing"), ("3", done)):
        h.tick(1)
        h.engine.receive(trig(trigger_id, threads=[chat(f"t{trigger_id}")]))
        h.pump()
        assert like(h.decide(h.requests[-1], {"kind": "stop", "ticketId": ticket_id, "reason": "stop it"}),
                    {"kind": "ignore", "reason": [{"code": "stopUnknown"}]})
    assert like(h.ticket(done)["ended"], {"by": "executor", "outcome": "completed"})


# spec:1263
def test_准入不收的触发不能直接决定_队列保持原样():
    h = Harness()
    h.engine.receive(trig("1", human=False))
    pending = h.pending()
    with refuses("notAdmissible", reason="not human"):
        h.engine.decide("1", {"kind": "ignore", "reason": "noise"})
    assert h.pending() == pending == ["1"]
    assert h.decisions() == []


# spec:1271
def test_报告失败后被人停掉的工单不能重试():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
    h.engine.stop_ticket(ticket_id)
    with refuses("notRetryable"):
        h.engine.retry_ticket(ticket_id)


# spec:1278
def test_提问只答复一次_结果只给做完的工作_同一个结果只发一次():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.report(ticket_id, {"state": "progress", "summary": "It reads the config.", "answers": "2"})
    assert like(h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "It reads the config."}), {"reused": False})
    with refuses("noAnswer"):
        h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Something else."})
    with refuses("notReady"):
        h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    sent = len(h.outbox)
    assert like(h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"}), {"reused": False})
    assert like(h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"}), {"reused": True})
    assert len(h.outbox) == sent + 1
    # MR 没合并就关了的工单、被人停掉的工单，没有结果可发
    closed = h.accepted("5", "t5")["ticketId"]
    h.engine.bind_thread(closed, gh("pr-1"), True)
    h.engine.thread_closed(gh("pr-1"), "closed", "2026-09-23T00:01:00Z")
    with refuses("notReady"):
        h.engine.reply({"purpose": "result", "ticketId": closed, "text": "x"})
    stopped = h.accepted("6", "t6")["ticketId"]
    h.engine.stop_ticket(stopped)
    with refuses("notReady"):
        h.engine.reply({"purpose": "result", "ticketId": stopped, "text": "x"})


# spec:1300
def test_重复报告送达不改期限_被评估持有的触发不能再排队_过期的触发排在前面():
    h = Harness()
    h.engine.receive(trig("1"))
    h.pump()
    eval_id = h.requests[0]["evalId"]
    h.engine.candidate_delivered(eval_id)
    deadline = h.thread("chat:t1")["evaluating"]["deadline"]
    h.tick(30)
    h.engine.candidate_delivered(eval_id)
    assert h.thread("chat:t1")["evaluating"]["deadline"] == deadline
    assert h.engine.reevaluate("1") is False
    # 期间到达的触发排在过期的那条后面，新的评估按这个顺序把两条都带上
    h.tick(1)
    h.engine.receive(trig("2", actor=GUEST))
    h.tick(30)
    h.engine.evaluate_due()
    assert h.thread("chat:t1")["evaluating"]["triggerIds"] == ["1", "2"]
    assert h.decisions() == []


# ---------------------------------------------------------------- 终审补充

# spec:1317
def test_评估只持有一条开过工单的触发时_记为忽略而不是未完成():
    h = Harness()
    decision = h.accepted()
    # 评估在触发开了工单之后还持有着它
    thread = h.thread("chat:t1")
    h.repo.threads.save({**thread, "evaluating": {
        "id": "old-eval", "threadKey": "chat:t1", "triggerIds": ["1"], "primary": "1", "strength": "explicit", "actor": MEMBER,
        "basis": EXPLICIT_BASIS, "policyVersion": "v1", "startedAt": "", "deadline": "2999-01-01T00:00:00.000Z"}})
    assert h.engine.decide("1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "replayed"}) == decision
    assert like(h.decisions()[-1], {"kind": "ignore", "reason": [{"code": "startedTicket", "triggerId": "1",
                                                                  "values": {"ticket": decision["ticketId"]}}]})
    assert not any(d["kind"] == "unavailable" for d in h.decisions())
    assert "evaluating" not in h.thread("chat:t1")


# spec:1330
def test_准入忽略了线程排队的触发时_清掉连续过期的计数():
    h = Harness()
    h.engine.receive(trig("1", human=False))
    h.repo.threads.save({**h.thread("chat:t1"), "expired": 1})
    h.pump()
    assert h.decisions() == [Partial(kind="ignore")]
    assert "expired" not in h.thread("chat:t1")


# spec:1340
def test_每次影响工单的策略变更都告诉执行器_重复之前的版本切换也算():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    effect = {"requesterAllowed": False}
    for from_, to in (("A", "B"), ("B", "A"), ("A", "B")):
        h.tick(1)
        h.engine.policy_changed(from_, to, {ticket_id: effect})
    assert len([i for i in h.ticket(ticket_id)["inputs"] if i["kind"] == "policy-changed"]) == 3


# spec:1349
def test_只为上下文关注的线程上_执行器不能确认也不能向人提问():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.follow_for_context(ticket_id, chat("side"))
    h.tick(1)
    h.engine.receive(trig("5", threads=[chat("side")], channel={"source": "chat", "thread": "side", "address": {"message": "5"}}))
    with refuses("notReached"):
        h.engine.reply(ack(ticket_id, "5"))
    with refuses("notAsker"):
        h.engine.report(ticket_id, {"state": "progress", "summary": "A question", "question": "Which page?", "askedBy": "5"})


# spec:1359
def test_来源给的关闭时间记成规范形式_工单的updatedAt不往回走():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-1"), True)
    h.tick(30)
    before = h.ticket(ticket_id)["updatedAt"]
    # 来源给的是整秒，而且 MR 合并早于工单最后一次变更
    h.engine.thread_closed(gh("pr-1"), "merged", "2026-09-23T00:00:05Z")
    done = h.ticket(ticket_id)
    assert like(done["ended"], {"at": "2026-09-23T00:00:05.000Z", "outcome": "merged"})
    assert done["follows"] == [{"thread": "gh:pr-1", "own": True, "closed": {"at": "2026-09-23T00:00:05.000Z", "outcome": "merged"}}]
    assert done["updatedAt"] >= before


# spec:1374
def test_已结束的工单只因人的消息恢复_转给它的机器消息不会():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    h.tick(1)
    h.engine.receive(trig("2", human=False, actor={"source": "chat", "id": "ci"}, threads=[chat("ci")]))
    h.engine.attach(ticket_id, "2")
    assert "ended" in h.ticket(ticket_id)
    # 人的消息把它恢复，连同之前等着的一起
    h.tick(1)
    h.engine.receive(trig("3"))
    assert "ended" not in h.ticket(ticket_id)


# ---------------------------------------------------------------- 跟进复核：一轮结束

def _merge(h: Harness) -> None:
    h.engine.thread_closed(gh("pr-9"), "merged", "2026-09-23T00:00:01Z")


# spec:1388
def test_合并只把执行器在就绪报告之后没有机会读的消息送去评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    # 人道了声谢，worker 在一轮里读到后没再理会；这一轮结束后又来了一条消息
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.read(ticket_id)
    h.tick(1)
    h.engine.turn_ended(ticket_id, h.running(ticket_id)["id"])
    h.tick(1)
    h.engine.receive(trig("3"))
    h.settle()
    _merge(h)
    assert h.pending() == ["3"]


# spec:1400
def test_合并时执行器还在做它接下的消息_工单保持打开_期间有过进展报告也一样():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.reply(ack(ticket_id, "2"))
    # 它一边做这条消息，一边在进展报告里回答了另一个问题
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "Answered the other question"})
    _merge(h)
    assert "ended" not in h.ticket(ticket_id)


# spec:1412
def test_关闭被推迟的工单_执行器随后放弃工作时按关闭的结果结束():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.engine.reply(ack(ticket_id, "2"))
    _merge(h)
    h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing more to do"})
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})


# spec:1423
def test_另一张工单交还过的消息_留着它的工单因这条消息被停掉时进评估():
    h = Harness()
    first = h.accepted()["ticketId"]
    second = h.accepted("2", "t2")["ticketId"]
    h.engine.bind_thread(second, chat("t1"), False)
    h.engine.receive(trig("3"))
    h.settle()
    assert h.engine.handback(second, "3", "It is about the first fix") is False
    h.engine.decide("3", {"kind": "stop", "ticketId": first, "reason": "asked to stop"})
    assert h.pending() == ["3"]


# spec:1433
def test_进行中的工单已有的消息被人送去评估又被主Session转回时_执行器再拿到一次():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.tick(1)
    h.engine.report(ticket_id, PROGRESS)
    h.engine.reevaluate("2")
    h.pump()
    h.engine.attach(ticket_id, "2")
    assert len([i for i in h.ticket(ticket_id)["inputs"] if i["kind"] == "trigger" and i["triggerId"] == "2"]) == 2


# spec:1444
def test_一轮结束时还在等送达_之后才到worker的消息_停止时进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, PROGRESS)
    h.tick(1)
    h.engine.receive(trig("2"))
    assignment_id = h.running(ticket_id)["id"]
    (input_id,) = h.ticket(ticket_id)["undelivered"]
    # 消息还在路上时这一轮结束了；之后它才到 worker，人在那一轮里停掉了工单
    h.tick(1)
    h.engine.turn_ended(ticket_id, assignment_id)
    h.tick(1)
    h.engine.inputs_delivered(ticket_id, assignment_id, [input_id])
    h.tick(1)
    h.engine.stop_ticket(ticket_id, "not needed")
    assert h.pending() == ["2"]


# spec:1459
def test_在worker的等待报告之前到_却在报告之后才送达的消息_停止时进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    assignment_id = h.running(ticket_id)["id"]
    (input_id,) = h.ticket(ticket_id)["undelivered"]
    # 消息还在路上时执行器报告 worker 在等；之后它才到 worker，人停掉了工单
    h.tick(1)
    h.engine.report(ticket_id, {"state": "waiting", "summary": "Waiting for input"})
    h.tick(1)
    h.engine.inputs_delivered(ticket_id, assignment_id, [input_id])
    h.tick(1)
    h.engine.stop_ticket(ticket_id, "not needed")
    assert h.pending() == ["2"]


# ---------------------------------------------------------------- 路由复核

# spec:1473
def test_同一条跟进到过两张工单_同一时刻的两次停止记成两个决定():
    h = Harness()
    first = h.accepted()["ticketId"]
    second = h.accepted("2", "t2")["ticketId"]
    h.engine.bind_thread(second, chat("t1"), False)
    h.engine.receive(trig("3"))
    h.settle()
    stops = [h.engine.decide("3", {"kind": "stop", "ticketId": ticket_id, "reason": "asked to stop"}) for ticket_id in (first, second)]
    assert len({stop["id"] for stop in stops}) == 2
    assert [h.repo.decisions.get(stop["id"])["ticketId"] for stop in stops] == [first, second]
    assert [h.ticket(ticket_id)["ended"]["outcome"] for ticket_id in (first, second)] == ["stopped", "stopped"]


# ---------------------------------------------------------------- worker 读到了什么

def _resumed():
    """一张结束后被人的消息 2 恢复的工单，新派工已启动，它的输入在等送达。"""
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, READY)
    h.tick(1)
    h.engine.receive(trig("2"))
    assignment_id = h.ticket(ticket_id)["assignments"][-1]["id"]
    h.tick(1)
    h.engine.assignment_started(ticket_id, assignment_id)
    note, message = h.ticket(ticket_id)["undelivered"]
    return h, ticket_id, assignment_id, note, message


# spec:1498
def test_唤醒工单的消息在进展报告之后才到worker_放弃报告时进评估():
    h, ticket_id, assignment_id, note, message = _resumed()
    # 消息第一次送达失败；worker 报告了进展，重试把消息交给它的时候它正在写“没事可做”
    h.engine.inputs_delivered(ticket_id, assignment_id, [note])
    h.engine.delivery_failed(ticket_id, assignment_id, "Session busy")
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "Looking"})
    h.tick(1)
    h.engine.inputs_delivered(ticket_id, assignment_id, [message])
    h.tick(1)
    h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
    assert h.pending() == ["2"]


# spec:1509
def test_唤醒工单的消息被worker的会话取进一步之后_算被后来的放弃报告读过():
    h, ticket_id, assignment_id, note, message = _resumed()
    # 会话在消息到达引发的那一步里就取走了它，早于执行器记下送达
    h.engine.inputs_read(ticket_id, [message])
    h.engine.inputs_delivered(ticket_id, assignment_id, [note, message])
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "Looking"})
    h.tick(1)
    h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
    assert h.pending() == []


# spec:1518
def test_worker最后一步之后才到_一轮结束前没有哪一步取走的消息_合并时进评估():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.tick(1)
    h.engine.turn_ended(ticket_id, h.running(ticket_id)["id"])
    h.engine.thread_closed(gh("pr-9"), "merged", "2026-09-23T00:00:01Z")
    assert h.pending() == ["2"]


# spec:1528
@pytest.mark.parametrize("taken", [False, True])
def test_失败和一轮结束之后重试送达的消息_要有一步取走它_才算被放弃报告读过(taken):
    h, ticket_id, assignment_id, note, message = _resumed()
    h.engine.inputs_delivered(ticket_id, assignment_id, [note])
    h.engine.delivery_failed(ticket_id, assignment_id, "Session busy")
    h.tick(1)
    h.engine.turn_ended(ticket_id, assignment_id)
    h.tick(1)
    h.engine.inputs_delivered(ticket_id, assignment_id, [message])
    if taken:
        h.read(ticket_id)
    h.tick(1)
    h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
    assert h.pending() == ([] if taken else ["2"])


# spec:1540
@pytest.mark.parametrize("reported", [False, True])
def test_重启打断之前某一步取走的消息_要等重启后的worker报告了才算读过(reported):
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, PROGRESS)
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.read(ticket_id)
    # 派工带着恢复说明在同一个会话里重新开始，会话里还留着那条消息
    h.tick(1)
    h.engine.assignment_interrupted(ticket_id, h.running(ticket_id)["id"])
    h.settle()
    if reported:
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "Recovered"})
    h.tick(1)
    h.engine.stop_ticket(ticket_id, "not needed")
    assert h.pending() == ([] if reported else ["2"])


# ---------------------------------------------------------------- 读取认领复核

def _input_of(h: Harness, ticket_id: str, trigger_id: str) -> str:
    return next(i for i in h.ticket(ticket_id)["inputs"] if i["kind"] == "trigger" and i["triggerId"] == trigger_id)["id"]


# spec:1555
def test_重试的新派工拿到失败的那次已送达_但worker一步都没取走的消息():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    input_id = _input_of(h, ticket_id, "2")
    assert h.ticket(ticket_id)["undelivered"] == []
    h.tick(1)
    h.engine.report(ticket_id, {"state": "failed", "summary": "The turn broke."})
    h.tick(1)
    h.engine.retry_ticket(ticket_id)
    assert h.ticket(ticket_id)["undelivered"] == [input_id]


# spec:1568
def test_重试只补被替换的那次派工已送达没取走的_不补更早一次结束时放掉的():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    # 跟进到了 worker，工单在任何一步取走它之前结束，消息进评估
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.tick(1)
    h.engine.report(ticket_id, DROPPED)
    assert h.pending() == ["2"]
    # 人的消息恢复了工单；worker 在取走它之前报告受阻，人重试
    h.tick(1)
    h.engine.receive(trig("3"))
    h.settle()
    h.tick(1)
    h.engine.report(ticket_id, {"state": "blocked", "summary": "Which page?"})
    h.tick(1)
    h.engine.retry_ticket(ticket_id)
    assert h.ticket(ticket_id)["undelivered"] == [_input_of(h, ticket_id, "3")]


# spec:1585
def test_重启的新派工拿到被替换的那次已送达没取走的_再重试时还会拿到():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    # 宿主在某一步取走消息之前重启，取消可能把它丢了：新派工拿到它
    h.tick(1)
    h.engine.assignment_interrupted(ticket_id, h.running(ticket_id)["id"])
    assert h.ticket(ticket_id)["undelivered"][0] == _input_of(h, ticket_id, "2")
    h.settle()
    # 那次派工在取走它之前受阻，人重试：重试的派工也拿到它
    h.tick(1)
    h.engine.report(ticket_id, {"state": "blocked", "summary": "Which page?"})
    h.tick(1)
    h.engine.retry_ticket(ticket_id)
    assert h.ticket(ticket_id)["undelivered"] == [_input_of(h, ticket_id, "2")]


# ---------------------------------------------------------------- 从卡片忽略

# spec:1603
def test_没经研判就忽略的推断候选带上标记_研判过的和明确候选的不带():
    h = Harness()
    h.accepted()
    # 访客的消息被送去评估，是推断候选，主 Session 直接从卡片忽略它
    h.engine.receive(trig("2", actor=GUEST))
    h.engine.reevaluate("2")
    h.pump()
    assert like(h.requests[-1], {"strength": "inferred"})
    assert h.engine.decide("2", {"kind": "ignore", "reason": "Plain chat."})["reason"] == [{"code": "ignoredFromCard"}, said("Plain chat.")]
    # 研判给出的忽略保留它的规则判断，不带标记
    h.engine.receive(trig("3", actor=GUEST))
    h.engine.reevaluate("3")
    h.pump()
    assert like(h.decide(h.requests[-1], {"kind": "ignore", "reason": "Plain chat.", "audit": {"rule_assessments": []}}),
                {"reason": [said("Plain chat.")], "audit": {"rule_assessments": []}})
    # 明确的候选总是从它的卡片路由
    h.engine.receive(trig("4", threads=[chat("t9")]))
    h.pump()
    assert h.engine.decide("4", {"kind": "ignore", "reason": "Thanks."})["reason"] == [said("Thanks.")]
    # 接下推断候选仍然需要研判命中的规则
    h.engine.receive(trig("5", actor=GUEST))
    h.engine.reevaluate("5")
    h.pump()
    with refuses("notExplicit"):
        h.engine.decide("5", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "chat"})


# ---------------------------------------------------------------- 补充：原型 spec 没有断言到、按原型源码补的几条
# 变异检查发现原型 spec 对下面几处实现不敏感（改坏了也照过）。行为以 src/core/*.ts 为准。

# tickets.ts unfinished()：读过的消息只有“非进展报告”或一轮结束才算处理完
def test_补充_合并时_worker读过并接下的消息之后只有进展报告_工单仍保持打开():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-9"), True)
    h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.read(ticket_id)
    h.engine.reply(ack(ticket_id, "2"))
    h.tick(1)
    h.engine.report(ticket_id, {"state": "progress", "summary": "Answered the other question"})
    _merge(h)
    assert "ended" not in h.ticket(ticket_id)
    # 之后它的就绪报告按 MR 的结果结束工单
    h.tick(1)
    h.engine.turn_ended(ticket_id, h.running(ticket_id)["id"])
    h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
    assert like(h.ticket(ticket_id)["ended"], {"outcome": "merged"})


# tickets.ts threadClosed()：关闭之后才恢复、并且已经做完的工作，结束时间记现在，不记关闭的时间
def test_补充_关闭之后恢复并已做完的工单_关闭报上来时按现在结束():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.bind_thread(ticket_id, gh("pr-12"), True)
    h.engine.report(ticket_id, {"state": "failed", "summary": "tests fail"})
    h.tick(10)
    h.engine.retry_ticket(ticket_id)
    h.settle()
    h.engine.report(ticket_id, READY)
    assert "ended" not in h.ticket(ticket_id)
    h.tick(5)
    h.engine.thread_closed(gh("pr-12"), "closed", "2026-09-23T00:00:05Z")
    assert like(h.ticket(ticket_id)["ended"], {"by": "executor", "outcome": "closed", "at": "2026-09-23T00:00:15.000Z"})
    assert h.ticket(ticket_id)["follows"] == [{"thread": "gh:pr-12", "own": True, "closed": {"at": "2026-09-23T00:00:05.000Z", "outcome": "closed"}}]


# model.ts readBefore()：读取时间要严格早于报告 / 一轮结束
def test_补充_和结束工单的报告同一时刻才被取走的消息_不算读过():
    h = Harness()
    ticket_id = h.accepted()["ticketId"]
    h.engine.report(ticket_id, PROGRESS)
    h.tick(1)
    h.engine.receive(trig("2"))
    h.settle()
    h.tick(1)
    h.read(ticket_id)
    h.engine.report(ticket_id, DROPPED)
    assert h.pending() == ["2"]


def test_ready报告时还有没送达的输入_先不结束_再跑一轮处理它们():
    # 2026-11-12：执行器这一轮跑的时候又来了事件，它报 ready 后工单直接结束，新事件没人处理
    h = Harness()
    decision = h.accepted()
    h.engine.receive(trig("2", actor=GUEST))      # 订阅扇出到进行中的工单，还没 settle：执行器这一轮没看到它
    delivers = len(h.delivers)
    h.engine.report(decision["ticketId"], READY)
    ticket = h.ticket(decision["ticketId"])
    assert "ended" not in ticket and len(ticket["undelivered"]) == 1 and len(h.delivers) > delivers
    h.settle()
    assert h.delivered[-1] == (ticket["id"], ticket["undelivered"][0])
    h.engine.report(decision["ticketId"], READY)  # 处理完再报 ready：这回结束
    assert like(h.ticket(decision["ticketId"]), {"ended": ANY, "undelivered": []})
