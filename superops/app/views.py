"""面板和命令行读的视图：只从 Store 读，从不写。

面板回答三个问题，按这个顺序：现在有没有事要我处理？系统健康吗、做的事对吗？某一件工作从触发到评估到工单的来龙去脉是什么？
所以总览先列等人的工单，再列问题；健康的状态不占地方。每个数都等于它链接到的列表的长度。
"""
from __future__ import annotations

from ..core.model import latest_report, live_ticket, now_iso, shift, thread_key
from ..core.reasons import reason_text
from ..core.stage import can_retry, ticket_stage
from ..infra.instance import current as current_instance
from .settings import policy_version, rule_of

STAGE_LABELS = {
    ("person", "delivery"): "输入没送到", ("person", "answer"): "等你回答", ("person", "review"): "等你验收",
    ("person", "blocked"): "受阻", ("person", "failed"): "失败", ("working", "queued"): "排队中", ("working", None): "进行中",
    ("idle", None): "等后续事件",
}
OUTCOME_LABELS = {"completed": "已完成", "merged": "已合并", "closed": "已关闭", "dropped": "无需处理", "stopped": "被人停止"}
DECISION_LABELS = {"accept": "接单", "stop": "停止", "ignore": "忽略", "unavailable": "未完成", "reopen": "重开"}
FILTERS = ("person", "open", "queued", "working", "idle", "ended", "all")


def _rule(rt, ticket: dict) -> dict | None:
    rule_id = ((ticket["mandate"].get("basis") or {}).get("rules") or [None])[0]
    return rule_of(rt.settings.current(), rule_id) if rule_id else None


def ticket_row(rt, ticket: dict) -> dict:
    stage = ticket_stage(ticket)
    rule = _rule(rt, ticket)
    report = latest_report(ticket)
    ended = ticket.get("ended")
    label = OUTCOME_LABELS.get(ended["outcome"], ended["outcome"]) if ended else STAGE_LABELS.get((stage["stage"], stage.get("action")), stage["stage"])
    return {"id": ticket["id"], "no": ticket.get("no"), "code": f"T{ticket.get('no')}", "title": ticket["title"], "stage": stage["stage"],
            "action": stage.get("action"), "label": label, "rule": (rule or {}).get("name"), "rule_id": (rule or {}).get("id"),
            "strength": ticket["mandate"]["strength"], "created_at": ticket["createdAt"], "updated_at": ticket["updatedAt"],
            "question": (ticket.get("attention") or {}).get("question"),
            "options": [{"key": o["key"], "label": o["label"], "require_reason": bool(o.get("require_reason"))}
                        for o in ((ticket.get("attention") or {}).get("card") or {}).get("options") or []],
            "expires_at": (ticket.get("attention") or {}).get("expiresAt"),
            "report": {"state": report["state"], "summary": report["summary"], "at": report["at"]} if report else None,
            "outcome": (ended or {}).get("outcome"), "ended_reason": (ended or {}).get("reason"), "ended_at": (ended or {}).get("at"),
            "can_retry": can_retry(ticket),
            "delivery_error": (ticket.get("delivery") or {}).get("error"), "executor": ((rule or {}).get("run") or {}).get("executor", "agent")}


def tickets(rt, filter_: str = "open", limit: int = 100) -> list[dict]:
    if filter_ == "ended":
        rows = [ticket_row(rt, t) for t in rt.store.tickets.recent(limit, open_=False)]
    elif filter_ == "all":
        rows = [ticket_row(rt, t) for t in rt.store.tickets.recent(limit)]
    else:
        rows = [ticket_row(rt, t) for t in sorted(rt.store.tickets.open(), key=lambda t: t["updatedAt"], reverse=True)]
        if filter_ == "person":
            rows = [r for r in rows if r["stage"] == "person"]
        elif filter_ == "queued":
            rows = [r for r in rows if r["action"] == "queued"]
        elif filter_ == "working":
            rows = [r for r in rows if r["stage"] == "working" and r["action"] != "queued"]
        elif filter_ == "idle":
            rows = [r for r in rows if r["stage"] == "idle"]
    return rows[:limit]


