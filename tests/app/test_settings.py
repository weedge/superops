"""设置与策略（superops/app/settings.py）：规则补全与校验、策略版本、规则匹配、准入、授权复核、批红权限、策略变更的影响、Settings。

准入 / 复核 / 影响的场景移植自原型的 tests/policy.spec.ts 和 docs/002-authorization.md。
"""
from __future__ import annotations

import copy
import itertools

import pytest
import yaml

from superops.app.settings import (Settings, SettingsPolicy, allowed_sender, deep_merge, listed_bot, matching_rules, normalize,
                                   normalize_rule, policy_effects, policy_version, rule_matches)
from superops.app.store import Store
from superops.core.errors import UserError

MEMBER, GUEST, STRANGER = "ou_alice", "ou_guest", "ou_stranger"
TEAM, OBSERVED = "team", "observed"          # TEAM 只收 @ 的消息；OBSERVED 收全部消息


class FakeSources:
    """最小的来源登记：只有 scope(source, meta)。chats: {群: 'mention' | 'all'}；git 没有接收范围的行；system 里的是系统来源。"""

    def __init__(self, chats: dict | None = None, *, direct: bool = True, system: tuple = ("pipeline", "schedule"),
                 observing: tuple = (), off: tuple = ()):
        self.chats = {TEAM: "mention", OBSERVED: "all"} if chats is None else chats
        self.direct, self.system, self.observing, self.off = direct, system, observing, off

    def scope(self, source: str, meta: dict) -> dict:
        if source in self.off:
            return {"listened": False, "reason": "source_off"}
        if source in self.system:
            return {"listened": True, "observed": True, "row": "events", "system": True}
        if source in self.observing:      # 收全部消息的非系统来源（例如盯着默认分支 CI 的代码托管来源）
            return {"listened": True, "observed": True, "row": meta.get("row", "ci")}
        if source == "git":
            return {"listened": True, "observed": False}
        chat = meta.get("chat")
        if chat == "p2p":
            return {"listened": True, "observed": False, "row": "direct"} if self.direct else {"listened": False, "reason": "direct_off"}
        if chat not in self.chats:
            return {"listened": False, "reason": "chat_not_listened"}
        return {"listened": True, "observed": self.chats[chat] == "all", "row": chat}


class Listening:
    """什么都收的来源范围（policy_effects 的场景用）。"""

    def __init__(self, row: str | None = None):
        self.row = row

    def scope(self, source: str, meta: dict) -> dict:
        return {"listened": True, "observed": True, **({"row": self.row} if self.row else {})}


RULES = [{"id": "copy", "name": "Copy", "instruction": "Fix typos."}, {"id": "visual", "name": "Visual", "instruction": "Fix colors."}]
BASE = {"eval": {"mode": "auto"},
        "policy": {"rules": RULES, "sources": {"ding": {"allowed": [{"id": MEMBER, "name": "Alice"}]}, "git": {"allowed": [{"id": "Alice"}]}}}}


def make(over: dict | None = None) -> dict:
    return normalize(deep_merge(BASE, over or {}))


def policy_of(over: dict | None = None, sources=None) -> SettingsPolicy:
    return SettingsPolicy(make(over), sources or FakeSources())


def with_table(admission: dict, mode: str = "auto", **part) -> dict:
    return {"eval": {"mode": mode}, "policy": {"sources": {"ding": {"admission": admission, **part}}}}


def thread(subscriptions=(), context=None) -> dict:
    out = {"key": "ding:x", "ref": {"source": "ding", "id": "x"}, "createdAt": "", "subscriptions": list(subscriptions),
           "correlated": [], "pending": [], "meta": {}}
    if context is not None:
        out["context"] = list(context)
    return out


def ding(actor: str = MEMBER, *, chat: str = TEAM, addressed: str | None = "mention", human: bool = True) -> dict:
    trigger = {"id": "t", "source": "ding", "threads": [{"source": "ding", "id": "x"}], "actor": {"source": "ding", "id": actor},
               "human": human, "at": "", "text": "hi", "meta": {"chat": chat, "message_id": "m1"}}
    if addressed:
        trigger["addressed"] = addressed
    return trigger


def direct(actor: str = MEMBER) -> dict:
    return ding(actor, chat="p2p", addressed="direct")


def git(login: str, addressed: str | None = None) -> dict:
    trigger = {"id": "g", "source": "git", "threads": [{"source": "git", "id": "1"}], "actor": {"source": "git", "id": login},
               "human": True, "at": "", "text": "x", "meta": {"number": 1}}
    if addressed:
        trigger["addressed"] = addressed
    return trigger


def system_event(source: str = "pipeline", **meta) -> dict:
    return {"id": "e", "source": source, "threads": [{"source": source, "id": "s"}], "actor": {"source": source, "id": ""},
            "human": False, "at": "", "text": "事件", "meta": {"type": "x.y", "origin": "cron", **meta}}


ENDED = {"id": "ticket", "ended": {"at": "", "by": "executor", "outcome": "closed"}}


def partial(result: dict, expected: dict) -> bool:
    """expected 是 result 的子集（嵌套 dict 逐层比）。"""
    return all(partial(result.get(k) or {}, v) if isinstance(v, dict) else result.get(k) == v for k, v in expected.items())


# ---------------------------------------------------------------- normalize / normalize_rule


def test_合法规则补全成固定形状():
    rule = normalize_rule({"id": "daily-brief", "instruction": "  每天写日报  ", "when": {"origin": "cron", "types": ["cron.daily_brief"]}})
    assert rule == {
        "id": "daily-brief", "name": "daily-brief", "enabled": True, "instruction": "每天写日报",
        "when": {"origin": ["cron"], "types": ["cron.daily_brief"]},
        "run": {"executor": "agent", "role": "write", "tools": "workspace"}, "tell": "digest", "priority": 2, "debounce": None,
        "max_rounds": None, "credits_per_run": None, "after": [], "after_max": "15m", "requires_approval": False, "idle_ttl": None,
        "unless_live": []}


def test_规则写明的字段原样保留_when的来源默认任意():
    rule = normalize_rule({"id": "r1", "name": "审稿", "enabled": False, "instruction": "x", "priority": "0", "tell": "always",
                           "when": {"types": ["content.*"], "match": "口播"}, "debounce": "10m", "max_rounds": 2, "credits_per_run": 24,
                           "after": ["a"], "after_max": "1h", "requires_approval": True, "idle_ttl": "2d", "unless_live": ["b"],
                           "run": {"role": "review", "agent": "codex", "tools": "readonly", "skill": "ops-x"}})
    assert rule["when"] == {"origin": ["*"], "types": ["content.*"], "match": "口播"}
    assert rule["run"] == {"role": "review", "agent": "codex", "tools": "readonly", "skill": "ops-x", "executor": "agent"}
    assert (rule["name"], rule["enabled"], rule["priority"], rule["tell"]) == ("审稿", False, 0, "always")
    assert (rule["debounce"], rule["max_rounds"], rule["credits_per_run"], rule["after_max"], rule["idle_ttl"]) == ("10m", 2, 24, "1h", "2d")
    assert (rule["after"], rule["unless_live"], rule["requires_approval"]) == (["a"], ["b"], True)
    # origin 给列表时原样用；没有 when 的是语义规则
    assert normalize_rule({"id": "r2", "instruction": "x", "when": {"origin": ["cron", "ci"], "types": ["a.b"]}})["when"]["origin"] == ["cron", "ci"]
    assert normalize_rule({"id": "r3", "instruction": "x"})["when"] is None


def test_脚本和只留底的规则不补Agent的角色与工具档位():
    script = normalize_rule({"id": "s", "instruction": "x", "run": {"executor": "script", "command": ["{python}", "-c", "pass"]}})
    assert script["run"] == {"executor": "script", "command": ["{python}", "-c", "pass"]}
    assert normalize_rule({"id": "n", "instruction": "x", "run": {"executor": "none"}})["run"] == {"executor": "none"}


