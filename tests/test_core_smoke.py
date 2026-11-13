from helpers import World
from superops.core import UserError, ticket_stage
import pytest


def test_explicit_request_becomes_ticket_and_followup_reaches_worker():
    w = World()
    trig, ticket = w.open_ticket()
    assert ticket_stage(ticket) == {"stage": "working"}
    assert w.rec.started == [(ticket["id"], ticket["assignments"][0]["id"])]
    follow = w.send("再加一条", addressed=None)          # 同一线程上人的跟进：直接到工单，不进评估
    assert w.repo.triggers.disposition(follow["id"])["fanout"] == [ticket["id"]]
    assert not w.repo.threads.get("chat:t1")["pending"]
    assert w.rec.delivered[-1][0] == ticket["id"]
    with pytest.raises(UserError) as e:
        w.engine.decide(follow["id"], {"kind": "ignore", "reason": "x"})
    assert e.value.code == "workerDecides"


def test_ready_report_completes_and_person_message_resumes():
    w = World()
    trig, ticket = w.open_ticket()
    w.tick(); w.engine.report(ticket["id"], {"state": "ready", "summary": "好了"})
    ended = w.ticket(ticket["id"])
    assert ended["ended"]["outcome"] == "completed"
    w.send("还有个问题", addressed=None)
    resumed = w.ticket(ticket["id"])
    assert "ended" not in resumed and len(resumed["assignments"]) == 2
    assert resumed["inputs"][-1]["kind"] == "resumed" or any(i["kind"] == "resumed" for i in resumed["inputs"])


def test_machine_trigger_is_inferred_and_needs_rules():
    w = World()
    trig = w.send("cron", human=False, actor="")
    w.engine.evaluate_due()
    assert w.rec.requests[-1]["strength"] == "inferred"
    with pytest.raises(UserError) as e:
        w.engine.decide(trig["id"], {"kind": "accept", "title": "t", "scope": "s", "reason": "r"})
    assert e.value.code == "notExplicit"
    decision = w.engine.decide(trig["id"], {"kind": "accept", "title": "t", "scope": "s", "reason": "r", "rules": ["r1"]})
    assert decision["kind"] == "accept" and decision["mandate"]["basis"]["rules"] == ["r1"]


def test_attention_card_answer_runs_only_by_approver():
    w = World()
    trig, ticket = w.open_ticket()
    card = {"options": [{"key": "approve", "label": "同意", "actions": [{"type": "content.publish.approved"}]},
                        {"key": "reject", "label": "不做", "require_reason": True}], "on_expire": "reject"}
    w.tick(); w.engine.report(ticket["id"], {"state": "waiting", "summary": "等批", "question": "今晚发这 3 条？", "card": card})
    assert ticket_stage(w.ticket(ticket["id"])) == {"stage": "person", "action": "answer"}
    stranger = w.send("T1 1", actor="mallory", addressed=None)
    with pytest.raises(UserError) as e:
        w.engine.answer(ticket["id"], 1, trigger_id=stranger["id"])
    assert e.value.code == "notApprover"
    with pytest.raises(UserError) as e:
        w.engine.answer(ticket["id"], "reject", by="alice")
    assert e.value.code == "reasonRequired"
    ok = w.send("T1 1", addressed=None)
    option = w.engine.answer(ticket["id"], "1", trigger_id=ok["id"])
    assert option["key"] == "approve"
    after = w.ticket(ticket["id"])
    assert "attention" not in after and after["answered"][0]["by"] == "alice"
    assert any(i["kind"] == "answer" for i in after["inputs"])
    assert [e for e in w.rec.events if e["type"] == "attention.answered"][0]["data"]["option"]["actions"]


def test_own_events_do_not_come_back_and_hold_delays_eval():
    w = World()
    trig, ticket = w.open_ticket(thread="content:c1")
    before = len(w.ticket(ticket["id"])["inputs"])
    w.send("content.script.ready", thread="content:c1", human=False, actor="", byTicket=ticket["id"],
           notBefore="2026-11-11T01:00:00.000Z")
    assert len(w.ticket(ticket["id"])["inputs"]) == before
    n = len(w.rec.requests)
    w.engine.evaluate_due()
    assert len(w.rec.requests) == n            # 来源说 01:00 之前别评估
    w.clock = "2026-11-11T01:00:01.000Z"
    w.engine.evaluate_due()
    assert len(w.rec.requests) == n + 1
