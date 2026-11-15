"""Runtime 的到期处理（问题超时、工单闲置）、执行器的 after 等待，以及研判（superops/app/assessor.py）：
规则直配的确定性检查（预算、unless_live）、语义规则的主动候选、主 Session 的 attach / stop、研判失败与面板问题。

都接在真的同步 Runtime 上（worlds.make_world）；时间靠给 runtime / worker / 副作用队列换时钟来快进，不真的等。
"""
from __future__ import annotations

import copy
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import AGENTS, prompts_seen
from worlds import base_policy, by_rule, decisions, done, make_world, sent, tickets  # noqa: F401

from superops.app import views
from superops.infra.timeutil import TZ
from superops.core import ticket_stage
from superops.core.model import now_iso, shift
from superops.core.reasons import reason_text

HOUR, DAY = 3600, 86400
MOCK = {"role": "write", "agent": "claude", "tools": "readonly"}
CARD_SCRIPT = ("import json,os; t=json.load(open(os.environ['OPS_TASK_FILE'])); c=t['events'][0]['data']['card']; "
               "print(json.dumps({'status':'needs_decision','summary':'要人拍板','needs_decision':True,'decision_card':c}))")
VIDEO_SCRIPT = "import json; print(json.dumps({'status':'done','summary':'出片了','credits_used':60}))"
RULES = [
    {"id": "card", "name": "请人拍板", "instruction": "把要人决定的事做成卡片", "when": {"types": ["ops.card"]},
     "run": {"executor": "script", "command": ["{python}", "-c", CARD_SCRIPT]}, "tell": "needed"},
    {"id": "publish-run", "name": "执行发布", "instruction": "执行人批准的发布", "when": {"types": ["content.publish.approved"]},
     "requires_approval": True, "run": done("已发布")},
    {"id": "lead-note", "name": "线索登记", "instruction": "登记线索状态", "when": {"types": ["lead.marked"]}, "run": done("已登记")},
    {"id": "watch", "name": "盯数据", "instruction": "盯一条内容的数据", "when": {"types": ["watch.start"]}, "run": MOCK, "idle_ttl": "1h"},
    {"id": "collect", "name": "采集", "instruction": "采集数据", "when": {"types": ["ops.collect"]}, "run": MOCK},
    {"id": "brief", "name": "日报", "instruction": "写日报", "when": {"types": ["ops.brief"]}, "after": ["collect"], "after_max": "15m",
     "run": done("日报写好了")},
    {"id": "video", "name": "生成视频", "instruction": "生成成片", "when": {"types": ["content.video.requested"]}, "credits_per_run": 60,
     "run": {"executor": "script", "command": ["{python}", "-c", VIDEO_SCRIPT]}},
    {"id": "script-writing", "name": "写脚本", "instruction": "写脚本并送审", "when": {"types": ["content.topic.approved"]}, "run": MOCK},
    {"id": "rewrite", "name": "改稿", "instruction": "按复核意见改稿", "when": {"types": ["content.compliance.failed"]},
     "unless_live": ["script-writing"], "run": done("改好了")},
    {"id": "sem-copy", "name": "文案纠错", "instruction": "群里有人明确指出错别字、措辞问题时改掉"},
    {"id": "sem-visual", "name": "视觉修正", "instruction": "群里有人明确指出配色、排版问题时改掉"},
    {"id": "sem-off", "name": "停用的语义规则", "instruction": "不用了", "enabled": False},
]
DING = {"sources": {"dingtalk": {"enabled": True, "chats": ["chatA"], "observe": "all"}}}


def policy(rules: list[dict] | None = None, mode: str = "auto") -> dict:
    out = base_policy(rules if rules is not None else RULES, **DING)
    out["eval"]["mode"] = mode
    out["policy"]["sources"]["dingtalk"] = {"allowed": [{"id": "u1", "name": "张三"}]}
    return out


@pytest.fixture
def rt(make_world):
    world = make_world(policy())
    world.ding_sent = []
    world.dingtalk.send = lambda message: world.ding_sent.append(message) or "sent"      # 不真的调钉钉
    return world


@pytest.fixture
def clock(monkeypatch):
    """把 Runtime 看到的“现在”往后拨（到期处理、健康检查用它）。"""
    def later(seconds: float) -> None:
        monkeypatch.setattr("superops.app.runtime.now_iso", lambda: shift(now_iso(), seconds))
    return later


def ask_card(rt, card: dict, subject: str = "batch:1") -> dict:
    rt.pipeline.emit({"type": "ops.card", "subject": subject, "summary": "今晚的发布", "data": {"card": card}}, trusted=True)
    rt.pump()
    ticket = by_rule(rt, "card")[-1]
    assert ticket_stage(ticket) == {"stage": "person", "action": "answer"}
    return ticket


PUBLISH = {"type": "content.publish.approved", "subject": "pack:a", "data": {"pack": "a"}}
CARD = {"title": "今晚发这条吗？", "proposal": "建议发布",
        "options": [{"key": "approve", "label": "发", "emit": [PUBLISH]}, {"key": "reject", "label": "不发", "require_reason": True, "record": "not_doing"}],
        "on_expire": "reject"}


def with_meta(rt, **meta) -> list[dict]:
    return [t for t in rt.store.recent_triggers(500) if all((t.get("meta") or {}).get(k) == v for k, v in meta.items())]


# ================================================================ 问题到期


def test_问题没到期时不动_到期后按on_expire的选项处理(rt, clock):
    ticket = ask_card(rt, CARD)
    expires = ticket["attention"]["expiresAt"]
    assert shift(now_iso(), 24 * HOUR - 60) < expires <= shift(now_iso(), 24 * HOUR)       # 默认 24 小时（agents.yaml 的 card_default_ttl）
    clock(23 * HOUR)
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["attention"]["id"] == ticket["attention"]["id"]
    clock(25 * HOUR)
    rt.pump()
    after = rt.store.tickets.get(ticket["id"])
    [answer] = after["answered"]
    assert {k: answer[k] for k in ("attentionId", "question", "option", "label", "by", "reason", "expired")} == {
        "attentionId": ticket["attention"]["id"], "question": "今晚发这条吗？", "option": "reject", "label": "不发", "by": "timeout",
        "reason": "到期未回复", "expired": True}
    assert "attention" not in after and after["ended"]["outcome"] == "completed"           # 脚本工单拿到回答后收尾
    assert by_rule(rt, "publish-run") == [] and with_meta(rt, type="content.publish.approved") == []
    notice = [m["text"] for m in sent(rt) if "到期没有回复" in m["text"]]
    assert len(notice) == 1 and "T1 到期没有回复，按「不发」处理" in notice[0] and "无后续动作" in notice[0]
    # 到期不是人的决定：不记入决策档案
    assert "到期未回复" not in (rt.home / "not-doing.md").read_text(encoding="utf-8")
    assert rt.digest(shift(now_iso(), -HOUR))["cards"] == [{"seq": 1, "title": "今晚发这条吗？", "status": "expired", "choice": "reject", "decided_by": "timeout"}]