def trigger_row(rt, trigger: dict) -> dict:
    disposition = rt.store.triggers.disposition(trigger["id"]) or {}
    meta = trigger.get("meta") or {}
    decision = rt.store.decisions.get(disposition["decisionId"]) if disposition.get("decisionId") else None
    fanout = [t for t in (rt.store.tickets.get(i) for i in disposition.get("fanout") or []) if t]
    opened = rt.store.tickets.get(decision["ticketId"]) if decision and decision.get("ticketId") else None
    if opened and all(t["id"] != opened["id"] for t in fanout):
        fanout = [opened, *fanout]
    if disposition.get("filtered"):
        went = "被来源过滤：" + reason_text(disposition["filtered"])
    elif decision:
        went = f"{DECISION_LABELS.get(decision['kind'], decision['kind'])}：{reason_text(decision['reason'])}"
    elif fanout:
        went = "已送达工单 " + "、".join(f"T{t.get('no')}" for t in fanout)
    elif disposition.get("evalId"):
        went = "评估中"
    else:
        went = "排队等评估"
    return {"id": trigger["id"], "source": trigger["source"], "at": trigger["at"], "human": trigger["human"], "actor": trigger["actor"].get("id"),
            "addressed": trigger.get("addressed"), "type": meta.get("type"), "origin": meta.get("origin"), "subject": meta.get("subject"),
            "text": (trigger.get("text") or "")[:400], "threads": [thread_key(r) for r in trigger["threads"]], "went": went,
            "decision_id": (decision or {}).get("id"), "decision_kind": (decision or {}).get("kind"),
            "tickets": [{"id": t["id"], "code": f"T{t.get('no')}"} for t in fanout], "filtered": bool(disposition.get("filtered")),
            "approved": meta.get("approved"), "url": trigger.get("url")}


def decision_row(rt, decision: dict) -> dict:
    ticket = rt.store.tickets.get(decision["ticketId"]) if decision.get("ticketId") else None
    audit = decision.get("audit") or {}
    return {"id": decision["id"], "at": decision["at"], "kind": decision["kind"], "label": DECISION_LABELS.get(decision["kind"], decision["kind"]),
            "strength": decision["strength"], "thread": decision["threadKey"], "reason": reason_text(decision["reason"]),
            "codes": [r["code"] for r in decision["reason"]], "rules": [m["rule_id"] for m in audit.get("matches") or []],
            "would_accept": bool(audit.get("would_accept")), "direct": bool(audit.get("direct")), "dismissed": bool(decision.get("dismissed")),
            "ticket": {"id": ticket["id"], "code": f"T{ticket.get('no')}", "title": ticket["title"]} if ticket else None,
            "trigger_ids": decision["triggerIds"]}


def problems(rt) -> list[dict]:
    """需要人知道的问题：部件报错、最近没完成的研判、发不出去的消息。偶发的一次网络错误不算。"""
    out = [{"kind": "health", "title": e["name"], "detail": e["detail"], "at": e["at"]} for e in rt.health.problems()]
    checked = rt.store.get("preflight") or {}
    for item in checked.get("missing") or []:   # 要提前登录 / 放密钥的：没准备好就一直列着，直到复查通过
        out.append({"kind": "setup", "title": f"没准备好：{item['title']}", "at": checked.get("at"),
                    "detail": f"{item['detail']}。影响：{'、'.join(item['rules'])}。补：{item['fix']}", "fix": item["fix"]})
    since = shift(now_iso(), -86400)
    for decision in rt.store.decisions.recent(50, kind="unavailable", since=since):
        if decision.get("dismissed"):
            continue
        thread = rt.store.threads.get(decision["threadKey"]) or {}
        latest = rt.store.decisions.by_thread(decision["threadKey"])
        if thread.get("pending") or thread.get("evaluating") or (latest and latest[-1]["id"] != decision["id"]):
            continue
        out.append({"kind": "eval", "title": "研判没有完成", "detail": reason_text(decision["reason"]), "at": decision["at"],
                    "decision_id": decision["id"], "trigger_id": decision["triggerIds"][-1], "thread": decision["threadKey"]})
    for kind, n, attempts, error in rt.store.all("SELECT kind, COUNT(*), MAX(attempts), MAX(error) FROM effect WHERE attempts >= 3 GROUP BY kind"):
        out.append({"kind": "effect", "title": f"{kind} 连续失败", "detail": f"{n} 项，最多已试 {attempts} 次：{error}", "at": now_iso()})
    return out


