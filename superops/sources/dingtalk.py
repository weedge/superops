"""钉钉来源：运营群里的消息作为触发，卡片和结果通过它的渠道发回群里。

事件 → 领域的映射：
- 「T12 1」「T12 3 这周人手不够」：对工单 T12 的回答。这条消息落在 T12 自己的线程上（人的跟进，直接到它的执行器），
  T12 有带选项的问题时同时按选项批红。只认 ID 不认昵称；全权审批人 = 策略里钉钉来源的 allowed，
  限定审批人 = answerers[规则]。
- 「T12 把标题改短一点」：不是选项 → 就是对 T12 的一条跟进消息，由它的执行器决定。
- 「记下来：…」「需求：…」「已发 pk_xx [链接]」「暂停 / 恢复 xx」：全权审批人的指令，确定性处理。
- @机器人 的其他消息：新线程上的明确请求，交给主 Session 路由。observe: all 时没 @ 的消息也作为候选（走准入表）。
- 运营群以外：别人单聊我、或在任何群里 @我 —— 交给 app/autoreply.py 自动回复（面板开关 auto / off），不进工单。
  配置 sources.dingtalk.auto_reply_listen: false 可以连监听都不起。
钉钉群没有话题，所以“在同一线程上跟进”靠工单编号：回复工单请带 T<编号>。

发：dws chat +messages-send（Markdown / 文件）。这个渠道会打扰人，所以要过发件箱的静默时段和每日卡片上限。
"""
from __future__ import annotations

import logging
import re
import threading

from ..infra import governance
from ..infra.notifier import DingTalkNotifier
from ..biz.notifier import listener
from ..core.model import now_iso
from ..core.reasons import key
from .kind import SourceKind

log = logging.getLogger("superops.dingtalk")
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def dingtalk_markdown(text: str) -> str:
    """钉钉的 Markdown 不认单个换行：相邻两行普通文字会被并成一行，引用块（>）还会把后面的行吞进去。
    这里把“该分开的相邻行”之间补一个空行；列表项之间、代码块里面保持原样。"""
    lines, out, fenced = text.split("\n"), [], False
    for i, line in enumerate(lines):
        out.append(line)
        if line.lstrip().startswith("```"):
            fenced = not fenced
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if fenced or not line.strip() or not nxt.strip():
            continue
        quote, nquote = line.lstrip().startswith(">"), nxt.lstrip().startswith(">")
        item, nitem = bool(_LIST.match(line)), bool(_LIST.match(nxt))
        special = lambda l: l.lstrip().startswith(("|", "```")) or bool(re.match(r"\s*#{1,6}\s", l))  # noqa: E731  「#话题」不是标题
        if quote != nquote or (item and not nitem) or (not item and not nitem and not quote and not special(line) and not special(nxt)):
            out.append("")
    return "\n".join(out)