def test_到期处理只做一次_之后人再回答得到的是已处理(rt, clock):
    ticket = ask_card(rt, CARD)
    clock(25 * HOUR)
    rt.pump()
    rt.pump()
    assert len(rt.store.tickets.get(ticket["id"])["answered"]) == 1
    assert len([m for m in sent(rt) if "到期没有回复" in m["text"]]) == 1
    late = rt.answer_by_code("T1", "approve")
    assert late == {"ok": True, "already": True, "choice": "reject"}
    rt.pump()
    assert by_rule(rt, "publish-run") == []


def test_到期选项里需要批准的事件发不出去_不需要批准的能发出去(rt, clock):
    card = {"title": "这条线索还跟吗？", "proposal": "继续跟进",
            "options": [{"key": "follow", "label": "跟进", "emit": [{"type": "lead.marked", "subject": "lead:l1", "data": {"state": "following"}}]},
                        {"key": "drop", "label": "不跟进", "emit": [{**PUBLISH, "subject": "pack:b", "data": {"pack": "b"}},
                                                                   {"type": "lead.marked", "subject": "lead:l1", "data": {"state": "dropped"}}]}],
            "on_expire": "drop"}
    ticket = ask_card(rt, card)
    clock(25 * HOUR)
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["answered"][0]["expired"] is True
    [blocked] = with_meta(rt, type="content.publish.approved")                             # 需要人批的：没有批准凭据，被来源拦下
    assert blocked["meta"]["unapproved"] is True and "approved" not in blocked["meta"]
    assert rt.store.triggers.disposition(blocked["id"])["filtered"] == {"code": "needs_approval"}
    assert by_rule(rt, "publish-run") == []
    [noted] = with_meta(rt, type="lead.marked")                                            # 登记类的：发出去了，但不带批准凭据
    assert "approved" not in noted["meta"] and noted["meta"]["data"]["state"] == "dropped" and noted["byTicket"] == ticket["id"]
    [lead] = by_rule(rt, "lead-note")
    assert lead["ended"]["outcome"] == "completed"


def test_人在到期前选中的选项_事件带着批准凭据发出去(rt, clock):
    ticket = ask_card(rt, CARD)
    assert rt.answer_by_code("T1", "approve", by="u1")["ok"]
    rt.pump()
    [approved] = with_meta(rt, type="content.publish.approved")
    assert approved["meta"]["approved"]["by"] == "u1" and approved["meta"]["approved"]["option"] == "approve"
    assert approved["meta"]["approved"]["ticketId"] == ticket["id"]
    assert len(by_rule(rt, "publish-run")) == 1
    clock(25 * HOUR)                                               # 之后到了原来的期限也没有别的事发生
    rt.pump()
    assert len(rt.store.tickets.get(ticket["id"])["answered"]) == 1 and "expired" not in rt.store.tickets.get(ticket["id"])["answered"][0]


def test_卡片没写on_expire时_到期按最后一个不做对外动作的选项处理(rt, clock):
    card = {"title": "发不发？", "proposal": "发", "options": [{"key": "approve", "label": "发", "emit": [PUBLISH]},
                                                             {"key": "hold", "label": "先放着"}, {"key": "tomorrow", "label": "明天再说"},
                                                             {"key": "force", "label": "强发", "emit": [PUBLISH]}]}
    ticket = ask_card(rt, card)
    clock(25 * HOUR)
    rt.pump()
    [answer] = rt.store.tickets.get(ticket["id"])["answered"]
    assert (answer["option"], answer["expired"]) == ("tomorrow", True)
    assert with_meta(rt, type="content.publish.approved") == []


def test_没有安全选项的卡片到期后保持等人(rt, clock):
    card = {"title": "发哪一版？", "proposal": "A 版", "options": [{"key": "a", "label": "A 版", "emit": [PUBLISH]},
                                                                {"key": "b", "label": "B 版", "emit": [{**PUBLISH, "subject": "pack:b"}]}]}
    ticket = ask_card(rt, card)
    clock(25 * HOUR)
    rt.pump()
    after = rt.store.tickets.get(ticket["id"])
    assert ticket_stage(after) == {"stage": "person", "action": "answer"} and not after.get("answered")
    assert with_meta(rt, type="content.publish.approved") == []


def test_卡片自带的expires_at优先于默认期限(rt):
    past = shift(now_iso(), -60)
    rt.pipeline.emit({"type": "ops.card", "subject": "batch:9", "data": {"card": {**CARD, "expires_at": past}}}, trusted=True)
    rt.pump()                                                      # 同一轮里：提问、发现已经过期、按安全选项处理
    [ticket] = by_rule(rt, "card")
    assert ticket["answered"][0]["expired"] is True and ticket["answered"][0]["option"] == "reject"


def test_卡片的expires_at带东八区偏移时也按真实时刻到期(rt):
    an_hour_ago = (datetime.now(TZ) - timedelta(hours=1)).isoformat(timespec="seconds")   # 例如 2026-11-11T13:00:00+08:00
    rt.pipeline.emit({"type": "ops.card", "subject": "batch:9", "data": {"card": {**CARD, "expires_at": an_hour_ago}}}, trusted=True)
    rt.pump()
    [ticket] = by_rule(rt, "card")
    assert (ticket.get("answered") or [{}])[0].get("expired") is True