@pytest.mark.parametrize("raw, message", [
    ({"instruction": "x"}, "id 不合法"),
    ({"id": "有空格 的", "instruction": "x"}, "id 不合法"),
    ({"id": "a" * 81, "instruction": "x"}, "id 不合法"),
    ("不是字典", "id 不合法"),
    ({"id": "r", "instruction": "x", "wehn": {}}, "不认识的键"),
    ({"id": "r"}, "缺少 instruction"),
    ({"id": "r", "instruction": "   "}, "缺少 instruction"),
    ({"id": "r", "instruction": "x", "when": {"origin": "cron"}}, "when 需要 types"),
    ({"id": "r", "instruction": "x", "when": "cron.x"}, "when 需要 types"),
    ({"id": "r", "instruction": "x", "run": {"executor": "robot"}}, "run.executor"),
    ({"id": "r", "instruction": "x", "run": {"executor": "script"}}, "需要 run.command"),
    ({"id": "r", "instruction": "x", "run": {"tools": "root"}}, "run.tools"),
    ({"id": "r", "instruction": "x", "tell": "shout"}, "tell"),
    ({"id": "r", "instruction": "x", "debounce": "一会儿"}, "时长"),
    ({"id": "r", "instruction": "x", "after_max": "soon"}, "时长"),
    ({"id": "r", "instruction": "x", "idle_ttl": "forever"}, "时长"),
])
def test_不合法的规则抛ValueError(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_rule(raw)


def test_when的types写成字符串时不能被拆成单个字符():
    try:
        rule = normalize_rule({"id": "r", "instruction": "x", "when": {"types": "content.*"}})
    except ValueError:
        return                                  # 拒绝也可以
    assert rule["when"]["types"] == ["content.*"]   # 或者像 origin 一样当成单元素列表
    assert not rule_matches(rule, system_event(type="deploy.mismatch"))


def test_when的match不是合法正则时在加载时就拒绝():
    with pytest.raises(ValueError):
        normalize_rule({"id": "r", "instruction": "x", "when": {"types": ["a.b"], "match": "("}})


def test_整份设置补全后的形状和默认值():
    settings = normalize({"policy": {"rules": [{"id": "r", "instruction": "x"}],
                                     "sources": {"ding": {"allowed": ["u1", {"id": 2, "name": "二"}], "bots": [{"id": "bot1"}],
                                                          "admission": {"team": {"unlistedAddressed": "explicit"}},
                                                          "answerers": {"r": ["u9", 10]}}}},
                          "sources": {"ding": {"enabled": False, "chats": ["team"]}, "cli": None}})
    assert settings["paused"] is False and settings["paused_keys"] == {}
    assert settings["eval"] == {"mode": "observe", "timeout_seconds": 600, "role": "decide", "effort": "medium"}
    assert settings["executor"] == {"max_workers": 4, "idle_ttl": "7d", "default": {"role": "write", "tools": "workspace", "timeout": "30m"}}
    assert settings["sources"] == {"ding": {"enabled": False, "config": {"chats": ["team"]}}, "cli": {"enabled": True, "config": {}}}
    assert settings["home"] == {"source": "cli", "thread": "home", "address": {}}
    assert settings["policy"]["sources"]["ding"] == {
        "allowed": [{"id": "u1"}, {"id": "2", "name": "二"}], "bots": [{"id": "bot1"}],
        "admission": {"team": {"unlistedAddressed": "explicit"}}, "answerers": {"r": ["u9", "10"]}}
    assert [r["id"] for r in settings["policy"]["rules"]] == ["r"]
    # 什么都没写也能补全
    empty = normalize({})
    assert empty["policy"] == {"rules": [], "sources": {}} and empty["eval"]["mode"] == "observe"


@pytest.mark.parametrize("raw, message", [
    ({"eval": {"mode": "yolo"}}, "eval.mode"),
    ({"policy": {"rules": [{"id": "a", "instruction": "x"}, {"id": "a", "instruction": "y"}]}}, "id 重复"),
    ({"policy": {"rules": [{"id": "a", "instruction": "x", "after": ["ghost"]}]}}, "after 指向不存在的规则 ghost"),
    ({"policy": {"rules": [{"id": "a", "instruction": "x", "unless_live": ["ghost"]}]}}, "unless_live 指向不存在的规则 ghost"),
    ({"policy": {"rules": [{"id": "a"}]}}, "缺少 instruction"),
    ({"policy": {"sources": {"ding": {"admission": {"team": {"unlistedAddressed": "always"}}}}}}, "准入表"),
    ({"policy": {"sources": {"ding": {"admission": {"team": {"someCell": "inferred"}}}}}}, "准入表"),
    # 没 @ 机器人的消息不能直接算明确请求
    ({"policy": {"sources": {"ding": {"admission": {"team": {"listedUnaddressed": "explicit"}}}}}}, "准入表"),
    ({"policy": {"sources": {"ding": {"admission": {"team": {"unlistedUnaddressed": "explicit"}}}}}}, "准入表"),
])
def test_不合法的设置抛ValueError(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize(raw)


def test_after和unless_live可以指向文件里写在后面的规则():
    settings = normalize({"policy": {"rules": [{"id": "brief", "instruction": "x", "after": ["collect"], "unless_live": ["collect"]},
                                               {"id": "collect", "instruction": "y"}]}})
    assert settings["policy"]["rules"][0]["after"] == ["collect"]


# ---------------------------------------------------------------- policy_version


def versioned(**changes) -> str:
    raw = {"eval": {"mode": "auto", "timeout_seconds": 120}, "executor": {"max_workers": 4},
           "policy": {"rules": [{"id": "r", "name": "规则", "instruction": "写日报", "when": {"origin": "cron", "types": ["cron.daily"]},
                                 "tell": "digest", "priority": 2}],
                      "sources": {"ding": {"allowed": [{"id": "u1"}]}}}}
    rule = raw["policy"]["rules"][0]
    for path, value in changes.items():
        if path.startswith("rule_"):
            rule[path[5:]] = value
        elif path == "mode":
            raw["eval"]["mode"] = value
        elif path == "timeout":
            raw["eval"]["timeout_seconds"] = value
        elif path == "workers":
            raw["executor"]["max_workers"] = value
        elif path == "ding":
            raw["policy"]["sources"]["ding"] = value
    settings = normalize(raw)
    settings["paused"] = bool(changes.get("paused"))
    settings["paused_keys"] = changes.get("paused_keys") or {}
    return policy_version(settings)


@pytest.mark.parametrize("change", [
    {"rule_instruction": "写周报"},
    {"rule_when": {"origin": "cron", "types": ["cron.weekly"]}},
    {"rule_when": {"origin": "ci", "types": ["cron.daily"]}},
    {"rule_when": {"origin": "cron", "types": ["cron.daily"], "match": "x"}},
    {"rule_when": None},
    {"rule_enabled": False},
    {"rule_requires_approval": True},
    {"rule_run": {"executor": "none"}},
    {"mode": "observe"},
    {"mode": "off"},
    {"ding": {"allowed": [{"id": "u1"}, {"id": "u2"}]}},
    {"ding": {"allowed": []}},
    {"ding": {"allowed": [{"id": "u1"}], "bots": [{"id": "b"}]}},
    {"ding": {"allowed": [{"id": "u1"}], "admission": {"team": {"unlistedAddressed": "explicit"}}}},
    {"ding": {"allowed": [{"id": "u1"}], "answerers": {"r": ["u9"]}}},
], ids=lambda c: "+".join(c))
def test_规则_接单模式_来源名单变了策略版本就变(change):
    assert versioned(**change) != versioned()


@pytest.mark.parametrize("change", [
    {"timeout": 300}, {"workers": 9}, {"rule_tell": "always"}, {"rule_priority": 0}, {"paused": True},
    {"paused_keys": {"rule:r": {"reason": "", "at": ""}}}, {"rule_debounce": "5m"}, {"rule_idle_ttl": "1d"},
    {"ding": {"allowed": [{"id": "u1"}], "admission": {}}},        # 空的准入表和没写一样
], ids=lambda c: "+".join(c))
def test_超时_并发_通知方式_优先级_暂停不改变策略版本(change):
    assert versioned(**change) == versioned()


def test_策略版本是稳定的_相同的设置给出相同的版本():
    assert versioned() == versioned() and len(versioned()) == 32


# ---------------------------------------------------------------- rule_matches / matching_rules


def event(type_: str | None = "content.script.ready", *, origin: str | None = None, subject: str | None = None, text: str = "",
          source: str = "pipeline") -> dict:
    meta = {k: v for k, v in (("type", type_), ("origin", origin), ("subject", subject)) if v is not None}
    return {"id": "e", "source": source, "text": text, "meta": meta}


def rule(when: dict | None, **extra) -> dict:
    return normalize_rule({"id": extra.pop("id", "r"), "instruction": "x", **({"when": when} if when else {}), **extra})


def test_规则按事件类型匹配_支持通配():
    assert rule_matches(rule({"types": ["content.script.ready"]}), event())
    assert not rule_matches(rule({"types": ["content.script.failed"]}), event())
    assert rule_matches(rule({"types": ["content.*"]}), event())
    assert rule_matches(rule({"types": ["gitlab.mr.*", "content.script.?eady"]}), event())
    assert not rule_matches(rule({"types": ["content.*"]}), event("social.metrics.anomaly"))
    assert not rule_matches(rule({"types": ["Content.*"]}), event())                       # 区分大小写
    assert not rule_matches(rule({"types": ["content"]}), event())                         # 不是前缀匹配


def test_没有when的规则和没有类型的触发不匹配():
    assert not rule_matches(rule(None), event())
    assert not rule_matches(rule({"types": ["*"]}), event(None))
    assert not rule_matches(rule({"types": ["*"]}), {"id": "m", "source": "cli", "text": "人的消息", "meta": {"row": "terminal"}})


def test_规则按事件来源过滤_没写origin时用触发的来源():
    cron = rule({"origin": "cron", "types": ["cron.*"]})
    assert rule_matches(cron, event("cron.daily", origin="cron"))
    assert not rule_matches(cron, event("cron.daily", origin="collector"))
    assert not rule_matches(cron, event("cron.daily"))                                      # 没写 origin：算作触发的来源 pipeline
    assert rule_matches(rule({"origin": "pipeline", "types": ["cron.*"]}), event("cron.daily"))
    assert rule_matches(rule({"origin": ["ci", "collector"], "types": ["cron.*"]}), event("cron.daily", origin="collector"))
    assert rule_matches(rule({"origin": "*", "types": ["cron.*"]}), event("cron.daily", origin="whatever"))
    assert rule_matches(rule({"types": ["cron.*"]}), event("cron.daily", origin="whatever"))


def test_match正则在对象和正文里找():
    matched = rule({"types": ["social.*"], "match": r"post:p\d+|掉量"})
    assert rule_matches(matched, event("social.metrics.anomaly", subject="post:p12"))
    assert rule_matches(matched, event("social.metrics.anomaly", subject="video:v1", text="昨天那条掉量了"))
    assert not rule_matches(matched, event("social.metrics.anomaly", subject="video:v1", text="一切正常"))
    assert not rule_matches(matched, event("content.script.ready", subject="post:p12"))     # 类型不对，正则对上也不算


def test_matching_rules默认只给启用的规则_保持文件里的顺序():
    settings = normalize({"policy": {"rules": [
        {"id": "off", "instruction": "x", "enabled": False, "when": {"types": ["content.*"]}},
        {"id": "first", "instruction": "x", "when": {"types": ["content.script.ready"]}},
        {"id": "other", "instruction": "x", "when": {"types": ["social.*"]}},
        {"id": "second", "instruction": "x", "when": {"types": ["content.*"]}},
        {"id": "semantic", "instruction": "x"}]}})
    assert [r["id"] for r in matching_rules(settings, event())] == ["first", "second"]
    assert [r["id"] for r in matching_rules(settings, event(), enabled_only=False)] == ["off", "first", "second"]
    assert matching_rules(settings, event("deploy.mismatch")) == []


# ---------------------------------------------------------------- gate：人的消息


def test_名单内的人at机器人是明确请求():
    policy = policy_of()
    assert policy.gate(ding(), thread(), []) == {"admissible": True, "strength": "explicit", "basis": {
        "source": "ding", "scope": {"chat": TEAM}, "row": TEAM, "sender": "Alice", "via": "addressed", "addressed": "mention"}}
    assert partial(policy.gate(direct(), thread(), []), {"admissible": True, "strength": "explicit", "basis": {"addressed": "direct", "row": "direct"}})
    # 账号比较不分大小写；名单项没有显示名时用 id 称呼发送人
    assert partial(policy.gate(git("alice", "mention"), thread(), []),
                   {"admissible": True, "strength": "explicit", "basis": {"source": "git", "sender": "Alice"}})


def test_授权里只记来源判断范围要用的键_不抄整份事件数据():
    trigger = ding()
    trigger["meta"].update({"row": TEAM, "type": "t", "origin": "o", "data": {"big": "x" * 100}, "role": "admin"})
    assert policy_of().gate(trigger, thread(), [])["basis"]["scope"] == {"chat": TEAM, "row": TEAM, "type": "t", "origin": "o"}


def test_每个来源只认自己的名单():
    settings = make()
    assert SettingsPolicy(settings, FakeSources()).gate(git(MEMBER, "mention"), thread(), []) == {"admissible": False, "reason": "not_a_member"}
    assert allowed_sender(settings, {"source": "git", "id": "ALICE"}) == {"id": "Alice"}
    assert allowed_sender(settings, {"source": "ding", "id": "Alice"}) is None
    assert allowed_sender(settings, {"source": "ding", "id": f" {MEMBER.upper()} "}) == {"id": MEMBER, "name": "Alice"}
    assert allowed_sender(settings, {"source": "nowhere", "id": MEMBER}) is None


def test_有工单的线程上的跟进_名单内明确_名单外推断():
    policy = policy_of()
    guest = policy.gate(ding(GUEST, addressed=None), thread(["ticket"]), [])
    assert partial(guest, {"admissible": True, "strength": "inferred", "basis": {"via": "followup", "source": "ding", "row": TEAM}})
    assert "sender" not in guest["basis"]
    assert partial(policy.gate(ding(addressed=None), thread(["ticket"]), []),
                   {"admissible": True, "strength": "explicit", "basis": {"via": "followup", "sender": "Alice"}})
    # 线程自己还没记下订阅、但传进来了它的工单，也算
    assert partial(policy.gate(git("someone"), thread(), [ENDED]), {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}})


def test_只为上下文订阅的线程不给跟进强度_按准入表走():
    policy = policy_of()
    context = thread(["ticket"], context=["ticket"])
    assert policy.gate(ding(addressed=None), context, [ENDED]) == {"admissible": False, "reason": "no_mention"}
    assert policy.gate(ding(GUEST, addressed=None), context, [ENDED]) == {"admissible": False, "reason": "not_a_member"}
    # 线程上另有一张真正参与的工单时，仍是跟进
    mixed = thread(["ticket", "worker"], context=["ticket"])
    assert partial(policy.gate(ding(GUEST, addressed=None), mixed, []), {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}})


