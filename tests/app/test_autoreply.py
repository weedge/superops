"""钉钉自动回复：开关、去重、运营群不接、限流、起草后发出、要本人拿主意的转告。"""
from __future__ import annotations

import pytest

from superops.app import autoreply as ar
from superops.executor.pool import Outcome


def msg(mid="m1", chat="cidX", text="这周手上最主要的一件事，现在到哪一步了", sender="李四", sid="ou_1"):
    return {"message_id": mid, "conversation_id": chat, "content": text, "sender": sender, "sender_open_dingtalk_id": sid,
            "create_time": "2026-11-11 18:40:40"}


@pytest.fixture
def bot(world, monkeypatch):
    sent, runs, notes = [], [], []
    reply = {"action": "reply", "reply": "在做 Popto 本地多 Agent 自动化的流程优化，发布已跑通。", "reason": "问进展", "escalate": False}

    def fake_run(**kw):
        runs.append(kw["prompt"]("claude", None, False))
        return Outcome(ok=True, output=dict(world.next_reply if hasattr(world, "next_reply") else reply), agent="claude")
    monkeypatch.setattr(world.agents, "run", fake_run)
    monkeypatch.setattr(world.autoreply, "send", lambda payload, text: (sent.append((payload, text)), {"ok": True})[1])
    monkeypatch.setattr(world.autoreply, "_history", lambda payload: [])
    monkeypatch.setattr(world, "notify", lambda kind, title, body, **k: notes.append((kind, title, body)) or {"ok": True})
    world.sent, world.runs, world.notes = sent, runs, notes
    return world


def test_默认关着_收到也不回(bot):
    assert bot.autoreply.mode() == "off"
    assert bot.autoreply.receive(msg(), "at") == "off"
    bot.effects.run_until_idle()
    assert bot.sent == [] and bot.runs == []


def test_打开后群里at我_起草并引用回复_同一条只回一次(bot):
    bot.autoreply.set_mode("auto", by="test")
    assert bot.autoreply.receive(msg(), "at") == "queued"
    bot.effects.run_until_idle()
    payload, text = bot.sent[0]
    assert payload["kind"] == "at" and payload["message_id"] == "m1"
    assert text.startswith("在做 Popto") and text.endswith(ar.signature(bot.cfg))
    assert "<<<UNTRUSTED_DATA" in bot.runs[0] and "这周手上最主要的一件事" in bot.runs[0]
    assert bot.autoreply.receive(msg(), "at") == "duplicate"
    rec = bot.autoreply.view()["recent"][0]
    assert rec["status"] == "sent" and rec["sender"] == "李四"


def test_单聊按对方ID发_运营群里的at不在这里回(bot):
    bot.autoreply.set_mode("auto")
    assert bot.autoreply.receive(msg(mid="m2", chat="cidOps"), "at", skip_chats=["cidOps"]) == "ops_chat"
    assert bot.autoreply.receive(msg(mid="m3", chat="cidO2O"), "direct", skip_chats=["cidO2O"]) == "queued"  # 单聊不受运营群限制
    bot.effects.run_until_idle()
    assert [p["kind"] for p, _ in bot.sent] == ["direct"] and bot.sent[0][0]["sender_id"] == "ou_1"


def test_要本人拿主意的_不回并转告(bot):
    bot.autoreply.set_mode("auto")
    bot.next_reply = {"action": "skip", "reply": "", "reason": "要他答应排期", "escalate": True, "escalate_note": "李四问下周能不能接新需求"}
    bot.autoreply.receive(msg(mid="m4", text="下周能接这个需求吗"), "direct")
    bot.effects.run_until_idle()
    assert bot.sent == []
    assert bot.notes and "李四" in bot.notes[0][1] and "下周能不能接" in bot.notes[0][2]
    assert bot.autoreply.view()["recent"][0]["status"] == "skipped"


def test_同一会话一小时最多回六条(bot):
    bot.autoreply.set_mode("auto")
    for i in range(ar.PER_CHAT_HOUR):
        bot.autoreply.receive(msg(mid=f"x{i}"), "at")
        bot.effects.run_until_idle()
    assert len(bot.sent) == ar.PER_CHAT_HOUR
    assert bot.autoreply.receive(msg(mid="x-last"), "at") == "limited"


def test_开关只认_auto_off(bot):
    with pytest.raises(ValueError):
        bot.autoreply.set_mode("on")


def test_接口和总览带开关(bot):
    from superops.app import http, views
    assert http.handle_post(bot, "/api/autoreply", {"mode": "auto"})["mode"] == "auto"
    assert http.handle_get(bot, "/api/autoreply", {})["mode"] == "auto"
    assert views.overview(bot)["autoreply"]["mode"] == "auto"
    http.handle_post(bot, "/api/autoreply", {"mode": "off"})
    assert bot.autoreply.mode() == "off"


GROUPS = {"cidSOS0000000==": "SOS报警记录群", "cidBASE000000==": "基础平台组告警群", "cidDUP1000000==": "重名群", "cidDUP2000000==": "重名群",
          "cidWORK000000==": "AdsTurbo&Popto&基础平台"}