def test_暂停期间不做到期处理_恢复后补上(rt, clock):
    ticket = ask_card(rt, CARD)
    rt.settings.pause(True)
    clock(25 * HOUR)
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["attention"] and not rt.store.tickets.get(ticket["id"]).get("answered")
    rt.settings.pause(False)
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["answered"][0]["expired"] is True


# ================================================================ 工单闲置


def idle_request(rt) -> tuple[dict, str]:
    asked = rt.cli.ask("MOCK_WAIT 盯一下这条视频的数据")
    rt.pump()
    [ticket] = [t for t in tickets(rt) if t["mandate"]["strength"] == "explicit"]
    assert ticket_stage(ticket) == {"stage": "idle"}
    return ticket, asked["thread"].split(":", 1)[1]


def test_闲置超过idle_ttl的工单以dropped结束_不打扰人(rt, clock, monkeypatch):
    ticket, _ = idle_request(rt)
    messages = len(sent(rt))
    clock(6 * DAY)                                                 # 默认 7 天
    rt.pump()
    assert "ended" not in rt.store.tickets.get(ticket["id"])
    clock(7 * DAY + 60)
    rt.pump()
    ended = rt.store.tickets.get(ticket["id"])["ended"]
    assert (ended["by"], ended["outcome"], ended["reason"]) == ("executor", "dropped", "闲置超期，没有等到后续事件")
    assert len(sent(rt)) == messages                               # 安静结束，不发消息
    assert len([r for r in rt.store.runs() if r["ticket"] == ticket["id"]]) == 1          # 也不再拉起执行器


def test_闲置结束的工单_人在它的线程上发消息会恢复它(rt, clock, monkeypatch):
    ticket, thread = idle_request(rt)
    with monkeypatch.context() as patched:
        patched.setattr("superops.app.runtime.now_iso", lambda: shift(now_iso(), 8 * DAY))
        rt.pump()
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "dropped"
    rt.cli.ask("回来了，接着看一下", thread=thread)
    rt.pump()
    resumed = rt.store.tickets.get(ticket["id"])
    assert sorted(i["kind"] for i in resumed["inputs"]) == ["resumed", "trigger"] and resumed["undelivered"] == []
    # 同一张工单在原授权下接着做：新的一次派工跑了一轮（假 Agent 看到起点里的 MOCK_WAIT，又报了 waiting）
    assert "ended" not in resumed and len(resumed["assignments"]) == 2 and ticket_stage(resumed) == {"stage": "idle"}
    assert [a["state"] for a in resumed["assignments"]] == ["ended", "running"]
    assert len(tickets(rt)) == 1
    assert len([r for r in rt.store.runs() if r["role"] == "decide"]) == 1                # 恢复不经过主 Session
    assert len([r for r in rt.store.runs() if r["ticket"] == ticket["id"]]) == 2


def test_已结束的脚本工单被人的消息恢复时_不重新执行脚本(rt):
    rt.pipeline.emit({"type": "lead.marked", "subject": "lead:l1"}, trusted=True)
    rt.pump()
    [ticket] = by_rule(rt, "lead-note")
    assert ticket["ended"]["outcome"] == "completed" and len(rt.store.runs(ticket["id"])) == 1
    thread = rt.store.triggers.get(ticket["origin"]["triggerId"])["threads"][0]        # 钉钉里回「T1 …」就落在工单起点的线程上
    thanks = {"id": "ding-thanks", "source": "dingtalk", "threads": [thread], "actor": {"source": "dingtalk", "id": "u1"}, "human": True,
              "addressed": "mention", "at": now_iso(), "text": "T1 谢谢", "meta": {"chat": "chatA", "row": "chatA"},
              "channel": {"source": "dingtalk", "thread": "t1", "address": {"chat": "chatA"}}}
    assert rt.receive(thanks) == "stored"
    rt.pump()
    assert [(r["role"], r["status"]) for r in rt.store.runs(ticket["id"])] == [("script", "ok")]


def test_规则可以有自己的idle_ttl(rt, clock):
    explicit, _ = idle_request(rt)
    rt.pipeline.emit({"type": "watch.start", "subject": "post:p1", "summary": "MOCK_WAIT 盯这条"}, trusted=True)
    rt.pump()
    [watch] = by_rule(rt, "watch")
    assert ticket_stage(watch) == {"stage": "idle"}
    clock(59 * 60)
    rt.pump()
    assert "ended" not in rt.store.tickets.get(watch["id"])
    clock(61 * 60)
    rt.pump()
    assert rt.store.tickets.get(watch["id"])["ended"]["outcome"] == "dropped"             # 规则写的 1 小时
    assert "ended" not in rt.store.tickets.get(explicit["id"])                            # 明确请求用默认的 7 天


def test_等人的工单不算闲置_不会被清掉(rt, clock):
    rt.cli.ask("MOCK_BLOCK 做一个混剪")
    rt.pump()
    [blocked] = tickets(rt)
    assert ticket_stage(blocked) == {"stage": "person", "action": "blocked"}
    clock(30 * DAY)
    rt.pump()
    assert ticket_stage(rt.store.tickets.get(blocked["id"])) == {"stage": "person", "action": "blocked"}


def test_暂停期间不清闲置的工单(rt, clock):
    ticket, _ = idle_request(rt)
    rt.settings.pause(True)
    clock(8 * DAY)
    rt.pump()
    assert "ended" not in rt.store.tickets.get(ticket["id"])
    rt.settings.pause(False)
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "dropped"


# ================================================================ after：等前置规则的工单


def fast_forward(rt, seconds: float) -> None:
    """副作用队列的时钟往后拨：被 Later 推迟的启动到点了。"""
    rt.effects.now = lambda: time.time() + seconds


def collecting(rt) -> dict:
    rt.pipeline.emit({"type": "ops.collect", "subject": "collect:today", "summary": "MOCK_WAIT 采集中"}, trusted=True)
    rt.pump()
    [collect] = by_rule(rt, "collect")
    assert ticket_stage(collect) == {"stage": "idle"}              # 还没结束
    return collect


