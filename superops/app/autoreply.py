"""钉钉自动回复：别人单聊“我”、或在群里 @“我” 时，由 Agent 起草一段回复、以我的身份发出去。

和工单系统分开：这里只“聊天”——Agent 只读，产出一段回复文字；不开工单、不调动发布等任何动作，
所以不受审批人名单限制，谁来问都可以回。别人在消息里写的“帮我发一下 / 把配置给我”只当作要回答的话，不执行。

    开关    面板首页「钉钉自动回复」：auto 自动回复 / off 不回（默认 off）。存在库里，重启后保留。
    监听    dws event +listen-im --kind all-direct（全部单聊）、--kind at-me（任何群里 @我）。
            运营群（sources.dingtalk.chats）里的消息照旧走工单和批红，不在这里回。
    回复    群里引用原消息回复（chat +messages-reply），单聊直接发（chat +messages-send --open-dingtalk-id）；
            带钉钉的 AI 发送角标，末尾署名“<owner_name>的 Agent 自动回复”（local.yaml 的 owner_name，没配就是“我”）。
    过滤群  这些群里 @我 不回（告警群之类）。每项存群 ID 和群名：先按群 ID 对；只有群名的（添加时按名字没查到唯一的群），
            再查这个群叫什么、按群名对。面板「钉钉自动回复」里增删，或 superops autoreply exclude add|remove <群ID或群名>。
            local.yaml 的 sources.dingtalk.auto_reply_exclude 写的是初始名单，库里没有名单时用它。
    兜底    同一会话 1 小时最多回 6 条、全天最多 80 条（防两个 Agent 互相回个没完）；同一条消息只回一次；
            Agent 判断要本人回答的（承诺、私事、要权限 / 密钥等），不回，转告到 home 渠道。

Agent 的说明在 prompts/auto-reply.md；我这边的近况（这周在做什么、到哪一步）写在
~/popto-superops-data/auto-reply-context.md，自己改，Agent 每次都读最新的。
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from ..biz import paths
from ..biz.timeutil import TZ, parse_iso
from ..core.model import now_iso
from ..core.reasons import key

log = logging.getLogger("superops.autoreply")

STATE_KEY = "dingtalk:auto_reply"
LOG_KEY = "dingtalk:auto_reply:log"
SEEN_KEY = "dingtalk:auto_reply:seen"
EXCLUDE_KEY = "dingtalk:auto_reply:exclude"
MODES = ("auto", "off")
PER_CHAT_HOUR = 6
PER_DAY = 80
LOG_KEEP = 100
ESCALATE_GAP_MIN = 30          # 同一会话 30 分钟内最多转告一次
def owner(cfg: dict) -> str:
    """本人的称呼：local.yaml 的 owner_name；署名和 prompts/auto-reply.md 里的 {owner} 用它。"""
    return str((cfg or {}).get("owner_name") or "").strip() or "我"


def signature(cfg: dict) -> str:
    return f"—— {owner(cfg)}的 Agent 自动回复"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reply", "reason", "escalate"],
    "properties": {
        "action": {"type": "string", "enum": ["reply", "skip"]},
        "reply": {"type": "string", "description": "要发出去的回复正文（纯文本，不带署名）；skip 时为空串"},
        "reason": {"type": "string", "description": "一句话：为什么这样回 / 为什么不回"},
        "escalate": {"type": "boolean", "description": "是否需要本人亲自跟进（转告到 home 渠道）"},
        "escalate_note": {"type": "string", "description": "转告本人的一句话：谁、问了什么、建议怎么处理"},
    },
}


def context_file() -> Path:
    return paths.ops_data() / "auto-reply-context.md"


class AutoReply:
    def __init__(self, rt):
        self.rt = rt
        self._names: dict[str, str] = {}      # 群 ID → 群名（按名字过滤时用），进程内缓存
        rt.effects.handle("autoreply", self._run, limit=lambda: 0 if rt.settings.current()["paused"] else 2, retry=(5.0, 120.0))

    # ---- 开关和记录（面板用）

    def mode(self) -> str:
        return (self.rt.store.get(STATE_KEY) or {}).get("mode", "off")

    def set_mode(self, mode: str, by: str = "panel") -> dict:
        if mode not in MODES:
            raise ValueError(f"自动回复只能是 {' / '.join(MODES)}")
        self.rt.store.set(STATE_KEY, {"mode": mode, "by": by, "at": now_iso()})
        log.info("钉钉自动回复切到 %s（%s）", mode, by)
        return self.view()

    def view(self) -> dict:
        state = self.rt.store.get(STATE_KEY) or {"mode": "off"}
        return {"mode": state.get("mode", "off"), "changed_at": state.get("at"), "changed_by": state.get("by"), "app": self.app(),
                "exclude": self.excludes(), "recent": list(reversed(self.rt.store.get(LOG_KEY) or []))[:30]}

    def app(self) -> str:
        """现在用的聊天软件：钉钉和飞书二选一，看哪个来源开着（都没开算钉钉）。"""
        try:
            sources = self.rt.settings.current()["sources"]
        except Exception:  # noqa: BLE001
            return "钉钉"
        return "飞书" if (sources.get("lark") or {}).get("enabled") else "钉钉"

    # ---- 过滤群

    def excludes(self) -> list[dict]:
        saved = self.rt.store.get(EXCLUDE_KEY)
        if saved is not None:
            return saved
        src = self.rt.cfg.get("sources") or {}
        initial = ((src.get("dingtalk") or {}).get("auto_reply_exclude") or []) + ((src.get("lark") or {}).get("auto_reply_exclude") or [])
        return [_entry(x) for x in initial if _entry(x)]

    def add_exclude(self, value: str) -> dict:
        """value 是群 ID（cid 开头）或群名。群 ID 顺手查出群名；群名能唯一查到群就补上 ID，查不到只按名字过滤。"""
        value = str(value or "").strip()
        if not value:
            raise ValueError("要填群 ID 或群名")
        if _is_cid(value):
            entry = {"id": value, "name": self.group_name(value)}
        else:
            entry = {"id": self.find_group(value), "name": value}
        rows = [e for e in self.excludes() if not _same(e, entry)] + [entry]
        self.rt.store.set(EXCLUDE_KEY, rows)
        return self.view()

    def remove_exclude(self, value: str) -> dict:
        value = str(value or "").strip()
        rows = [e for e in self.excludes() if value not in (e.get("id"), e.get("name"))]
        self.rt.store.set(EXCLUDE_KEY, rows)
        return self.view()

    def excluded(self, chat: str) -> dict | None:
        """这个群在不在过滤名单里：先按群 ID，再按群名（只有名单里有只写了群名的项时才去查群名）。"""
        rows = self.excludes()
        for e in rows:
            if e.get("id") and e["id"] == chat:
                return e
        named = [e for e in rows if not e.get("id") and e.get("name")]
        if named and chat:
            name = self.group_name(chat)
            for e in named:
                if name and e["name"] == name:
                    return e
        return None

    def group_name(self, chat: str) -> str | None:
        if chat in self._names:
            return self._names[chat]
        if chat.startswith("oc_"):          # 飞书群：按群名过滤只认已知的名字，不去钉钉查
            return None
        data = _dws(["chat", "+conversation-info", "--group", chat])
        name = (((data.get("result") or {}).get("conversationInfo")) or {}).get("title")
        if name:
            self._names[chat] = name
        return name

    def find_group(self, name: str) -> str | None:
        """按群名找群 ID：只认名字完全一样且唯一的那个；零个或多个都返回 None（只按名字过滤）。"""
        data = _dws(["chat", "+chat-search", "--query", name])
        hits = [c for c in data.get("chats") or [] if (c.get("name") or c.get("title")) == name]
        if len(hits) == 1:
            cid = hits[0].get("openConversationId")
            if cid:
                self._names[cid] = name
            return cid
        return None

    def _log(self, entry: dict) -> None:
        rows = (self.rt.store.get(LOG_KEY) or []) + [{"at": now_iso(), **entry}]
        self.rt.store.set(LOG_KEY, rows[-LOG_KEEP:])

    # ---- 收（钉钉监听线程调用，只做轻量判断，真正的回复排进效果队列）

    def receive(self, raw: dict, kind: str, *, skip_chats: list[str] = (), platform: str = "dingtalk") -> str:
        """kind: direct（单聊）/ at（群里 @我）。platform: dingtalk / lark（飞书，raw 是 notifier.lark.normalize 的结果）。
        返回处理结果（测试用）。"""
        mid = str(raw.get("message_id") or raw.get("event_id") or "")
        text = str(raw.get("content") or "").strip()
        chat = str(raw.get("conversation_id") or raw.get("chat_id") or "")
        if not mid or not text:
            return "empty"
        if kind == "at" and chat in skip_chats:
            return "ops_chat"            # 运营群：照旧走工单 / 批红
        if self.mode() != "auto":
            return "off"
        if kind == "at" and self.excluded(chat):
            return "excluded"            # 过滤群（告警群之类）：不回，也不记，免得刷满记录
        seen = self.rt.store.get(SEEN_KEY) or []
        if mid in seen:
            return "duplicate"
        self.rt.store.set(SEEN_KEY, (seen + [mid])[-500:])
        who = str(raw.get("sender") or raw.get("sender_open_dingtalk_id") or raw.get("sender_id") or "对方")
        limited = self._over_limit(chat)
        if limited:
            self._log({"kind": kind, "platform": platform, "chat": chat, "sender": who, "text": text[:300], "status": "limited", "note": limited})
            return "limited"
        payload = {"kind": kind, "platform": platform, "message_id": mid, "chat": chat, "sender": who,
                   "sender_id": raw.get("sender_open_dingtalk_id") or raw.get("sender_id"),
                   "text": text[:4000], "at": raw.get("create_time") or now_iso(),
                   "quoted": (raw.get("quoted_message") or {}).get("content") if isinstance(raw.get("quoted_message"), dict) else None}
        self.rt.store.atomic(lambda: self.rt.effects.enqueue("autoreply", key("autoreply", mid), payload))
        return "queued"

    def _escalated_recently(self, chat: str) -> bool:
        since = datetime.now(TZ) - timedelta(minutes=ESCALATE_GAP_MIN)
        for r in self.rt.store.get(LOG_KEY) or []:
            if r.get("chat") == chat and r.get("escalated"):
                try:
                    if parse_iso(r["at"]).astimezone(TZ) >= since:
                        return True
                except (KeyError, ValueError):
                    continue
        return False

    def _over_limit(self, chat: str) -> str | None:
        now = datetime.now(TZ)
        sent = []
        for r in self.rt.store.get(LOG_KEY) or []:
            if r.get("status") == "sent":
                try:
                    sent.append((parse_iso(r["at"]).astimezone(TZ), r.get("chat")))
                except (KeyError, ValueError):
                    continue
        if sum(1 for at, _ in sent if at.date() == now.date()) >= PER_DAY:
            return f"今天已经自动回了 {PER_DAY} 条，到上限了"
        if sum(1 for at, c in sent if c == chat and at >= now - timedelta(hours=1)) >= PER_CHAT_HOUR:
            return f"这个会话 1 小时内已经回了 {PER_CHAT_HOUR} 条（防止和别的 Agent 互相回个没完）"
        return None

    # ---- 回（效果队列里跑）

    def _run(self, payload: dict, effect_key: str) -> None:
        rt = self.rt
        if self.mode() != "auto":     # 排队期间被关掉了
            self._log({**_brief(payload), "status": "off", "note": "排队期间自动回复被关掉，没回"})
            return
        history = self._history(payload)
        run_dir = paths.ops_data() / "work" / "_autoreply" / datetime.now(TZ).strftime("%Y%m%d") / effect_key[:12]
        settings = rt.settings.current()
        outcome = rt.agents.run(role=settings["eval"]["role"], schema=SCHEMA, run_dir=run_dir, tools="readonly", effort="low",
                                prompt=lambda agent, errors, fallback: self.prompt(payload, history, errors),
                                timeout=240.0, priority=1, check=_check)
        if not outcome.ok:
            self._log({**_brief(payload), "status": "failed", "note": (outcome.error_detail or "Agent 没跑成")[:200]})
            raise RuntimeError(f"自动回复没生成出来：{outcome.error_detail}")
        out = outcome.output
        if out.get("escalate") and self._escalated_recently(payload["chat"]):
            out = {**out, "escalate": False, "reason": (out.get("reason") or "") + f"（这个会话 {ESCALATE_GAP_MIN} 分钟内已经转告过，这次不再转告）"}
        if out.get("escalate"):
            rt.notify("message", f"有人找你：{payload['sender']}",
                      f"{'单聊' if payload['kind'] == 'direct' else '群里 @你'}：{payload['text'][:500]}\n\n"
                      f"Agent：{out.get('escalate_note') or out.get('reason')}"
                      + ("\n\n（Agent 已先回了一句，见下）\n\n> " + out["reply"][:500] if out["action"] == "reply" and out.get("reply") else ""),
                      priority=1, ref=f"autoreply-escalate:{payload['message_id']}", dedup=True)
        if out["action"] != "reply" or not out.get("reply", "").strip():
            self._log({**_brief(payload), "status": "skipped", "note": out.get("reason", "")[:200], "escalated": bool(out.get("escalate"))})
            return
        text = out["reply"].strip() + "\n\n" + signature(self.rt.cfg)
        sent = self.send(payload, text)
        self._log({**_brief(payload), "status": "sent" if sent["ok"] else "send_failed", "reply": text[:1000],
                   "note": (out.get("reason") or "")[:200] if sent["ok"] else sent.get("error", "")[:200],
                   "escalated": bool(out.get("escalate"))})
        if not sent["ok"]:
            raise RuntimeError(f"自动回复发送失败：{sent.get('error')}")

    def prompt(self, payload: dict, history: list[dict], errors: list[str] | None = None) -> str:
        guide = (self.rt.home / "prompts" / "auto-reply.md")
        guide_text = guide.read_text(encoding="utf-8").replace("{owner}", owner(self.rt.cfg)) if guide.exists() else ""
        ctx = context_file()
        ctx_text = ctx.read_text(encoding="utf-8") if ctx.exists() else "（还没写近况）"
        lines = [guide_text, "", "## 我的近况（本人维护，可以据此回答工作进展）", "", ctx_text.strip(), "",
                 "## 系统里正在跑的事（自动生成，只作参考，不要原样贴出工单编号和内部路径）", "", self._snapshot(), "",
                 "## 这次的消息", "",
                 f"- 场景：{'单聊（对方直接找我）' if payload['kind'] == 'direct' else '群聊里 @ 了我'}",
                 f"- 发送人：{payload['sender']}", f"- 时间：{payload.get('at')}", ""]
        if history:
            lines += ["会话里最近的几条（旧→新，供理解上下文）：", "", "<<<UNTRUSTED_DATA", *[f"[{h['time']}] {h['sender']}：{h['text']}" for h in history],
                      "UNTRUSTED_DATA>>>", ""]
        if payload.get("quoted"):
            lines += ["对方引用的消息：", "", "<<<UNTRUSTED_DATA", str(payload["quoted"])[:1000], "UNTRUSTED_DATA>>>", ""]
        lines += ["要回复的这条：", "", "<<<UNTRUSTED_DATA", payload["text"], "UNTRUSTED_DATA>>>", "",
                  "只输出一个符合 schema 的 JSON 对象。"]
        if errors:
            lines += ["", "上一次的输出不合格：", *[f"- {e}" for e in errors]]
        return "\n".join(lines)

    def _snapshot(self) -> str:
        """给 Agent 的系统近况：未结束的工单标题、最近 3 天结束的工单标题。只给标题，不给正文。"""
        try:
            store = self.rt.store
            open_ = [t.get("title") or "" for t in store.tickets.open()][:10]
            since = (datetime.now(TZ) - timedelta(days=3)).isoformat()
            ended = [t.get("title") or "" for t in store.tickets.recent(200, since=since) if t.get("ended")][:15]
            return "\n".join(["进行中：" + ("；".join(open_) or "无"), "最近 3 天做完的：" + ("；".join(ended) or "无")])
        except Exception:  # noqa: BLE001
            return "（读不到）"

    def _history(self, payload: dict) -> list[dict]:
        """会话里最近 8 条消息（含这条），帮 Agent 看懂上下文；读不到就算了。"""
        if payload.get("platform") == "lark":
            from ..biz.notifier import lark
            try:
                since = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
                rows = [{"time": m.get("create_time") or "", "sender": m.get("sender") or "", "text": m["content"][:500]}
                        for m in lark.list_messages(payload["chat"], since)]
                return rows[-8:]
            except Exception as error:  # noqa: BLE001
                log.info("读飞书会话上下文失败：%s", error)
                return []
        target = ["--open-dingtalk-id", payload["sender_id"]] if payload["kind"] == "direct" and payload.get("sender_id") else ["--group", payload["chat"]]
        try:
            p = subprocess.run(["dws", "chat", "+chat-messages", *target, "--limit", "8", "--no-reactions", "--format", "json"],
                               capture_output=True, text=True, timeout=45)
            data = json.loads(p.stdout or "{}")
            items = data.get("items") or data.get("messages") or []
            rows = [{"time": str(m.get("time") or m.get("createTime") or ""), "sender": str(m.get("sender") or ""),
                     "text": str(m.get("text") or m.get("content") or "")[:500]} for m in items]
            return sorted(rows, key=lambda r: r["time"])[-8:]
        except Exception as error:  # noqa: BLE001
            log.info("读会话上下文失败：%s", error)
            return []

    def send(self, payload: dict, text: str) -> dict:
        """以我的身份发出。群里引用原消息回复；单聊直接发给对方。自动回复开关就是授权，所以带 --yes。
        飞书：单聊和群里都用引用回复（lark-cli im +messages-reply --as user），发出的消息记下 ID，轮询时不当成新消息。"""
        if payload.get("platform") == "lark":
            from ..biz.notifier import lark
            try:
                mid = lark.reply(payload["message_id"], text)
            except lark.LarkError as error:
                return {"ok": False, "error": str(error)[:200]}
            source = getattr(self.rt, "lark", None)
            if source and mid:
                source.remember_sent(mid)
            return {"ok": True}
        if payload["kind"] == "direct" and payload.get("sender_id"):
            cmd = ["dws", "chat", "+messages-send", "--as", "user", "--open-dingtalk-id", payload["sender_id"], "--text", text]
        else:
            cmd = ["dws", "chat", "+messages-reply", "--message-id", payload["message_id"], "--content", text]
        try:
            p = subprocess.run([*cmd, "--yes", "--format", "json"], capture_output=True, text=True, timeout=60)
            data = json.loads(p.stdout or "{}")
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)[:200]}
        if p.returncode == 0 and data.get("success") is not False and not data.get("errorCode"):
            return {"ok": True}
        return {"ok": False, "error": str(data.get("errorMsg") or p.stderr or p.stdout)[:200]}


def _is_cid(value: str) -> bool:
    return value.startswith("cid") and len(value) > 10


def _entry(x) -> dict | None:
    if isinstance(x, dict):
        e = {"id": x.get("id") or None, "name": x.get("name") or None}
    else:
        x = str(x or "").strip()
        e = {"id": x, "name": None} if _is_cid(x) else {"id": None, "name": x}
    return e if (e["id"] or e["name"]) else None


def _same(a: dict, b: dict) -> bool:
    return bool((a.get("id") and a["id"] == b.get("id")) or (a.get("name") and a["name"] == b.get("name")))


def _dws(args: list[str]) -> dict:
    try:
        p = subprocess.run(["dws", *args, "--format", "json"], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout or "{}")
        return data if isinstance(data, dict) else {}
    except Exception as error:  # noqa: BLE001
        log.info("dws %s 失败：%s", " ".join(args[:2]), error)
        return {}


def _brief(payload: dict) -> dict:
    return {"kind": payload["kind"], "platform": payload.get("platform", "dingtalk"), "chat": payload["chat"], "sender": payload["sender"],
            "text": payload["text"][:300], "message_id": payload["message_id"]}


def _check(out: dict) -> list[str]:
    if out["action"] == "reply" and not out.get("reply", "").strip():
        return ["action 是 reply 时 reply 不能为空"]
    if len(out.get("reply") or "") > 1500:
        return ["回复太长：控制在 1500 字以内，聊天里说重点"]
    return []