def test_名单内的人没at的消息只在来源收全部消息的范围里成为推断候选():
    observed = ding(chat=OBSERVED, addressed=None)
    assert policy_of().gate(observed, thread(), []) == {"admissible": True, "strength": "inferred", "basis": {
        "source": "ding", "scope": {"chat": OBSERVED}, "row": OBSERVED, "sender": "Alice", "via": "admission",
        "cell": "listedUnaddressed", "observation": {"mode": "auto"}}}
    assert policy_of({"eval": {"mode": "observe"}}).gate(observed, thread(), [])["basis"]["observation"] == {"mode": "observe"}
    assert policy_of({"eval": {"mode": "off"}}).gate(observed, thread(), []) == {"admissible": False, "reason": "no_mention"}
    # 只收 @ 的群里，没 @ 的消息不走准入表
    assert policy_of().gate(ding(addressed=None), thread(), []) == {"admissible": False, "reason": "no_mention"}


def test_私聊线程不给名单外的人跟进强度():
    assert policy_of().gate(direct(GUEST), thread(["ticket"]), []) == {"admissible": False, "reason": "not_a_member"}
    # 名单内的人私聊照常是明确请求
    assert policy_of().gate(direct(), thread(["ticket"]), [])["strength"] == "explicit"


def test_名单外的人at机器人_默认不准入():
    policy = policy_of()
    assert policy.gate(ding(STRANGER), thread(), []) == {"admissible": False, "reason": "not_a_member"}
    assert policy.gate(direct(STRANGER), thread(), []) == {"admissible": False, "reason": "not_a_member"}
    assert policy.gate(ding(STRANGER, chat=OBSERVED, addressed=None), thread(), []) == {"admissible": False, "reason": "not_a_member"}
    # 名单内的人 @ 机器人，不管准入表怎么写都是明确请求
    strict = policy_of(with_table({TEAM: {"unlistedAddressed": "ignore"}}))
    assert strict.gate(ding(), thread(), [])["strength"] == "explicit"


