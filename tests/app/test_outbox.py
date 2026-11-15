"""发件箱（superops/app/outbox.py）：幂等排队、撤回、同一工单同一会话的顺序、来源关着 / 会话没了、暂停、静默时段和每日卡片上限。

依据：原型 docs/003 的 “Channel failure” “Pause processing”、tests/outbox.spec.ts，以及 outbox.py 自己的说明。
时间全部是假的：回执的时间戳（store.now_iso）、副作用队列的时钟、发件箱的本地时间来自同一个 FakeTime。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from superops.app.effects import Effects
from superops.app.health import HealthBoard
from superops.app.outbox import Outbox, parse_quiet, quiet_until
from superops.app.store import Store
from superops.infra.timeutil import TZ
from superops.core.model import to_iso


class FakeTime:
    def __init__(self):
        self.at = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)     # 周三上午 10 点
        self.ticks = 0

    def local(self) -> datetime:
        return self.at

    def epoch(self) -> float:
        return self.at.timestamp()

    def iso(self) -> str:
        """每次调用往后挪 1 毫秒：先排队的时间戳一定更早。"""
        self.ticks += 1
        return to_iso(self.at + timedelta(milliseconds=self.ticks))

    def advance(self, seconds: float = 0, **kwargs) -> None:
        self.at += timedelta(seconds=seconds, **kwargs)

    def at_clock(self, hour: int, minute: int = 0, days: int = 0) -> None:
        self.at = (self.at + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


class FakeSources:
    """最小的来源登记：enabled / interrupts / send。result[source] 是 send 的返回值，或者一个要抛的异常。"""

    def __init__(self):
        self.on = {"chat": True, "ding": True}
        self.loud = {"ding"}
        self.result: dict[str, object] = {}
        self.fail_ids: dict[str, Exception] = {}
        self.sent: list[dict] = []

    def enabled(self, source: str) -> bool:
        return self.on.get(source, False)

    def interrupts(self, source: str) -> bool:
        return source in self.loud

    def send(self, message: dict) -> str:
        if message["id"] in self.fail_ids:
            raise self.fail_ids[message["id"]]
        result = self.result.get(message["channel"]["source"], "sent")
        if isinstance(result, Exception):
            raise result
        if result == "sent":
            self.sent.append(message)
        return result

    @property
    def ids(self) -> list[str]:
        return [m["id"] for m in self.sent]


CHAT = {"source": "chat", "thread": "t1", "address": {"chat": "c1"}}
DING = {"source": "ding", "thread": "home", "address": {"chat": "cid"}}


def msg(id_: str, *, channel: dict = CHAT, kind: str = "reply", ticket: str | None = None, priority: int | None = None, text: str | None = None) -> dict:
    message = {"id": id_, "channel": channel, "text": text or f"正文 {id_}", "kind": kind}
    if ticket:
        message["ticketId"] = ticket
    if priority is not None:
        message["priority"] = priority
    return message


@pytest.fixture
def box(tmp_path, monkeypatch):
    class Box:
        pass
    b = Box()
    b.time = FakeTime()
    monkeypatch.setattr("superops.app.store.now_iso", b.time.iso)
    b.store = Store(tmp_path / "db.sqlite")
    b.health = HealthBoard()
    b.effects = Effects(b.store, b.health, now=b.time.epoch, sync=True)
    b.sources = FakeSources()
    b.paused = False
    b.cfg = {"quiet_hours": None, "max_cards_per_day": None}
    b.outbox = Outbox(b.store, b.effects, b.sources, paused=lambda: b.paused, config=lambda: b.cfg, local_now=b.time.local)
    b.effects.start()
    b.run = b.effects.run_until_idle
    b.attempts = lambda id_: b.store.one("SELECT attempts FROM effect WHERE kind='reply' AND key=?", (id_,))
    yield b
    b.store.close()


# ---------------------------------------------------------------- 排队、状态、撤回


def test_排队后发出_状态从pending到sent(box):
    assert box.outbox.status("m1") is None
    box.outbox.send(msg("m1", ticket="T", priority=1))
    assert box.outbox.status("m1") == "pending" and box.sources.sent == []          # send 只排队
    box.run()
    assert box.outbox.status("m1") == "sent"
    [sent] = box.sources.sent
    assert {k: sent[k] for k in ("id", "channel", "text", "kind", "ticketId", "priority")} == {
        "id": "m1", "channel": CHAT, "text": "正文 m1", "kind": "reply", "ticketId": "T", "priority": 1}
    receipt = box.store.receipts.get("m1")
    assert receipt["settledAt"] and receipt["reason"] is None
    assert box.effects.pending("reply") is False


def test_id相同只排一次_保留最先排队的那条(box):
    box.outbox.send(msg("m1", text="第一版"))
    box.outbox.send(msg("m1", text="第二版"))
    assert box.store.one("SELECT COUNT(*) FROM outbox") == 1 and box.store.one("SELECT COUNT(*) FROM effect") == 1
    box.run()
    box.outbox.send(msg("m1", text="发出之后又来一次"))
    box.run()
    assert [m["text"] for m in box.sources.sent] == ["第一版"]
    assert box.effects.pending("reply") is False


def test_回滚的事务里排队的消息不留回执也不发(box):
    def work():
        box.outbox.send(msg("m1"))
        raise RuntimeError("回滚")

    with pytest.raises(RuntimeError):
        box.store.atomic(work)
    box.run()
    assert box.outbox.status("m1") is None and box.sources.sent == []


def test_withdraw只对没发出的有效(box):
    box.outbox.send(msg("pending"))
    box.outbox.send(msg("gone"))
    box.outbox.withdraw("gone")
    receipt = box.store.receipts.get("gone")
    assert (receipt["status"], receipt["reason"]) == ("dropped", "withdrawn") and receipt["settledAt"]
    assert box.effects.pending("reply", "gone") is False
    box.run()
    assert box.sources.ids == ["pending"]
    box.outbox.withdraw("pending")                               # 已经发出的撤不回
    assert box.outbox.status("pending") == "sent" and box.store.receipts.get("pending")["reason"] is None
    box.outbox.withdraw("never-queued")                          # 没排过的撤回不出错
    assert box.outbox.status("never-queued") is None


def test_撤回过的消息不会再排第二次(box):
    box.outbox.send(msg("m1"))
    box.outbox.withdraw("m1")
    box.outbox.send(msg("m1"))
    box.run()
    assert box.outbox.status("m1") == "dropped" and box.sources.sent == []


def test_notice走同一个出口_key相同只发一次(box):
    box.outbox.notice(CHAT, "告警", key="n1", priority=0, ticket_id="T9")
    box.outbox.notice(CHAT, "告警（重复）", key="n1")
    box.outbox.notice(CHAT, "文件", key="n2", kind="file")
    box.run()
    assert [(m["id"], m["text"], m["kind"], m["priority"], m.get("ticketId")) for m in box.sources.sent] == [
        ("n1", "告警", "notice", 0, "T9"), ("n2", "文件", "file", 2, None)]


# ---------------------------------------------------------------- 发送失败、来源的回答


def test_发送失败留在pending_从5秒起退避重试(box):
    box.sources.result["chat"] = RuntimeError("网络抖了")
    box.outbox.send(msg("m1"))
    box.run()
    assert box.outbox.status("m1") == "pending" and box.attempts("m1") == 1
    box.time.advance(4)
    assert box.run() == 0
    box.time.advance(1)
    box.run()
    assert box.attempts("m1") == 2                               # 第二次 10 秒后
    box.sources.result.pop("chat")
    box.time.advance(9)
    assert box.run() == 0
    box.time.advance(1)
    box.run()
    assert box.outbox.status("m1") == "sent" and box.sources.ids == ["m1"]


def test_来源关着时消息等着_开了再发(box):
    box.sources.on["chat"] = False
    box.outbox.send(msg("m1"))
    box.run()
    assert box.outbox.status("m1") == "pending" and box.attempts("m1") == 0        # 等，不算失败
    assert box.health.problems() == []
    box.time.advance(29)
    box.sources.on["chat"] = True
    assert box.run() == 0                                        # 30 秒后才再看
    box.time.advance(1)
    box.run()
    assert box.outbox.status("m1") == "sent"


def test_来源没安装时消息也等着(box):
    box.outbox.send(msg("m1", channel={"source": "probe", "thread": "p1", "address": {}}))
    box.run()
    assert box.outbox.status("m1") == "pending" and box.attempts("m1") == 0
    box.sources.on["probe"] = True
    box.time.advance(30)
    box.run()
    assert box.outbox.status("m1") == "sent"


@pytest.mark.parametrize("answer, reason", [("gone", "channelGone"), ("ignored", "channelIgnored")])
def test_来源说会话没了或不收_记为dropped并带原因(box, answer, reason):
    box.sources.result["chat"] = answer
    box.outbox.send(msg("m1", ticket="T1"))
    box.run()
    receipt = box.store.receipts.get("m1")
    assert (receipt["status"], receipt["reason"]) == ("dropped", reason)
    assert receipt["settledAt"] and receipt["ticketId"] == "T1" and receipt["text"] == "正文 m1"   # 记录还在
    assert box.effects.pending("reply") is False                 # 不再重试
    box.sources.result.pop("chat")
    box.outbox.send(msg("m1"))                                   # 丢弃过的不会再排
    box.run()
    assert box.outbox.status("m1") == "dropped" and box.sources.sent == []


def test_暂停时不发_恢复后发出(box):
    box.paused = True
    box.outbox.send(msg("held"))
    box.run()
    box.time.advance(600)
    box.run()
    assert box.outbox.status("held") == "pending" and box.sources.sent == [] and box.attempts("held") == 0
    box.paused = False
    box.run()
    assert box.outbox.status("held") == "sent"


def test_已经结清的回执再被调度时什么都不做(box):
    box.outbox.send(msg("m1"))
    box.store.receipts.settle("m1", "dropped", "withdrawn")     # 回执没了但副作用还在排队
    box.run()
    assert box.sources.sent == [] and box.effects.pending("reply") is False
    box.outbox.deliver("no-such-message")                       # 没有回执的也不出错


# ---------------------------------------------------------------- 同一工单同一会话的顺序


def test_同一工单发往同一会话的消息按排队顺序发_前一条没发出去后一条等着(box):
    box.sources.fail_ids["ack"] = RuntimeError("发不出去")
    box.outbox.send(msg("ack", ticket="T1", kind="acknowledgement"))
    box.outbox.send(msg("result", ticket="T1"))
    box.run()
    assert box.sources.ids == [] and box.outbox.status("result") == "pending"
    assert box.attempts("ack") == 1 and box.attempts("result") == 0                # 等着的那条不算失败
    box.sources.fail_ids.clear()
    box.time.advance(5)
    box.run()
    assert box.sources.ids == ["ack", "result"]


def test_前一条连续失败3次之后放行后面的(box):
    box.sources.fail_ids["ack"] = RuntimeError("发不出去")
    box.outbox.send(msg("ack", ticket="T1", kind="acknowledgement"))
    box.outbox.send(msg("result", ticket="T1"))
    box.run()                                                    # 第 1 次失败
    box.time.advance(5)
    box.run()                                                    # 第 2 次失败
    assert box.attempts("ack") == 2 and box.sources.ids == []
    box.time.advance(10)
    box.run()                                                    # 第 3 次失败：它成了一个问题，不再挡着后面的
    assert box.attempts("ack") == 3
    box.time.advance(5)
    box.run()
    assert box.sources.ids == ["result"] and box.outbox.status("ack") == "pending"
    assert [p["name"] for p in box.health.problems()] == ["effect:reply"]


def test_别的工单_没有工单的消息_发往别的会话的消息都不等它(box):
    box.sources.fail_ids["ack"] = RuntimeError("发不出去")
    other_thread = {**CHAT, "thread": "t2"}
    box.outbox.send(msg("ack", ticket="T1", kind="acknowledgement"))
    box.outbox.send(msg("same-ticket", ticket="T1"))
    box.outbox.send(msg("other-ticket", ticket="T2"))
    box.outbox.send(msg("no-ticket"))
    box.outbox.send(msg("other-conversation", ticket="T1", channel=other_thread))
    box.run()
    assert box.sources.ids == ["other-ticket", "no-ticket", "other-conversation"]
    assert box.outbox.status("same-ticket") == "pending"


def test_前一条被撤回或丢弃之后_后一条不再等(box):
    box.sources.on["chat"] = False
    box.outbox.send(msg("question", ticket="T1", kind="attention"))
    box.outbox.send(msg("result", ticket="T1"))
    box.run()
    assert box.sources.ids == []
    box.outbox.withdraw("question")
    box.sources.on["chat"] = True
    box.time.advance(30)
    box.run()
    assert box.sources.ids == ["result"]


def test_后排队的消息不会挡住先排队的(box):
    box.outbox.send(msg("first", ticket="T1"))
    box.outbox.send(msg("second", ticket="T1"))
    assert box.store.receipts.behind(box.store.receipts.get("first")) is False
    assert box.store.receipts.behind(box.store.receipts.get("second")) is True
    box.run()
    assert box.sources.ids == ["first", "second"]


def test_同一毫秒内排队的两条消息也保持顺序(box, monkeypatch):
    monkeypatch.setattr("superops.app.store.now_iso", lambda: "2026-11-11T02:00:00.000Z")
    box.sources.fail_ids["ack"] = RuntimeError("发不出去")
    box.outbox.send(msg("ack", ticket="T1", kind="acknowledgement"))
    box.outbox.send(msg("result", ticket="T1"))
    box.run()
    assert box.sources.ids == []                                 # result 应该等 ack


def test_会话是否相同不取决于渠道字典的键顺序(box):
    box.sources.fail_ids["ack"] = RuntimeError("发不出去")
    reordered = {"address": {"chat": "c1"}, "thread": "t1", "source": "chat"}
    box.outbox.send(msg("ack", ticket="T1", kind="acknowledgement"))
    box.outbox.send(msg("result", ticket="T1", channel=reordered))
    box.run()
    assert box.sources.ids == []


# ---------------------------------------------------------------- 打扰人的来源：静默时段、每日卡片上限


def test_静默时段里打扰人的来源只发P0_过了时段再发其余的(box):
    box.cfg["quiet_hours"] = "22:30-08:00"
    box.time.at_clock(23, 0)
    box.outbox.send(msg("p2", channel=DING))
    box.outbox.send(msg("p1", channel=DING, priority=1))
    box.outbox.send(msg("p0", channel=DING, priority=0))
    box.outbox.send(msg("quiet-source", channel=CHAT))          # 不打扰人的来源不受限
    box.run()
    assert box.sources.ids == ["p0", "quiet-source"]
    assert box.attempts("p2") == 0 and box.attempts("p1") == 0   # 顺延，不算失败
    box.time.at_clock(7, 59, days=1)
    box.run()
    assert box.sources.ids == ["p0", "quiet-source"]
    box.time.at_clock(8, 0)
    box.time.advance(1)
    box.run()
    assert sorted(box.sources.ids) == ["p0", "p1", "p2", "quiet-source"]


def test_静默时段的顺延最多一小时看一次_最少30秒(box):
    box.cfg["quiet_hours"] = "22:30-08:00"
    box.time.at_clock(23, 0)
    box.outbox.send(msg("m", channel=DING))
    box.run()
    due = box.store.one("SELECT due FROM effect WHERE key='m'")
    assert due - box.time.epoch() == 3600                        # 离结束还有 9 小时：一小时后再看
    box.time.at_clock(7, 59, days=1)
    box.time.advance(50)                                         # 07:59:50，离结束 10 秒
    box.run()
    assert box.store.one("SELECT due FROM effect WHERE key='m'") - box.time.epoch() == 30


def test_没配静默时段时什么时候都发(box):
    box.time.at_clock(3, 0)
    box.outbox.send(msg("m", channel=DING))
    box.run()
    assert box.sources.ids == ["m"]


def test_打扰人的来源每天的问题卡片到上限后顺延到第二天_P0和别的消息不受限(box):
    box.cfg["max_cards_per_day"] = 2
    for n in (1, 2, 3):
        box.outbox.send(msg(f"card{n}", channel=DING, kind="attention", priority=1))
    box.run()
    assert box.sources.ids == ["card1", "card2"] and box.outbox.status("card3") == "pending"
    assert box.attempts("card3") == 0
    box.outbox.send(msg("urgent", channel=DING, kind="attention", priority=0))
    box.outbox.send(msg("result", channel=DING, kind="reply"))
    box.outbox.send(msg("notice", channel=DING, kind="notice", priority=1))
    box.time.advance(1800)
    box.run()
    assert box.sources.ids == ["card1", "card2", "urgent", "result", "notice"]
    box.time.at_clock(23, 59)
    box.run()
    assert box.outbox.status("card3") == "pending"               # 当天一直顺延
    box.time.at_clock(0, 30, days=1)
    box.run()
    assert box.outbox.status("card3") == "sent"                  # 过了本地的零点就是新的一天


def test_每日上限只数当天已发出的卡片_没发出的和丢弃的不算(box):
    box.cfg["max_cards_per_day"] = 1
    box.outbox.send(msg("withdrawn", channel=DING, kind="attention"))
    box.outbox.withdraw("withdrawn")
    box.sources.result["ding"] = "gone"
    box.outbox.send(msg("dropped", channel=DING, kind="attention"))
    box.run()
    box.sources.result.pop("ding")
    box.outbox.send(msg("card", channel=DING, kind="attention"))
    box.run()
    assert box.sources.ids == ["card"]


def test_不打扰人的来源不受每日卡片上限限制(box):
    box.cfg["max_cards_per_day"] = 1
    for n in (1, 2, 3):
        box.outbox.send(msg(f"card{n}", channel=CHAT, kind="attention"))
    box.run()
    assert box.sources.ids == ["card1", "card2", "card3"]


def test_静默时段里的P0卡片不受每日上限限制(box):
    box.cfg.update({"quiet_hours": "22:30-08:00", "max_cards_per_day": 1})
    box.time.at_clock(23, 0)
    for n in (1, 2):
        box.outbox.send(msg(f"p0-{n}", channel=DING, kind="attention", priority=0))
    box.run()
    assert box.sources.ids == ["p0-1", "p0-2"]


# ---------------------------------------------------------------- quiet_until / parse_quiet


def at(hour: int, minute: int = 0, second: int = 0, day: int = 11) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def test_parse_quiet():
    assert parse_quiet("22:30-08:00") == (22 * 60 + 30, 8 * 60)
    assert parse_quiet("09:05-9:50") == (545, 590)
    assert parse_quiet(None) is None and parse_quiet("") is None and parse_quiet("22:30") is None


@pytest.mark.parametrize("now, until", [
    (at(22, 29), None),                      # 开始前一分钟
    (at(22, 29, 59), None),
    (at(22, 30), at(8, 0, day=12)),          # 开始的那一分钟就算
    (at(23, 59, 59), at(8, 0, day=12)),      # 午夜前：结束在第二天
    (at(0, 0), at(8, 0)),                    # 午夜后：结束在当天
    (at(7, 59, 59), at(8, 0)),
    (at(8, 0), None),                        # 结束的那一分钟已经不算
    (at(12, 0), None),
])
def test_quiet_until跨午夜的时段(now, until):
    assert quiet_until(now, "22:30-08:00") == until


@pytest.mark.parametrize("now, until", [
    (at(11, 59), None), (at(12, 0), at(14, 0)), (at(13, 59, 59), at(14, 0)), (at(14, 0), None), (at(23, 0), None), (at(0, 0), None),
])
def test_quiet_until不跨午夜的时段(now, until):
    assert quiet_until(now, "12:00-14:00") == until


def test_quiet_until在午夜整点开始或结束的时段():
    assert quiet_until(at(23, 30), "22:00-00:00") == at(0, 0, day=12)
    assert quiet_until(at(0, 0), "22:00-00:00") is None
    assert quiet_until(at(0, 0), "00:00-06:00") == at(6, 0)
    assert quiet_until(at(23, 59), "00:00-06:00") is None


def test_quiet_until没配时段时不静默():
    assert quiet_until(at(3, 0), None) is None and quiet_until(at(3, 0), "") is None