def test_after规则的工单等前置规则的工单结束才启动(rt):
    collect = collecting(rt)
    rt.pipeline.emit({"type": "ops.brief", "subject": "brief:today"}, trusted=True)
    rt.pump()
    [brief] = by_rule(rt, "brief")
    assert ticket_stage(brief) == {"stage": "working", "action": "queued"}
    assert [r for r in rt.store.runs() if r["ticket"] == brief["id"]] == []
    fast_forward(rt, 60)                                           # 过了一会儿再看：前置还没完，接着等
    rt.pump()
    assert ticket_stage(rt.store.tickets.get(brief["id"])) == {"stage": "working", "action": "queued"}
    assert rt.store.one("SELECT attempts FROM effect WHERE kind='launch'") == 0            # 等待不算失败
    rt.engine.drop_ticket(collect["id"], "采集结束")
    fast_forward(rt, 120)
    rt.pump()
    done_ = rt.store.tickets.get(brief["id"])
    assert done_["ended"]["outcome"] == "completed" and done_["assignments"][-1]["report"]["summary"] == "日报写好了"


def test_after等满after_max就不再等(rt, monkeypatch):
    collect = collecting(rt)
    rt.pipeline.emit({"type": "ops.brief", "subject": "brief:today"}, trusted=True)
    rt.pump()
    [brief] = by_rule(rt, "brief")
    monkeypatch.setattr("superops.executor.worker.now_iso", lambda: shift(now_iso(), 14 * 60))
    fast_forward(rt, 14 * 60)
    rt.pump()
    assert ticket_stage(rt.store.tickets.get(brief["id"])) == {"stage": "working", "action": "queued"}
    monkeypatch.setattr("superops.executor.worker.now_iso", lambda: shift(now_iso(), 15 * 60 + 5))
    fast_forward(rt, 15 * 60 + 5)
    rt.pump()
    assert rt.store.tickets.get(brief["id"])["ended"]["outcome"] == "completed"
    assert "ended" not in rt.store.tickets.get(collect["id"])      # 前置的还在跑


def test_前置规则没有未结束的工单时立刻启动(rt):
    collect = collecting(rt)
    rt.engine.drop_ticket(collect["id"], "采集结束")
    rt.pipeline.emit({"type": "ops.brief", "subject": "brief:today"}, trusted=True)
    rt.pump()
    [brief] = by_rule(rt, "brief")
    assert brief["ended"]["outcome"] == "completed"


# ================================================================ 规则直配：预算、unless_live、没有规则


def latest(rt, thread: str) -> dict:
    return rt.store.decisions.by_thread(thread)[-1]


def test_规则直配的接单把命中的规则写进授权_不调用模型(rt):
    rt.pipeline.emit({"type": "lead.marked", "subject": "lead:l1"}, trusted=True)
    rt.pump()
    [ticket] = by_rule(rt, "lead-note")
    decision = latest(rt, "pipeline:lead:l1")
    assert decision["kind"] == "accept" and decision["reason"] == [{"code": "ruleMatched", "values": {"rule": "线索登记"}}]
    assert decision["audit"] == {"direct": True, "event": "lead.marked", "origin": "pipeline",
                                 "matches": [{"rule_id": "lead-note", "reason": "事件类型直接命中规则的 when"}]}
    assert ticket["mandate"]["strength"] == "inferred" and ticket["mandate"]["basis"]["rules"] == ["lead-note"]
    assert ticket["mandate"]["scope"] == "登记线索状态" and ticket["title"] == "线索登记 · lead:l1"
    assert [r["role"] for r in rt.store.runs()] == ["script"]      # 没有 decide


def test_没有规则命中的系统事件直接忽略(rt):
    rt.pipeline.emit({"type": "deploy.finished", "subject": "web"}, trusted=True)
    rt.pump()
    decision = latest(rt, "pipeline:web")
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "noRuleMatched"}]
    assert decision["audit"] == {"direct": True, "event": "deploy.finished"}
    assert tickets(rt) == [] and rt.store.runs() == []


def test_会超出当日预算的规则被忽略并记原因overBudget(rt):
    rt.pipeline.emit({"type": "content.video.requested", "subject": "video:v1"}, trusted=True)
    rt.pump()
    [first] = by_rule(rt, "video")
    assert first["ended"]["outcome"] == "completed" and rt.counter("credits") == 60   # 脚本报告用了 60 积分
    rt.pipeline.emit({"type": "content.video.requested", "subject": "video:v2"}, trusted=True)
    rt.pump()
    assert len(by_rule(rt, "video")) == 1                          # 60 + 60 > 100
    decision = latest(rt, "pipeline:video:v2")
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "overBudget", "values": {"rule": "生成视频", "budget": "积分"}}]
    assert decision["audit"]["direct"] is True and decision["audit"]["matches"][0]["rule_id"] == "video"
    assert reason_text(decision["reason"]) == "规则「生成视频」会超出今日 积分 预算"
    assert len([r for r in rt.store.runs() if r["role"] == "script"]) == 1


def test_刚好用到预算上限的还能接_没有credits_per_run的规则不受预算影响(rt):
    rt.add_counter("credits", 40)
    rt.pipeline.emit({"type": "content.video.requested", "subject": "video:v1"}, trusted=True)     # 40 + 60 = 100，没超
    rt.pump()
    assert len(by_rule(rt, "video")) == 1 and rt.counter("credits") == 100
    rt.pipeline.emit({"type": "lead.marked", "subject": "lead:l1"}, trusted=True)
    rt.pump()
    assert len(by_rule(rt, "lead-note")) == 1


def test_没配预算时不限制(make_world):
    agents = copy.deepcopy(AGENTS)
    agents["budgets"] = {}
    rt = make_world(policy(), agents)
    for n in (1, 2, 3):
        rt.pipeline.emit({"type": "content.video.requested", "subject": f"video:v{n}"}, trusted=True)
        rt.pump()
    assert len(by_rule(rt, "video")) == 3