def test_名单外的人at机器人_按准入表的格子成为明确请求或推断候选():
    explicit = policy_of(with_table({TEAM: {"unlistedAddressed": "explicit"}})).gate(ding(STRANGER), thread(), [])
    assert explicit == {"admissible": True, "strength": "explicit", "basis": {
        "source": "ding", "scope": {"chat": TEAM}, "row": TEAM, "via": "admission", "cell": "unlistedAddressed", "addressed": "mention"}}
    inferred = policy_of(with_table({TEAM: {"unlistedAddressed": "inferred"}})).gate(ding(STRANGER), thread(), [])
    assert partial(inferred, {"admissible": True, "strength": "inferred",
                              "basis": {"via": "admission", "cell": "unlistedAddressed", "addressed": "mention", "observation": {"mode": "auto"}}})
    # 推断候选要等一个会研判的模式
    off = policy_of(with_table({TEAM: {"unlistedAddressed": "inferred"}}, "off"))
    assert off.gate(ding(STRANGER), thread(), []) == {"admissible": False, "reason": "not_a_member"}
    # 私聊是自己的一行；别的行的格子管不到这个群
    private = policy_of(with_table({"direct": {"unlistedAddressed": "explicit"}}))
    assert partial(private.gate(direct(STRANGER), thread(), []),
                   {"admissible": True, "strength": "explicit", "basis": {"row": "direct", "cell": "unlistedAddressed", "addressed": "direct"}})
    other_row = policy_of(with_table({OBSERVED: {"unlistedAddressed": "explicit"}}))
    assert other_row.gate(ding(STRANGER), thread(), []) == {"admissible": False, "reason": "not_a_member"}


def test_没at的消息按准入表的另外两个格子_只在收全部消息的范围里():
    ignore = policy_of(with_table({OBSERVED: {"listedUnaddressed": "ignore"}}))
    assert ignore.gate(ding(chat=OBSERVED, addressed=None), thread(), []) == {"admissible": False, "reason": "admission_ignored"}
    chatter = policy_of(with_table({OBSERVED: {"unlistedUnaddressed": "inferred"}})).gate(ding(STRANGER, chat=OBSERVED, addressed=None), thread(), [])
    assert partial(chatter, {"admissible": True, "strength": "inferred",
                             "basis": {"via": "admission", "cell": "unlistedUnaddressed", "row": OBSERVED, "observation": {"mode": "auto"}}})
    assert "addressed" not in chatter["basis"] and "sender" not in chatter["basis"]
    # 只收 @ 的群收不到没 @ 的消息：格子写了 inferred 也不准入
    mention_only = policy_of(with_table({TEAM: {"listedUnaddressed": "inferred", "unlistedUnaddressed": "inferred"}}))
    assert mention_only.gate(ding(addressed=None), thread(), []) == {"admissible": False, "reason": "no_mention"}
    assert mention_only.gate(ding(STRANGER, addressed=None), thread(), []) == {"admissible": False, "reason": "not_a_member"}


def test_off模式下推断的跟进不准入_名单内的人的跟进照常():
    off = policy_of({"eval": {"mode": "off"}})
    assert off.gate(ding(GUEST, addressed=None), thread(["ticket"]), []) == {"admissible": False, "reason": "not_a_member"}
    assert partial(off.gate(ding(addressed=None), thread(["ticket"]), []), {"admissible": True, "strength": "explicit", "basis": {"via": "followup"}})


@pytest.mark.parametrize("mode", ["auto", "observe", "off"])
def test_格子设为明确请求时_有工单的线程上名单外的人at机器人也是明确请求(mode):
    opened = policy_of(with_table({TEAM: {"unlistedAddressed": "explicit"}}, mode))
    assert partial(opened.gate(ding(STRANGER), thread(["ticket"]), []),
                   {"admissible": True, "strength": "explicit", "basis": {"via": "admission", "cell": "unlistedAddressed"}})
    # 格子不是 explicit：跟进仍是推断的，off 模式下不准入
    strict = policy_of(with_table({TEAM: {"unlistedAddressed": "ignore"}}, mode)).gate(ding(STRANGER), thread(["ticket"]), [])
    if mode == "off":
        assert strict["admissible"] is False
    else:
        assert partial(strict, {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}})


def test_有工单的线程上的跟进不看准入表():
    strict = policy_of(with_table({OBSERVED: {"listedUnaddressed": "ignore", "unlistedUnaddressed": "ignore"}}))
    assert partial(strict.gate(ding(chat=OBSERVED, addressed=None), thread(["ticket"]), []),
                   {"admissible": True, "strength": "explicit", "basis": {"via": "followup"}})
    assert partial(strict.gate(ding(STRANGER, chat=OBSERVED, addressed=None), thread(["ticket"]), []),
                   {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}})


# ---------------------------------------------------------------- gate：机器的消息


BOTS = {"policy": {"sources": {"ding": {"bots": [{"id": "cli_alerts", "name": "Alerts"}, {"id": "dependabot"}]}}}}


def test_机器名单上的机器人只能成为推断候选():
    policy = policy_of(BOTS)
    alert = ding("cli_alerts", chat=OBSERVED, addressed=None, human=False)
    assert policy.gate(alert, thread(), []) == {"admissible": True, "strength": "inferred", "basis": {
        "source": "ding", "scope": {"chat": OBSERVED}, "row": OBSERVED, "via": "bot", "sender": "Alerts", "observation": {"mode": "auto"}}}
    assert policy.gate(ding("DependaBot", chat=OBSERVED, addressed=None, human=False), thread(), [])["basis"]["sender"] == "dependabot"
    assert listed_bot(make(BOTS), {"source": "ding", "id": "CLI_ALERTS"}) == {"id": "cli_alerts", "name": "Alerts"}
    # @ 了机器人也不是明确请求；有工单的线程上也不算跟进
    mention = policy.gate(ding("cli_alerts", chat=OBSERVED, human=False), thread(["ticket"]), [ENDED])
    assert partial(mention, {"admissible": True, "strength": "inferred", "basis": {"via": "bot", "addressed": "mention"}})
    # 只收 @ 的群里：@ 了才收得到
    assert partial(policy.gate(ding("cli_alerts", human=False), thread(), []),
                   {"admissible": True, "strength": "inferred", "basis": {"via": "bot", "addressed": "mention", "row": TEAM}})
    assert policy.gate(ding("cli_alerts", addressed=None, human=False), thread(), []) == {"admissible": False, "reason": "not_human"}


def test_不在机器名单上的机器人不准入_off模式下名单上的也不准入():
    policy = policy_of(BOTS)
    assert policy.gate(ding("renovate", chat=OBSERVED, addressed=None, human=False), thread(), []) == {"admissible": False, "reason": "not_human"}
    assert policy.gate(ding(MEMBER, human=False), thread(), []) == {"admissible": False, "reason": "not_human"}   # 人的名单不算机器名单
    off = policy_of(deep_merge(BOTS, {"eval": {"mode": "off"}}))
    assert off.gate(ding("cli_alerts", chat=OBSERVED, addressed=None, human=False), thread(), []) == {"admissible": False, "reason": "admission_ignored"}
    # 来源不收这个范围的
    assert policy.gate(ding("cli_alerts", chat="elsewhere", human=False), thread(), []) == {"admissible": False, "reason": "not_human"}


def test_没有发送人的系统事件_系统来源默认作为推断候选():
    policy = policy_of()
    gate = policy.gate(system_event("pipeline", subject="post:p1", data={"n": 1}), thread(), [])
    assert gate == {"admissible": True, "strength": "inferred", "basis": {
        "source": "pipeline", "scope": {"type": "x.y", "origin": "cron"}, "row": "events", "via": "admission",
        "cell": "unlistedUnaddressed", "observation": {"mode": "auto"}}}
    assert policy_of({"eval": {"mode": "observe"}}).gate(system_event("schedule"), thread(), [])["basis"]["observation"] == {"mode": "observe"}
    assert policy_of({"eval": {"mode": "off"}}).gate(system_event(), thread(), []) == {"admissible": False, "reason": "admission_ignored"}
    # 准入表把系统来源的这一格写成 ignore 就不收
    closed = policy_of({"policy": {"sources": {"pipeline": {"admission": {"events": {"unlistedUnaddressed": "ignore"}}}}}})
    assert closed.gate(system_event(), thread(), []) == {"admissible": False, "reason": "admission_ignored"}


