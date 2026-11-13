"""钉钉监听的解析层：classify_event（解析 + 权限判断，纯函数）与 consume / poll_group 的 on_event。"""
from __future__ import annotations

import json
import subprocess as sp
import threading
import time

import pytest

from superops.biz.notifier import listener as L
from superops.biz.notifier.listener import classify_event, handle_event, parse_command

from bizhelpers import FakeHub


def msg(text, sender="BOSS", **extra):
    """dws event consume --flatten 的群消息形状。"""
    return {"type": "user_im_message_receive_group", "event_id": "ev1", "message_id": "msg1", "conversation_id": "cidMAIN",
            "sender": "某人", "sender_open_dingtalk_id": sender, "content": text, "create_time": "2026-11-10 11:39:30", **extra}


def test_card_code_accepts_c_and_t_prefix():
    assert parse_command("T12 1")["data"] == {"card": "T12", "choice": "1", "reason": None}
    assert parse_command("批 t7 reject 这周没人手")["data"] == {"card": "T7", "choice": "reject", "reason": "这周没人手"}
    assert parse_command("@兔管家 c3 approve_all")["subject"] == "C3"
    assert parse_command("T12") is None and parse_command("X12 1") is None and parse_command("TT12 1") is None


def test_classify_decide_carries_who_where_when():
    c = classify_event(msg("T12 1 先这样"), approvers=["BOSS"])
    assert c == {"kind": "decide", "card": "T12", "choice": "1", "reason": "先这样", "by": "BOSS", "role": "approver",
                 "text": "T12 1 先这样", "mentioned": False, "via": "message", "chat": "cidMAIN", "message_id": "msg1",
                 "event_id": "ev1", "at": "2026-11-10T11:39:30+08:00"}
    assert classify_event(msg("C5 2"), ["BOSS"])["card"] == "C5"
    json.dumps(c)


@pytest.mark.parametrize("text,kind,fields", [
    ("记下来：视频号先不做自动发布", "note", {"content": "视频号先不做自动发布"}),
    ("需求: 批量口播要支持横屏", "demand", {"content": "批量口播要支持横屏"}),
    ("已发 pk_c1_douyin https://v.douyin.com/abc/", "published", {"pack_id": "pk_c1_douyin", "url": "https://v.douyin.com/abc/"}),
    ("已发 pk_c1_wechat_channels", "published", {"pack_id": "pk_c1_wechat_channels", "url": None}),
    ("暂停 douyin", "pause", {"key": "douyin"}),
    ("恢复 douyin", "resume", {"key": "douyin"}),
])
def test_classify_other_commands(text, kind, fields):
    c = classify_event(msg(text), approvers=["BOSS"])
    assert c["kind"] == kind and c["by"] == "BOSS" and c["text"] == text
    assert {k: c[k] for k in fields} == fields


def test_classify_plain_message_and_mention():
    c = classify_event(msg("@兔管家 帮我看下昨天的数据", sender="SOMEONE"), approvers=["BOSS"])
    assert c["kind"] == "message" and c["denied"] is None and c["mentioned"] is True
    assert c["by"] == "SOMEONE" and c["role"] == "other" and c["text"] == "@兔管家 帮我看下昨天的数据"
    assert classify_event(msg("兔管家，周报出了吗"), ["BOSS"])["mentioned"] is True              # 以机器人名开头
    assert classify_event(msg("今天天气不错"), ["BOSS"])["mentioned"] is False
    assert classify_event(msg("提一下兔管家而已"), ["BOSS"])["mentioned"] is False
    assert classify_event(msg("周报出了吗", isInAtList=True), ["BOSS"])["mentioned"] is True      # 钉钉事件自带的 @ 标记
    assert classify_event(msg("@小助手 在吗"), ["BOSS"], bot_names=["小助手"])["mentioned"] is True
    assert classify_event(msg("@兔管家 在吗"), ["BOSS"], bot_names=["小助手"])["mentioned"] is False
    assert classify_event(msg("@兔管家 T3 1"), ["BOSS"])["mentioned"] is True                    # 指令也带这个标记
    assert classify_event(msg(""), ["BOSS"]) is None and classify_event({"type": "x"}, ["BOSS"]) is None