def test_unless_live_同一线程上有那条规则的工单还在进行时不另开(rt):
    rt.pipeline.emit({"type": "content.topic.approved", "subject": "content:c1", "summary": "MOCK_WAIT 选题：三步做口播"}, trusted=True)
    rt.pump()
    [writer] = by_rule(rt, "script-writing")
    assert ticket_stage(writer) == {"stage": "idle"}
    rt.pipeline.emit({"type": "content.compliance.failed", "subject": "content:c1", "summary": "MOCK_WAIT 第 2 段有收益承诺"}, trusted=True)
    rt.pump()
    assert by_rule(rt, "rewrite") == []
    decision = latest(rt, "pipeline:content:c1")
    assert decision["kind"] == "ignore" and decision["reason"] == [
        {"code": "continuesTicket", "values": {"rule": "改稿", "ticket": f"T{writer['no']}"}}]
    assert any(i["kind"] == "trigger" for i in rt.store.tickets.get(writer["id"])["inputs"])       # 事件送到了写稿的工单
    # 写稿的工单结束之后，同样的事件就由改稿规则另开一张
    rt.engine.drop_ticket(writer["id"], "不写了")
    rt.pipeline.emit({"type": "content.compliance.failed", "subject": "content:c1", "summary": "第 3 段也有问题"}, trusted=True)
    rt.pump()
    [rewrite] = by_rule(rt, "rewrite")
    assert rewrite["ended"]["outcome"] == "completed"


def test_unless_live只看同一个线程_别的对象上的工单不相干(rt):
    rt.pipeline.emit({"type": "content.topic.approved", "subject": "content:c1", "summary": "MOCK_WAIT 选题"}, trusted=True)
    rt.pump()
    rt.pipeline.emit({"type": "content.compliance.failed", "subject": "content:c2", "summary": "别的稿子"}, trusted=True)
    rt.pump()
    assert len(by_rule(rt, "rewrite")) == 1


# ================================================================ 语义规则的主动候选（主 Session）


def chatter(rt, text: str, mid: str = "m1", actor: str = "u1") -> dict:
    """群里一条没 @ 机器人的消息（钉钉来源收全部消息，名单内的人）：推断候选。返回评估中的候选。"""
    trigger = {"id": f"ding-{mid}", "source": "dingtalk", "threads": [{"source": "dingtalk", "id": f"chatA/{mid}"}],
               "actor": {"source": "dingtalk", "id": actor}, "human": True, "at": now_iso(), "text": text,
               "meta": {"chat": "chatA", "row": "chatA", "message_id": mid},
               "channel": {"source": "dingtalk", "thread": f"chatA/{mid}", "address": {"chat": "chatA"}}}
    assert rt.receive(trigger) == "stored"
    rt.engine.evaluate_due()
    candidate = rt.engine.candidate(trigger["id"])
    assert candidate and candidate["strength"] == "inferred"
    return candidate


def judged(copy_: bool | None = True, visual: bool | None = False, **extra) -> list[dict]:
    out = [{"rule_id": rule, "matched": matched, "reason": f"{rule} 的判断"}
           for rule, matched in (("sem-copy", copy_), ("sem-visual", visual)) if matched is not None]
    return out + [{"rule_id": rule, "matched": matched, "reason": "多出来的"} for rule, matched in extra.items()]


SETTLED = {"requested": True, "settled": True, "evidence": "“标题里‘视屏’是错别字，改成‘视频’”"}


def accept(candidate: dict, **over) -> dict:
    return {"trigger_id": candidate["triggers"][-1]["id"], "kind": "accept", "reason": "明确指出了错别字", "title": "改标题错别字",
            "name": "fix-typo", "scope": "把标题里的“视屏”改成“视频”", "rule_assessments": judged(), "direction": SETTLED, **over}


def test_auto模式下主动候选命中语义规则就接单_命中的规则写进授权(rt):
    candidate = chatter(rt, "标题里“视屏”是错别字，改成“视频”")
    decision = rt.assessor.apply(candidate, accept(candidate, acknowledgement="收到，这就改"))
    assert decision["kind"] == "accept" and decision["strength"] == "inferred"
    assert decision["audit"] == {"rule_assessments": judged(), "direction": SETTLED,
                                 "matches": [{"rule_id": "sem-copy", "reason": "sem-copy 的判断"}]}
    ticket = rt.store.tickets.get(decision["ticketId"])
    mandate = ticket["mandate"]
    assert mandate["strength"] == "inferred" and mandate["basis"]["rules"] == ["sem-copy"]
    assert (mandate["basis"]["via"], mandate["basis"]["cell"], mandate["basis"]["sender"]) == ("admission", "listedUnaddressed", "张三")
    assert mandate["scope"] == "把标题里的“视屏”改成“视频”" and ticket["title"] == "改标题错别字" and ticket["execution"]["name"] == "t1-fix-typo"
    assert rt.settings.policy().revalidate(mandate) is True
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "completed"
    assert [m["kind"] for m in rt.ding_sent] == ["acknowledgement"]                        # 确认发回群里；结果进日报，不打扰人
    assert not rt.effects.pending("assessment")                    # 评估结束，排着的研判被放掉


def test_命中多条语义规则时都写进授权(rt):
    candidate = chatter(rt, "标题有错别字，封面配色也太暗了")
    decision = rt.assessor.apply(candidate, accept(candidate, rule_assessments=judged(True, True)))
    assert rt.store.tickets.get(decision["ticketId"])["mandate"]["basis"]["rules"] == ["sem-copy", "sem-visual"]


@pytest.mark.parametrize("assessments, why", [
    ([], "一条都没判"),
    (judged(True, None), "漏了一条"),
    (judged(True, False) + judged(True, None), "同一条判了两次"),
    (judged(True, None) + judged(True, None), "一条判了两次、另一条没判"),
    (judged(True, False, **{"sem-off": False}), "把停用的规则也判了"),
    (judged(True, False, **{"lead-note": False}), "把带 when 的规则也判了"),
    (judged(True, False, ghost=True), "有不存在的规则"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_rule_assessments必须覆盖每条已启用的语义规则恰好一次(rt, assessments, why):
    candidate = chatter(rt, "标题里有错别字")
    decision = rt.assessor.apply(candidate, accept(candidate, rule_assessments=assessments))
    assert decision["kind"] == "unavailable" and decision["reason"][0]["code"] == "assessFailed", why
    assert "rule_assessments" in decision["reason"][0]["values"]["text"]
    assert tickets(rt) == []


def test_rule_assessments的顺序无所谓(rt):
    candidate = chatter(rt, "标题里有错别字")
    decision = rt.assessor.apply(candidate, accept(candidate, rule_assessments=judged()[::-1]))
    assert decision["kind"] == "accept"


@pytest.mark.parametrize("direction", [None, {}, {"requested": True, "settled": False}, {"requested": False, "settled": True},
                                       {"requested": True}, {"settled": True}])
def test_方向没定的主动候选忽略(rt, direction):
    candidate = chatter(rt, "这个标题是不是可以再想想")
    decision = rt.assessor.apply(candidate, accept(candidate, direction=direction))
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "directionUnsettled"}]
    assert decision["audit"]["rule_assessments"] == judged() and decision["audit"]["matches"] == [{"rule_id": "sem-copy", "reason": "sem-copy 的判断"}]
    assert tickets(rt) == []


def test_没有命中任何语义规则的主动候选忽略(rt):
    candidate = chatter(rt, "帮我订个会议室")
    decision = rt.assessor.apply(candidate, accept(candidate, rule_assessments=judged(False, False)))
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "noRuleMatched"}]
    assert decision["audit"]["matches"] == [] and tickets(rt) == []


