"""定时来源（superops/sources/schedule.py）和流水线来源（superops/sources/pipeline.py）。

定时来源用一个最小的假宿主和可注入的时钟；流水线来源接在真的同步 Runtime 上（它要用引擎、设置和发件箱）。
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta

import pytest
import yaml

from conftest import AGENTS
from worlds import base_policy, by_rule, decisions, done, make_world, sent, tickets  # noqa: F401

from superops.app.health import HealthBoard
from superops.app.store import Store
from superops.infra.timeutil import TZ, iso
from superops.core import ticket_stage
from superops.core.model import ms, now_iso, shift
from superops.core.reasons import key
from superops.sources.schedule import ScheduleSource


# ================================================================ 定时来源


class FakeHost:
    """ScheduleSource 用到的宿主窄接口：store / cfg / home / health / receive。"""

    def __init__(self, tmp_path, schedules: list[dict], cfg: dict | None = None):
        self.home = tmp_path
        (tmp_path / "schedules.yaml").write_text(yaml.safe_dump(schedules, allow_unicode=True), encoding="utf-8")
        self.store = Store(tmp_path / "db.sqlite")
        self.cfg = cfg or {}
        self.health = HealthBoard()
        self.received: list[dict] = []
        self.result = "stored"

    def receive(self, trigger: dict) -> str:
        self.received.append(trigger)
        return self.result


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def set(self, hour: int, minute: int = 0, second: int = 0, day: int | None = None) -> None:
        self.at = self.at.replace(hour=hour, minute=minute, second=second, **({"day": day} if day else {}))


def local(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)        # 2026-11-11 是周三


@pytest.fixture
def cron(tmp_path):
    made = []

    def make(schedules: list[dict], *, at: datetime = local(11, 8, 0), cfg: dict | None = None):
        host = FakeHost(tmp_path, schedules, cfg)
        clock = Clock(at)
        source = ScheduleSource(host, now=clock)
        made.append(host)
        return source, host, clock

    yield make
    for host in made:
        host.store.close()


def test_第一次见到某个定时不补发_从下一次开始(cron):
    source, host, clock = cron([{"type": "cron.daily_brief", "at": "08:30"}], at=local(11, 9, 0))   # 今天 08:30 已经过了
    source.tick()
    source.tick()
    assert host.received == []
    assert host.store.get("schedule:cron.daily_brief") == {"last_due": iso(local(11, 8, 30)), "fired_at": None}
    clock.set(8, 29, 59, day=12)
    source.tick()
    assert host.received == []
    clock.set(8, 30, 0)
    source.tick()
    assert [t["meta"]["type"] for t in host.received] == ["cron.daily_brief"]


def test_到点触发一次且只一次(cron):
    source, host, clock = cron([{"type": "cron.daily_brief", "at": "08:30"}])
    source.tick()                                                  # 08:00 第一次见到
    for minute, second in ((30, 0), (30, 2), (31, 0), (59, 59)):
        clock.set(8, minute, second)
        source.tick()
    assert len(host.received) == 1
    clock.set(8, 29, day=12)
    source.tick()
    assert len(host.received) == 1
    clock.set(8, 30)
    source.tick()
    source.tick()
    assert len(host.received) == 2 and host.received[0]["id"] != host.received[1]["id"]
    assert host.store.get("schedule:cron.daily_brief") == {"last_due": iso(local(12, 8, 30)), "fired_at": iso(local(12, 8, 30))}


def test_定时触发的形状_线程按定时类型_没有发送人_时间是应触发的时刻(cron):
    source, host, clock = cron([{"type": "cron.collect", "at": "08:30", "data": {"platforms": ["douyin"]}}])
    source.tick()
    clock.set(8, 30, 40)                                           # 晚了 40 秒才轮到这一轮 pass
    source.tick()
    due = local(11, 8, 30)
    assert host.received == [{
        "id": key("schedule", "cron.collect", iso(due)), "source": "schedule", "threads": [{"source": "schedule", "id": "cron.collect"}],
        "actor": {"source": "schedule", "id": ""}, "human": False, "at": due.isoformat(), "text": "定时触发 cron.collect",
        "meta": {"type": "cron.collect", "origin": "cron", "subject": "cron.collect", "due": iso(due), "data": {"platforms": ["douyin"]}}}]


def test_没写data的定时_触发里不带data(cron):
    source, host, clock = cron([{"type": "cron.x", "at": "08:30"}])
    source.tick()
    clock.set(8, 30)
    source.tick()
    assert "data" not in host.received[0]["meta"]


def test_错过超过max_late不补_之后也不会再补这一次(cron):
    source, host, clock = cron([{"type": "cron.login_check", "at": "16:30", "max_late": "30m"}])
    source.tick()
    clock.set(17, 0, 1)                                            # 机器睡过去了，晚了 30 分 1 秒
    source.tick()
    source.tick()
    assert host.received == []
    assert host.store.get("schedule:cron.login_check")["last_due"] == iso(local(11, 16, 30))
    clock.set(16, 30, day=12)                                      # 第二天照常
    source.tick()
    assert len(host.received) == 1 and host.received[0]["meta"]["due"] == iso(local(12, 16, 30))


def test_错过但没超过max_late的_醒来后补发最近一次(cron):
    source, host, clock = cron([{"type": "cron.login_check", "at": "16:30", "max_late": "30m"}, {"type": "cron.brief", "at": "08:30"}])
    source.tick()
    clock.set(17, 0, 0)                                            # 正好晚 30 分钟：还补
    source.tick()
    assert [(t["meta"]["type"], t["meta"]["due"]) for t in host.received] == [
        ("cron.login_check", iso(local(11, 16, 30))), ("cron.brief", iso(local(11, 8, 30)))]   # 默认 max_late 是 12 小时
    assert host.received[0]["at"] == local(11, 16, 30).isoformat()


def test_睡过了好几个周期_只补最近的一次(cron):
    source, host, clock = cron([{"type": "cron.collect", "every": "30m"}], at=local(11, 8, 10))
    source.tick()
    clock.set(11, 47)                                              # 错过了 08:30 … 11:30 七次
    source.tick()
    assert [t["meta"]["due"] for t in host.received] == [iso(local(11, 11, 30))]


def test_默认max_late是12小时(cron):
    source, host, clock = cron([{"type": "cron.brief", "at": "08:30"}])
    source.tick()
    clock.set(20, 30, 1)
    source.tick()
    assert host.received == []


def test_requires指向的配置为空时不触发_配上之后才触发(cron):
    schedules = [{"type": "cron.ci_poll", "every": "30m", "requires": "ci.projects"}, {"type": "cron.login", "at": "08:30", "requires": "publishers"}]
    source, host, clock = cron(schedules, cfg={"ci": {"projects": []}})
    source.tick()
    clock.set(8, 30)
    source.tick()
    assert host.received == [] and host.store.get("schedule:cron.ci_poll") is None
    host.cfg.update({"ci": {"projects": ["popto/web"]}, "publishers": {"douyin": {"enabled": True}}})
    source.tick()                                                  # 配上之后算第一次见到：不补之前的
    assert host.received == []
    clock.set(9, 0)
    source.tick()
    assert [t["meta"]["type"] for t in host.received] == ["cron.ci_poll"]
    host.cfg["ci"] = {}                                            # 又清空了：到点也不触发
    clock.set(9, 30)
    source.tick()
    assert len(host.received) == 1


def test_every按间隔触发_从零点对齐(cron):
    source, host, clock = cron([{"type": "cron.collect", "every": "30m"}], at=local(11, 8, 10))
    source.tick()
    assert host.store.get("schedule:cron.collect")["last_due"] == iso(local(11, 8, 0))
    for hour, minute in ((8, 29), (8, 30), (8, 45), (9, 0), (9, 1)):
        clock.set(hour, minute)
        source.tick()
    assert [t["meta"]["due"] for t in host.received] == [iso(local(11, 8, 30)), iso(local(11, 9, 0))]


def test_每周的定时只在那一天触发(cron):
    source, host, clock = cron([{"type": "cron.weekly_review", "at": "MON 09:00"}], at=local(11, 8, 0))    # 周三
    source.tick()
    assert host.store.get("schedule:cron.weekly_review")["last_due"] == iso(local(9, 9, 0))               # 上周一
    for day in (12, 13, 14, 15):
        clock.set(9, 0, day=day)
        source.tick()
    assert host.received == []
    clock.set(9, 0, day=16)                                        # 周一
    source.tick()
    assert [t["meta"]["due"] for t in host.received] == [iso(local(16, 9, 0))]


def test_各个定时互不影响_健康板报告定时个数和刚触发的(cron):
    source, host, clock = cron([{"type": "cron.a", "at": "08:30"}, {"type": "cron.b", "at": "09:00"}])
    source.tick()
    assert host.health.get("source:schedule")["state"] == "ready" and host.health.get("source:schedule")["detail"] == "2 个定时"
    clock.set(8, 30)
    source.tick()
    assert [t["meta"]["type"] for t in host.received] == ["cron.a"]
    assert host.health.get("source:schedule")["detail"] == "2 个定时，刚触发 cron.a"
    clock.set(9, 0)
    source.tick()
    assert [t["meta"]["type"] for t in host.received] == ["cron.a", "cron.b"]


def test_手动fire_同一个时刻的id相同_由引擎去重(cron):
    source, host, clock = cron([])
    first = source.fire("cron.daily_brief", local(11, 8, 30))
    host.result = "duplicate"
    again = source.fire("cron.daily_brief", local(11, 8, 30))
    assert first == {"id": key("schedule", "cron.daily_brief", iso(local(11, 8, 30))), "result": "stored"}
    assert again == {"id": first["id"], "result": "duplicate"}
    now = source.fire("cron.daily_brief")                          # 不给时刻就是现在
    assert now["id"] != first["id"] and host.received[-1]["meta"]["due"] == iso(local(11, 8, 0))
    assert source.fire("cron.x", data={"k": 1})["id"] and host.received[-1]["meta"]["data"] == {"k": 1}


def test_定时来源是系统来源_没有可回复的会话_reload重读文件(cron):
    source, host, clock = cron([{"type": "cron.a", "at": "08:30"}])
    assert source.scope({}) == {"listened": True, "observed": True, "row": "schedule", "system": True}
    assert source.send({"id": "m", "channel": {"source": "schedule"}, "text": "x", "kind": "reply"}) == "ignored"
    assert [s["type"] for s in source.specs()] == ["cron.a"]
    (host.home / "schedules.yaml").write_text(yaml.safe_dump([{"type": "cron.b", "at": "09:00"}]), encoding="utf-8")
    assert [s["type"] for s in source.specs()] == ["cron.a"]       # 不 reload 用缓存的
    source.reload()
    assert [s["type"] for s in source.specs()] == ["cron.b"]


# ================================================================ 流水线来源


PUBLISH = "import json,os; t=json.load(open(os.environ['OPS_TASK_FILE'])); e=t['events'][0]; print(json.dumps({'status':'done','summary':'已发布 '+e['data']['pack']+' 批准人 '+e['data'].get('approved',{}).get('by','无')}))"
RULES = [
    {"id": "anomaly", "name": "异常复盘", "instruction": "复盘异常", "when": {"types": ["social.metrics.anomaly"]}, "run": done("复盘完了")},
    {"id": "comment-triage", "name": "评论分拣", "instruction": "分拣评论", "when": {"types": ["social.comment.new"]}, "debounce": "10m",
     "run": done("分拣完了")},
    {"id": "comment-fast", "name": "评论快报", "instruction": "x", "when": {"types": ["social.comment.*"]}, "debounce": "2m", "run": {"executor": "none"}},
    {"id": "comment-off", "name": "停用的", "instruction": "x", "when": {"types": ["social.comment.new"]}, "debounce": "3h", "enabled": False,
     "run": {"executor": "none"}},
    {"id": "publish-run", "name": "执行发布", "instruction": "执行人批准的发布", "when": {"types": ["content.publish.approved"]},
     "requires_approval": True, "run": {"executor": "script", "command": ["{python}", "-c", PUBLISH]}},
    {"id": "cleanup", "name": "清理", "instruction": "清理过期文件", "when": {"types": ["ops.cleanup.approved"]}, "requires_approval": True,
     "enabled": False, "run": {"executor": "none"}},
]


@pytest.fixture
def rt(make_world):
    return make_world(base_policy(RULES))


def stored(rt, result: dict) -> dict:
    return rt.store.triggers.get(result["id"])


def test_流水线事件变成触发_线程按对象划分(rt):
    result = rt.pipeline.emit({"source": "collector", "type": "social.metrics.anomaly", "subject": "post:p9", "summary": "播放异常",
                               "link": "https://example.com/p9", "payload_ref": "runs/x/payload.json", "data": {"views": 12},
                               "occurred_at": "2026-11-11T08:00:00+08:00"}, trusted=True)
    assert result["routed"] == "trigger"
    trigger = stored(rt, result)
    assert trigger["source"] == "pipeline" and trigger["threads"] == [{"source": "pipeline", "id": "post:p9"}]
    assert trigger["actor"] == {"source": "pipeline", "id": ""} and trigger["human"] is False
    assert trigger["at"] == "2026-11-11T00:00:00.000Z" and trigger["text"] == "播放异常" and trigger["url"] == "https://example.com/p9"
    assert trigger["meta"] == {"type": "social.metrics.anomaly", "origin": "collector", "subject": "post:p9", "data": {"views": 12},
                               "link": "https://example.com/p9", "payload_ref": "runs/x/payload.json"}
    other = rt.pipeline.emit({"type": "social.metrics.anomaly", "subject": "post:p10"}, trusted=True)
    same = rt.pipeline.emit({"type": "social.metrics.recovered", "subject": "post:p9"}, trusted=True)
    assert stored(rt, other)["threads"] == [{"source": "pipeline", "id": "post:p10"}]
    assert [t["id"] for t in rt.store.thread_triggers("pipeline:post:p9")] == [same["id"], result["id"]]     # 同一对象的事件在同一个线程
    assert rt.store.threads.get("pipeline:post:p10") is not None


def test_没有对象的事件_线程按事件类型_缺省的来源和正文(rt):
    result = rt.pipeline.emit({"type": "deploy.finished"}, trusted=True)
    trigger = stored(rt, result)
    assert trigger["threads"] == [{"source": "pipeline", "id": "deploy.finished"}] and trigger["text"] == "deploy.finished"
    assert trigger["meta"] == {"type": "deploy.finished", "origin": "pipeline", "data": {}} and "url" not in trigger
    assert abs(ms(trigger["at"]) - ms(now_iso())) < 5000


def test_事件数据不是字典时包一层_正文截到500字_带上产出方和所属工单(rt):
    result = rt.pipeline.emit({"type": "x.y", "data": [1, 2], "summary": "长" * 600}, by_ticket="ticket-1", producer="claude")
    trigger = stored(rt, result)
    assert trigger["meta"]["data"] == {"value": [1, 2], "producer": "claude"} and len(trigger["text"]) == 500
    assert trigger["byTicket"] == "ticket-1"
    kept = stored(rt, rt.pipeline.emit({"type": "x.y", "data": {"producer": "codex"}}, producer="claude"))
    assert kept["meta"]["data"]["producer"] == "codex"             # 事件自己写了产出方就不覆盖


def test_没有type的事件被拒绝(rt):
    with pytest.raises(ValueError):
        rt.pipeline.emit({"subject": "post:p1"})
    with pytest.raises(ValueError):
        rt.pipeline.emit({"type": ""})


def test_dedup_key相同的事件只收一次(rt):
    event = {"type": "social.metrics.anomaly", "subject": "post:p9", "dedup_key": "anomaly:p9:2026-11-11"}
    first = rt.pipeline.emit(event, trusted=True)
    again = rt.pipeline.emit({**event, "summary": "重发"}, trusted=True)
    assert first == {"id": key("pipeline", "anomaly:p9:2026-11-11"), "routed": "trigger"}
    assert again == {"id": first["id"], "routed": "duplicate"}
    assert len(rt.store.thread_triggers("pipeline:post:p9")) == 1
    rt.pump()
    assert len(by_rule(rt, "anomaly")) == 1
    # 不带 dedup_key 的每次都是新事件
    a = rt.pipeline.emit({"type": "x.y", "subject": "s"}, trusted=True)
    b = rt.pipeline.emit({"type": "x.y", "subject": "s"}, trusted=True)
    assert a["id"] != b["id"] and a["routed"] == b["routed"] == "trigger"


def test_规则的debounce变成触发的notBefore_到点之前不评估(rt):
    before = now_iso()
    result = rt.pipeline.emit({"type": "social.comment.new", "subject": "post:p1", "summary": "第 1 条评论"}, trusted=True)
    trigger = stored(rt, result)
    # 命中的已启用规则里最长的那个（10 分钟）；停用规则的 3 小时不算
    assert ms(shift(before, 600)) <= ms(trigger["notBefore"]) <= ms(shift(now_iso(), 600))
    assert rt.store.threads.get("pipeline:post:p1")["holdUntil"] == trigger["notBefore"]
    rt.pump()
    thread = rt.store.threads.get("pipeline:post:p1")
    assert thread["pending"] == [result["id"]] and not thread.get("evaluating")
    assert tickets(rt) == [] and decisions(rt, "pipeline:post:p1") == []
    # 没有 debounce 的规则命中的事件不带 notBefore，马上评估
    quick = stored(rt, rt.pipeline.emit({"type": "social.metrics.anomaly", "subject": "post:p2"}, trusted=True))
    assert "notBefore" not in quick
    rt.pump()
    assert len(by_rule(rt, "anomaly")) == 1 and by_rule(rt, "comment-triage") == []


def test_debounce期间同一对象的事件攒成一批_到点后只开一张工单(rt):
    ids = [rt.pipeline.emit({"type": "social.comment.new", "subject": "post:p1", "summary": f"第 {n} 条评论"}, trusted=True)["id"] for n in (1, 2, 3)]
    rt.pump()
    assert tickets(rt) == []
    hold = rt.store.threads.get("pipeline:post:p1")["holdUntil"]
    assert hold == max(rt.store.triggers.get(i)["notBefore"] for i in ids)                # 等到最后一条的防抖到点
    rt.engine.options.now = lambda: shift(now_iso(), 601)          # 10 分钟过去了
    rt.pump()
    [ticket] = by_rule(rt, "comment-triage")
    assert ticket["origin"]["triggerIds"] == ids and ticket["ended"]["outcome"] == "completed"
    assert "holdUntil" not in rt.store.threads.get("pipeline:post:p1")


def test_需要批准的事件没有批准凭据时记为filtered(rt):
    result = rt.pipeline.emit({"type": "content.publish.approved", "subject": "pack:x", "data": {"pack": "x"}}, by_ticket="some-ticket", producer="claude")
    assert result["routed"] == "filtered"
    trigger = stored(rt, result)
    assert trigger["meta"]["unapproved"] is True
    assert rt.store.triggers.disposition(result["id"]) == {"filtered": {"code": "needs_approval"}, "fanout": [],
                                                           "at": rt.store.triggers.disposition(result["id"])["at"]}
    rt.pump()
    assert tickets(rt) == [] and decisions(rt) == []               # 被来源过滤的不进评估
    assert rt.store.threads.get("pipeline:pack:x") is None


def test_确定性脚本发的需要批准的事件可以通过(rt):
    result = rt.pipeline.emit({"type": "content.publish.approved", "subject": "pack:x", "data": {"pack": "x"}}, trusted=True)
    assert result["routed"] == "trigger" and "unapproved" not in stored(rt, result)["meta"] and "approved" not in stored(rt, result)["meta"]
    rt.pump()
    [run] = by_rule(rt, "publish-run")
    assert run["assignments"][-1]["report"]["summary"] == "已发布 x 批准人 无"


def test_带着人的批准凭据的事件可以通过_凭据跟着事件到执行器(rt):
    approved = {"ticket": "T1", "ticketId": "t-card", "attentionId": "att-1", "option": "approve", "label": "发", "by": "u1", "at": now_iso()}
    result = rt.pipeline.emit({"type": "content.publish.approved", "subject": "pack:y", "data": {"pack": "y"}}, approved=approved, by_ticket="t-card")
    assert result["routed"] == "trigger" and stored(rt, result)["meta"]["approved"] == approved
    rt.pump()
    [run] = by_rule(rt, "publish-run")
    assert run["assignments"][-1]["report"]["summary"] == "已发布 y 批准人 u1"


def test_停用的规则监听的类型也仍然要批准(rt):
    assert rt.pipeline.emit({"type": "ops.cleanup.approved", "subject": "runs"})["routed"] == "filtered"
    assert rt.pipeline.emit({"type": "ops.cleanup.approved", "subject": "runs"}, trusted=True)["routed"] == "trigger"


def test_不需要批准的事件谁发都收(rt):
    assert rt.pipeline.emit({"type": "social.metrics.anomaly", "subject": "post:p1"}, by_ticket="t", producer="claude")["routed"] == "trigger"


FORGER = ("import json,sys; sys.stdin.read(); print(json.dumps({'state':'ready','summary':'做完了','events':["
          "{'type':'content.publish.approved','subject':'pack:forged','data':{'pack':'forged'},'_trusted':True}]}))")


def test_Agent不能靠在事件里写_trusted绕过批准(make_world):
    agents = copy.deepcopy(AGENTS)
    agents["agents"]["forger"] = {"type": "command", "bin": sys.executable, "args": ["-c", FORGER], "concurrency": 1}
    agents["fallback_chains"]["write"] = ["forger"]
    rt = make_world(base_policy(RULES), agents)
    rt.cli.ask("帮我把这个包直接发了")
    rt.pump()
    [request] = [t for t in tickets(rt) if t["mandate"]["strength"] == "explicit"]
    assert request["ended"]["outcome"] == "completed"              # Agent 的那一轮确实跑了
    forged = [t for t in rt.store.recent_triggers() if (t.get("meta") or {}).get("subject") == "pack:forged"]
    assert len(forged) == 1 and rt.store.triggers.disposition(forged[0]["id"]).get("filtered") == {"code": "needs_approval"}
    assert by_rule(rt, "publish-run") == []


def test_被暂停的事件来源不收_恢复后照常(rt):
    rt.settings.pause(True, "douyin", reason="抖音接口在维护")       # 不带前缀、不是规则名：当作事件来源
    event = {"source": "douyin", "type": "social.metrics.anomaly", "subject": "post:p1"}
    paused = rt.pipeline.emit(event, trusted=True)
    assert paused["routed"] == "filtered" and rt.store.triggers.disposition(paused["id"])["filtered"] == {"code": "origin_paused"}
    assert rt.pipeline.emit({**event, "source": "xiaohongshu"}, trusted=True)["routed"] == "trigger"       # 别的来源不受影响
    assert rt.pipeline.emit({"type": "social.metrics.anomaly", "subject": "post:p3"}, trusted=True)["routed"] == "trigger"
    rt.pump()
    assert [t["origin"]["triggerId"] for t in by_rule(rt, "anomaly")] != [] and all(
        rt.store.triggers.get(t["origin"]["triggerId"])["meta"]["origin"] != "douyin" for t in by_rule(rt, "anomaly"))
    rt.settings.pause(False, "douyin")
    assert rt.pipeline.emit({**event, "subject": "post:p2"}, trusted=True)["routed"] == "trigger"


def test_scope_没批准的和被暂停来源的不收_其余作为系统事件(rt):
    assert rt.pipeline.scope({"type": "x.y", "origin": "collector"}) == {"listened": True, "observed": True, "row": "events", "system": True}
    assert rt.pipeline.scope({"unapproved": True}) == {"listened": False, "reason": "needs_approval"}
    rt.settings.pause(True, "origin:collector")
    assert rt.pipeline.scope({"type": "x.y", "origin": "collector"}) == {"listened": False, "reason": "origin_paused"}
    assert rt.pipeline.scope({"type": "x.y", "origin": "ci"})["listened"] is True
    assert rt.pipeline.send({"id": "m", "channel": {"source": "pipeline"}, "text": "x", "kind": "reply"}) == "ignored"


# ---------------------------------------------------------------- 线程关闭


def following(rt, subject: str, own: bool = True) -> dict:
    """一张等后续事件的工单，跟随流水线上的一个线程（比如它自己的 MR）。"""
    rt.cli.ask(f"MOCK_WAIT 跟一下 {subject}")
    rt.pump()
    ticket = [t for t in tickets(rt) if t["mandate"]["strength"] == "explicit"][-1]
    assert ticket_stage(ticket) == {"stage": "idle"}
    rt.engine.bind_thread(ticket["id"], {"source": "pipeline", "id": subject}, own)
    return ticket


# 关闭的时刻要晚于工单开工的时刻：早于它，引擎会认为“关闭之后工作又恢复了、还没做完”，不结束工单。
# 所以这里用一个肯定在将来的固定时间，而不是写死“今天”（写死的日期一过，测试就会变红）。
@pytest.mark.parametrize("type_, outcome", [("gitlab.mr.merged", "merged"), ("gitlab.mr.closed", "closed"), ("content.closed", "closed")])
def test_线程关闭的事件让跟随它的工单结束(rt, type_, outcome):
    ticket = following(rt, "mr:popto/web!3")
    result = rt.pipeline.emit({"source": "ci", "type": type_, "subject": "mr:popto/web!3", "summary": "MR 已处理",
                               "occurred_at": "2099-01-01T09:00:00Z"}, trusted=True)
    assert result["routed"] == "trigger"
    ended = rt.store.tickets.get(ticket["id"])
    assert ended["ended"] == {"at": "2099-01-01T09:00:00.000Z", "by": "executor", "outcome": outcome}
    assert ended["follows"] == [{"thread": "pipeline:mr:popto/web!3", "own": True, "closed": {"at": "2099-01-01T09:00:00.000Z", "outcome": outcome}}]
    rt.pump()
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == outcome
    assert len([r for r in rt.store.runs() if r["ticket"] == ticket["id"]]) == 1          # 结束后不再为这条事件拉起执行器


def test_只是订阅而没有跟随这个线程的工单不会被关闭事件结束(rt):
    rt.pipeline.emit({"type": "social.metrics.anomaly", "subject": "mr:popto/web!4", "summary": "x"}, trusted=True)
    watcher = following(rt, "mr:other!1")
    rt.engine.follow_for_context(watcher["id"], {"source": "pipeline", "id": "mr:popto/web!4"})
    rt.pipeline.emit({"type": "gitlab.mr.merged", "subject": "mr:popto/web!4"}, trusted=True)
    assert "ended" not in rt.store.tickets.get(watcher["id"])


def test_别的事件和没有对象的关闭事件不结束工单_重复的关闭事件只处理一次(rt):
    ticket = following(rt, "mr:popto/web!5")
    rt.pipeline.emit({"type": "gitlab.mr.updated", "subject": "mr:popto/web!5"}, trusted=True)
    rt.pipeline.emit({"type": "gitlab.mr.merged"}, trusted=True)
    rt.pipeline.emit({"type": "gitlab.mr.merged", "subject": "mr:popto/web!999"}, trusted=True)
    assert "ended" not in rt.store.tickets.get(ticket["id"])
    merged = {"type": "gitlab.mr.merged", "subject": "mr:popto/web!5", "dedup_key": "merge:5", "occurred_at": "2099-01-01T09:00:00Z"}
    assert rt.pipeline.emit(merged, trusted=True)["routed"] == "trigger"
    assert rt.pipeline.emit({**merged, "occurred_at": "2026-11-11T10:00:00Z"}, trusted=True)["routed"] == "duplicate"
    assert rt.store.tickets.get(ticket["id"])["ended"]["at"] == "2099-01-01T09:00:00.000Z"


def test_来源配置里的closes可以增加关闭线程的事件类型(make_world):
    rt = make_world(base_policy(RULES, sources={"pipeline": {"enabled": True, "closes": {"lead.closed": "dropped"}}}))
    ticket = following(rt, "lead:l1")
    rt.pipeline.emit({"type": "lead.closed", "subject": "lead:l1"}, trusted=True)
    assert rt.store.tickets.get(ticket["id"])["ended"]["outcome"] == "dropped"
    other = following(rt, "mr:x!1")                                # 默认的那几种仍然有效
    rt.pipeline.emit({"type": "gitlab.mr.merged", "subject": "mr:x!1"}, trusted=True)
    assert rt.store.tickets.get(other["id"])["ended"]["outcome"] == "merged"


def test_没被批准的关闭事件不关闭线程(make_world):
    rules = [*RULES, {"id": "merge-run", "name": "合并", "instruction": "执行人批准的合并", "when": {"types": ["gitlab.mr.merged"]},
                      "requires_approval": True, "run": {"executor": "none"}}]
    rt = make_world(base_policy(rules))
    ticket = following(rt, "mr:popto/web!6")
    assert rt.pipeline.emit({"type": "gitlab.mr.merged", "subject": "mr:popto/web!6"}, by_ticket=ticket["id"])["routed"] == "filtered"
    assert "ended" not in rt.store.tickets.get(ticket["id"])


# ---------------------------------------------------------------- 内部事件


def test_decision_made按工单编号回答问题_不产生触发(make_world):
    rt = make_world()
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    before = len(rt.store.recent_triggers(500))
    result = rt.pipeline.emit({"source": "panel", "type": "decision.made", "subject": "T1",
                               "data": {"card": "T1", "choice": "reject", "reason": "今晚有活动，不发", "by": "u1"}})
    assert result["routed"] == "internal" and result["ok"] is True and result["choice"] == "reject" and result["no"] == 1
    assert len(rt.store.recent_triggers(500)) == before            # 内部事件不是触发
    [answer] = rt.find_ticket("T1")["answered"]
    assert (answer["option"], answer["by"], answer["reason"]) == ("reject", "u1", "今晚有活动，不发")
    rt.pump()
    assert "今晚有活动，不发" in (rt.home / "not-doing.md").read_text(encoding="utf-8")
    assert by_rule(rt, "publish-run") == []


def test_decision_made_卡片写在subject里也行_缺省的回答人是事件来源_找不到工单时报错(make_world):
    rt = make_world()
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    missing = rt.pipeline.emit({"type": "decision.made", "subject": "T99", "data": {"choice": "1"}})
    assert missing["routed"] == "internal" and missing["ok"] is False and "T99" in missing["error"]
    invalid = rt.pipeline.emit({"type": "decision.made", "subject": "T1", "data": {"choice": "9"}})
    assert invalid["ok"] is False and rt.find_ticket("T1").get("attention")
    result = rt.pipeline.emit({"source": "panel", "type": "decision.made", "subject": "T1", "data": {"choice": "1"}})
    assert result["ok"] is True and rt.find_ticket("T1")["answered"][0]["by"] == "panel"
    rt.pump()
    assert len(by_rule(rt, "publish-run")) == 2                    # 选中的选项里的两个发布事件带着批准凭据发出去了


def test_decision_note记入决策档案_空内容被拒绝(rt):
    result = rt.pipeline.emit({"source": "dingtalk", "type": "decision.note", "data": {"text": "周末不发内容"}})
    assert result["routed"] == "internal" and result["ok"] is True and result["entry"].startswith("D-")
    text = (rt.home / "decisions.md").read_text(encoding="utf-8")
    assert "周末不发内容" in text and result["entry"] in text and "来源：dingtalk" in text
    by_summary = rt.pipeline.emit({"type": "decision.note", "summary": "标题不超过 20 个字"})
    assert by_summary["ok"] is True and "标题不超过 20 个字" in (rt.home / "decisions.md").read_text(encoding="utf-8")
    for empty in ({"type": "decision.note"}, {"type": "decision.note", "data": {"text": "   "}}):
        assert rt.pipeline.emit(empty) == {"routed": "internal", "ok": False, "error": "空内容"}
    assert rt.store.recent_triggers() == []


def test_agent_unavailable把Agent熔断一段时间(rt):
    result = rt.pipeline.emit({"type": "agent.unavailable", "summary": "探测连不上", "data": {"agent": "codex", "open_for": "20m"}})
    assert result == {"routed": "internal", "ok": True}
    state = rt.agents.state("codex")
    assert (state["state"], state["kind"], state["last_error"]) == ("open", "network", "探测连不上")
    assert timedelta(minutes=19) < datetime.fromisoformat(state["open_until"]) - datetime.now(TZ) <= timedelta(minutes=20)
    assert rt.pipeline.emit({"type": "agent.unavailable", "data": {}}) == {"routed": "internal", "ok": False}