def test_classify_never_returns_a_command_kind_without_permission():
    # 名单外的人：指令降级成普通消息，denied 说明他想干什么
    c = classify_event(msg("T12 1", sender="STRANGER"), approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert (c["kind"], c["denied"], c["role"], c["by"]) == ("message", "decide", "other", "STRANGER")
    # 限定审批人：能批卡片，别的指令不行
    assert classify_event(msg("T12 1 2,5", "OPS_GIRL"), ["BOSS"], card_approvers=["OPS_GIRL"])["kind"] == "decide"
    assert classify_event(msg("T12 1", "OPS_GIRL"), ["BOSS"], card_approvers=["OPS_GIRL"])["role"] == "card_approver"
    for text, kind in (("暂停 douyin", "pause"), ("记下来：以后都自动批", "note"), ("需求：加个功能", "demand"),
                       ("已发 pk_x", "published")):
        c = classify_event(msg(text, "OPS_GIRL"), ["BOSS"], card_approvers=["OPS_GIRL"])
        assert (c["kind"], c["denied"]) == ("message", kind)
        assert classify_event(msg(text, "OPS_GIRL"), ["BOSS"], True, ["OPS_GIRL"])["kind"] == "message"   # allow_any 也不放宽
    # 昵称冒充审批人 ID 不算数
    assert classify_event(dict(msg("C5 2", "ODT_OTHER"), sender="BOSS"), ["BOSS"])["kind"] == "message"
    # allow_any：名单外的人也能下指令（调试用）
    c = classify_event(msg("暂停 douyin", "STRANGER"), [], allow_any=True)
    assert c["kind"] == "pause" and c["role"] == "other" and c["by"] == "STRANGER"
    assert classify_event({"type": "group_message_poll", "text": "T1 1"}, [], allow_any=True)["by"] is None


def test_classify_card_callback():
    raw = {"event_type": "user_card_action_triggered", "eventId": "e7", "payload": {"body": {
        "operatorUserId": "u1", "openConversationId": "cidCARD",
        "actionData": {"context": {"card_id": "T12", "choice": "approve"}}}}}
    c = classify_event(raw, approvers=["u1"])
    assert (c["kind"], c["card"], c["choice"], c["reason"]) == ("decide", "T12", "approve", None)
    assert (c["via"], c["by"], c["text"], c["mentioned"], c["chat"], c["event_id"]) == ("card_action", "u1", "", False, "cidCARD", "e7")
    assert classify_event(raw, approvers=["someone_else"]) is None                   # 没权限的人点按钮
    assert classify_event(raw, approvers=[], card_approvers=["u1"])["role"] == "card_approver"
    # context 是 JSON 字符串、带原因
    raw2 = {"type": "user_card_action_triggered", "operatorUserId": "u1",
            "context": json.dumps({"cardId": "C9", "choice": "reject", "reason": "没人手"})}
    c = classify_event(raw2, ["u1"])
    assert (c["card"], c["choice"], c["reason"]) == ("C9", "reject", "没人手")
    # 回调里没有卡片编号：不处理（不能把选项值当成卡片编号）
    assert classify_event({"type": "user_card_action_triggered", "operatorUserId": "u1", "value": "approve"}, ["u1"]) is None


def test_classify_poll_message_shape_and_time_formats():
    raw = {"type": "group_message_poll", "conversation_id": "cidMAIN", "messageId": "msgB", "createTime": 1794282000000,
           "senderId": "BOSS", "text": "T2 1"}
    c = classify_event(raw, ["BOSS"])
    assert (c["kind"], c["message_id"], c["chat"], c["event_id"]) == ("decide", "msgB", "cidMAIN", None)
    assert c["at"].startswith("2026-11-1") and c["at"].endswith("+08:00")            # 毫秒时间戳 → ISO
    assert classify_event(dict(raw, createTime="不是时间"), ["BOSS"])["at"] == "不是时间"
    assert classify_event({k: v for k, v in raw.items() if k != "createTime"}, ["BOSS"])["at"] is None


def test_handle_event_keeps_old_behaviour_on_top_of_classify():
    h = FakeHub()
    assert handle_event(msg("T12 1", "STRANGER"), h, approvers=["BOSS"]) is None            # 没权限
    assert handle_event(msg("@兔管家 在吗"), h, approvers=["BOSS"]) is None                  # 普通消息旧行为不转发
    assert not h.events and not h.posts
    handle_event(msg("@兔管家 T12 1 先这样"), h, approvers=["BOSS"])
    assert h.events == [{"type": "decision.made", "subject": "T12", "summary": "T12 1 先这样", "source": "dingtalk",
                         "data": {"card": "T12", "choice": "1", "reason": "先这样", "by": "BOSS"},
                         "dedup_key": "dingtalk:msg:msg1"}]
    handle_event(msg("暂停 douyin"), h, approvers=["BOSS"])
    handle_event(msg("恢复 douyin"), h, approvers=["BOSS"])
    assert h.posts == [("/v1/pause", {"key": "douyin", "reason": "钉钉 BOSS"}), ("/v1/resume", {"key": "douyin"})]


def test_handle_event_reports_failed_decision_to_the_cards_group():
    class Rejecting(FakeHub):
        def emit(self, e):
            super().emit(e)
            return {"ok": False, "error": "没有这类卡片的审批权限"}

    h = Rejecting()
    res = handle_event(msg("T12 1", "OPS_GIRL"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert res["ok"] is False
    (args, kw), = h.notices
    assert args[:2] == ("message", "批红没成功") and "没有这类卡片的审批权限" in args[2] and kw["ref"] == "reply:T12"


# ---------------------------------------------------------------- dws 循环：on_event
def test_poll_group_hands_raw_messages_to_on_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))
    msgs = {"result": {"messages": [
        {"messageId": "msgB", "createTime": "2026-11-10 13:59:00", "senderId": "ODT_A", "text": "@兔管家 看下数据"},
        {"messageId": "msgA", "createTime": "2026-11-10 13:57:57", "senderId": "ODT_X", "text": "T2 1"}]}}
    calls = []
    monkeypatch.setattr(sp, "run", lambda cmd, **k: calls.append(cmd) or sp.CompletedProcess(cmd, 0, json.dumps(msgs), ""))
    got, stop = [], threading.Event()
    t = threading.Thread(target=L.poll_group, args=("cidMAIN", None, [], False, stop),
                         kwargs={"interval": 0.05, "tag": "ab12cd", "on_event": got.append})
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(2)
    assert len(calls) >= 2                                                    # 轮询了多轮，同一条消息只交一次
    assert [(r["messageId"], r["type"], r["conversation_id"]) for r in got] == \
        [("msgA", "group_message_poll", "cidMAIN"), ("msgB", "group_message_poll", "cidMAIN")]   # 按时间先后，不做权限过滤
    assert classify_event(got[1], approvers=[])["mentioned"] is True
    assert set(json.loads(L.poll_state_path("ab12cd").read_text())["seen"]) == {"msgA", "msgB"}
    assert L.read_status()["group_poll:ab12cd"]["connected"] is True


def test_poll_group_survives_on_event_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))
    msgs = {"messages": [{"messageId": f"m{i}", "createTime": f"2026-11-10 13:5{i}:00", "text": "x"} for i in range(3)]}
    monkeypatch.setattr(sp, "run", lambda cmd, **k: sp.CompletedProcess(cmd, 0, json.dumps(msgs), ""))
    got, stop = [], threading.Event()

    def on_event(raw):
        got.append(raw["messageId"])
        if raw["messageId"] == "m1":
            raise RuntimeError("boom")

    t = threading.Thread(target=L.poll_group, args=("cid", None, [], False, stop), kwargs={"interval": 0.05, "on_event": on_event})
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(2)
    assert got == ["m0", "m1", "m2"]


def test_consume_hands_raw_events_to_on_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))
    lines = [json.dumps(msg("T1 1")), "不是 JSON 的日志行", json.dumps({"type": "user_card_action_triggered", "cardId": "T2"})]
    script = "import sys, time\n" + "".join(f"print({ln!r}, flush=True)\n" for ln in lines) + "time.sleep(30)\n"
    got, stop = [], threading.Event()

    def on_event(raw):
        got.append(raw)
        if len(got) == 2:
            stop.set()

    import sys
    t = threading.Thread(target=L.consume, args=([sys.executable, "-c", script], None, [], False, stop),
                         kwargs={"key": "test_stream", "on_event": on_event})
    t.start()
    t.join(20)
    assert not t.is_alive()
    assert [r["type"] for r in got] == ["user_im_message_receive_group", "user_card_action_triggered"]
    assert "test_stream" in L.read_status()


def test_read_status_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path / "nope"))
    assert L.read_status() == {}