def agent_roster(rt, settings: dict) -> dict:
    """运转图的 Agent 卡片：运行状态 + agents.yaml 的称号和职责 + 主跑哪些规则（run.agent 写的是它；确定性脚本归 script）
    + 在哪些角色的降级链里排第二位往后（兜底）。"""
    states = rt.agents.states()
    for name, a in states.items():
        a["rules"], a["backup"] = [], []
    for r in settings["policy"]["rules"]:
        run = r.get("run") or {}
        who = "script" if run.get("executor") == "script" else run.get("agent") if run.get("executor", "agent") == "agent" else None
        if who in states:
            states[who]["rules"].append({"id": r["id"], "name": r["name"], "enabled": r["enabled"]})
    for role, chain in (rt.cfg.get("fallback_chains") or {}).items():
        for name in (chain or [])[1:]:
            if name in states and role not in states[name]["backup"]:
                states[name]["backup"].append(role)
    return states


def credit_counters(rt) -> dict:
    """今日积分：计数器名和预算键都是 agents.yaml 的 instance.credits.counter。"""
    inst = current_instance()
    return {"name": inst.credits_counter, "label": inst.credits_label, "used": rt.counter(inst.credits_counter),
            "budget": (rt.cfg.get("budgets") or {}).get(inst.credits_counter)}


def overview(rt) -> dict:
    since = shift(now_iso(), -86400)
    settings = rt.settings.current()
    person = tickets(rt, "person")
    decisions = rt.store.decisions.recent(2000, since=since)
    by_kind: dict[str, int] = {}
    for decision in decisions:
        by_kind[decision["kind"]] = by_kind.get(decision["kind"], 0) + 1
    recent = rt.store.tickets.recent(2000, since=since)
    open_rows = tickets(rt, "open", 1000)
    return {
        "needs_person": person, "problems": problems(rt),
        "flow": {"triggers": rt.store.trigger_stats(since), "decisions": by_kind,
                 "would_accept": sum(1 for d in decisions if (d.get("audit") or {}).get("would_accept")),
                 "tickets_created": sum(1 for t in recent if t["createdAt"] >= since),
                 "tickets_ended": sum(1 for t in recent if t.get("ended") and t["ended"]["at"] >= since)},
        "open": {"total": len(open_rows), "person": len(person), "working": sum(1 for r in open_rows if r["stage"] == "working"),
                 "idle": sum(1 for r in open_rows if r["stage"] == "idle")},
        "executor": rt.executor.load(), "agents": agent_roster(rt, settings),
        "mode": settings["eval"]["mode"], "paused": settings["paused"], "paused_keys": settings["paused_keys"],
        "autoreply": rt.autoreply.view() if getattr(rt, "autoreply", None) else None,
        "policy_version": policy_version(settings)[:12], "counters": credit_counters(rt),
        "at": now_iso()}