def test_没有发送人的事件_非系统来源默认忽略_准入表写了才收():
    sources = FakeSources(observing=("ci",))
    event_ = system_event("ci")
    assert policy_of(None, sources).gate(event_, thread(), []) == {"admissible": False, "reason": "admission_ignored"}
    table = {"policy": {"sources": {"ci": {"admission": {"ci": {"unlistedUnaddressed": "inferred"}}}}}}
    assert partial(policy_of(table, sources).gate(event_, thread(), []),
                   {"admissible": True, "strength": "inferred", "basis": {"via": "admission", "cell": "unlistedUnaddressed", "row": "ci",
                                                                          "observation": {"mode": "auto"}}})
    assert policy_of(deep_merge(table, {"eval": {"mode": "off"}}), sources).gate(event_, thread(), []) == {"admissible": False, "reason": "admission_ignored"}
    # 来源不收全部消息的范围里（比如某个 PR 自己的 CI），没人 @ 的系统事件不进来
    assert policy_of(table, FakeSources()).gate(system_event("git"), thread(), []) == {"admissible": False, "reason": "not_human"}
    assert policy_of(table, FakeSources(off=("ci",))).gate(event_, thread(), []) == {"admissible": False, "reason": "not_human"}


def test_admits_inferred只在auto模式且规则都启用时成立():
    settings = make({"policy": {"rules": [*RULES, {"id": "off", "instruction": "x", "enabled": False}]}})
    auto = SettingsPolicy(settings, FakeSources())
    assert auto.admits_inferred(["copy"]) and auto.admits_inferred(["copy", "visual"])
    assert not auto.admits_inferred([]) and not auto.admits_inferred(["unknown"]) and not auto.admits_inferred(["copy", "off"])
    assert not policy_of({"eval": {"mode": "observe"}}).admits_inferred(["copy"])


def test_策略对象带着版本_暂停和研判超时():
    settings = make({"eval": {"timeout_seconds": 90}})
    settings["paused"] = True
    policy = SettingsPolicy(settings, FakeSources())
    assert (policy.version, policy.paused, policy.eval_timeout_ms, policy.mode) == (policy_version(settings), True, 90_000, "auto")


# ---------------------------------------------------------------- revalidate（docs/002 “Revalidation before resume”）


def mandate(strength: str, actor: str, basis: dict, source: str = "ding") -> dict:
    return {"strength": strength, "policyVersion": "v", "basis": basis, "actor": {"source": source, "id": actor}, "scope": ""}


ADDRESSED = {"via": "addressed", "source": "ding", "scope": {"chat": TEAM}, "row": TEAM, "sender": "Alice", "addressed": "mention"}
NOBODY = {"policy": {"sources": {"ding": {"allowed": []}}}}


def test_复核_来源必须还在收原来的线程():
    explicit = mandate("explicit", MEMBER, ADDRESSED)
    assert policy_of().revalidate(explicit) is True
    silent = FakeSources(chats={})
    assert policy_of(None, silent).revalidate(explicit) is False
    # 明确的跟进授权也一样要来源还在收
    followup = mandate("explicit", MEMBER, {"via": "followup", "source": "ding", "scope": {"chat": TEAM}, "row": TEAM})
    assert policy_of(None, silent).revalidate(followup) is False
    assert policy_of(None, FakeSources(off=("ding",))).revalidate(followup) is False
    # 但不要求来源还收那里的全部消息
    was_observed = mandate("explicit", MEMBER, {**ADDRESSED, "scope": {"chat": OBSERVED}, "row": OBSERVED})
    assert policy_of(None, FakeSources(chats={OBSERVED: "mention"})).revalidate(was_observed) is True


def test_复核_明确的跟进授权直接通过():
    followup = mandate("explicit", GUEST, {"via": "followup", "source": "ding", "scope": {"chat": TEAM}, "row": TEAM})
    for over in (None, NOBODY, {"eval": {"mode": "off"}}, {"policy": {"rules": []}}):
        assert policy_of(over).revalidate(followup) is True
    # 没记来源的（引擎自己的记录）也通过
    assert policy_of(NOBODY).revalidate(mandate("explicit", "", {"via": "followup"})) is True


def test_复核_推断的跟进授权要auto模式且命中的规则仍启用():
    followup = mandate("inferred", GUEST, {"via": "followup", "source": "ding", "scope": {"chat": TEAM}, "row": TEAM, "rules": ["copy"]})
    assert policy_of().revalidate(followup) is True
    disabled = {"policy": {"rules": [{**RULES[0], "enabled": False}, RULES[1]]}}
    deleted = {"policy": {"rules": [RULES[1]]}}
    for over in ({"eval": {"mode": "observe"}}, {"eval": {"mode": "off"}}, disabled, deleted):
        assert policy_of(over).revalidate(followup) is False


def test_复核_其余授权要发送人仍在来源的名单上():
    explicit = mandate("explicit", MEMBER, ADDRESSED)
    assert policy_of(NOBODY).revalidate(explicit) is False
    assert policy_of({"policy": {"sources": {"ding": {"allowed": [{"id": "ou_bob"}]}}}}).revalidate(explicit) is False
    # 模式和规则与明确请求无关
    assert policy_of({"eval": {"mode": "off"}, "policy": {"rules": []}}).revalidate(explicit) is True


def test_复核_机器人的授权要它仍在机器名单上_且auto模式_规则启用():
    bot = mandate("inferred", "cli_alerts", {"via": "bot", "source": "ding", "scope": {"chat": OBSERVED}, "row": OBSERVED, "sender": "Alerts",
                                             "observation": {"mode": "auto"}, "rules": ["copy"]})
    assert policy_of(BOTS).revalidate(bot) is True
    assert policy_of().revalidate(bot) is False                                             # 不在机器名单上了
    assert policy_of(deep_merge(BOTS, {"eval": {"mode": "observe"}})).revalidate(bot) is False
    assert policy_of(deep_merge(BOTS, {"policy": {"rules": [{**RULES[0], "enabled": False}]}})).revalidate(bot) is False
    assert policy_of(BOTS, FakeSources(chats={})).revalidate(bot) is False


def test_复核_系统事件的授权要系统来源还在收_auto模式_规则启用():
    basis = {"via": "admission", "cell": "unlistedUnaddressed", "source": "pipeline", "scope": {"type": "x.y", "origin": "cron"},
             "row": "events", "observation": {"mode": "auto"}, "rules": ["copy"]}
    system = mandate("inferred", "", basis, source="pipeline")
    assert policy_of().revalidate(system) is True
    assert policy_of({"eval": {"mode": "observe"}}).revalidate(system) is False
    assert policy_of({"policy": {"rules": [RULES[1]]}}).revalidate(system) is False
    assert policy_of(None, FakeSources(off=("pipeline",))).revalidate(system) is False
    closed = {"policy": {"sources": {"pipeline": {"admission": {"events": {"unlistedUnaddressed": "ignore"}}}}}}
    assert policy_of(closed).revalidate(system) is False


def test_复核_准入表格子给的强度低于接单时的强度就作废():
    gate = policy_of(with_table({TEAM: {"unlistedAddressed": "explicit"}})).gate(ding(STRANGER), thread(), [])
    accepted = mandate(gate["strength"], STRANGER, gate["basis"])
    assert policy_of(with_table({TEAM: {"unlistedAddressed": "explicit"}})).revalidate(accepted) is True
    assert policy_of(with_table({TEAM: {"unlistedAddressed": "inferred"}})).revalidate(accepted) is False
    assert policy_of(with_table({})).revalidate(accepted) is False


CELLS = ("listedUnaddressed", "unlistedAddressed", "unlistedUnaddressed")
ORIGINS = (("listedUnaddressed", "inferred"), ("unlistedAddressed", "explicit"), ("unlistedAddressed", "inferred"), ("unlistedUnaddressed", "inferred"))


