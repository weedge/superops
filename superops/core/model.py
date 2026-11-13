"""领域模型：概念和它们各自守的规则，与具体来源、策略、执行器无关（docs/001）。

所有聚合都是可以直接存成 JSON 的普通 dict，键名沿用 dsh-superagent 原型（驼峰），
这样两边的记录、文档和测试可以逐条对照。可选字段不存在就是没有，不写 None。

    Trigger      {id, source, threads:[ThreadRef], actor:{source,id}, human, addressed?, at, text,
                  channel?, url?, notBefore?, byTicket?, meta}
    Disposition  {filtered?, evalId?, decisionId?, fanout:[ticketId], context?, requeued?, handbacks?, acked?, at}
    Thread       {key, ref, createdAt, lastAt?, subscriptions, context?, correlated, pending, evaluating?,
                  expired?, holdUntil?, meta}
    Evaluation   {id, threadKey, triggerIds, primary?, strength, actor, basis, policyVersion, startedAt,
                  delivered?, deadline, pausedAt?}
    Decision     {id, evalId, threadKey, triggerIds, policyVersion, at, kind, reason:[Reason], strength,
                  ticketId?, mandate?, audit?, dismissed?}
    Mandate      {strength, policyVersion, basis, actor, scope}
    Ticket       {id, title, decisionId, mandate, channels, ended?, attention?, answered?, assignments, inputs,
                  undelivered, follows?, answers?, createdAt, updatedAt, origin:{triggerId,triggerIds},
                  execution?, delivery?, no?, thread?}
    Assignment   {id, state, createdAt, startedAt?, endedAt?, endedReason?, report?, deliveredAt?, idleAt?}
    Attention    {id, question, triggerId?, card?, expiresAt?}

相对原型多出来的四处（都是已有概念的属性，不是新概念）：
- Trigger.notBefore：来源声明“这个时间之前先别评估”（防抖属于来源，Eval 从不等待）；
- Trigger.byTicket：这条事件是哪张工单的执行器产出的，扇出时不回送给它自己；
- Attention.card：问题带选项（批红卡片），人选中的选项里写好的动作才会被执行；
- Ticket.thread：工单自己的线程（ticket:T12）。人按编号对工单说的话落在这里，只到这一张工单，
  不会惊动同一个业务线程（尤其是共用的定时线程）上的其他工单。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

Strength = str          # explicit | inferred
ReportState = str       # progress | waiting | ready | blocked | failed | dropped
TicketOutcome = str     # completed | merged | closed | dropped | stopped
Trigger = dict
Thread = dict
Ticket = dict
Decision = dict
Mandate = dict
Assignment = dict


def thread_key(ref: dict) -> str:
    """线程在整个领域里的稳定键。"""
    return f"{ref['source']}:{ref['id']}"


def parse_time(value: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def to_iso(d: datetime) -> str:
    """统一成 UTC、毫秒、Z 结尾：存下来的时间可以直接按文本比较。"""
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_time(value: str) -> str:
    d = parse_time(value)
    return to_iso(d) if d else value


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def shift(value: str, seconds: float) -> str:
    d = parse_time(value)
    return to_iso(datetime.fromtimestamp(d.timestamp() + seconds, timezone.utc))


def ms(value: str | None) -> float:
    d = parse_time(value) if value else None
    return d.timestamp() * 1000 if d else float("nan")


def live_ticket(ticket: dict) -> bool:
    """工单是否未结束。"""
    return not ticket.get("ended")


def ticket_outcome(ticket: dict) -> str | None:
    return (ticket.get("ended") or {}).get("outcome")


def running_assignment(ticket: dict) -> dict | None:
    return next((a for a in ticket["assignments"] if a["state"] == "running"), None)


def latest_assignment(ticket: dict) -> dict | None:
    return ticket["assignments"][-1] if ticket["assignments"] else None


def latest_report(ticket: dict) -> dict | None:
    """工单任一派工最近的一次报告，也就是工单最后说了什么。"""
    for a in reversed(ticket["assignments"]):
        if a.get("report"):
            return a["report"]
    return None


def read_until(ticket: dict, progress: bool = True) -> str:
    """执行器最晚到什么时候有机会读到送达的内容：最近一次报告（progress=False 时不算进展报告）或最近一轮结束，取较晚者。"""
    report_at = ""
    for a in reversed(ticket["assignments"]):
        r = a.get("report")
        if r and (progress or r["state"] != "progress"):
            report_at = r["at"]
            break
    return max([report_at, *[a.get("idleAt") or "" for a in ticket["assignments"]]])


def waits_for_person(ticket: dict) -> bool:
    """工单是否在等人：最近的派工报告 blocked / failed，之后没有人的消息到达过。"""
    report = (latest_assignment(ticket) or {}).get("report")
    if not report or report["state"] not in ("blocked", "failed"):
        return False
    return not any(i["kind"] == "trigger" and i.get("human") and not i.get("context") and i["at"] > report["at"]
                   for i in ticket["inputs"])


def reached(ticket: dict, trigger: dict, thread: Callable[[str], dict | None], context: bool = True) -> bool:
    """触发是否到过这张工单：是它的起点、作为输入送达过，或在它订阅的线程上。
    context=False 时不算只为了解上下文而关注的线程（那里只读不写）。"""
    if ticket["origin"]["triggerId"] == trigger["id"]:
        return True
    is_input = any(i["kind"] == "trigger" and i["triggerId"] == trigger["id"] for i in ticket["inputs"])

    def on(ref: dict) -> bool:
        cur = thread(thread_key(ref))
        return bool(cur) and ticket["id"] in cur["subscriptions"] and (context or ticket["id"] not in (cur.get("context") or []))

    if any(on(ref) for ref in trigger["threads"]):
        return True
    all_context = all(ticket["id"] in ((thread(thread_key(ref)) or {}).get("context") or []) for ref in trigger["threads"])
    return is_input and (context or not all_context)


def followup_owners(trigger: dict, repo: Any) -> list[str]:
    """由哪些工单的执行器来决定一条人的跟进消息：消息在它们自己的线程上到达、它们还能处理、且没有交还。
    工单在进行中就算；已结束（不是被人停掉）而这条消息正等着唤醒它也算。
    人把消息重新送去评估后、或已有评估持有它时，没有归属。有归属期间消息不进评估，主 Session 只能停工。"""
    if not trigger.get("human"):
        return []
    disposition = repo.triggers.disposition(trigger["id"])
    if not disposition or disposition.get("requeued"):
        return []
    returned = {h["ticketId"] for h in disposition.get("handbacks") or []}
    ctx = disposition.get("context") or []
    reach = [i for i in disposition["fanout"] if i not in returned and i not in ctx]
    if not reach:
        return []
    primary = repo.threads.get(thread_key(trigger["threads"][0]))
    if primary and trigger["id"] in ((primary.get("evaluating") or {}).get("triggerIds") or []):
        return []
    owners = []
    for tid in reach:
        ticket = repo.tickets.get(tid)
        if not ticket:
            continue
        waiting = (ticket.get("ended") or {}).get("by") != "human" and any(
            i["kind"] == "trigger" and i["triggerId"] == trigger["id"] and i["id"] in ticket["undelivered"]
            for i in ticket["inputs"])
        if live_ticket(ticket) or waiting:
            owners.append(tid)
    return owners


def settled(decision: dict) -> bool:
    """一个决定是否了结了人的消息所求：停止（不论成不成）只管那张工单，转给工单的交给它的执行器，未完成的什么都没定。"""
    return decision["kind"] not in ("stop", "unavailable") and not any(
        r["code"] in ("stopNotExplicit", "stopUnknown", "routed") for r in decision["reason"])