def test_observe模式只记录本来会接单_什么都不启动(rt):
    rt.settings.set_mode("observe")
    candidate = chatter(rt, "标题里“视屏”是错别字")
    decision = rt.assessor.apply(candidate, accept(candidate))
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "observed", "values": {"text": "明确指出了错别字"}}]
    audit = decision["audit"]
    assert audit["would_accept"] is True and audit["proposal"] == {"title": "改标题错别字", "scope": "把标题里的“视屏”改成“视频”"}
    assert audit["matches"] == [{"rule_id": "sem-copy", "reason": "sem-copy 的判断"}] and audit["rule_assessments"] == judged()
    rt.pump()
    assert tickets(rt) == [] and rt.ding_sent == []
    assert views.decision_row(rt, decision)["would_accept"] is True
    assert [d["id"] for d in rt.store.decisions.recent(kind="ignore") if (d.get("audit") or {}).get("would_accept")] == [decision["id"]]


def test_observe模式下方向没定或没命中规则的_不算本来会接单(rt):
    rt.settings.set_mode("observe")
    first = chatter(rt, "随便聊聊", mid="m1")
    unsettled = rt.assessor.apply(first, accept(first, direction={"requested": False, "settled": False}))
    second = chatter(rt, "订会议室", mid="m2")
    unmatched = rt.assessor.apply(second, accept(second, rule_assessments=judged(False, False)))
    assert [unsettled["reason"][0]["code"], unmatched["reason"][0]["code"]] == ["directionUnsettled", "noRuleMatched"]
    assert "would_accept" not in unsettled["audit"] and "would_accept" not in unmatched["audit"]


def test_主Session对主动候选的忽略_带着逐条判断就记进审计_不带的标成从卡片直接忽略(rt):
    first = chatter(rt, "今天中午吃什么", mid="m1")
    plain = rt.assessor.apply(first, {"trigger_id": first["triggers"][-1]["id"], "kind": "ignore", "reason": "普通闲聊"})
    assert plain["kind"] == "ignore" and [r["code"] for r in plain["reason"]] == ["ignoredFromCard", "said"] and "audit" not in plain
    second = chatter(rt, "这个颜色还行吧", mid="m2")
    assessed = rt.assessor.apply(second, {"trigger_id": second["triggers"][-1]["id"], "kind": "ignore", "reason": "只是感叹",
                                          "rule_assessments": judged(False, False)})
    assert [r["code"] for r in assessed["reason"]] == ["said"] and assessed["audit"] == {"rule_assessments": judged(False, False)}


def test_明确请求不需要逐条判断规则_任何模式下都接(rt):
    rt.settings.set_mode("observe")
    asked = rt.cli.ask("MOCK_WAIT 帮我看看昨天那条视频")
    rt.engine.evaluate_due()
    candidate = rt.engine.candidate(asked["id"])
    decision = rt.assessor.apply(candidate, {"trigger_id": asked["id"], "kind": "accept", "reason": "名单内的人明确提出", "title": "看视频",
                                             "scope": "按消息所说"})
    assert decision["kind"] == "accept" and decision["strength"] == "explicit" and "rules" not in decision["mandate"]["basis"]


def test_被暂停的语义规则不接单(rt):
    rt.settings.pause(True, "rule:sem-copy", reason="先停一下")
    candidate = chatter(rt, "标题里“视屏”是错别字")
    decision = rt.assessor.apply(candidate, accept(candidate))
    assert decision["kind"] != "accept" and tickets(rt) == []


def test_没有启用的语义规则时_人的主动候选直接忽略_不调用模型(make_world):
    rt = make_world(policy([r for r in RULES if r.get("when") or r.get("enabled") is False]))
    rt.dingtalk.send = lambda message: "sent"
    trigger = {"id": "ding-m1", "source": "dingtalk", "threads": [{"source": "dingtalk", "id": "chatA/m1"}],
               "actor": {"source": "dingtalk", "id": "u1"}, "human": True, "at": now_iso(), "text": "标题里有错别字",
               "meta": {"chat": "chatA", "row": "chatA"}}
    rt.receive(trigger)
    rt.pump()
    [decision] = rt.store.decisions.by_thread("dingtalk:chatA/m1")
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "noRuleMatched"}] and decision["audit"] == {"direct": True}
    assert rt.store.runs() == []


# ---------------------------------------------------------------- 经主 Session 走完整条路（给假 Agent 准备输出）


ROUTER = str(Path(__file__).with_name("route_agent.py"))


@pytest.fixture
def routed(make_world, tmp_path):
    """主 Session 换成 route_agent.py：它输出 answer 文件里写好的决定。"""
    answer = tmp_path / "route.json"
    agents = copy.deepcopy(AGENTS)
    agents["agents"]["router"] = {"type": "command", "bin": sys.executable, "args": [ROUTER, str(answer)], "concurrency": 2}
    agents["fallback_chains"]["decide"] = ["router"]
    world = make_world(policy(), agents)
    world.dingtalk.send = lambda message: "sent"
    world.say = lambda value: answer.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return world