@pytest.fixture
def dws(monkeypatch):
    calls = []

    def fake(args):
        calls.append(args)
        if args[1] == "+conversation-info":
            name = GROUPS.get(args[3])
            return {"result": {"conversationInfo": {"title": name}}} if name else {}
        if args[1] == "+chat-search":
            return {"chats": [{"name": n, "openConversationId": c} for c, n in GROUPS.items() if args[3] in n]}
        return {}
    monkeypatch.setattr(ar, "_dws", fake)
    return calls


def test_过滤群_填群名查到唯一的群ID_按ID过滤(bot, dws):
    bot.autoreply.set_mode("auto")
    view = bot.autoreply.add_exclude("SOS报警记录群")
    assert view["exclude"] == [{"id": "cidSOS0000000==", "name": "SOS报警记录群"}]
    assert bot.autoreply.receive(msg(mid="a1", chat="cidSOS0000000=="), "at") == "excluded"
    assert bot.autoreply.receive(msg(mid="a2", chat="cidWORK000000=="), "at") == "queued"
    assert bot.autoreply.receive(msg(mid="a3", chat="cidSOS0000000=="), "direct") == "queued"   # 只过滤群里的 @，不管单聊
    bot.effects.run_until_idle()
    assert all(r["chat"] != "cidSOS0000000==" or r["kind"] == "direct" for r in bot.autoreply.view()["recent"])


def test_过滤群_填群ID顺手查出群名(bot, dws):
    view = bot.autoreply.add_exclude("cidBASE000000==")
    assert view["exclude"] == [{"id": "cidBASE000000==", "name": "基础平台组告警群"}]


def test_过滤群_群名查不到唯一的_按群名过滤(bot, dws):
    bot.autoreply.set_mode("auto")
    view = bot.autoreply.add_exclude("重名群")
    assert view["exclude"] == [{"id": None, "name": "重名群"}]
    assert bot.autoreply.receive(msg(mid="b1", chat="cidDUP2000000=="), "at") == "excluded"   # 查这个群叫什么，按名字对上
    assert bot.autoreply.receive(msg(mid="b2", chat="cidWORK000000=="), "at") == "queued"


def test_过滤群_移除_不重复添加_关着时不去查(bot, dws):
    bot.autoreply.add_exclude("SOS报警记录群")
    bot.autoreply.add_exclude("cidSOS0000000==")
    assert len(bot.autoreply.excludes()) == 1
    assert bot.autoreply.remove_exclude("SOS报警记录群")["exclude"] == []
    with pytest.raises(ValueError):
        bot.autoreply.add_exclude("  ")
    dws.clear()
    bot.autoreply.add_exclude("重名群")
    dws.clear()
    assert bot.autoreply.receive(msg(mid="c1", chat="cidDUP1000000=="), "at") == "off" and dws == []


def test_过滤群_库里没有名单时用配置里的初始名单(bot, dws):
    bot.cfg.setdefault("sources", {}).setdefault("dingtalk", {})["auto_reply_exclude"] = [
        {"id": "cidSOS0000000==", "name": "SOS报警记录群"}, "基础平台组告警群"]
    assert bot.autoreply.excludes() == [{"id": "cidSOS0000000==", "name": "SOS报警记录群"}, {"id": None, "name": "基础平台组告警群"}]
    assert bot.autoreply.excluded("cidBASE000000==")["name"] == "基础平台组告警群"


def test_过滤群_接口(bot, dws):
    from superops.app import http
    assert http.handle_post(bot, "/api/autoreply/exclude", {"action": "add", "group": "基础平台组告警群"})["exclude"][0]["id"] == "cidBASE000000=="
    assert http.handle_post(bot, "/api/autoreply/exclude", {"action": "remove", "group": "cidBASE000000=="})["exclude"] == []


def test_同一会话30分钟内只转告一次(bot):
    bot.autoreply.set_mode("auto")
    bot.next_reply = {"action": "skip", "reply": "", "reason": "线上告警", "escalate": True, "escalate_note": "有告警"}
    for i in range(3):
        bot.autoreply.receive(msg(mid=f"e{i}", chat="cidAlarm", text="🔴 生产环境告警"), "at")
        bot.effects.run_until_idle()
    assert len(bot.notes) == 1
    recent = bot.autoreply.view()["recent"]
    assert [r["escalated"] for r in recent] == [False, False, True]


def test_署名和提示词里的本人称呼来自_owner_name(bot, monkeypatch):
    assert ar.signature({}) == "—— 我的 Agent 自动回复"
    monkeypatch.setitem(bot.cfg, "owner_name", "张三")
    assert ar.signature(bot.cfg) == "—— 张三的 Agent 自动回复"
    (bot.home / "prompts").mkdir(parents=True, exist_ok=True)
    (bot.home / "prompts" / "auto-reply.md").write_text("你是{owner}的 Agent。", encoding="utf-8")
    bot.autoreply.set_mode("auto", by="test")
    bot.autoreply.receive(msg(), "at")
    bot.effects.run_until_idle()
    assert bot.sent[0][1].endswith("—— 张三的 Agent 自动回复")
    assert "{owner}" not in bot.runs[0] and "你是张三的 Agent" in bot.runs[0]