class DingTalkSource(SourceKind):
    id = "dingtalk"
    title = "钉钉"
    interrupts = True

    def __init__(self, host):
        super().__init__(host)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- 配置

    def notifier_cfg(self) -> dict:
        return ((self.host.cfg.get("notifier") or {}).get("dingtalk")) or {}

    def chats(self) -> list[str]:
        cfg = self.config()
        chats = [str(c) for c in cfg.get("chats") or []]
        legacy = self.notifier_cfg().get("chat_id")
        if legacy and str(legacy) not in chats:
            chats.insert(0, str(legacy))
        for chat in (cfg.get("rule_chats") or {}).values():
            if chat and str(chat) not in chats:
                chats.append(str(chat))
        return chats

    def mention_names(self) -> list[str]:
        """消息里出现“@<这些名字>”或以它开头，就算冲着系统说的（提请求）。默认是机器人名“兔管家”。"""
        bot = (self.host.cfg.get("notifier") or {}).get("bot_name", "兔管家")
        return list(dict.fromkeys([bot, *(self.config().get("mention_names") or [])]))

    def _people(self) -> tuple[list[str], list[str]]:
        part = self.host.settings.current()["policy"]["sources"].get(self.id) or {}
        approvers = [e["id"] for e in part.get("allowed") or []]
        limited = sorted({i for ids in (part.get("answerers") or {}).values() for i in ids})
        return approvers, limited

    def scope(self, meta: dict) -> dict:
        chat = meta.get("chat")
        if chat and chat not in self.chats():
            return {"listened": False, "reason": "chat_not_listened"}
        return {"listened": True, "observed": self.config().get("observe", "mention") == "all", "row": chat or "direct"}

    # ---- 收

    def start(self) -> None:
        self._stop.clear()
        approvers, limited = self._people()
        for index, chat in enumerate(self.chats()):
            cmd = ["dws", "event", "consume", "user_im_message_receive_group", "--group", chat, "--flatten", "-f", "ndjson"]
            self._spawn(listener.consume, cmd, None, approvers, False, self._stop, card_approvers=limited,
                        key=f"group-{index}", on_event=self.on_raw)
            self._spawn(listener.poll_group, chat, None, approvers, False, self._stop, card_approvers=limited,
                        tag=f"sa{index}", on_event=self.on_raw)
        cmd = ["dws", "event", "consume", "user_card_action_triggered", "--flatten", "-f", "ndjson"]
        self._spawn(listener.consume, cmd, None, approvers, False, self._stop, card_approvers=limited, key="card-action", on_event=self.on_raw)
        if self.config().get("auto_reply_listen", True) and getattr(self.host, "autoreply", None):
            for kind, flag in (("direct", "all-direct"), ("at", "at-me")):
                cmd = ["dws", "event", "+listen-im", "--kind", flag, "-f", "ndjson"]
                self._spawn(listener.consume, cmd, None, approvers, False, self._stop, key=f"autoreply-{kind}",
                            on_event=lambda raw, kind=kind: self.on_auto(raw, kind))
        self.report("ready", f"监听 {len(self.chats())} 个群")

    def _spawn(self, target, *args, **kwargs) -> None:
        thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True, name=f"dingtalk-{len(self._threads)}")
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        status = listener.read_status() if self._threads else {}
        down = [k for k, v in status.items() if not v.get("connected")]
        if self._threads and status and len(down) == len(status):
            self.report("error", "钉钉事件长连接都没连上：" + str(next(iter(status.values())).get("last_error") or "")[:120])
        elif self._threads:
            self.report("ready", f"监听 {len(self.chats())} 个群")

    def on_auto(self, raw: dict, kind: str) -> None:
        try:
            self.host.autoreply.receive(raw, kind, skip_chats=self.chats())
        except Exception:  # noqa: BLE001
            log.exception("处理自动回复消息失败")

    def on_raw(self, raw: dict) -> None:
        try:
            self.handle(raw)
        except Exception:  # noqa: BLE001
            log.exception("处理钉钉事件失败")

    def handle(self, raw: dict) -> dict | None:
        """一条 dws 原始事件 → 触发 / 指令。返回处理结果（测试用）。"""
        approvers, limited = self._people()
        c = listener.classify_event(raw, approvers, False, limited, bot_names=self.mention_names())
        if not c:
            return None
        by, chat, text = c.get("by") or "", c.get("chat") or (self.chats() or [None])[0], c.get("text") or ""
        kind = c["kind"]
        if kind == "message" and c.get("denied"):
            self._say(chat, f"没有权限：只有名单内的审批人可以{ {'decide': '批卡片', 'note': '记决定', 'demand': '转需求', 'published': '登记发布', 'pause': '暂停', 'resume': '恢复'}.get(c['denied'], '下这条指令')}。",
                      key("denied", c.get("message_id") or text))
            return {"denied": c["denied"]}
        if kind == "note":
            entry = governance.append_decision(c["content"], source=f"{self.title} {by}", home=self.host.home)
            self._say(chat, f"✅ 已记入决策档案：{entry}", key("note", c.get("message_id") or c["content"]))
            return {"note": entry}
        if kind == "demand":
            return self.host.pipeline.emit({"source": self.id, "type": "demand.manual", "subject": "demand:manual",
                                            "summary": c["content"][:300], "data": {"text": c["content"], "by": by},
                                            "dedup_key": f"{self.id}:{c.get('message_id') or c['content']}"}, trusted=True)
        if kind == "published":
            return self.host.pipeline.emit({"source": self.id, "type": "content.published.manual", "subject": f"pack:{c['pack_id']}",
                                            "summary": f"已发 {c['pack_id']}", "dedup_key": f"{self.id}:{c.get('message_id') or c['pack_id']}",
                                            "data": {"pack_id": c["pack_id"], "by": by, **({"url": c["url"]} if c.get("url") else {})}}, trusted=True)
        if kind in ("pause", "resume"):
            everything = c["key"] in ("all", "全部", "所有")
            result = self.host.settings.pause(kind == "pause", None if everything else c["key"], reason=f"{self.title} {by}", origin=self.id)
            self._say(chat, f"✅ 已{'暂停' if kind == 'pause' else '恢复'} {c['key']}", key(kind, c.get("message_id") or now_iso()))
            self.host.kick()
            return result
        base = {"source": self.id, "actor": {"source": self.id, "id": by}, "human": True, "at": c.get("at") or now_iso(), "text": text,
                "meta": {"chat": chat, "row": chat, "message_id": c.get("message_id"), "role": c.get("role")}}
        trigger_id = key(self.id, c.get("message_id") or c.get("event_id") or f"{chat}:{base['at']}:{text}")
        if kind == "decide":
            if not str(c["card"]).upper().startswith("T"):
                return None        # C12 是旧 ops 的卡片编号：两套并行听同一个群时，各认各的
            ticket = self.host.find_ticket(c["card"])
            if not ticket:
                self._say(chat, f"找不到工单 {c['card']}", key("unknown", trigger_id))
                return {"ok": False}
            thread = self.host.ticket_thread(ticket)
            trigger = {**base, "id": trigger_id, "threads": [thread], "addressed": "mention",
                       "channel": {"source": self.id, "thread": f"t{ticket.get('no')}", "address": {"chat": chat}}}
            if self.host.receive(trigger) == "duplicate":
                return {"duplicate": True}
            options = ((ticket.get("attention") or {}).get("card") or {}).get("options") or []
            choice = str(c.get("choice") or "")
            is_option = choice in [o.get("key") for o in options] or (choice.isdigit() and 1 <= int(choice) <= len(options))
            if not options or not is_option:
                return {"followup": ticket["id"]}     # 不是选项：就是给这张工单的一条跟进消息
            result = self.host.answer_by_code(c["card"], choice, by=by, reason=c.get("reason"), trigger_id=trigger_id)
            if not result.get("ok"):
                self._say(chat, f"T{ticket.get('no')} 没批成：{result.get('error')}", key("answer-failed", trigger_id))
            return result
        if not c.get("mentioned") and self.config().get("observe", "mention") != "all":
            return None
        thread_id = f"{chat}/{c.get('message_id') or trigger_id[:12]}"
        trigger = {**base, "id": trigger_id, "threads": [{"source": self.id, "id": thread_id}],
                   "channel": {"source": self.id, "thread": thread_id, "address": {"chat": chat}}}
        if c.get("mentioned"):
            trigger["addressed"] = "mention"
        return {"result": self.host.receive(trigger), "id": trigger_id}

    # ---- 发

    def _say(self, chat: str | None, text: str, message_key: str) -> None:
        self.host.outbox.notice({"source": self.id, "thread": "home", "address": {"chat": chat} if chat else {}}, text, key=message_key, priority=1)

    def send(self, message: dict) -> str:
        notifier = DingTalkNotifier(self.notifier_cfg())
        chat = (message["channel"].get("address") or {}).get("chat") or (self.chats() or [None])[0]
        if message.get("kind") == "file" and message.get("file"):
            result = notifier.send_file(message["file"], title=message.get("title"), target=chat)
        else:
            kind = "alert" if message.get("priority", 2) == 0 else "message"
            result = notifier.send(kind, "", dingtalk_markdown(message["text"]), priority=message.get("priority", 2), target=chat)
        if not result.get("ok"):
            raise RuntimeError(f"钉钉发送失败：{result.get('error')}")
        return "sent"
