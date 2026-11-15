"""飞书来源：以我本人身份轮询群消息（批红、跟进、指令同钉钉）、轮询单聊和 @我（自动回复）、发消息记下 ID 防回声。"""
from __future__ import annotations

import pytest

from superops.infra.notifier import lark


def msg(mid, text, sender="ou_me", chat="oc_ops", t="2026-11-12T19:00:05+08:00", **kw):
    return {"message_id": mid, "chat_id": chat, "conversation_id": chat, "content": text, "sender_id": sender,
            "sender": "张三" if sender == "ou_me" else "同事", "create_time": t, "chat_type": kw.get("chat_type", "group"),
            "is_at_me": kw.get("is_at_me", False)}


@pytest.fixture
def src(world, monkeypatch):
    s = world.lark
    monkeypatch.setattr(s, "chats", lambda: ["oc_ops"])
    monkeypatch.setattr(s, "_people", lambda: (["ou_me"], []))
    s.me = "ou_me"
    return s


def test_normalize_认几种字段写法_只认open_id():
    a = lark.normalize({"message_id": "om_1", "chat_id": "oc_1", "content": "T12 1", "sender": {"id": "ou_a", "name": "张三"},
                        "create_time": "1794470000000"})
    assert a["sender_id"] == "ou_a" and a["create_time"].startswith("2026-11-12") and a["content"] == "T12 1"
    b = lark.normalize({"message_id": "om_2", "body": {"content": '{"text":"hi"}'}, "sender": {"sender_id": {"open_id": "ou_b"}},
                        "chat": {"chat_id": "oc_2", "chat_type": "p2p"}})
    assert (b["content"], b["sender_id"], b["chat_id"], b["chat_type"]) == ("hi", "ou_b", "oc_2", "p2p")
    assert lark.normalize({"message_id": "om_3", "content": "  "}) is None
    assert lark.message_id_of({"data": {"message_id": "om_9"}}) == "om_9"


def test_轮询运营群_第一次从现在开始_系统发的跳过_我打的T1_1照常批红(src, world, monkeypatch):
    world.schedule.fire("cron.publish_batch_card")
    world.pump()
    assert world.store.tickets.by_no(1).get("attention")
    calls = []
    monkeypatch.setattr(lark, "list_messages", lambda chat, since: calls.append(since) or [
        msg("om_sys", "**T1 · 今晚发布 2 条**"), msg("om_me", "T1 1"), msg("om_me", "T1 1")])
    assert src.poll_groups() == 0 and calls == []                         # 第一次：只记下“从现在开始”
    src.remember_sent("om_sys")                                          # 系统以我的身份发的卡片
    assert src.poll_groups() == 1 and calls                              # 重复的 om_me 只处理一次
    world.pump()
    assert not world.store.tickets.by_no(1).get("attention")             # 批掉了
    assert src.poll_groups() == 0                                        # 再拉到同样的消息：已处理过


def test_名单外的人发指令不执行_at兔管家是新请求(src, world, monkeypatch):
    said = []
    monkeypatch.setattr(src, "_say", lambda chat, text, k: said.append(text))
    src.host.store.set("lark:cursor:chat:oc_ops", "2026-11-12T19:00:00+08:00")
    monkeypatch.setattr(lark, "list_messages", lambda chat, since: [msg("om_x", "暂停 all", sender="ou_other"),
                                                                    msg("om_y", "@兔管家 帮我看下今天的数据", sender="ou_me")])
    received = []
    real = world.receive
    monkeypatch.setattr(world, "receive", lambda t: received.append(t) or real(t))
    src.poll_groups()
    assert said and "没有权限" in said[0] and not world.settings.is_paused(None)
    assert received and received[-1]["source"] == "lark" and received[-1]["addressed"] == "mention"
    assert received[-1]["channel"] == {"source": "lark", "thread": received[-1]["threads"][0]["id"], "address": {"chat": "oc_ops"}}


