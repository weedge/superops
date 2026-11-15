"""Runtime：组合根。造出各个部件、把它们接起来、管启动和停止；自己不含领域规则。

    来源 ──触发──▶ Engine ──端口──▶ Assessor（研判） / TicketExecutor（执行） / Outbox（找人）
                     │                      └──────────── 都通过 Effects 排队，提交后才做 ───────────┘
                     └─ 领域事件 ─▶ 这里：问题变成卡片发出去；人答了问题 → 记档、发出选项里写好的事件、回执

一轮 pass（每 2 秒，或有人 kick）：来源轮询 → 引擎 evaluate_due → 到期处理（问题超时、工单闲置）。
"""
from __future__ import annotations

import logging
import re
import secrets
import os
import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..infra import config as cfgmod, governance, paths
from ..infra.timeutil import TZ, parse_duration
from ..core import Engine, EngineOptions
from ..core.errors import UserError
from ..core.model import live_ticket, ms, now_iso, parse_time, shift, to_iso
from ..core.reasons import key, reason_text
from ..core.stage import ticket_stage
from ..executor.pool import AgentPool
from ..executor.worker import TicketExecutor
from ..sources.cli import CliSource
from ..sources.kind import SourceRegistry
from ..sources.pipeline import PipelineSource
from ..sources.schedule import ScheduleSource
from . import preflight as preflight_mod, prompts
from .assessor import Assessor
from .effects import Effects
from .health import HealthBoard
from .outbox import Outbox
from .settings import Settings, rule_of
from .store import Store

log = logging.getLogger("superops.runtime")
PICK = re.compile(r"\s*#?(\d+(?:\s*[,，、\s]\s*#?\d+)*)\s*")


class LocalClient:
    """进程内的“中台客户端”：上下文提供者和脚本用的那几个接口，直接调 Runtime，不走 HTTP。"""

    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self.base = rt.api_url

    def get(self, path: str, **params) -> dict:
        if path == "/v1/digest":
            return self.rt.digest(params.get("since"), params.get("until"))
        if path == "/v1/counter":
            return {"name": params.get("name"), "value": self.rt.counter(params.get("name", ""))}
        if path == "/v1/health":
            return {"ok": True}
        raise KeyError(path)

    def post(self, path: str, body: dict | None = None, retries: int = 0) -> dict:
        body = body or {}
        if path == "/v1/events":
            return self.rt.pipeline.emit(body, trusted=True)
        if path == "/v1/notify":
            return self.rt.notify(body.get("kind", "message"), body.get("title", ""), body.get("body", ""),
                                  priority=int(body.get("priority", 2)), ref=body.get("ref"), dedup=bool(body.get("dedup")))
        if path == "/v1/counter":
            self.rt.add_counter(body["name"], float(body["value"]))
            return {"ok": True}
        raise KeyError(path)

    def emit(self, evt: dict) -> dict:
        return self.post("/v1/events", evt)

    def notify(self, kind: str, title: str, body: str, priority: int = 2, ref: str | None = None, dedup: bool = False) -> dict:
        return self.post("/v1/notify", {"kind": kind, "title": title, "body": body, "priority": priority, "ref": ref, "dedup": dedup})

    def health(self) -> bool:
        return True