def ticket_page(rt, ticket: dict) -> dict:
    rule = _rule(rt, ticket)
    decision = rt.store.decisions.get(ticket["decisionId"])
    origin = rt.store.triggers.get(ticket["origin"]["triggerId"])
    inputs = []
    for item in ticket["inputs"][-60:]:
        row = {"id": item["id"], "kind": item["kind"], "at": item["at"], "waiting": item["id"] in ticket["undelivered"],
               "human": bool(item.get("human")), "context": bool(item.get("context"))}
        if item["kind"] == "trigger":
            trigger = rt.store.triggers.get(item["triggerId"])
            row.update({"trigger_id": item["triggerId"], "text": ((trigger or {}).get("text") or "")[:400], "note": item.get("note"),
                        "type": ((trigger or {}).get("meta") or {}).get("type"), "actor": ((trigger or {}).get("actor") or {}).get("id")})
        elif item["kind"] == "answer":
            row["text"] = f"{item['by']} 选了「{item.get('label') or item['option']}」" + (f"：{item['reason']}" if item.get("reason") else "")
        else:
            row["text"] = item.get("text") or item["kind"]
        inputs.append(row)
    threads = [{"key": t["key"], "context": ticket["id"] in (t.get("context") or []),
                "bound": any(f["thread"] == t["key"] for f in ticket.get("follows") or [])}
               for t in rt.store.threads.list() if ticket["id"] in t["subscriptions"]]
    messages = [m for m in rt.store.receipts.list(limit=500) if m.get("ticketId") == ticket["id"]][::-1]
    mandate = ticket["mandate"]
    return {**ticket_row(rt, ticket), "scope": mandate.get("scope"), "policy_version": mandate["policyVersion"][:12],
            "basis": {k: v for k, v in mandate["basis"].items() if k != "scope"}, "actor": mandate["actor"],
            "rule_instruction": (rule or {}).get("instruction"), "tell": (rule or {}).get("tell"),
            "decision": decision_row(rt, decision) if decision else None, "origin": trigger_row(rt, origin) if origin else None,
            "attention": ticket.get("attention"), "answered": ticket.get("answered") or [], "inputs": inputs, "threads": threads,
            "follows": ticket.get("follows") or [], "directory": (ticket.get("execution") or {}).get("directory"),
            "assignments": [{"id": a["id"], "state": a["state"], "created_at": a["createdAt"], "started_at": a.get("startedAt"),
                             "ended_at": a.get("endedAt"), "ended_reason": a.get("endedReason"), "report": a.get("report")}
                            for a in ticket["assignments"]][::-1],
            "runs": rt.store.runs(ticket["id"], 50),
            "messages": [{"id": m["id"], "kind": m["kind"], "status": m["status"], "at": m["at"], "text": m["text"][:1500], "reason": m.get("reason"),
                          "channel": m["channel"]} for m in messages]}


def threads(rt, filter_: str = "recent", limit: int = 60) -> list[dict]:
    rows = []
    since = shift(now_iso(), -86400)
    for thread in rt.store.threads.list():
        if filter_ == "subscribed" and not thread["subscriptions"]:
            continue
        if filter_ == "evaluating" and not (thread["pending"] or thread.get("evaluating")):
            continue
        if filter_ == "recent" and (thread.get("lastAt") or "") < since:
            continue
        subscribed = [t for t in (rt.store.tickets.get(i) for i in thread["subscriptions"]) if t]
        rows.append({"key": thread["key"], "source": thread["ref"]["source"], "last_at": thread.get("lastAt"), "pending": len(thread["pending"]),
                     "evaluating": bool(thread.get("evaluating")), "hold_until": thread.get("holdUntil"),
                     "tickets": [{"id": t["id"], "code": f"T{t.get('no')}", "title": t["title"], "live": live_ticket(t),
                                  "context": t["id"] in (thread.get("context") or [])} for t in subscribed]})
        if len(rows) >= limit:
            break
    return rows


def thread_page(rt, key_: str) -> dict | None:
    thread = rt.store.threads.get(key_)
    if not thread:
        return None
    return {**next(iter(r for r in threads(rt, "all", 100000) if r["key"] == key_), {"key": key_}),
            "correlated": thread.get("correlated") or [],
            "triggers": [trigger_row(rt, t) for t in rt.store.thread_triggers(key_, 80)],
            "decisions": [decision_row(rt, d) for d in rt.store.decisions.by_thread(key_)][::-1][:50]}