def test_轮询单聊和at我_交给自动回复_我自己发的不回(src, world, monkeypatch):
    src.host.store.set("lark:cursor:autoreply", "2026-11-12T19:00:00+08:00")
    got = []
    monkeypatch.setattr(world.autoreply, "receive", lambda raw, kind, skip_chats=(), platform="dingtalk": got.append((raw["message_id"], kind, platform)) or "queued")
    monkeypatch.setattr(lark, "search", lambda since, p2p=False, at_me=False: (
        [msg("om_p", "在吗", sender="ou_a", chat="oc_dm", chat_type="p2p"), msg("om_mine", "好的", sender="ou_me", chat="oc_dm")] if p2p
        else [msg("om_at", "@张三 这周进展？", sender="ou_b", chat="oc_work", is_at_me=True)]))
    assert src.poll_autoreply() == 2
    assert got == [("om_p", "direct", "lark"), ("om_at", "at", "lark")]
    assert src.poll_autoreply() == 0


def test_发消息走本人身份_记下消息ID(src, monkeypatch):
    sent = []
    monkeypatch.setattr(lark, "send_markdown", lambda chat, text, idempotency_key=None: sent.append((chat, text)) or "om_new")
    assert src.send({"channel": {"source": "lark", "address": {"chat": "oc_ops"}}, "text": "**T3 · 结果**"}) == "sent"
    assert sent == [("oc_ops", "**T3 · 结果**")] and "om_new" in src.host.store.get("lark:sent")
    monkeypatch.setattr(lark, "send_markdown", lambda *a, **k: (_ for _ in ()).throw(lark.LarkError("token expired")))
    with pytest.raises(RuntimeError, match="飞书发送失败"):
        src.send({"channel": {"source": "lark", "address": {}}, "text": "x"})


def test_自动回复_飞书消息用引用回复_回过的不再当新消息(world, monkeypatch):
    replies = []
    monkeypatch.setattr(lark, "reply", lambda mid, text: replies.append((mid, text)) or "om_reply")
    r = world.autoreply.send({"platform": "lark", "kind": "direct", "message_id": "om_p", "chat": "oc_dm", "sender_id": "ou_a"}, "收到")
    assert r == {"ok": True} and replies == [("om_p", "收到")] and "om_reply" in world.store.get("lark:sent")
    world.autoreply.set_mode("auto")
    assert world.autoreply.receive(msg("om_q", "在吗", sender="ou_a", chat="oc_dm"), "direct", platform="lark") == "queued"


def test_钉钉和飞书只能二选一():
    from superops.app.settings import normalize
    with pytest.raises(ValueError, match="二选一"):
        normalize({"sources": {"dingtalk": {"enabled": True}, "lark": {"enabled": True}}})
    assert normalize({"sources": {"dingtalk": {"enabled": False}, "lark": {"enabled": True}}})["sources"]["lark"]["enabled"]
    assert normalize({"sources": {"dingtalk": {"enabled": True}}})["sources"]["dingtalk"]["enabled"]


def test_自动回复的名字跟着开着的那个(world, monkeypatch):
    assert world.autoreply.app() == "钉钉"
    cur = world.settings.current()
    monkeypatch.setattr(world.settings, "current", lambda: {**cur, "sources": {**cur["sources"], "lark": {"enabled": True, "config": {}}}})
    assert world.autoreply.view()["app"] == "飞书"


def test_真实格式_系统消息跳过_分钟精度的本地时间():
    # 2026-11-12 对着“测试superops”群核对过的 +chat-messages-list 输出
    data = {"ok": True, "data": {"messages": [
        {"chat_id": "oc_t", "content": "张三 started the group chat.", "create_time": "2026-11-12 19:34", "message_id": "om_s",
         "msg_type": "system", "sender": {"id": "", "id_type": "", "sender_type": ""}},
        {"chat_id": "oc_t", "content": "T12 1", "create_time": "2026-11-12 19:37", "message_id": "om_u", "msg_type": "text",
         "sender": {"id": "ou_me", "id_type": "open_id", "sender_type": "user"}}]}}
    rows = [n for n in (lark.normalize(m) for m in lark._items(data)) if n]
    assert [r["message_id"] for r in rows] == ["om_u"] and rows[0]["create_time"] == "2026-11-12T19:37:00+08:00"