def test_复核_准入表授权的全部组合():
    """原型 policy.spec.ts 的组合表：线程必须还在收；后来进了名单的人 @ 机器人是明确请求；后来进名单的人用名单内那一格；
    格子给的强度不能低于接单时；推断的还要 auto 模式和规则启用。"""
    sender, wrong = "ou_sender", []
    combos = itertools.product(ORIGINS, (True, False), ("off", "observe", "auto"), (True, False), ("all", "mention", "none"), (True, False))
    count = 0
    for (cell, strength), listed, mode, rule_on, scope, holds in combos:
        count += 1
        grant = lambda c: "ignore" if not holds else (strength if c == "unlistedAddressed" else "inferred")
        settings = normalize({"eval": {"mode": mode}, "policy": {
            "rules": [{"id": "copy", "instruction": "x", "enabled": rule_on}],
            "sources": {"ding": {"allowed": [{"id": sender}] if listed else [], "admission": {TEAM: {c: grant(c) for c in CELLS}}}}}})
        sources = FakeSources(chats={} if scope == "none" else {TEAM: scope})
        basis = {"via": "admission", "source": "ding", "scope": {"chat": TEAM}, "row": TEAM, "cell": cell,
                 **({"addressed": "mention"} if cell == "unlistedAddressed" else {}),
                 **({"observation": {"mode": "auto"}, "rules": ["copy"]} if strength == "inferred" else {})}
        if scope == "none":
            expected = False
        elif cell == "unlistedAddressed" and listed:
            expected = True
        else:
            now_cell = "listedUnaddressed" if cell == "unlistedUnaddressed" and listed else cell
            if (now_cell == "listedUnaddressed" and not listed) or not holds:
                expected = False
            else:
                expected = strength == "explicit" or (mode == "auto" and rule_on)
        actual = SettingsPolicy(settings, sources).revalidate(mandate(strength, sender, basis))
        if actual is not expected:
            wrong.append((cell, strength, listed, mode, rule_on, scope, holds, actual))
    assert count == 288 and wrong == []


# ---------------------------------------------------------------- may_answer


def test_批红权限_全权审批人_限定某条规则的人_其他人():
    settings = make({"policy": {"sources": {"ding": {"answerers": {"copy": ["ou_helper"], "visual": ["ou_painter"]}},
                                            "git": {"answerers": {"copy": ["ou_gitter"]}}}}})
    policy = SettingsPolicy(settings, FakeSources())
    card = {"id": "t1", "mandate": mandate("inferred", "", {"via": "admission", "rules": ["copy"]}, source="pipeline")}
    request = {"id": "t2", "mandate": mandate("explicit", MEMBER, ADDRESSED)}                 # 明确请求没有规则
    both = {"id": "t3", "mandate": mandate("inferred", "", {"via": "admission", "rules": ["visual", "copy"]}, source="pipeline")}
    who = lambda actor, source="ding": {"actor": {"source": source, "id": actor}, "human": True}
    assert policy.may_answer(who(MEMBER), card) and policy.may_answer(who(MEMBER), request)   # 名单上的人什么都能批
    assert policy.may_answer(who("OU_HELPER"), card) is True                                   # 不分大小写
    assert policy.may_answer(who("ou_helper"), request) is False
    assert policy.may_answer(who("ou_painter"), card) is False and policy.may_answer(who("ou_painter"), both) is True
    assert policy.may_answer(who(STRANGER), card) is False
    # 每个来源各自的名单：钉钉的限定审批人在别的来源上不算
    assert policy.may_answer(who("ou_helper", "git"), card) is False
    assert policy.may_answer(who("ou_gitter", "git"), card) is True
    assert policy.may_answer(who("ou_helper", "nowhere"), card) is False


# ---------------------------------------------------------------- policy_effects


def ticket(id_: str, strength: str, actor: str, basis: dict) -> dict:
    return {"id": id_, "mandate": mandate(strength, actor, basis)}


def edit(settings: dict, **rules) -> dict:
    """改规则：edit(s, copy={'instruction': …})；值为 None 表示删掉这条规则。"""
    out = copy.deepcopy(settings)
    out["policy"]["rules"] = [{**r, **(rules.get(r["id"]) or {})} for r in out["policy"]["rules"] if rules.get(r["id"], {}) is not None]
    return out


def allow(settings: dict, *ids: str) -> dict:
    out = copy.deepcopy(settings)
    out["policy"]["sources"]["ding"]["allowed"] = [{"id": i} for i in ids]
    return out


def mode(settings: dict, value: str) -> dict:
    out = copy.deepcopy(settings)
    out["eval"]["mode"] = value
    return out


def test_规则改了或停用了_只告诉凭这条规则接的工单():
    base = make()
    inferred = ticket("t1", "inferred", GUEST, {"via": "addressed", "rules": ["copy"]})
    explicit = ticket("t2", "explicit", MEMBER, {"via": "addressed", "source": "ding"})
    scope = Listening()
    # 工单没凭它接单的规则变了，与它无关
    assert policy_effects([inferred, explicit], base, edit(base, visual={"enabled": False}), scope) == {}
    assert policy_effects([inferred, explicit], base, edit(base, visual={"instruction": "别的"}), scope) == {}
    assert policy_effects([inferred, explicit], base, edit(base, copy={"instruction": "Fix typos only."}), scope) == {
        "t1": {"rules": [{"id": "copy", "name": "Copy", "change": "changed", "instruction": "Fix typos only."}]}}
    changed_when = policy_effects([inferred], base, edit(base, copy={"when": {"origin": ["*"], "types": ["a.b"]}}), scope)
    assert changed_when == {"t1": {"rules": [{"id": "copy", "name": "Copy", "change": "changed", "instruction": "Fix typos."}]}}
    assert policy_effects([inferred], base, edit(base, copy={"enabled": False}), scope) == {
        "t1": {"rules": [{"id": "copy", "name": "Copy", "change": "disabled"}]}}
    # 删掉的规则按停用报
    assert policy_effects([inferred], base, edit(base, copy=None), scope) == {"t1": {"rules": [{"id": "copy", "name": "Copy", "change": "disabled"}]}}
    # 本来就停用的规则再怎么改都不用说；不影响版本的字段（tell）变了也不说
    off = edit(base, copy={"enabled": False})
    assert policy_effects([inferred], off, edit(off, copy={"instruction": "改了"}), scope) == {}
    assert policy_effects([inferred], off, edit(off, copy=None), scope) == {}
    assert policy_effects([inferred], base, edit(base, copy={"tell": "always"}), scope) == {}


def test_工单凭多条规则接单时_各条的变化都列出来():
    base = make()
    both = ticket("t1", "inferred", GUEST, {"via": "addressed", "rules": ["copy", "visual"]})
    now = edit(base, copy={"instruction": "新的"}, visual={"enabled": False})
    assert policy_effects([both], base, now, Listening()) == {"t1": {"rules": [
        {"id": "copy", "name": "Copy", "change": "changed", "instruction": "新的"}, {"id": "visual", "name": "Visual", "change": "disabled"}]}}


def test_请求人被移出名单_告诉他的工单_名单加人与谁都无关():
    base = make()
    inferred = ticket("t1", "inferred", GUEST, {"via": "addressed", "rules": ["copy"]})
    explicit = ticket("t2", "explicit", MEMBER, {"via": "addressed", "source": "ding"})
    scope = Listening()
    assert policy_effects([inferred, explicit], base, allow(base, MEMBER, "ou_bob"), scope) == {}
    assert policy_effects([inferred, explicit], base, allow(base), scope) == {
        "t2": {"requesterAllowed": False, "requester": {"source": "ding", "id": MEMBER}}}
    # 名单上的机器人账号被移出名单，同样点名它
    bot = ticket("t4", "explicit", "cli_ci_bot", {"via": "addressed", "source": "ding"})
    assert policy_effects([bot], allow(base, "cli_ci_bot"), base, scope) == {
        "t4": {"requesterAllowed": False, "requester": {"source": "ding", "id": "cli_ci_bot"}}}