def decisions(rt, kind: str | None = None, limit: int = 80) -> list[dict]:
    if kind == "would_accept":
        return [decision_row(rt, d) for d in rt.store.decisions.recent(1000, kind="ignore") if (d.get("audit") or {}).get("would_accept")][:limit]
    return [decision_row(rt, d) for d in rt.store.decisions.recent(limit, kind=kind)]


def sources(rt) -> list[dict]:
    since = shift(now_iso(), -86400)
    stats = rt.store.trigger_stats(since)
    out = []
    for name, kind in rt.sources.kinds.items():
        health = rt.health.get(f"source:{name}") or {}
        filtered = rt.store.one("SELECT COUNT(*) FROM trigger t JOIN disposition d ON d.id = t.id WHERE t.source=? AND t.at>=? "
                                "AND d.value LIKE '%\"filtered\"%'", (name, since)) or 0
        dropped = [m for m in rt.store.receipts.list(status="dropped", since=since, limit=200)
                   if m["channel"]["source"] == name and m.get("reason") != "withdrawn"]
        out.append({"id": name, "title": kind.title, "enabled": rt.sources.enabled(name), "system": kind.system, "interrupts": kind.interrupts,
                    "state": health.get("state") or ("off" if not rt.sources.enabled(name) else "ready"), "detail": health.get("detail") or "",
                    "triggers_24h": stats.get(name, 0), "filtered_24h": filtered, "dropped_24h": len(dropped)})
    return out


def settings_view(rt) -> dict:
    settings = rt.settings.current()
    return {"mode": settings["eval"]["mode"], "paused": settings["paused"], "paused_keys": settings["paused_keys"],
            "policy_version": policy_version(settings), "eval": settings["eval"], "executor": settings["executor"], "home": settings["home"],
            "rules": [{"id": r["id"], "name": r["name"], "enabled": r["enabled"], "instruction": r["instruction"],
                       "when": r["when"], "executor": r["run"].get("executor"), "agent": r["run"].get("agent"), "role": r["run"].get("role"),
                       "skill": r["run"].get("skill"), "tools": r["run"].get("tools"), "tell": r["tell"], "priority": r["priority"],
                       "requires_approval": r["requires_approval"], "paused": f"rule:{r['id']}" in settings["paused_keys"],
                       "semantic": not r.get("when")} for r in settings["policy"]["rules"]],
            "sources": {name: {"allowed": part["allowed"], "bots": part["bots"], "admission": part["admission"], "answerers": part["answerers"]}
                        for name, part in settings["policy"]["sources"].items()},
            "schedules": rt.schedule.specs(), "history": rt.settings.history()}


def search(rt, query: str, limit: int = 30) -> dict:
    q = query.strip()
    if not q:
        return {"tickets": [], "triggers": [], "threads": []}
    ticket = rt.find_ticket(q)
    like = f"%{q}%"
    import json
    found = [json.loads(r[0]) for r in rt.store.all("SELECT value FROM ticket WHERE value LIKE ? ORDER BY updated_at DESC LIMIT ?", (like, limit))]
    if ticket:  # 按编号找到的排最前
        found = [ticket, *[t for t in found if t["id"] != ticket["id"]]]
    triggers = [json.loads(r[0]) for r in rt.store.all("SELECT value FROM trigger WHERE id=? OR value LIKE ? ORDER BY at DESC LIMIT ?", (q, like, limit))]
    keys = [r[0] for r in rt.store.all("SELECT key FROM thread WHERE key LIKE ? ORDER BY last_at DESC LIMIT ?", (like, limit))]
    return {"tickets": [ticket_row(rt, t) for t in found], "triggers": [trigger_row(rt, t) for t in triggers], "threads": keys}