def test_群可以填群名_第一次用到时换成群ID并记下_面板按群名显示(world, monkeypatch):
    s = world.lark
    monkeypatch.setattr(s, "config", lambda: {"chats": ["测试superops", "oc_other"], "rule_chats": {"lead-card": "测试superops"}})
    asked = []
    monkeypatch.setattr(lark, "find_chat", lambda name: asked.append(name) or "oc_test")
    assert s.chats() == ["oc_test", "oc_other"] and s.chats() == ["oc_test", "oc_other"]
    assert asked == ["测试superops"]                                          # 只查一次
    assert world.store.get("chat:names") == {"oc_test": "测试superops"}
    sent = []
    monkeypatch.setattr(lark, "send_markdown", lambda chat, text, idempotency_key=None: sent.append(chat) or "om_1")
    s.send({"channel": {"address": {"chat": "测试superops"}}, "text": "hi", "id": "m1"})   # home 也可以写群名
    assert sent == ["oc_test"]


def test_群名换不出来_先跳过并在来源状态里报出_过一分钟再试(world, monkeypatch):
    s = world.lark
    monkeypatch.setattr(s, "config", lambda: {"chats": ["测试superops"]})
    calls = []

    def fail(name):
        calls.append(name)
        raise lark.LarkError(f"按群名「{name}」找到 0 个群，请直接填群 ID（oc_ 开头）")

    monkeypatch.setattr(lark, "find_chat", fail)
    assert s.chats() == [] and s.chats() == [] and len(calls) == 1            # 一分钟内不重复查
    monkeypatch.setattr(s, "_threads", [object()])
    s.tick()
    health = world.health.get("source:lark")
    assert health["state"] == "error" and "测试superops" in health["detail"] and "0 个群" in health["detail"]
    with pytest.raises(RuntimeError, match="测试superops"):
        s.send({"channel": {"address": {"chat": "测试superops"}}, "text": "hi", "id": "m2"})
    s._chat_errors["测试superops"] = (0.0, s._chat_errors["测试superops"][1])  # 过了一分钟
    monkeypatch.setattr(lark, "find_chat", lambda name: "oc_test")
    assert s.chats() == ["oc_test"] and not s.unresolved_chats()


def test_find_chat_只认完全一样且唯一的群名(monkeypatch):
    monkeypatch.setattr(lark, "run", lambda args, **kw: {"data": {"chats": [
        {"chat_id": "oc_1", "name": "测试superops"}, {"chat_id": "oc_2", "name": "测试superops-备份"}]}})
    assert lark.find_chat("测试superops") == "oc_1"
    with pytest.raises(lark.LarkError, match="找到 0 个群"):
        lark.find_chat("测试")


def test_登录状态_新旧两种输出都认():
    new = {"identity": "user", "identities": {
        "bot": {"status": "ready", "available": True},
        "user": {"status": "ready", "available": True, "message": "User identity: ready", "openId": "ou_me", "userName": "本人",
                 "tokenStatus": "valid"}}}
    assert lark.parse_auth_status(new) == {"ok": True, "user": "本人", "open_id": "ou_me", "detail": "已登录"}
    expired = {"identities": {"user": {**new["identities"]["user"], "tokenStatus": "expired"}}}
    assert lark.parse_auth_status(expired)["ok"] is False
    missing = {"identities": {"user": {"status": "missing", "available": False, "message": "User identity: not logged in"}}}
    assert lark.parse_auth_status(missing) == {"ok": False, "user": None, "open_id": None, "detail": "User identity: not logged in"}
    old = {"identity": "user", "tokenStatus": "valid", "userName": "本人", "userOpenId": "ou_me"}
    assert lark.parse_auth_status(old)["ok"] is True and lark.parse_auth_status(old)["open_id"] == "ou_me"
