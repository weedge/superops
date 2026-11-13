"""飞书来源：和钉钉二选一的完整渠道（两个都开会被配置校验拒绝），以“我本人”的身份（lark-cli --as user）收发。

和钉钉来源一样（指令解析、批红、跟进、@ 提请求都继承 DingTalkSource.handle）：
- 「T12 1」「T12 3 原因」：对工单的回答 / 批红；「T12 …」不是选项就是跟进；只认 open_id，不认昵称。
- 「记下来：…」「需求：…」「已发 pk_xx」「暂停 / 恢复 xx」：名单内的人的指令。
- 「@兔管家 …」（文字里带机器人名，或以它开头）：新的明确请求，交给主 Session。
- 飞书单聊我、或在任何群里 @我 —— 交给 app/autoreply.py 自动回复（面板开关 auto / off、过滤群、限流都和钉钉共用）。

和钉钉不一样的地方：飞书的消息事件只能以机器人身份订阅，没有“别人发给我本人”的事件，所以这里是轮询：
- 运营群（sources.lark.chats）：每 poll_seconds（默认 15）秒 `im +chat-messages-list --start <上次>`；
- 自动回复：每 auto_reply_poll_seconds（默认 30）秒 `im +messages-search --chat-type p2p` 和 `--is-at-me`。
第一次启动从“现在”开始，不回放历史。系统也是用我本人的身份发消息，所以发出的每条消息记下 ID，轮询到就跳过；
我自己在飞书里亲手打的「T12 1」照常处理（审批人名单 policy.sources.lark.allowed 里要有我的 open_id）。
飞书群有话题，但这里和钉钉一样按工单编号跟进：回复工单请带 T<编号>。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..biz.notifier import lark
from ..biz.timeutil import TZ
from ..core.model import now_iso
from .dingtalk import DingTalkSource

log = logging.getLogger("superops.lark")
SENT_KEY = "lark:sent"          # 系统以我的身份发出去的消息 ID：轮询到不当成新消息
SEEN_KEY = "lark:seen"          # 已经处理过的消息 ID：轮询窗口有重叠，靠它去重
CURSOR = "lark:cursor:"         # 每个群 / 自动回复上次拉到的时间
OVERLAP = timedelta(seconds=10)


class LarkSource(DingTalkSource):
    id = "lark"
    title = "飞书"
    interrupts = True

    def __init__(self, host):
        super().__init__(host)
        self.last_error: str | None = None
        self.me: str | None = None

    # ---- 配置

    def notifier_cfg(self) -> dict:
        return {}

    def chats(self) -> list[str]:
        cfg = self.config()
        chats = [str(c) for c in cfg.get("chats") or []]
        for chat in (cfg.get("rule_chats") or {}).values():
            if chat and str(chat) not in chats:
                chats.append(str(chat))
        return chats

    # ---- 收（轮询）

    def start(self) -> None:
        self._stop.clear()
        status = lark.auth_status()
        self.me = status.get("open_id")
        if not status.get("ok"):
            self.report("error", f"飞书本人身份没登录：{status.get('detail')}（lark-cli auth login）")
        self._spawn(self._loop, "groups", float(self.config().get("poll_seconds", 15)), self.poll_groups)
        if self.config().get("auto_reply_listen", True) and getattr(self.host, "autoreply", None):
            self._spawn(self._loop, "autoreply", float(self.config().get("auto_reply_poll_seconds", 30)), self.poll_autoreply)
        if status.get("ok"):
            self.report("ready", f"轮询 {len(self.chats())} 个群")

    def _loop(self, name: str, every: float, work) -> None:
        while not self._stop.is_set():
            try:
                work()
                self.last_error = None
            except Exception as error:  # noqa: BLE001
                self.last_error = f"{name}：{str(error)[:200]}"
                log.warning("飞书轮询（%s）失败：%s", name, error)
            self._stop.wait(every)

    def tick(self) -> None:
        if not self._threads:
            return
        if self.last_error:
            self.report("error", f"飞书轮询出错：{self.last_error[:150]}")
        else:
            self.report("ready", f"轮询 {len(self.chats())} 个群")

    def _since(self, name: str) -> str | None:
        """上次拉到的时间（往前多拉 10 秒，靠已处理名单去重）；第一次返回 None：从现在开始。"""
        last = self.host.store.get(CURSOR + name)
        if not last:
            self.host.store.set(CURSOR + name, now_iso())
            return None
        try:
            t = datetime.fromisoformat(str(last).replace("Z", "+00:00")) - OVERLAP
        except ValueError:
            return None
        return t.astimezone(TZ).isoformat(timespec="seconds")

    def _fresh(self, messages: list[dict], name: str) -> list[dict]:
        """去掉系统自己发的、处理过的；推进游标。"""
        sent = set(self.host.store.get(SENT_KEY) or [])
        seen = list(self.host.store.get(SEEN_KEY) or [])
        out = []
        for m in messages:
            if m["message_id"] in sent or m["message_id"] in seen:
                continue
            seen.append(m["message_id"])
            out.append(m)
        self.host.store.set(SEEN_KEY, seen[-2000:])
        latest = max([m.get("create_time") or "" for m in messages] + [str(self.host.store.get(CURSOR + name) or "")])
        if latest:
            self.host.store.set(CURSOR + name, latest)
        return out

    def poll_groups(self) -> int:
        n = 0
        for chat in self.chats():
            since = self._since(f"chat:{chat}")
            if since is None:
                continue
            for m in self._fresh(lark.list_messages(chat, since), f"chat:{chat}"):
                m["chat_id"] = m.get("chat_id") or chat
                self.on_raw(m)
                n += 1
        return n

    def poll_autoreply(self) -> int:
        since = self._since("autoreply")
        if since is None:
            return 0
        found = [(m, "direct") for m in lark.search(since, p2p=True)] + [(m, "at") for m in lark.search(since, at_me=True)]
        fresh = {m["message_id"] for m in self._fresh([m for m, _ in found], "autoreply")}
        n = 0
        for m, kind in found:
            if m["message_id"] not in fresh or (self.me and m.get("sender_id") == self.me):
                continue              # 我自己发的（包括自动回复发出去的）不回
            fresh.discard(m["message_id"])
            try:
                self.host.autoreply.receive(m, kind, skip_chats=self.chats(), platform="lark")
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("处理飞书自动回复消息失败")
        return n

    # ---- 发

    def remember_sent(self, message_id: str | None) -> None:
        if message_id:
            self.host.store.set(SENT_KEY, ((self.host.store.get(SENT_KEY) or []) + [message_id])[-1000:])

    def send(self, message: dict) -> str:
        chat = (message["channel"].get("address") or {}).get("chat") or (self.chats() or [None])[0]
        if not chat:
            raise RuntimeError("飞书来源没有配置群（sources.lark.chats）")
        try:
            if message.get("kind") == "file" and message.get("file"):
                mid = lark.send_file(chat, message["file"])
            else:
                mid = lark.send_markdown(chat, message["text"], idempotency_key=message.get("id"))
        except lark.LarkError as error:
            raise RuntimeError(f"飞书发送失败：{error}") from error
        self.remember_sent(mid)
        return "sent"
