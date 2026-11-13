"""管理 API：面板、命令行和确定性脚本共用的 HTTP 入口。只监听本机（或配置的内网地址）。

    /                    面板（单页）
    /api/...             面板和命令行：视图（GET）和人的操作（POST）
    /v1/...              脚本契约：发事件、发通知、计数器、摘要、暂停（和 ops 的中台接口一致）

每种失败一个状态码：对象不存在 404，被规则拒绝 400（带错误码，面板按码给文案），没带令牌 401，跨站 403，内部错误 500。
写操作只接受同源或带令牌的请求：浏览器里别的网页不能替你点“停止”。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..core.errors import UserError
from . import views

log = logging.getLogger("superops.http")
MAX_BODY = 2 * 1024 * 1024
PANEL = Path(__file__).resolve().parent.parent / "panel" / "index.html"


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


def _ticket(rt, ref: str) -> dict:
    ticket = rt.find_ticket(unquote(ref))
    if not ticket:
        raise ApiError(404, f"找不到工单 {ref}", "unknownTicket")
    return ticket


# 面板“设计”页能看的文档：docs/ 下全部、仓库根目录这几篇、prompts/ 下全部提示词、skills/ 下全部 Skill 文档。
# 只按名字取（名字由文件名定），不接受路径。
PANEL_DOC_NAMES = {"整体设计.md": ("design", "整体设计"), "整体流程.md": ("flow", "整体流程")}     # 置顶、名字沿用老链接
PANEL_ROOT_DOCS = [("readme", "README.md", "仓库说明"), ("agents", "AGENTS.md", "Agent 总规则"), ("claude", "CLAUDE.md", "Claude 专属说明"),
                   ("decisions", "decisions.md", "定的事"), ("not-doing", "not-doing.md", "定了不做的事")]
PROMPT_TITLES = {"shared": "公共背景", "orchestrator": "主 Session", "worker": "执行器", "subscriptions": "订阅", "replies": "回复",
                 "dispatch": "路由", "admission": "研判主动候选", "work": "怎么工作", "handoff-worker": "需要人决定时",
                 "auto-reply": "自动回复", "team/communication": "团队 · 沟通", "team/scope": "团队 · 工单范围", "team/delivery": "团队 · 交付要求"}


def _doc_title(p: Path, fallback: str) -> str:
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return fallback


def _skill_title(p: Path, fallback: str) -> str:
    """SKILL.md 开头 description 的第一句（去掉系统名前缀），比如“每日运营日报”。"""
    try:
        m = re.search(r"^description:\s*(.+)$", p.read_text(encoding="utf-8")[:2000], re.M)
    except OSError:
        m = None
    if not m:
        return fallback
    text = re.sub(r"^.*?·\s*", "", m.group(1).strip()) if "·" in m.group(1) else m.group(1).strip()
    return re.split(r"[。；.]", text, maxsplit=1)[0][:24] or fallback


def panel_docs(home) -> dict[str, tuple[str, str, str]]:
    """名字 → (标题, 仓库里的相对路径, 分组)，按面板上的顺序。"""
    root = Path(home)
    out: dict[str, tuple[str, str, str]] = {}
    files = sorted((root / "docs").glob("*.md")) if (root / "docs").is_dir() else []
    pinned = [p for k in PANEL_DOC_NAMES for p in files if p.name == k]
    for p in pinned + [p for p in files if p.name not in PANEL_DOC_NAMES]:
        if p.name in PANEL_DOC_NAMES:
            name, title = PANEL_DOC_NAMES[p.name]
        else:
            m = re.fullmatch(r"\d{3}-(.+)\.md", p.name)
            name, title = (m.group(1) if m else p.stem), _doc_title(p, p.stem)
        out.setdefault(name, (title, f"docs/{p.name}", "设计文档"))
    for name, rel, title in PANEL_ROOT_DOCS:
        if (root / rel).is_file():
            out.setdefault(name, (title, rel, "仓库规则"))
    prompts = root / "prompts"
    for p in sorted(prompts.rglob("*.md")) if prompts.is_dir() else []:
        key = p.relative_to(prompts).with_suffix("").as_posix()
        out.setdefault("prompt-" + key.replace("/", "-"), (PROMPT_TITLES.get(key) or _doc_title(p, key), p.relative_to(root).as_posix(), "提示词"))
    skills = root / "skills"
    for d in sorted(x for x in skills.iterdir() if x.is_dir()) if skills.is_dir() else []:
        main = d / "SKILL.md"
        if main.is_file():
            out.setdefault("skill-" + d.name, (_skill_title(main, d.name), main.relative_to(root).as_posix(), "Skill"))
        for p in sorted(d.rglob("*.md")):
            if p != main:
                out.setdefault(f"skill-{d.name}-{p.relative_to(d).with_suffix('').as_posix().replace('/', '-')}",
                               (f"{_skill_title(main, d.name)} · {_doc_title(p, p.stem)}", p.relative_to(root).as_posix(), "Skill"))
    return out


def panel_doc(rt, name: str) -> dict:
    docs = panel_docs(rt.home)
    if name not in docs:
        raise ApiError(404, "没有这篇文档")
    title, rel, group = docs[name]
    p = Path(rt.home) / rel
    from datetime import datetime
    return {"name": name, "title": title, "group": group, "path": rel, "markdown": p.read_text(encoding="utf-8"),
            "updated_at": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "docs": [{"name": k, "title": v[0], "path": v[1], "group": v[2]} for k, v in docs.items()]}


# ── 发布台（面板“内容 → 发布中心 / 账号”）──
PUBLISH_OPEN = ("ready", "carded", "approved", "drafted", "failed")    # 还没发完的发布包


def _desk(rt):
    desk = getattr(rt, "_publish_desk", None)
    if desk is None:
        from ..biz.config import load_agents_config
        from ..core.model import now_iso
        from .publishing import PublishDesk
        desk = rt._publish_desk = PublishDesk(load_agents_config, now_iso)
    return desk


def _publish_call(fn, *args, **kw):
    from .publishing import PublishError
    try:
        return fn(*args, **kw)
    except PublishError as e:
        raise ApiError(e.status, str(e)) from e


def publish_overview(rt) -> dict:
    """账号、正在跑的浏览器操作、还没发完的发布包（带预览用的文案和素材清单）、最近发出去的 20 个。"""
    from ..biz import db
    desk = _desk(rt)
    ops = db.open_ops()
    try:
        marks = ",".join("?" * len(PUBLISH_OPEN))
        open_ids = [r[0] for r in ops.execute(f"SELECT id FROM publish_packs WHERE status IN ({marks}) ORDER BY scheduled_at, content_id, platform",
                                              PUBLISH_OPEN)]
        done_ids = [r[0] for r in ops.execute("SELECT id FROM publish_packs WHERE status='published' ORDER BY updated_at DESC LIMIT 20")]
        posts = {r["pack"]: dict(r) for r in db.rows(ops.execute(
            "SELECT p.id post_id, p.url, p.published_at, p.title, p.publish_mode, pp.id pack FROM posts p JOIN publish_packs pp"
            " ON pp.content_id=p.content_id AND pp.platform=p.platform WHERE pp.status='published'"))} if done_ids else {}
    finally:
        ops.close()
    packs = []
    for pid in open_ids + done_ids:
        try:
            packs.append({**desk.pack(pid), "post": posts.get(pid)})
        except Exception as e:  # noqa: BLE001  一个发布包读不出来不影响别的
            packs.append({"id": pid, "error": str(e)[:200]})
    return {"accounts": desk.accounts(), "jobs": desk.job_list(), "packs": packs}



DEMAND_OPEN = ("open", "watching", "shelved")     # 还没批准的需求：可以批准、观察、搁置、恢复


def demands_action(rt, body: dict) -> dict:
    """需求池上人的决定。approve：等于在周报卡片上选了“按建议做”——发出带人工批准凭据的 demand.approved，
    开一张“需求 → PRD → 开发”工单（先写 PRD 请人批，批了才开发）；watch / shelve / reopen：只改需求池里的状态；
    add：提一条新需求（同 superops demand "……"），由需求挖掘归并或新建。"""
    from ..biz import db as bizdb
    from ..core.model import now_iso
    action, by = str(body.get("action") or ""), str(body.get("by") or "panel")
    if action == "add":
        text = str(body.get("text") or "").strip()
        if not text:
            raise ApiError(400, "要写需求内容")
        return rt.pipeline.emit({"source": "panel", "type": "demand.manual", "subject": "demand:manual", "summary": text[:300],
                                 "data": {"text": text[:4000], "by": by}})
    did = str(body.get("id") or "")
    ops = bizdb.open_ops()
    try:
        row = ops.execute("SELECT id, title, COALESCE(status, 'open') AS status FROM demands WHERE id=?", (did,)).fetchone()
        if not row:
            raise ApiError(404, f"没有需求 {did}")
        title, status, at = row["title"], row["status"], now_iso()
        if action in ("approve", "watch", "shelve", "reopen") and status not in DEMAND_OPEN:
            raise ApiError(409, f"这条需求现在是「{status}」，已经批准过了")
        if action == "approve":
            if body.get("confirm") is not True:
                raise ApiError(400, "批准开发要在面板上确认一次（confirm: true）")
            approved = {"by": by, "at": at, "option": "approve", "label": "面板批准开发", "via": "panel"}
            out = rt.pipeline.emit({"source": "panel", "type": "demand.approved", "subject": f"demand:{did}", "summary": f"面板批准开发：{title}"[:300],
                                    "data": {"demand_id": did, "title": title, "by": by, "via": "panel"},
                                    "dedup_key": f"panel-demand:{did}:{at}"}, approved=approved)
            ops.execute("UPDATE demands SET status='approved', decided_ref=?, updated_at=? WHERE id=?", (f"panel:{by}:{at}", at, did))
            ops.commit()
            return {"ok": True, "id": did, "status": "approved", "routed": out}
        new = {"watch": "watching", "shelve": "shelved", "reopen": "open"}.get(action)
        if not new:
            raise ApiError(400, f"不认识的动作 {action}")
        reason = str(body.get("reason") or "").strip()
        if action == "shelve" and not reason:
            raise ApiError(400, "搁置要写一句原因")
        ref = f"panel:{by}:{at}" + (f"：{reason[:300]}" if reason else "")
        ops.execute("UPDATE demands SET status=?, decided_ref=?, updated_at=? WHERE id=?", (new, ref, at, did))
        ops.commit()
        return {"ok": True, "id": did, "status": new}
    finally:
        ops.close()

def publish_packs_action(rt, body: dict) -> dict:
    """approve：等于在群里批了发布卡片——发出带人工批准凭据的 content.publish.approved，按各平台的发布模式执行；
    record：人在平台上发完了，补登记（可带作品链接）；try / retry：交给发布台的子进程。"""
    from ..core.model import now_iso
    desk, action, by = _desk(rt), str(body.get("action") or ""), str(body.get("by") or "panel")
    if action in ("try", "retry"):
        return _publish_call(desk.start, action, str(body.get("id") or ""), by=by)
    if action == "record":
        pid = str(body.get("id") or "")
        p = _publish_call(desk.pack_row, pid)
        url = str(body.get("url") or "").strip()
        if url and not re.match(r"https?://", url):
            raise ApiError(400, "作品链接要以 http:// 或 https:// 开头")
        if p["status"] == "published":
            raise ApiError(409, "这个发布包已经登记为已发布")
        return rt.pipeline.emit({"source": "panel", "type": "content.published.manual", "subject": f"pack:{pid}", "summary": f"已发 {pid}（面板）",
                                 "data": {"pack_id": pid, "by": by, **({"url": url} if url else {})}})
    if action == "approve":
        ids = [str(x) for x in (body.get("ids") or []) if str(x).strip()]
        if not ids:
            raise ApiError(400, "没有选发布包")
        if body.get("confirm") is not True:
            raise ApiError(400, "批准发布要在面板上确认一次（confirm: true）")
        at, out = now_iso(), []
        for pid in ids:
            p = _publish_call(desk.pack_row, pid)
            if p["status"] not in ("ready", "carded", "failed"):
                raise ApiError(409, f"{pid} 现在是「{p['status']}」，不能再批准发布")
        approved = {"by": by, "at": at, "option": "approve", "label": "面板批准发布", "via": "panel"}
        for pid in ids:
            out.append(rt.pipeline.emit({"source": "panel", "type": "content.publish.approved", "subject": f"pack:{pid}",
                                         "summary": f"面板批准发布 {pid}", "data": {"pack_id": pid, "by": by, "via": "panel"},
                                         "dedup_key": f"panel-approve:{pid}:{at}"}, approved=approved))
        return {"ok": True, "approved": ids, "routed": out}
    raise ApiError(400, f"不认识的动作 {action}")


def publish_accounts_action(rt, body: dict) -> dict:
    """check / login / open：交给发布台的子进程；logout：登录目录挪到回收处；rename：记一下这个平台登的是哪个号。"""
    desk, action, pf = _desk(rt), str(body.get("action") or ""), str(body.get("platform") or "")
    if action == "logout":
        return _publish_call(desk.logout, pf)
    if action == "rename":
        from .publishing import _write_account
        if pf not in {a["platform"] for a in desk.accounts()}:
            raise ApiError(400, f"不认识的平台 {pf}")
        _write_account(pf, nickname=str(body.get("name") or "").strip()[:40] or None)
        return {"ok": True}
    return _publish_call(desk.start, action, pf, by=str(body.get("by") or "panel"))


def handle_get(rt, path: str, q: dict) -> dict | list:
    if path in ("/api/health", "/v1/health"):
        return {"ok": True, "problems": len(rt.health.problems())}
    if path == "/api/overview":
        return views.overview(rt)
    if path == "/api/tickets":
        return views.tickets(rt, q.get("filter", "open"), int(q.get("limit", 100)))
    m = re.fullmatch(r"/api/tickets/([^/]+)", path)
    if m:
        return views.ticket_page(rt, _ticket(rt, m.group(1)))
    if path == "/api/threads":
        return views.threads(rt, q.get("filter", "recent"), int(q.get("limit", 60)))
    if path == "/api/thread":
        page = views.thread_page(rt, q.get("key", ""))
        if not page:
            raise ApiError(404, "没有这个线程")
        return page
    if path == "/api/triggers":
        human = {"1": True, "0": False}.get(str(q.get("human") or ""))     # human=1 只要人发的（面板“一条指令的旅程”用）
        return [views.trigger_row(rt, t) for t in rt.store.recent_triggers(int(q.get("limit", 60)), source=q.get("source"), human=human)]
    if path == "/api/decisions":
        return views.decisions(rt, q.get("kind") or None, int(q.get("limit", 80)))
    if path == "/api/sources":
        return views.sources(rt)
    if path == "/api/settings":
        return views.settings_view(rt)
    if path == "/api/search":
        return views.search(rt, q.get("q", ""))
    if path == "/api/autoreply":
        return rt.autoreply.view()
    if path == "/api/chat":
        from . import chat
        return chat.view(rt)
    if path == "/api/docs":
        return panel_doc(rt, q.get("name") or "design")
    if path == "/api/skills":   # Skill 列表；?name= 单个的详情；?name=&version= 某个历史版本
        from . import skills
        if q.get("name") and q.get("version"):
            return skills.version(rt, q["name"], q["version"])
        return skills.detail(rt, q["name"]) if q.get("name") else skills.catalog(rt)
    if path == "/api/preflight":
        result = rt.preflight(refresh=q.get("refresh") in ("1", "true"))
        if q.get("refresh") in ("1", "true"):
            rt.announce_preflight(result)
        return result
    if path == "/api/health/all":
        return rt.health.all()
    if path == "/api/messages":
        return rt.store.receipts.list(status=q.get("status") or None, limit=int(q.get("limit", 60)))
    if path == "/api/publish":
        return publish_overview(rt)
    if path == "/api/publish/pack":
        return _publish_call(_desk(rt).pack, str(q.get("id") or ""))
    if path == "/api/business":
        from ..biz import bizviews
        return bizviews.business(int(q.get("days", 7)))
    if path == "/v1/digest":
        return rt.digest(q.get("since"), q.get("until"))
    if path == "/v1/counter":
        return {"name": q.get("name"), "value": rt.counter(q.get("name", ""))}
    raise ApiError(404, "没有这个接口")


def handle_post(rt, path: str, body: dict, trusted: bool = False) -> dict | list:
    """trusted = 请求带着宿主交给确定性脚本的令牌。只有这样发来的事件才能越过“需要人批准”的守门。"""
    if path == "/api/demands":   # 需求池：{action: approve|watch|shelve|reopen, id, confirm?, reason?} 或 {action: add, text}
        return demands_action(rt, body)
    if path == "/api/publish/packs":
        return publish_packs_action(rt, body)
    if path == "/api/publish/accounts":
        return publish_accounts_action(rt, body)
    m = re.fullmatch(r"/api/tickets/([^/]+)/(stop|retry|answer|reply|status|note)", path)
    if m:
        ticket, action = _ticket(rt, m.group(1)), m.group(2)
        if action == "stop":      # 面板里的停止是命令：直接作用在工单上，记为人工停止
            rt.engine.stop_ticket(ticket["id"], body.get("reason") or None)
        elif action == "retry":
            rt.engine.retry_ticket(ticket["id"])
        elif action == "status":
            return {"asked": rt.engine.request_status(ticket["id"])}
        elif action == "note":
            rt.engine.note(ticket["id"], str(body.get("text") or ""))
        elif action == "answer":
            result = rt.answer_by_code(f"T{ticket.get('no')}", body.get("choice"), by=body.get("by") or "operator", reason=body.get("reason") or None)
            if not result.get("ok"):
                raise ApiError(400, result.get("error") or "没批成", result.get("code"))
            return result
        else:                     # 操作者对这张工单说一句话：落在它自己的线程上，由它的执行器决定
            thread = rt.ticket_thread(ticket)
            from ..core.model import now_iso
            from ..core.reasons import key
            at = now_iso()
            trigger = {"id": key("cli-reply", ticket["id"], at, str(body.get("text"))), "source": "cli", "threads": [thread],
                       "actor": {"source": "cli", "id": body.get("by") or "operator"}, "human": True, "addressed": "direct", "at": at,
                       "text": str(body.get("text") or ""), "channel": {"source": "cli", "thread": f"t{ticket.get('no')}", "address": {}},
                       "meta": {"row": "terminal"}}
            return {"result": rt.receive(trigger), "id": trigger["id"]}
        rt.kick()
        return views.ticket_row(rt, rt.store.tickets.get(ticket["id"]))
    m = re.fullmatch(r"/api/triggers/([^/]+)/reevaluate", path)
    if m:
        queued = rt.engine.reevaluate(m.group(1))
        rt.kick()
        return {"queued": queued}
    m = re.fullmatch(r"/api/decisions/([^/]+)/dismiss", path)
    if m:
        return views.decision_row(rt, rt.engine.dismiss(m.group(1)))
    if path == "/api/thread/unsubscribe":
        return {"dropped": rt.engine.unsubscribe(str(body.get("key") or ""))}
    if path == "/api/ask":
        result = rt.cli.ask(str(body.get("text") or ""), thread=body.get("thread") or None, actor=body.get("by") or "operator")
        return result
    if path == "/api/autoreply":
        return rt.autoreply.set_mode(str(body.get("mode")), by=str(body.get("origin") or "panel"))
    if path == "/api/autoreply/exclude":
        if body.get("action") == "remove":
            return rt.autoreply.remove_exclude(str(body.get("group") or ""))
        return rt.autoreply.add_exclude(str(body.get("group") or ""))
    if path == "/api/mode":
        return rt.settings.set_mode(str(body.get("mode")), origin=body.get("origin") or "panel")
    if path in ("/api/pause", "/v1/pause", "/v1/resume"):
        paused = path != "/v1/resume" and bool(body.get("paused", True))
        key_ = body.get("key") or None
        result = rt.settings.pause(paused, None if key_ in (None, "all") else str(key_), reason=str(body.get("reason") or ""), origin="api")
        rt.kick()
        return result
    if path == "/api/reload":
        rt.schedule.reload()
        return rt.settings.reload(origin=body.get("origin") or "panel")
    if path == "/api/fire":
        result = rt.schedule.fire(str(body["type"]), data=body.get("data"))
        return result
    if path == "/api/skills":   # {action: save|restore, name, markdown?, base_updated_at?, version?}
        from . import skills
        return skills.act(rt, body)
    if path == "/api/topics/plan":   # 手动排选题：{range: next_week|next_7_days, note?}；开一张“下周内容日历”工单
        rng = str(body.get("range") or "next_week")
        if rng not in ("next_week", "next_7_days"):
            raise UserError("invalidSettings", {"error": f"排期范围只能是 next_week / next_7_days：{rng}"})
        note = str(body.get("note") or "").strip()[:500]
        return rt.schedule.fire("cron.topic_planning", data={"manual": True, "by": body.get("origin") or "panel", "range": rng,
                                                              **({"note": note} if note else {})})
    if path == "/api/chat":   # 聊天渠道：{action: switch|set_group|add_approver|remove_approver, app, group?, id?, name?}
        from . import chat
        return chat.act(rt, body)
    if path == "/api/terms":   # 监听意图词：{action: add|enable|disable|remove, term, category?}
        from ..biz import db as bizdb, ingest
        ops = bizdb.open_ops()
        try:
            ingest.sync_terms(ops)    # 先把 keywords.yaml 的初始词落库，免得删掉的词下次又被同步回来
            return ingest.edit_term(ops, str(body.get("action") or ""), str(body.get("term") or ""), body.get("category") or None)
        finally:
            ops.close()
    if path == "/api/metrics/collect":   # 手动采集：开一张“手动采集指标”工单；post_ids 为空就采最近 30 天全部
        ids = [str(x) for x in (body.get("post_ids") or []) if str(x).strip()]
        return rt.schedule.fire("cron.collect_metrics_now", data={"post_ids": ids, "by": body.get("origin") or "panel"})
    if path == "/api/agents":
        name = str(body.get("agent"))
        if body.get("action") == "reset":
            rt.store.set(f"agent:{name}", {"state": "closed"})
        elif body.get("action") == "usage":
            rt.agents.set_usage(name, float(body["ratio"]))
        elif body.get("action") == "open":
            rt.agents.open(name, "network", "手动熔断", cooldown=body.get("for") or "30m")
        return rt.agents.states()
    if path == "/api/pass":
        rt.run_pass()
        rt.kick()
        return {"ok": True}
    if path == "/v1/events":
        return rt.pipeline.emit(body, trusted=trusted)
    if path == "/v1/notify":
        return rt.notify(body.get("kind", "message"), body.get("title", ""), body.get("body", ""), priority=int(body.get("priority", 2)),
                         ref=body.get("ref"), dedup=bool(body.get("dedup")))
    if path == "/v1/counter":
        rt.add_counter(str(body["name"]), float(body["value"]))
        return {"ok": True, "value": rt.counter(str(body["name"]))}
    raise ApiError(404, "没有这个接口")


def make_handler(rt, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, write: bool) -> None:
            sent = self.headers.get("X-Ops-Token") or (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            self.trusted = bool(sent) and sent == rt.script_token
            if token and sent != token and not self.trusted:
                # 没带令牌：只放行本机来的请求
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    raise ApiError(401, "需要令牌")
            if write:
                origin = self.headers.get("Origin")
                host = self.headers.get("Host") or ""
                if origin and urlparse(origin).netloc != host:
                    raise ApiError(403, "不接受跨站请求")

        def _run(self, write: bool) -> None:
            url = urlparse(self.path)
            try:
                self._authorized(write)
                if not write and url.path in ("/", "/index.html"):
                    self._send(200, PANEL.read_bytes() if PANEL.exists() else "面板文件不存在".encode(), "text/html; charset=utf-8")
                    return
                if not write and url.path == "/api/publish/media":     # 发布包里的封面 / 视频 / 截图：面板预览和下载
                    from .publishing import content_type
                    q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    f = _publish_call(_desk(rt).media_path, str(q.get("pack") or ""), str(q.get("name") or ""), q.get("shot") == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type(f))
                    self.send_header("Content-Length", str(f.stat().st_size))
                    self.send_header("Cache-Control", "private, max-age=300")
                    if q.get("download") == "1":
                        from urllib.parse import quote
                        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(f.name)}")
                    self.end_headers()
                    with f.open("rb") as fh:
                        import shutil
                        shutil.copyfileobj(fh, self.wfile, 1 << 20)
                    return
                if write:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "请求体太大")
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw.decode() or "{}")
                    except (ValueError, UnicodeDecodeError) as error:
                        raise ApiError(400, f"请求体不是合法的 JSON：{error}") from error
                    if not isinstance(body, dict):
                        raise ApiError(400, "请求体必须是 JSON 对象")
                    result = handle_post(rt, url.path, body, self.trusted)
                else:
                    result = handle_get(rt, url.path, {k: v[-1] for k, v in parse_qs(url.query).items()})
                self._send(200, result)
            except ApiError as error:
                self._send(error.status, {"error": str(error), "code": error.code})
            except UserError as error:
                self._send(404 if error.missing else 400, {"error": str(error), "code": error.code})
            except (KeyError, ValueError, TypeError) as error:
                self._send(400, {"error": f"请求不合法：{error}"})
            except Exception as error:  # noqa: BLE001
                log.exception("接口 %s 出错", url.path)
                self._send(500, {"error": str(error)[:300]})

        def do_GET(self):  # noqa: N802
            self._run(False)

        def do_POST(self):  # noqa: N802
            self._run(True)

    return Handler


def serve(rt, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    hub = rt.cfg.get("hub") or {}
    listen = int(hub.get("port", 8797) if port is None else port)   # 0 = 让系统挑一个空闲端口（测试用）
    server = ThreadingHTTPServer((host or hub.get("host", "127.0.0.1"), listen), make_handler(rt, hub.get("token")))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server