class Runtime:
    def __init__(self, *, home: Path | None = None, sync: bool = False, cfg: dict | None = None, db: str | Path | None = None):
        self.home = home or paths.ops_home()
        self.data = paths.ensure_data_dirs()
        self.cfg = cfg if cfg is not None else cfgmod.load_agents_config(self.home)
        hub = self.cfg.get("hub") or {}
        self.api_url = f"http://{hub.get('host', '127.0.0.1')}:{hub.get('port', 8797)}"
        self.script_token = secrets.token_hex(16)   # 只交给宿主拉起的确定性脚本：带它发的事件才算可信
        self.store = Store(db or self.data / "superops.sqlite")
        self.health = HealthBoard()
        self.effects = Effects(self.store, self.health, sync=sync)
        self.sources = SourceRegistry(lambda: self.settings.current())
        self.settings = Settings(self.store, self.sources, home=self.home, on_change=self._policy_changed)
        self.settings.current()  # 策略不合法就在这里报错，不带病启动
        self.agents = AgentPool(self.cfg, self.store, self.settings, self.health)
        self.outbox = Outbox(self.store, self.effects, self.sources, paused=lambda: self.settings.current()["paused"], config=lambda: self.cfg)
        self.executor = TicketExecutor(self)
        self.assessor = Assessor(self)
        from .autoreply import AutoReply
        self.autoreply = AutoReply(self)
        self.engine = Engine(EngineOptions(repo=self.store, policy=self.settings.policy, scope=self.sources, assessor=self.assessor,
                                           executor=self.executor, outbox=self.outbox, prepare_ticket=self.executor.prepare, emit=self._on_event))
        self.local_client = LocalClient(self)
        self.schedule, self.pipeline, self.cli = ScheduleSource(self), PipelineSource(self), CliSource(self)
        for kind in (self.schedule, self.pipeline, self.cli):
            self.sources.register(kind)
        try:
            from ..sources.dingtalk import DingTalkSource
            self.dingtalk = DingTalkSource(self)
            self.sources.register(self.dingtalk)
        except ImportError:  # 钉钉来源是可选的
            self.dingtalk = None
        try:
            from ..sources.lark import LarkSource
            self.lark = LarkSource(self)
            self.sources.register(self.lark)
        except ImportError:  # 飞书来源同样可选
            self.lark = None
        self.sync = sync
        self.echo = False
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._pass_lock = threading.Lock()

    # ---- 生命周期

    def start(self) -> None:
        self.settings.start()
        self.executor.resume()
        self.effects.start()
        if not self.sync:
            self.sources.start()
            self._thread = threading.Thread(target=self._loop, name="passes", daemon=True)
            self._thread.start()
            threading.Thread(target=self._startup_preflight, name="preflight", daemon=True).start()
        self.health.set("runtime", "ready", "已启动")

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        self.executor.shutdown()
        self.sources.stop()
        self.effects.stop()
        if self._thread:
            self._thread.join(timeout=5)
        self.store.close()

    def kick(self) -> None:
        self._kick.set()
        self.effects.kick()

    # ---- 启动检查：要提前登录的、要提前放好的密钥

    def preflight(self, *, refresh: bool = False, logins: bool = True, check=None) -> dict:
        """上一次检查的结果；refresh 时重新查一遍（logins=True 会无界面打开 Chrome 看各平台登录态）。"""
        cached = self.store.get("preflight")
        if cached and not refresh:
            return cached
        result = preflight_mod.run(self.cfg, self.settings.current(), logins=logins, check=check)
        self.store.set("preflight", result)
        return result

    def announce_preflight(self, result: dict) -> bool:
        """没准备好的提前告诉人：发到 home 渠道，同样的缺口一天只说一次。全部就绪不出声。"""
        text = preflight_mod.render(result)
        if not text:
            self.health.set("preflight", "ready", "登录态和密钥都已就绪")
            return False
        self.health.set("preflight", "waiting", f"{len(result['missing'])} 项没准备好：" + "、".join(i["title"] for i in result["missing"]))
        message_key = preflight_mod.fingerprint(result)
        if self.outbox.status(message_key):
            return False
        self.outbox.notice(self.home_channel(), text, key=message_key, priority=2)
        return True

    def _startup_preflight(self) -> None:
        try:
            result = self.preflight(refresh=True)
            for item in result["missing"]:
                log.warning("启动检查：%s 没准备好（%s）。影响：%s。补：%s", item["title"], item["detail"], "、".join(item["rules"]), item["fix"])
            if not result["missing"]:
                log.info("启动检查：登录态和密钥都已就绪")
            self.announce_preflight(result)
        except Exception:  # noqa: BLE001
            log.exception("启动检查出错")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._kick.wait(2.0)
            self._kick.clear()
            if self._stop.is_set():
                break
            try:
                self.run_pass()
                self.health.set("runtime", "ready", "")
            except Exception as error:  # noqa: BLE001
                log.exception("这一轮 pass 出错")
                self.health.set("runtime", "error", str(error)[:300])

    def run_pass(self) -> None:
        """一次只跑一轮；跑的过程中有人要求的，合并成之后的一轮。"""
        with self._pass_lock:
            self.sources.tick()
            self.engine.evaluate_due()
            self._housekeeping()
            self._eval_health()

    def pump(self, rounds: int = 20) -> None:
        """同步模式（测试、一次性命令）：反复跑 pass 和副作用，直到没有可做的。"""
        for _ in range(rounds):
            self.run_pass()
            if not self.effects.run_until_idle():
                self.run_pass()
                if not self.effects.run_until_idle():
                    break

    # ---- 来源的入口

    def receive(self, trigger: dict) -> str:
        result = self.engine.receive(trigger)
        self.kick()
        return result

    # ---- 领域事件 → 外部动作（都在提交之后）

    def _on_event(self, event: dict) -> None:
        handler = {"attention.asked": self._attention_asked, "attention.answered": self._attention_answered,
                   "ticket.ended": self._ticket_ended}.get(event["type"])
        if handler:
            self.store.after_commit(lambda: self._guard(handler, event["data"]))

    @staticmethod
    def _guard(handler, data: dict) -> None:
        try:
            handler(data)
        except Exception:  # noqa: BLE001
            log.exception("处理领域事件失败")

    def _ticket_ended(self, data: dict) -> None:
        """结束的工单不再订阅共用的定时线程：否则每次定时都要把历史上所有的工单读一遍。
        人之后按编号找它，走的是它自己的线程。"""
        ticket = self.store.tickets.get(data["ticketId"])
        origin = self.store.triggers.get(ticket["origin"]["triggerId"]) if ticket else None
        for ref in (origin or {}).get("threads") or []:
            if ref["source"] == "schedule":
                self.engine.unsubscribe_ticket(ticket["id"], f"{ref['source']}:{ref['id']}", "工单已结束")

    def ticket_thread(self, ticket: dict) -> dict:
        """人按编号对这张工单说话时，消息落在哪个线程。"""
        if ticket.get("thread"):
            return ticket["thread"]
        origin = self.store.triggers.get(ticket["origin"]["triggerId"]) or {}
        return (origin.get("threads") or [{"source": "ticket", "id": f"T{ticket.get('no')}"}])[0]

    def _attention_asked(self, data: dict) -> None:
        """工单提了一个要人决定的问题：原样变成一张带编号的卡片，发到引出它的消息所在的会话（否则工单开始的会话）。"""
        ticket = self.store.tickets.get(data["ticketId"])
        attention = (ticket or {}).get("attention")
        if not attention or attention["id"] != data["attentionId"]:
            return
        rule = self._rule(ticket)
        text = prompts.render_attention(ticket, attention, rule)
        try:
            self.engine.reply({"purpose": "attention", "ticketId": ticket["id"], "question": attention["question"], "text": text,
                               "priority": (rule or {}).get("priority", 1)})
        except UserError as error:
            log.warning("工单 T%s 的问题发不出去：%s", ticket.get("no"), error)

    def _attention_answered(self, data: dict) -> None:
        """人答了问题：记入决策档案、发出选项里写好的事件（带批准凭据）、给一条回执。"""
        ticket = self.store.tickets.get(data["ticketId"])
        if not ticket:
            return
        option, by, reason = data["option"], data.get("by") or "operator", data.get("reason")
        record = next((a for a in reversed(ticket.get("answered") or []) if a["attentionId"] == data["attentionId"]), {})
        title = record.get("question") or ticket["title"]
        no = ticket.get("no", "?")
        emits = list(enumerate(option.get("emit") or [], 1))      # 带上明细序号：同一张卡片里同类型、同主题的几条要分得开
        picked = self.store.get(f"picked:{data['attentionId']}")
        if picked:
            emits = [emits[i - 1] for i in picked if 1 <= i <= len(emits)]
        approved = {"ticket": f"T{no}", "ticketId": ticket["id"], "attentionId": data["attentionId"], "option": option.get("key"),
                    "label": option.get("label"), "by": by, "at": now_iso()}
        context = {"card_id": f"T{no}", "choice": option.get("key"), "reason": None if picked else reason, "decided_by": by}
        routed = []
        # 到期按安全选项处理时不带批准凭据：需要人批的事件类型（发布、回复、合并…）会被流水线来源拦下，
        # 只有不需要批准的登记类事件（如把线索记为“不跟进”）能发出去。
        for index, event in emits:
            result = self.pipeline.emit({"source": "decision", "type": event["type"], "subject": event.get("subject"),
                                         "summary": event.get("summary") or f"{title} · {option.get('label')}",
                                         "link": event.get("link"), "payload_ref": event.get("payload_ref"),
                                         "data": {**(event.get("data") or {}), **context},
                                         # 2026-11-12：去重键原来没有序号，竞品评审批的 10 条 demand.manual 同主题，只进了 1 条
                                         "dedup_key": f"decision:{data['attentionId']}:{index}:{event['type']}:{event.get('subject')}"},
                                        approved=None if data.get("expired") else approved, by_ticket=ticket["id"])
            if result.get("routed") == "trigger":
                routed.append(result)
        entry = None
        if reason and not picked and not data.get("expired"):
            if option.get("record") == "not_doing":
                entry = governance.append_not_doing(title, reason, source=f"工单 T{no}（{by}）", reply=option.get("reply"), home=self.home)
            elif option.get("record") == "decision":
                entry = governance.append_decision(reason, source=f"工单 T{no}（{by}）", home=self.home)
        label = option.get("label") or option.get("key")
        head = f"T{no} 到期没有回复，按「{label}」处理" if data.get("expired") else (
            f"已收到 T{no} →「{label}」" + (f"（只批第 {'、'.join(map(str, picked))} 条）" if picked else ""))
        lines = [f"✅ {head}", "", f"问题：{title}"]
        if reason and not picked:
            lines.append(f"原因：{reason}")
        if entry:
            lines.append("已记入决策档案")
        if data.get("expired"):
            lines.append("接下来：" + (f"登记了 {len(routed)} 项（不做对外动作）" if routed else "无后续动作"))
        else:
            lines.append("接下来：" + (f"发出 {len(routed)} 个已批准的动作，自动进行；有需要拍板的会再问" if routed else "无后续动作"))
        self.notify_ticket(ticket, "\n".join(lines), priority=1, key_=key("answer-receipt", data["attentionId"]))
        self.kick()

    # ---- 人的操作（钉钉回复、终端、面板）

    def find_ticket(self, ref: str) -> dict | None:
        """按 T12 / 12 / 工单 id（或前缀）找工单。"""
        ref = str(ref).strip()
        match = re.fullmatch(r"[TtCc]?(\d+)", ref)
        if match:
            return self.store.tickets.by_no(int(match.group(1)))
        ticket = self.store.tickets.get(ref)
        if ticket or len(ref) < 6:
            return ticket
        rows = self.store.all("SELECT value FROM ticket WHERE id LIKE ? LIMIT 2", (ref + "%",))
        import json
        return json.loads(rows[0][0]) if len(rows) == 1 else None

    def answer_by_code(self, code: str, choice, *, by: str = "operator", reason: str | None = None, trigger_id: str | None = None) -> dict:
        """回答一张工单的问题。带 trigger_id 时按那条消息的发送人查审批权限；不带时是本机操作者的命令。
        「T4 1 3,5」：选项的 emit 和明细一一对应时，只批第 3、5 条。"""
        ticket = self.find_ticket(code)
        if not ticket:
            return {"ok": False, "error": f"找不到工单 {code}"}
        attention = ticket.get("attention")
        if not attention:
            last = (ticket.get("answered") or [{}])[-1]
            return {"ok": True, "already": True, "choice": last.get("option")} if last else {"ok": False, "error": f"T{ticket.get('no')} 现在没有待回答的问题"}
        card = attention.get("card") or {}
        options = card.get("options") or []
        if not options:  # 没有选项的开放问题：回答就是一条人的消息，交给执行器
            return {"ok": False, "error": f"T{ticket.get('no')} 的问题没有选项，请直接回复文字"}
        chosen = None
        cs = str(choice).strip() if choice is not None else ""
        chosen = next((o for o in options if o.get("key") == cs), None)
        if chosen is None and cs.isdigit() and 1 <= int(cs) <= len(options):
            chosen = options[int(cs) - 1]
        if chosen is None:
            return {"ok": False, "error": f"无效选项 {choice!r}，可选：" + "、".join(f"{i}={o.get('key')}" for i, o in enumerate(options, 1))}
        emits = chosen.get("emit") or []
        picked = None
        match = PICK.fullmatch(reason or "")
        if match and emits and len(emits) == len(card.get("items") or []):
            picked = sorted({int(x) for x in re.findall(r"\d+", match.group(1))})
            bad = [i for i in picked if not 1 <= i <= len(emits)]
            if bad:
                return {"ok": False, "error": f"明细序号超出范围：{bad}（共 {len(emits)} 条）"}
            if picked == list(range(1, len(emits) + 1)):
                picked, reason = None, None      # 全部明细都点了（或只有一条）：就是普通的通过
            else:
                self.store.set(f"picked:{attention['id']}", picked)
                reason = "只批明细第 " + "、".join(map(str, picked)) + " 条"
        try:
            option = self.engine.answer(ticket["id"], chosen["key"], trigger_id=trigger_id, by=by, reason=reason)
        except UserError as error:
            self.store.delete(f"picked:{attention['id']}")
            return {"ok": False, "error": str(error), "code": error.code}
        self.kick()
        return {"ok": True, "ticket": ticket["id"], "no": ticket.get("no"), "choice": option.get("key")}

    # ---- 找人（都走发件箱）

    def home_channel(self, rule_id: str | None = None) -> dict:
        """没有会话可回的工单（定时、流水线起的）找人的地方；可以按规则分到别的群。"""
        settings = self.settings.current()
        home = dict(settings["home"])
        chats = ((settings["sources"].get(home.get("source")) or {}).get("config") or {}).get("rule_chats") or {}
        if rule_id and chats.get(rule_id):
            home = {**home, "thread": f"chat-{chats[rule_id]}", "address": {**(home.get("address") or {}), "chat": chats[rule_id]}}
        home.setdefault("address", {})
        home.setdefault("thread", "home")
        return home

    def notify_ticket(self, ticket: dict, text: str, *, priority: int = 2, key_: str | None = None) -> None:
        channel = (self.store.triggers.get(ticket["origin"]["triggerId"]) or {}).get("channel") or (ticket["channels"] or [self.home_channel()])[0]
        self.outbox.notice(channel, text, key=key_ or key("notice", ticket["id"], text), priority=priority, ticket_id=ticket["id"])

    def notify(self, kind: str, title: str, body: str, *, priority: int = 2, ref: str | None = None, dedup: bool = False) -> dict:
        """宿主和脚本要说的话（告警、发布结果、要人手动发布的包、文件）。kind: message | alert | file。"""
        text = body if kind == "file" else (f"**{title}**\n\n{body}" if title and title not in body else body)
        if kind == "alert":
            text = "🚨 " + text
            priority = min(priority, 1)
        message_key = key("notify", kind, ref) if (dedup and ref) else key("notify", kind, ref or "", title, body, now_iso())
        if self.outbox.status(message_key):
            return {"ok": True, "duplicate": True}
        message = {"id": message_key, "channel": self.home_channel(), "text": text, "kind": "file" if kind == "file" else "notice",
                   "priority": priority}
        if kind == "file":
            message["file"] = body
            message["title"] = title
        self.outbox.send(message)
        return {"ok": True, "id": message_key}

    # ---- 计数器、脚本环境

    @staticmethod
    def _today() -> str:
        return datetime.now(TZ).date().isoformat()

    def add_counter(self, name: str, value: float) -> None:
        self.store.add_counter(name, value, self._today())

    def counter(self, name: str) -> float:
        return self.store.counter(name, self._today())

    def script_env(self, run_dir: Path) -> dict:
        """确定性脚本的环境：能回调宿主，带可信令牌。"""
        return {**self.agent_dirs(run_dir), "OPS_TASK_FILE": str(Path(run_dir) / "task.json"), "OPS_HUB_URL": self.api_url,
                "OPS_HUB_TOKEN": self.script_token}

    def agent_dirs(self, run_dir: Path) -> dict:
        """Agent 的环境：只有目录，没有宿主地址和令牌——它要发的事件写在报告里，由宿主来发。"""
        return {"OPS_RUN_DIR": str(run_dir), "OPS_DATA": str(paths.ops_data()), "OPS_HOME": str(self.home),
                "SUPEROPS_DATA": str(paths.ops_data()), "SUPEROPS_HOME": str(self.home)}

    # ---- 到期处理、健康

    def _rule(self, ticket: dict) -> dict | None:
        rule_id = ((ticket["mandate"].get("basis") or {}).get("rules") or [None])[0]
        return rule_of(self.settings.current(), rule_id) if rule_id else None

    def _housekeeping(self) -> None:
        if self.settings.current()["paused"]:
            return
        now = now_iso()
        default_idle = self.settings.current()["executor"]["idle_ttl"]
        for ticket in self.store.tickets.open():
            attention = ticket.get("attention")
            if attention and attention.get("expiresAt") and attention["expiresAt"] <= now:
                card = attention.get("card") or {}
                safe = card.get("on_expire") or next((o["key"] for o in reversed(card.get("options") or []) if not o.get("emit")), None)
                if safe:
                    try:  # 到期按卡片上写好的安全选项处理：它不做任何对外动作
                        self.engine.answer(ticket["id"], safe, by="timeout", reason="到期未回复", expired=True)
                    except UserError as error:
                        log.info("T%s 的问题到期处理失败：%s", ticket.get("no"), error)
                continue
            if ticket_stage(ticket)["stage"] == "idle":
                ttl = parse_duration((self._rule(ticket) or {}).get("idle_ttl") or default_idle)
                if ttl and ms(now) - ms(ticket["updatedAt"]) > ttl.total_seconds() * 1000:
                    self.engine.drop_ticket(ticket["id"], "闲置超期，没有等到后续事件")

    def _eval_health(self) -> None:
        since = shift(now_iso(), -3600)
        unresolved = [d for d in self.store.decisions.recent(50, kind="unavailable", since=since) if not d.get("dismissed")]
        if unresolved:
            self.health.set("evals", "error", f"最近一小时有 {len(unresolved)} 次研判没有完成：{reason_text(unresolved[0]['reason'])}")
        else:
            self.health.set("evals", "ready", "")
        cb = parse_duration(self.cfg.get("alert_after_unavailable", "2h"))
        for name, state in self.agents.states().items():
            since_ = state.get("unavailable_since")
            if state.get("state") == "open" and since_ and cb:
                from ..infra.timeutil import now as biz_now, parse_iso
                if biz_now() - parse_iso(since_) > cb:
                    self.notify("alert", f"Agent {name} 已不可用超过 {self.cfg.get('alert_after_unavailable', '2h')}",
                                f"原因：{state.get('kind')}，{state.get('last_error') or ''}", priority=0, ref=f"agent-down:{name}:{since_}", dedup=True)

    # ---- 自己重启（面板切换聊天渠道后用）

    def restartable(self) -> bool:
        """是不是 launchd 拉起来的：是的话退出后会被自动拉起（KeepAlive）。"""
        from ..infra.instance import current
        return str(os.environ.get("XPC_SERVICE_NAME") or "").startswith(current().launchd_label)

    def request_restart(self, reason: str) -> bool:
        """过一秒给自己发 SIGTERM：superops run 收到后先关端口、等手头的工单收尾再退出，launchd 再拉起来。
        不是 launchd 起的就不动，返回 False（面板提示人手动重启）。"""
        if not self.restartable() or self.sync:
            return False
        log.warning("宿主将重启：%s", reason)
        threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return True

    def _policy_changed(self, old: str, new: str, effects: dict) -> None:
        self.engine.policy_changed(old, new, effects)
        self.kick()

    # ---- 摘要（日报 / 周报的上下文；形状沿用 ops 的 /v1/digest）

    def digest(self, since: str | None, until: str | None = None) -> dict:
        since_iso = to_iso(parse_time(since)) if since and parse_time(since) else shift(now_iso(), -86400)
        until_iso = to_iso(parse_time(until)) if until and parse_time(until) else now_iso()
        settings = self.settings.current()
        by_rule: dict[str, list] = {}
        cards, pending = [], []
        for ticket in self.store.tickets.recent(500, since=since_iso):
            if ticket["updatedAt"] > until_iso:
                continue
            rule = self._rule(ticket)
            stage = ticket_stage(ticket)
            report = next((a["report"] for a in reversed(ticket["assignments"]) if a.get("report")), None)
            state = (ticket.get("ended") or {}).get("outcome") or "/".join(v for v in (stage["stage"], stage.get("action")) if v)
            by_rule.setdefault((rule or {}).get("name") or "明确请求", []).append(
                {"id": f"T{ticket.get('no')}", "state": state, "title": ticket["title"], "summary": (report or {}).get("summary"),
                 "error": (report or {}).get("summary") if (report or {}).get("state") in ("failed", "blocked") else None})
            for answer in ticket.get("answered") or []:
                if since_iso <= answer["at"] <= until_iso:
                    cards.append({"seq": ticket.get("no"), "title": answer["question"], "status": "expired" if answer.get("expired") else "decided",
                                  "choice": answer["option"], "decided_by": answer["by"]})
        for ticket in self.store.tickets.open():
            if ticket.get("attention"):
                pending.append({"seq": ticket.get("no"), "title": ticket["attention"]["question"], "status": "sent",
                                "created_at": ticket["attention"].get("at")})
        runs = [r for r in self.store.runs(limit=2000, since=since_iso) if (r["finished_at"] or "") != ""]
        usage: dict[str, dict] = {}
        for run in runs:
            entry = usage.setdefault(run["agent"], {"runs": 0, "ok": 0})
            entry["runs"] += 1
            entry["ok"] += run["status"] == "ok"
        record_only, would_accept, counts = [], [], {}
        for decision in self.store.decisions.recent(1000, kind="ignore", since=since_iso):
            codes = {r["code"] for r in decision["reason"]}
            trigger = self.store.triggers.get(decision["triggerIds"][-1]) or {}
            type_ = (trigger.get("meta") or {}).get("type")
            if "recordOnly" in codes and type_:
                counts[type_] = counts.get(type_, 0) + 1
                if type_ != "social.metrics.snapshot" and len(record_only) < 40:
                    record_only.append({"type": type_, "subject": (trigger.get("meta") or {}).get("subject"), "summary": trigger.get("text"),
                                        "received_at": trigger.get("at")})
            if "observed" in codes:
                would_accept.append({"at": decision["at"], "reason": reason_text(decision["reason"]), "thread": decision["threadKey"]})
        feedback = sum(1 for t in self.store.recent_triggers(2000, since=since_iso) if (t.get("meta") or {}).get("type") == "popto.feedback.new")
        return {"since": since_iso, "until": until_iso, "tasks_by_rule": by_rule,
                "fallback_runs": [r for r in runs if r["is_fallback"] and r["status"] == "ok"],
                "failed_runs": [r for r in runs if r["status"] not in ("ok", "cancelled")], "agent_usage": usage,
                "record_only_events": [{"type": t, "n": n} for t, n in sorted(counts.items())], "record_only_items": record_only,
                "cards": cards, "pending_cards": pending, "agents": self.agents.states(), "unread_feedback": feedback,
                "would_accept": would_accept[:40], "mode": settings["eval"]["mode"], "problems": self.health.problems()}