def heard(rt, text: str, mid: str = "m1") -> str:
    trigger = {"id": f"ding-{mid}", "source": "dingtalk", "threads": [{"source": "dingtalk", "id": f"chatA/{mid}"}],
               "actor": {"source": "dingtalk", "id": "u1"}, "human": True, "at": now_iso(), "text": text,
               "meta": {"chat": "chatA", "row": "chatA", "message_id": mid},
               "channel": {"source": "dingtalk", "thread": f"chatA/{mid}", "address": {"chat": "chatA"}}}
    assert rt.receive(trigger) == "stored"
    return f"dingtalk:chatA/{mid}"


def test_主动候选经主Session研判后接单(routed):
    rt = routed
    rt.say([{"trigger_id": "$TRIGGER", "kind": "accept", "reason": "明确指出了错别字", "title": "改标题错别字", "scope": "改错别字",
             "rule_assessments": judged(), "direction": SETTLED}])
    thread = heard(rt, "标题里“视屏”是错别字，改成“视频”")
    rt.pump()
    decision = latest(rt, thread)
    assert decision["kind"] == "accept" and decision["audit"]["matches"] == [{"rule_id": "sem-copy", "reason": "sem-copy 的判断"}]
    [ticket] = tickets(rt)
    assert ticket["mandate"]["basis"]["rules"] == ["sem-copy"] and ticket["ended"]["outcome"] == "completed"
    assert [r["role"] for r in rt.store.runs()][::-1] in (["decide", "write"], ["write", "decide"])
    prompt = next(p["prompt"] for p in prompts_seen(rt) if "## 起点" in p["prompt"] or "这张工单" in p["prompt"])
    assert "群里有人明确指出错别字" in prompt                       # 执行器拿到了命中的规则的说明


def test_研判的输出不合格_重试后还不行_记为unavailable(routed):
    rt = routed
    rt.say("INVALID")
    thread = heard(rt, "标题里有错别字")
    rt.pump()
    [decision] = rt.store.decisions.by_thread(thread)
    assert decision["kind"] == "unavailable" and decision["reason"][0]["code"] == "assessFailed"
    assert [(r["role"], r["status"]) for r in rt.store.runs()] == [("decide", "invalid")] * 2      # 同一个 Agent 带着错误重试了一次
    assert tickets(rt) == []


def test_研判给出不属于这个候选的trigger_id_算不合格(routed):
    rt = routed
    rt.say([{"trigger_id": "someone-elses-trigger", "kind": "accept", "reason": "x", "title": "x", "rule_assessments": judged(), "direction": SETTLED}])
    thread = heard(rt, "标题里有错别字")
    rt.pump()
    [decision] = rt.store.decisions.by_thread(thread)
    assert decision["kind"] == "unavailable" and "输出不符合要求" in decision["reason"][0]["values"]["text"]
    assert "trigger_id" in rt.store.runs()[0]["error_detail"] and tickets(rt) == []


def test_研判给出空的decisions_算不合格(routed):
    rt = routed
    rt.say([])
    thread = heard(rt, "标题里有错别字")
    rt.pump()
    assert latest(rt, thread)["kind"] == "unavailable" and "decisions 不能为空" in rt.store.runs()[0]["error_detail"]


# ================================================================ 研判失败：unavailable、面板问题、dismiss


def failed_assessment(rt, mid: str = "m1") -> dict:
    candidate = chatter(rt, "标题里有错别字", mid=mid)
    decision = rt.assessor.apply(candidate, accept(candidate, rule_assessments=[]))
    assert decision["kind"] == "unavailable"
    return decision


def test_研判失败的评估出现在面板的问题列表里_dismiss后消失(rt):
    assert views.problems(rt) == []
    decision = failed_assessment(rt)
    [problem] = views.problems(rt)
    assert {k: problem[k] for k in ("kind", "title", "decision_id", "trigger_id", "thread")} == {
        "kind": "eval", "title": "研判没有完成", "decision_id": decision["id"], "trigger_id": "ding-m1", "thread": "dingtalk:chatA/m1"}
    assert problem["detail"].startswith("研判没有完成：") and "rule_assessments" in problem["detail"]
    rt.run_pass()
    assert rt.health.get("evals")["state"] == "error" and "1 次研判没有完成" in rt.health.get("evals")["detail"]
    assert any(p["kind"] == "health" and p["title"] == "evals" for p in views.problems(rt))
    dismissed = rt.engine.dismiss(decision["id"])
    assert dismissed["dismissed"] is True
    rt.run_pass()
    assert views.problems(rt) == [] and rt.health.get("evals")["state"] == "ready"
    assert views.decision_row(rt, rt.store.decisions.get(decision["id"]))["dismissed"] is True


def test_只有unavailable的决定可以dismiss(rt):
    from superops.core.errors import UserError
    candidate = chatter(rt, "今天中午吃什么")
    ignored = rt.assessor.apply(candidate, {"trigger_id": candidate["triggers"][-1]["id"], "kind": "ignore", "reason": "闲聊"})
    for decision_id in (ignored["id"], "no-such-decision"):
        with pytest.raises(UserError) as error:
            rt.engine.dismiss(decision_id)
        assert error.value.code == "unknownProblem"


def test_重新评估之后问题从面板上消失_新的决定出来后旧的不再算问题(rt):
    decision = failed_assessment(rt)
    time.sleep(0.005)                                              # 评估的 id 含毫秒时间戳；人不会在同一毫秒里点“重新评估”
    assert rt.engine.reevaluate("ding-m1") is True
    assert views.problems(rt) == []                                # 线程又在排队了
    rt.engine.evaluate_due()
    candidate = rt.engine.candidate("ding-m1")
    again = rt.assessor.apply(candidate, accept(candidate))
    assert again["kind"] == "accept" and again["id"] != decision["id"]
    assert [p for p in views.problems(rt) if p["kind"] == "eval"] == []


def test_面板只列24小时内的研判失败_健康项只看最近一小时(rt, clock, monkeypatch):
    failed_assessment(rt)
    monkeypatch.setattr("superops.app.views.now_iso", lambda: shift(now_iso(), 23 * HOUR))
    assert len(views.problems(rt)) == 1
    monkeypatch.setattr("superops.app.views.now_iso", lambda: shift(now_iso(), 25 * HOUR))
    assert views.problems(rt) == []
    clock(2 * HOUR)
    rt.run_pass()
    assert rt.health.get("evals")["state"] == "ready"