def test_接单模式离开auto_只告诉推断接单的工单不再主动接():
    auto = make()
    scope = Listening()
    proactive = ticket("t3", "inferred", MEMBER, {"via": "admission", "cell": "listedUnaddressed", "source": "ding", "scope": {"chat": OBSERVED},
                                                  "rules": ["copy"], "observation": {"mode": "auto"}})
    explicit = ticket("t2", "explicit", MEMBER, {"via": "addressed", "source": "ding"})
    followup = ticket("t5", "inferred", GUEST, {"via": "followup", "source": "ding", "scope": {"chat": TEAM}, "rules": ["copy"]})
    explicit_followup = ticket("t6", "explicit", MEMBER, {"via": "followup", "source": "ding", "scope": {"chat": TEAM}, "rules": ["copy"]})
    for target in ("observe", "off"):
        # 写这条消息的人仍然可以发起工作，所以不是 requesterAllowed
        assert policy_effects([proactive, explicit, followup, explicit_followup], auto, mode(auto, target), scope) == {
            "t3": {"proactive": False}, "t5": {"proactive": False}}
    # 反过来回到 auto、或本来就不在 auto，都不用说
    assert policy_effects([proactive, followup], mode(auto, "observe"), auto, scope) == {}
    assert policy_effects([proactive, followup], mode(auto, "observe"), mode(auto, "off"), scope) == {}


def test_准入表格子变弱_点名发送人_模式变化只说不再主动接():
    def table(cell: str, mode_: str = "auto") -> dict:
        return make(with_table({OBSERVED: {"unlistedUnaddressed": cell}}, mode_))
    scope = Listening(OBSERVED)
    proactive = ticket("t3", "inferred", GUEST, {"via": "admission", "cell": "unlistedUnaddressed", "source": "ding", "scope": {"chat": OBSERVED},
                                                 "row": OBSERVED, "rules": ["copy"], "observation": {"mode": "auto"}})
    assert policy_effects([proactive], table("inferred"), table("inferred"), scope) == {}
    assert policy_effects([proactive], table("inferred"), table("ignore"), scope) == {
        "t3": {"requesterAllowed": False, "requester": {"source": "ding", "id": GUEST}}}
    assert policy_effects([proactive], table("inferred"), table("inferred", "observe"), scope) == {"t3": {"proactive": False}}


def test_规则停用同时让授权失效时_只报规则停用():
    base = make()
    proactive = ticket("t1", "inferred", "", {"via": "admission", "cell": "unlistedUnaddressed", "source": "pipeline", "scope": {},
                                              "row": "events", "rules": ["copy"], "observation": {"mode": "auto"}})
    proactive["mandate"]["actor"]["source"] = "pipeline"
    assert policy_effects([proactive], base, edit(base, copy={"enabled": False}), FakeSources()) == {
        "t1": {"rules": [{"id": "copy", "name": "Copy", "change": "disabled"}]}}


# ---------------------------------------------------------------- Settings


POLICY_FILE = {
    "eval": {"mode": "auto", "timeout_seconds": 120},
    "executor": {"max_workers": 4},
    "policy": {"sources": {"ding": {"allowed": [{"id": MEMBER, "name": "Alice"}]}},
               "rules": [{"id": "collect", "name": "采集", "instruction": "采集数据", "when": {"origin": "cron", "types": ["cron.collect"]}},
                         {"id": "brief", "name": "日报", "instruction": "写日报", "when": {"origin": "cron", "types": ["cron.brief"]}}]},
}