def test_研判方都不可用时这次评估记为unavailable(make_world):
    agents = copy.deepcopy(AGENTS)
    agents["agents"]["router"] = {"type": "command", "bin": sys.executable, "args": ["-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"]}
    agents["fallback_chains"]["decide"] = ["router"]
    rt = make_world(policy(), agents)
    asked = rt.cli.ask("帮我看看昨天那条视频")
    rt.pump()
    [decision] = rt.store.decisions.by_thread(asked["thread"])
    assert decision["kind"] == "unavailable" and decision["strength"] == "explicit" and decision["reason"][0]["code"] == "assessFailed"
    assert "boom" in decision["reason"][0]["values"]["text"]
    assert tickets(rt) == [] and [p["decision_id"] for p in views.problems(rt) if p["kind"] == "eval"] == [decision["id"]]
    assert rt.health.get("evals")["state"] == "error"
    assert not rt.effects.pending("assessment")                    # 不会一直重试；等下一条触发或人来重新评估


# ================================================================ 主 Session 的 attach / stop


def explicit(rt, text: str, thread: str | None = None) -> tuple[dict, dict]:
    asked = rt.cli.ask(text, thread=thread)
    rt.engine.evaluate_due()
    return rt.engine.candidate(asked["id"]), asked


def test_attach把消息转给已有的工单_不另开工单(rt):
    ticket, _ = idle_request(rt)
    candidate, asked = explicit(rt, "昨天那条再帮我对比一下前天的")
    result = rt.assessor.apply(candidate, {"trigger_id": asked["id"], "kind": "attach", "ticket_id": ticket["id"], "note": "接着做",
                                           "reason": "明确指向这张工单"})
    assert result["id"] == ticket["id"]
    [decision] = rt.store.decisions.by_thread(asked["thread"])
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "routed", "values": {"ticket": ticket["id"]}}]
    attached = rt.store.tickets.get(ticket["id"])
    item = next(i for i in attached["inputs"] if i["kind"] == "trigger")
    assert (item["triggerId"], item["note"], item["human"]) == (asked["id"], "接着做", True)
    assert rt.store.threads.get(asked["thread"])["subscriptions"] == [ticket["id"]]        # 工单订阅了这条消息的线程
    assert rt.store.triggers.disposition(asked["id"])["fanout"] == [ticket["id"]]
    rt.pump()
    assert len(tickets(rt)) == 1 and rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "completed"
    assert len([r for r in rt.store.runs() if r["ticket"] == ticket["id"]]) == 2


def test_attach经假Agent走完整条路(rt):
    ticket, _ = idle_request(rt)
    asked = rt.cli.ask(f"转给 {ticket['id']} 再看一下前天的")
    rt.pump()
    assert len(tickets(rt)) == 1 and rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "completed"
    assert rt.store.decisions.by_thread(asked["thread"])[-1]["reason"][0]["code"] == "routed"


@pytest.mark.parametrize("case", ["不存在的工单", "被人停掉的工单"])
def test_attach被引擎拒绝时记为unavailable(rt, case):
    ticket, _ = idle_request(rt)
    target = "0" * 32
    if case == "被人停掉的工单":
        rt.engine.stop_ticket(ticket["id"], "不用盯了")
        target = ticket["id"]
    candidate, asked = explicit(rt, "接着那张工单做")
    decision = rt.assessor.apply(candidate, {"trigger_id": asked["id"], "kind": "attach", "ticket_id": target, "reason": "指向那张工单"})
    assert decision["kind"] == "unavailable" and decision["reason"][0]["code"] == "assessFailed"
    assert decision["reason"][0]["values"]["text"].startswith("attach 被拒绝：")
    assert len(tickets(rt)) == 1 and [p["kind"] for p in views.problems(rt)] == ["eval"]


def test_stop由名单内的人明确提出时停掉工单(rt):
    ticket, _ = idle_request(rt)
    candidate, asked = explicit(rt, "那条不用盯了")
    decision = rt.assessor.apply(candidate, {"trigger_id": asked["id"], "kind": "stop", "ticket_id": ticket["id"], "reason": "人要求停止"})
    assert (decision["kind"], decision["ticketId"]) == ("stop", ticket["id"])
    ended = rt.store.tickets.get(ticket["id"])["ended"]
    assert (ended["by"], ended["outcome"], ended["reason"]) == ("human", "stopped", "人要求停止")
    rt.pump()
    assert len(tickets(rt)) == 1


def test_stop经假Agent走完整条路(rt):
    ticket, _ = idle_request(rt)
    rt.cli.ask(f"停掉 {ticket['id']}")
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "stopped" and len(tickets(rt)) == 1


def test_stop指向不存在或已结束的工单_记为忽略(rt):
    ticket, _ = idle_request(rt)
    rt.engine.drop_ticket(ticket["id"], "结束了")
    for n, target in enumerate(("0" * 32, ticket["id"], None)):
        candidate, asked = explicit(rt, f"把那张停了 {n}")
        decision = rt.assessor.apply(candidate, {"trigger_id": asked["id"], "kind": "stop", "ticket_id": target, "reason": "人要求停止"})
        assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "stopUnknown"}]
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "dropped"


def test_推断的候选不能停工单(rt):
    ticket, _ = idle_request(rt)
    candidate = chatter(rt, "那个盯数据的可以停了吧")
    decision = rt.assessor.apply(candidate, {"trigger_id": candidate["triggers"][-1]["id"], "kind": "stop", "ticket_id": ticket["id"],
                                             "reason": "有人说可以停"})
    assert decision["kind"] == "ignore" and decision["reason"] == [{"code": "stopNotExplicit"}]
    assert "ended" not in rt.store.tickets.get(ticket["id"])


def test_评估已经结束后再来的决定不产生新的决定(rt):
    candidate = chatter(rt, "标题里有错别字")
    first = rt.assessor.apply(candidate, accept(candidate))
    assert first["kind"] == "accept"
    assert rt.assessor.apply(candidate, accept(candidate)) is None                         # 同一次评估的第二个决定被拒绝
    assert len(rt.store.decisions.by_thread("dingtalk:chatA/m1")) == 1 and len(tickets(rt)) == 1