class Changes:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, old: str, new: str, effects: dict) -> None:
        self.calls.append((old, new, effects))


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("SUPEROPS_HOME", str(home))
    monkeypatch.setenv("SUPEROPS_DATA", str(data))

    class Env:
        pass
    e = Env()
    e.home, e.data = home, data
    e.store = Store(tmp_path / "db.sqlite")
    e.changes = Changes()
    e.write = lambda content, name="policy.yaml", where=home: (where / name).write_text(
        content if isinstance(content, str) else yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    e.write(POLICY_FILE)
    e.settings = Settings(e.store, FakeSources(), home=home, on_change=e.changes)
    e.settings.current()                    # 和 Runtime 一样：造出来就先读一遍文件
    yield e
    e.store.close()


def open_ticket(store, id_: str, rule_id: str) -> dict:
    """一张凭规则主动接的、还没结束的工单（系统事件起的）。"""
    saved = {"id": id_, "no": 1, "title": id_, "createdAt": "2026-11-11T00:00:00.000Z", "updatedAt": "2026-11-11T00:00:00.000Z",
             "assignments": [], "inputs": [], "undelivered": [], "channels": [],
             "mandate": {"strength": "inferred", "policyVersion": "v", "scope": "", "actor": {"source": "pipeline", "id": ""},
                         "basis": {"via": "admission", "cell": "unlistedUnaddressed", "source": "pipeline", "scope": {"type": "cron.collect"},
                                   "row": "events", "rules": [rule_id], "observation": {"mode": "auto"}}}}
    store.tickets.save(saved)
    return saved


def test_读文件得到当前设置_数据目录的local_yaml按同名键深度覆盖(env):
    current = env.settings.current()
    assert current["eval"]["mode"] == "auto" and [r["id"] for r in current["policy"]["rules"]] == ["collect", "brief"]
    assert current["paused"] is False and current["paused_keys"] == {}
    env.write({"eval": {"timeout_seconds": 60}, "policy": {"sources": {"ding": {"answerers": {"collect": ["ou_helper"]}}}},
               "sources": {"ding": {"chats": ["cid1"]}}, "agents": {"ignored": True}}, name="local.yaml", where=env.data)
    env.settings.reload()
    merged = env.settings.current()
    assert merged["eval"] == {"mode": "auto", "timeout_seconds": 60, "role": "decide", "effort": "medium"}
    assert merged["policy"]["sources"]["ding"]["allowed"] == [{"id": MEMBER, "name": "Alice"}]       # 没被覆盖的键还在
    assert merged["policy"]["sources"]["ding"]["answerers"] == {"collect": ["ou_helper"]}
    assert merged["sources"]["ding"] == {"enabled": True, "config": {"chats": ["cid1"]}}
    assert "agents" not in merged


def test_policy给出当前设置下的策略对象(env):
    policy = env.settings.policy()
    assert policy.version == policy_version(env.settings.current()) and policy.mode == "auto" and policy.paused is False
    assert policy.gate(ding(), thread(), [])["strength"] == "explicit"


def test_set_mode改接单模式_版本变化_留历史并通知(env):
    before = policy_version(env.settings.current())
    result = env.settings.set_mode("observe", origin="panel")
    after = policy_version(env.settings.current())
    assert result == {"policy_version": after, "changed": True, "mode": "observe", "paused": False, "paused_keys": [], "tickets_notified": []}
    assert before != after and env.settings.current()["eval"]["mode"] == "observe"
    assert env.changes.calls == [(before, after, {})]
    [record] = env.settings.history()
    assert {k: record[k] for k in ("revision", "origin", "from", "to", "tickets")} == {
        "revision": 1, "origin": "panel", "from": before, "to": after, "tickets": []}
    assert record["at"].endswith("Z") and env.store.get("policy-version") == after


def test_set_mode设成同一个模式什么都不记_不认识的模式被拒绝(env):
    result = env.settings.set_mode("auto")
    assert result["changed"] is False and "tickets_notified" not in result
    assert env.changes.calls == [] and env.settings.history() == []
    with pytest.raises(UserError) as error:
        env.settings.set_mode("yolo")
    assert error.value.code == "invalidSettings" and env.settings.current()["eval"]["mode"] == "auto"


def test_运行时开关存在库里_换一个Settings实例还在(env):
    env.settings.set_mode("off")
    env.settings.pause(True)
    env.settings.pause(True, "rule:collect", reason="维护")
    again = Settings(env.store, FakeSources(), home=env.home)
    current = again.current()
    assert current["eval"]["mode"] == "off" and current["paused"] is True and list(current["paused_keys"]) == ["rule:collect"]


def test_pause全部_不改策略版本_不通知_不留历史(env):
    before = policy_version(env.settings.current())
    result = env.settings.pause(True)
    assert result == {"policy_version": before, "changed": False, "mode": "auto", "paused": True, "paused_keys": []}
    assert env.settings.current()["paused"] is True and env.settings.policy().paused is True
    assert env.settings.pause(False)["paused"] is False and env.settings.current()["paused"] is False
    assert env.changes.calls == [] and env.settings.history() == []


def test_pause单独暂停规则_Agent_事件来源(env):
    s = env.settings
    assert s.pause(True, "rule:collect", reason="采集源在维护")["paused_keys"] == ["rule:collect"]
    s.pause(True, "agent:claude")
    s.pause(True, "origin:douyin")
    assert s.is_paused("rule:collect") and s.is_paused("agent:claude") and s.is_paused("origin:douyin")
    assert not s.is_paused("rule:brief") and not s.is_paused("agent:codex")
    assert s.is_paused("rule:brief", "agent:claude")                       # 任意一个暂停了就算
    entry = s.current()["paused_keys"]["rule:collect"]
    assert entry["reason"] == "采集源在维护" and entry["at"].endswith("Z")
    assert s.current()["paused"] is False                                  # 单独暂停不等于暂停全部
    assert s.pause(False, "agent:claude")["paused_keys"] == ["origin:douyin", "rule:collect"]
    assert not s.is_paused("agent:claude")
    s.pause(False, "agent:never-paused")                                   # 恢复一个没暂停的不出错
    assert env.changes.calls == [] and env.settings.history() == []        # 这些都不是策略变更


def test_pause不带前缀的名字_先按规则id再按规则名_否则当作事件来源(env):
    s = env.settings
    assert s.normalize_pause_key("collect") == "rule:collect"
    assert s.normalize_pause_key("日报") == "rule:brief"
    assert s.normalize_pause_key("douyin") == "origin:douyin"
    assert s.normalize_pause_key("agent:claude") == "agent:claude"
    s.pause(True, "日报")
    s.pause(True, "douyin")
    assert sorted(s.current()["paused_keys"]) == ["origin:douyin", "rule:brief"]
    s.pause(False, "brief")                                                # 用 id 恢复按名字暂停的同一条规则
    assert sorted(s.current()["paused_keys"]) == ["origin:douyin"]


def test_reload读到改过的文件_版本变化并通知(env):
    before = policy_version(env.settings.current())
    changed = copy.deepcopy(POLICY_FILE)
    changed["policy"]["rules"][0]["instruction"] = "只采集抖音"
    env.write(changed)
    assert env.settings.current()["policy"]["rules"][0]["instruction"] == "采集数据"      # 不 reload 不会自己变
    result = env.settings.reload()
    after = result["policy_version"]
    assert result["changed"] is True and after != before
    assert env.settings.current()["policy"]["rules"][0]["instruction"] == "只采集抖音"
    assert env.changes.calls == [(before, after, {})] and env.settings.history()[0]["origin"] == "file"
    # 文件没变再 reload：什么都不记
    assert env.settings.reload()["changed"] is False and len(env.changes.calls) == 1 and len(env.settings.history()) == 1


def test_reload只改了不影响版本的字段_生效但不算策略变更(env):
    changed = copy.deepcopy(POLICY_FILE)
    changed["eval"]["timeout_seconds"] = 300
    changed["policy"]["rules"][0]["tell"] = "always"
    env.write(changed)
    assert env.settings.reload()["changed"] is False
    assert env.settings.current()["eval"]["timeout_seconds"] == 300 and env.settings.current()["policy"]["rules"][0]["tell"] == "always"
    assert env.changes.calls == [] and env.settings.history() == []


@pytest.mark.parametrize("broken", [
    {"policy": {"rules": [{"id": "collect", "instruction": "x"}, {"id": "collect", "instruction": "y"}]}},   # id 重复
    {"policy": {"rules": [{"id": "collect"}]}},                                                              # 缺 instruction
    {"eval": {"mode": "yolo"}},
    "policy: [unclosed",                                                                                    # 不是合法 YAML
    None,                                                                                                   # 文件没了
], ids=["重复id", "缺instruction", "模式不合法", "YAML语法错", "文件不存在"])
def test_reload遇到不合法的文件就拒绝_保留原来的设置(env, broken):
    env.settings.set_mode("observe")
    kept = copy.deepcopy(env.settings.current())
    calls, history = len(env.changes.calls), len(env.settings.history())
    if broken is None:
        (env.home / "policy.yaml").unlink()
    else:
        env.write(broken)
    with pytest.raises(UserError) as error:
        env.settings.reload()
    assert error.value.code == "invalidSettings"
    assert env.settings.current() == kept
    assert len(env.changes.calls) == calls and len(env.settings.history()) == history
    env.settings.pause(True)                                               # 之后的运行时开关仍然基于原来的设置
    assert env.settings.current()["policy"] == kept["policy"] and env.settings.current()["paused"] is True


@pytest.mark.parametrize("broken", ["policy: 就这样\n", "- a\n- b\n", "policy:\n  sources: [ding]\n"], ids=["policy是字符串", "整份是列表", "sources是列表"])
def test_reload遇到形状不对的文件也报invalidSettings(env, broken):
    kept = copy.deepcopy(env.settings.current())
    env.write(broken)
    try:
        with pytest.raises(UserError) as error:
            env.settings.reload()
        assert error.value.code == "invalidSettings"
    finally:
        assert env.settings.current() == kept                              # 不管报什么错，原设置都得还在


def test_reload之后运行时的模式和暂停仍然盖在文件之上(env):
    env.settings.set_mode("observe")
    env.settings.pause(True, "rule:collect")
    changed = copy.deepcopy(POLICY_FILE)
    changed["eval"]["mode"] = "off"
    changed["policy"]["rules"][1]["instruction"] = "写周报"
    env.write(changed)
    env.settings.reload()
    current = env.settings.current()
    assert current["eval"]["mode"] == "observe" and list(current["paused_keys"]) == ["rule:collect"]
    assert current["policy"]["rules"][1]["instruction"] == "写周报"


def test_版本变化时带上受影响的工单(env):
    affected = open_ticket(env.store, "t-collect", "collect")
    open_ticket(env.store, "t-brief", "brief")
    ended = open_ticket(env.store, "t-ended", "collect")
    env.store.tickets.save({**ended, "ended": {"at": "2026-11-11T01:00:00.000Z", "by": "executor", "outcome": "completed"}})
    changed = copy.deepcopy(POLICY_FILE)
    changed["policy"]["rules"][0]["instruction"] = "只采集抖音"
    env.write(changed)
    result = env.settings.reload(origin="mr")
    effects = {affected["id"]: {"rules": [{"id": "collect", "name": "采集", "change": "changed", "instruction": "只采集抖音"}]}}
    assert result["tickets_notified"] == ["t-collect"]                    # 已结束的、没凭这条规则接的都不在内
    [(old, new, got)] = env.changes.calls
    assert got == effects and old != new
    assert env.settings.history()[0]["tickets"] == ["t-collect"] and env.settings.history()[0]["origin"] == "mr"
    # 模式离开 auto：两张主动接的工单都被告知不再主动接
    env.settings.set_mode("observe")
    assert env.changes.calls[-1][2] == {"t-collect": {"proactive": False}, "t-brief": {"proactive": False}}
    assert env.settings.history()[0]["tickets"] == ["t-brief", "t-collect"]


def test_历史记录新的在前_limit只取最近几条(env):
    for target in ("observe", "off", "auto", "observe"):
        env.settings.set_mode(target, origin=f"to-{target}")
    history = env.settings.history()
    assert [h["revision"] for h in history] == [4, 3, 2, 1]
    assert [h["origin"] for h in history] == ["to-observe", "to-auto", "to-off", "to-observe"]
    assert all(a["from"] == b["to"] for a, b in zip(history, history[1:]))   # 每一条接着上一条
    assert [h["revision"] for h in env.settings.history(limit=2)] == [4, 3]


def test_start_停机期间策略变了按一次策略变更处理(env):
    env.settings.start()                                                   # 第一次启动：只记下版本
    first = env.store.get("policy-version")
    assert first == policy_version(env.settings.current()) and env.changes.calls == [] and env.settings.history() == []
    changed = copy.deepcopy(POLICY_FILE)
    changed["policy"]["rules"].pop()
    env.write(changed)
    restarted = Settings(env.store, FakeSources(), home=env.home, on_change=env.changes)
    restarted.start()
    second = policy_version(restarted.current())
    assert second != first and env.changes.calls == [(first, second, {})]
    assert restarted.history()[0]["origin"] == "file" and env.store.get("policy-version") == second
    restarted.start()                                                      # 版本没变再启动：不重复通知
    assert len(env.changes.calls) == 1


def test_没有on_change也能保存(env):
    quiet = Settings(env.store, FakeSources(), home=env.home)
    assert quiet.set_mode("off")["changed"] is True and len(quiet.history()) == 1
