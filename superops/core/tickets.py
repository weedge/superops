"""工单：从接单到结束的生命周期、订阅和输入。"""
from __future__ import annotations

import json

from .errors import UserError
from .model import (latest_assignment, live_ticket, ms, read_until, running_assignment, settled, thread_key,
                    waits_for_person)
from .part import EnginePart
from .reasons import key
from .stage import can_retry

STATUS_REQUEST = "请报告这张工单当前的状态。这只是询问状态：不要恢复执行，也不要做任何对外操作。"


def same_channel(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def automatic(item: dict | None) -> bool:
    """这条输入是否不是人写的：CI、机器、流水线事件，或主 Session 的备注。"""
    return bool(item) and (item["kind"] == "note" or (item["kind"] == "trigger" and not item.get("human")))


def read_before(item: dict, until: str) -> bool:
    """worker 是否在 until（它最近的报告或一轮结束）之前读到了这条触发输入。"""
    if "readAt" not in item:
        delivered = item.get("deliveredAt")
        return (delivered if delivered and delivered > item["at"] else item["at"]) <= until
    return bool(item["readAt"]) and item["readAt"] < until


class Tickets(EnginePart):
    """为一次接单创建工单（带授权、订阅和第一次派工），绑定和关联线程，加输入，恢复、重试、重启、结束工单，取消订阅。"""

    def __init__(self, options, replies):
        super().__init__(options)
        self.replies = replies

    def create(self, ticket_id: str, decision_id: str, mandate: dict, evaluation: dict, outcome: dict, at: str) -> None:
        trigger_ids = evaluation["triggerIds"]
        primary = self.repo.triggers.get(evaluation.get("primary") or trigger_ids[-1])
        origin = {"triggerId": (primary or {}).get("id") or trigger_ids[0], "triggerIds": trigger_ids}
        ticket = {"id": ticket_id, "title": outcome["title"], "decisionId": decision_id, "mandate": mandate, "channels": [],
                  "assignments": [], "inputs": [], "undelivered": [], "createdAt": at, "updatedAt": at, "origin": origin}
        for tid in trigger_ids:
            channel = (self.repo.triggers.get(tid) or {}).get("channel")
            if channel and not any(same_channel(c, channel) for c in ticket["channels"]):
                ticket["channels"].append(channel)
        if self.options.prepare_ticket:
            ticket = self.options.prepare_ticket(ticket, outcome) or ticket
        assignment = self._new_assignment(ticket)
        self.repo.tickets.save(ticket)
        for tid in trigger_ids:
            for ref in (self.repo.triggers.get(tid) or {}).get("threads") or []:
                self._subscribe(ticket_id, ref)
        if ticket.get("thread"):  # 工单自己的线程：人按工单编号说的话落在这里，只到这一张工单
            self._subscribe(ticket_id, ticket["thread"])
        if outcome.get("acknowledgement") and primary:
            self.replies.acknowledge_accepted(ticket_id, primary, outcome["acknowledgement"])
        self.options.executor.start(self.require(ticket_id), assignment)
        self.emit("ticket.created", ticketId=ticket_id, decisionId=decision_id, strength=mandate["strength"])

    def bind(self, ticket_id: str, thread: dict, own: bool) -> dict:
        """让未结束的工单跟随来源的一个线程（例如它的 MR）；own = 这个线程是工单自己开的。"""
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        self._subscribe(ticket_id, thread)
        ticket = self.require(ticket_id)
        followed = thread_key(thread)
        follow = next((f for f in ticket.get("follows") or [] if f["thread"] == followed), None)
        if follow:  # 重新绑定会把关过的线程重新打开；own 一旦为真就保持
            follow["own"] = follow["own"] or own
            follow.pop("closed", None)
        else:
            ticket["follows"] = [*(ticket.get("follows") or []), {"thread": followed, "own": own}]
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        return ticket

    def thread_closed(self, thread: dict, outcome: str, at: str) -> None:
        """来源的事实：一个线程关了（例如 MR 合并）。跟随它的未结束工单在跟完它的线程后结束，除非工作还没做完。"""
        closed = thread_key(thread)
        for ticket in self.repo.tickets.open():
            follow = next((f for f in ticket.get("follows") or [] if f["thread"] == closed and not f.get("closed")), None)
            if not follow:
                continue
            follow["closed"] = {"at": at, "outcome": outcome}
            self.repo.tickets.save(ticket)
            done = self.follows_done(ticket)
            if not done or self._unfinished(ticket, at):
                continue
            resumed = ms((latest_assignment(ticket) or {}).get("createdAt")) > ms(at)
            self.end(ticket["id"], "executor", done["outcome"], self.now() if resumed else done["at"])

    def follows_done(self, ticket: dict) -> dict | None:
        """跟随线程的工单何时算跟完：它自己开的线程都关了；没有自己开的，则它跟随的每个线程都关了。按最晚的那次关闭结束。"""
        follows = ticket.get("follows") or []
        own = [f for f in follows if f["own"]]
        counted = own or follows
        if not counted or any(not f.get("closed") for f in counted):
            return None
        return sorted((f["closed"] for f in counted), key=lambda c: c["at"])[-1]

    def attachable(self, ticket_id: str, trigger_id: str) -> dict:
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        self.left_to_workers(trigger)
        current = self.require(ticket_id)
        if (current.get("ended") or {}).get("by") == "human":
            raise UserError("ticketStopped")
        if not live_ticket(current) and not self.policy().revalidate(current["mandate"]):
            raise UserError("mandateRevoked")
        return trigger

    def attach(self, ticket_id: str, trigger: dict, note: str | None = None) -> None:
        """把工单订阅到这条触发的线程，并把触发作为输入（带路由备注）加给它；之后由它的执行器来决定这条消息。"""
        for ref in trigger["threads"]:
            self._subscribe(ticket_id, ref)
        current = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": self.now()}
        disposition = {k: v for k, v in current.items() if k not in ("context", "requeued")}
        context, requeued = current.get("context"), current.get("requeued")
        followed = [i for i in context or [] if i != ticket_id]
        disposition["fanout"] = list(dict.fromkeys([*current["fanout"], ticket_id]))
        if followed:
            disposition["context"] = followed
        self.repo.triggers.save_disposition(trigger["id"], disposition)
        ticket = self.require(ticket_id)
        iid = key("input", ticket_id, "trigger", trigger["id"])
        existing = next((i for i in ticket["inputs"] if i["id"] == iid), None)
        at = self.now()

        def make(input_id: str) -> dict:
            item = {"id": input_id, "kind": "trigger", "triggerId": trigger["id"], "at": at}
            if note:
                item["note"] = note
            if trigger["human"]:
                item["human"] = True
            return item

        if not existing or existing["kind"] != "trigger":
            self.add_input(ticket, make(iid))
        elif note and iid in ticket["undelivered"]:
            existing["note"] = note
            ticket["updatedAt"] = at
            self.repo.tickets.save(ticket)
        elif requeued or ticket_id not in current["fanout"] or ticket_id in (context or []) or not live_ticket(ticket):
            # 人把它送去评估过、工单在执行器接手前放掉了它、只当上下文读过，或工单在拿到它之后结束了：再给一次
            self.add_input(ticket, make(key("input", ticket_id, "trigger", trigger["id"], "attached", at)))
        elif note:  # 执行器已经有这条消息，备注随它再送一次
            self.add_input(ticket, make(key("input", ticket_id, "trigger", trigger["id"], "note", note)))

    def request_status(self, ticket_id: str) -> bool:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket) or ((running_assignment(ticket) or {}).get("report") or {}).get("state") != "waiting":
            return False
        self.add_input(ticket, {"id": f"status:{ticket_id}:{ticket['updatedAt']}", "kind": "text", "text": STATUS_REQUEST, "at": self.now()})
        return True

    def stop(self, ticket_id: str, reason: str | None = None) -> dict:
        self.end(ticket_id, "human", "stopped", self.now(), reason)
        return self.require(ticket_id)

    def retry(self, ticket_id: str) -> dict:
        ticket = self.require(ticket_id)
        latest = latest_assignment(ticket)
        if not can_retry(ticket) or not latest:
            raise UserError("notRetryable")
        self._end_assignment(ticket, latest, "retried")
        self._requeue_untaken(ticket, latest)
        assignment = self._new_assignment(ticket)
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        self.options.executor.stop(ticket, latest, "retried")
        self.options.executor.start(ticket, assignment)
        return ticket

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        ticket = self.require(ticket_id)
        if any(thread_key(ref) == thread for ref in (self.source(ticket) or {}).get("threads") or []):
            raise UserError("sourceThread")
        if any(f["thread"] == thread for f in ticket.get("follows") or []):
            raise UserError("followedThread")
        if ticket_id not in ((self.repo.threads.get(thread) or {}).get("subscriptions") or []):
            raise UserError("notFollowing")
        self.drop_subscriptions(thread, "executor", ticket_id, reason)
        for trigger in self._triggers_of(ticket):
            if any(thread_key(ref) == thread for ref in trigger["threads"]) and ticket_id in self.owners(trigger):
                self.hand_back(ticket_id, trigger, reason)

    def policy_changed(self, from_: str, to: str, effects: dict) -> None:
        """策略变更只告诉受影响的未结束工单（先接的先说），其余不打扰。"""
        at = self.now()
        for ticket in sorted(self.repo.tickets.open(), key=lambda t: t["createdAt"]):
            effect = effects.get(ticket["id"])
            if effect:
                self.add_input(ticket, {"id": key("input", ticket["id"], "policy", from_, to, at), "kind": "policy-changed",
                                        "from": from_, "to": to, "at": at, "effect": effect})

    def drop_subscriptions(self, thread: str, by: str, ticket_id: str | None = None, reason: str | None = None) -> bool:
        current = self.repo.threads.get(thread)
        tickets = [i for i in (current or {}).get("subscriptions") or [] if ticket_id is None or i == ticket_id]
        if not current or not tickets:
            return False
        current["subscriptions"] = [i for i in current["subscriptions"] if i not in tickets]
        self.repo.threads.save(current)
        for tid in tickets:
            ticket = self.repo.tickets.get(tid)
            if not ticket or not any(f["thread"] == thread for f in ticket.get("follows") or []):
                continue
            follows = [f for f in ticket["follows"] if f["thread"] != thread]
            if follows:
                ticket["follows"] = follows
            else:
                ticket.pop("follows", None)
            self.repo.tickets.save(ticket)
        for tid in tickets:
            self.emit("thread.unsubscribed", threadKey=current["key"], ticketId=tid, by=by, reason=reason)
        return True

    def add_input(self, ticket: dict, item: dict) -> None:
        """幂等地加一条输入并请执行器送达；最近的派工还没启动（上次启动失败）且不在等人时，重新启动它。"""
        if any(i["id"] == item["id"] for i in ticket["inputs"]):
            return
        ticket["inputs"].append(item)
        ticket["updatedAt"] = self.now()
        held = live_ticket(ticket) and automatic(item) and waits_for_person(ticket)  # 等人期间压住自动输入
        if not held:
            ticket["undelivered"].append(item["id"])
        self.repo.tickets.save(ticket)
        if not live_ticket(ticket) or held:
            return
        running, latest = running_assignment(ticket), latest_assignment(ticket)
        if running:
            self.options.executor.deliver(ticket, running)
        elif latest and latest["state"] == "pending" and not waits_for_person(ticket):
            self.options.executor.start(ticket, latest)

    def wake(self, ticket_id: str) -> None:
        """已结束的工单在它的线程上等来了人的消息：在原授权下恢复，不评估、不产生决定。
        当前策略不再支持其授权的，保持结束并放掉等它的消息。暂停期间消息先等着。"""
        ticket = self.repo.tickets.get(ticket_id)
        if not ticket or live_ticket(ticket) or (ticket.get("ended") or {}).get("by") == "human" or not ticket["undelivered"]:
            return
        policy = self.policy()
        if policy.paused:
            return
        if not policy.revalidate(ticket["mandate"]):
            self._refuse(ticket)
            return
        by_id = {i["id"]: i for i in ticket["inputs"]}
        first = next((by_id[i] for i in ticket["undelivered"] if i in by_id and by_id[i]["kind"] == "trigger" and by_id[i].get("human")), None)
        if not first:
            return
        at = self.now()
        ticket.pop("ended", None)
        ticket["updatedAt"] = at
        assignment = self._new_assignment(ticket)
        resumed = {"id": key("input", ticket["id"], "resumed", first["triggerId"]), "kind": "resumed", "triggerId": first["triggerId"], "at": at}
        ticket["inputs"].append(resumed)
        ticket["undelivered"].insert(0, resumed["id"])
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)
        self.emit("ticket.resumed", ticketId=ticket_id, triggerId=first["triggerId"])

    def restart(self, ticket: dict, running: dict, reason: str, note: str) -> None:
        """用一次新的派工替换正在跑的那次，新派工先收到 note。"""
        self._end_assignment(ticket, running, reason)
        self._requeue_untaken(ticket, running)
        assignment = self._new_assignment(ticket)
        at = self.now()
        item = {"id": key("input", ticket["id"], "restart", assignment["id"]), "kind": "text", "text": note, "at": at}
        ticket["inputs"].append(item)
        ticket["undelivered"].append(item["id"])
        ticket["updatedAt"] = at
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)

    def end(self, ticket_id: str, by: str, outcome: str, at: str | None = None, reason: str | None = None) -> None:
        """结束工单。at 是结束这件事发生的时间（例如 MR 合并的时间）。
        工单放掉执行器从没收到的消息，以及由它决定、但执行器没来得及读、也没确认 / 答复 / 交还的人的消息。"""
        at = at or self.now()
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        stopping = [a for a in ticket["assignments"] if a["state"] != "ended"]
        until = read_until(ticket)
        waiting = self._triggers_of(ticket, ticket["undelivered"])
        left = self._untaken(ticket, [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger"
                                      and (not read_before(i, until) or i["id"] in ticket["undelivered"])])
        ticket["ended"] = {"at": at, "by": by, "outcome": outcome}
        if reason and reason != outcome:
            ticket["ended"]["reason"] = reason
        ticket["undelivered"] = []
        ticket["updatedAt"] = self.now()
        attention = ticket.pop("attention", None)
        ticket.pop("delivery", None)
        for assignment in stopping:
            assignment["state"] = "ended"
            assignment["endedAt"] = at
        self.repo.tickets.save(ticket)
        if attention:
            self.replies.withdraw_attention(attention["id"])
        self._let_go(ticket_id, waiting, left)
        self.emit("ticket.ended", ticketId=ticket_id, by=by, outcome=outcome, reason=ticket["ended"].get("reason"))
        for assignment in stopping:
            self.options.executor.stop(ticket, assignment, reason or outcome)

    def note(self, ticket_id: str, text: str) -> dict:
        """主 Session 给未结束工单的执行器一条事实（例如另一张工单的发现）；不请求什么，也不授予什么。"""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        at = self.now()
        self.add_input(ticket, {"id": key("input", ticket_id, "note", at, text), "kind": "note", "text": text, "at": at})
        return self.require(ticket_id)

    def follow_for_context(self, ticket_id: str, ref: dict) -> bool:
        """执行器为了解上下文订阅一个相关线程：消息会到它，但不带来权限，也不唤醒已结束的它。"""
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        if ticket_id in self.thread(ref)["subscriptions"]:
            return False
        self._subscribe(ticket_id, ref)
        thread = self.thread(ref)
        thread["context"] = [*(thread.get("context") or []), ticket_id]
        self.repo.threads.save(thread)
        return True

    # ---- 内部

    def _subscribe(self, ticket_id: str, ref: dict) -> None:
        thread = self.thread(ref)
        if ticket_id not in thread["subscriptions"]:
            thread["subscriptions"].append(ticket_id)
            self.emit("thread.subscribed", threadKey=thread["key"], ticketId=ticket_id)
        if ticket_id in (thread.get("context") or []):
            thread["context"] = [i for i in thread["context"] if i != ticket_id]
        self.repo.threads.save(thread)

    def _triggers_of(self, ticket: dict, ids: list[str] | None = None) -> list[dict]:
        out = []
        for item in ticket["inputs"]:
            if item["kind"] == "trigger" and (ids is None or item["id"] in ids):
                trigger = self.repo.triggers.get(item["triggerId"])
                if trigger:
                    out.append(trigger)
        return out

    def _taken_up(self, ticket: dict, trigger: dict) -> bool:
        acked = (self.repo.triggers.disposition(trigger["id"]) or {}).get("acked")
        return ticket["id"] in acked if acked else self.options.outbox.status(key("ack", trigger["id"])) is not None

    def _untaken(self, ticket: dict, ids: list[str]) -> list[dict]:
        """这些输入里，由工单决定、而执行器没有接下的人的消息。"""
        return [t for t in self._triggers_of(ticket, ids) if ticket["id"] in self.owners(t) and not self._taken_up(ticket, t)]

    def _let_go(self, ticket_id: str, waiting: list[dict], left: list[dict]) -> None:
        """结束（或无法恢复）的工单放掉消息：不再算作到过它；left 里没有别的工单决定、没被接下、也没被决定了结的，送去评估。"""
        for trigger in [*waiting, *left]:
            disposition = self.repo.triggers.disposition(trigger["id"])
            if disposition and ticket_id in disposition["fanout"]:
                self.repo.triggers.save_disposition(trigger["id"], {**disposition, "fanout": [i for i in disposition["fanout"] if i != ticket_id]})
        for trigger in left:
            disposition = self.repo.triggers.disposition(trigger["id"]) or {}
            decision = self.repo.decisions.get(disposition["decisionId"]) if disposition.get("decisionId") else None
            stopped = bool(decision) and decision["kind"] == "stop" and decision.get("ticketId") == ticket_id and not disposition.get("handbacks")
            decided = bool(decision) and (settled(decision) or stopped)
            if not self.owners(trigger) and not disposition.get("acked") and not decided:
                self.queue_for_eval(trigger)

    def _refuse(self, ticket: dict) -> None:
        waiting = self._triggers_of(ticket, ticket["undelivered"])
        left = self._untaken(ticket, ticket["undelivered"])
        ticket["undelivered"] = []
        self.repo.tickets.save(ticket)
        self._let_go(ticket["id"], waiting, left)

    def _requeue_untaken(self, ticket: dict, replaced: dict) -> None:
        """送达给被替换的派工、但 worker 一步都没取走的触发输入，放回队首。"""
        untaken = [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger" and "readAt" in i and i["readAt"] is None
                   and i.get("deliveredTo") == replaced["id"] and i["id"] not in ticket["undelivered"]]
        ticket["undelivered"] = [*untaken, *ticket["undelivered"]]

    def _unfinished(self, ticket: dict, at: str) -> bool:
        """线程在 at 关闭时工单的工作是否还没做完。"""
        until = read_until(ticket, False)
        ids = [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger" and not read_before(i, until)]
        taken = any(ticket["id"] in self.owners(t) and self._taken_up(ticket, t) for t in self._triggers_of(ticket, ids))
        latest = latest_assignment(ticket)
        if taken or not (ms((latest or {}).get("createdAt")) > ms(at)):
            return taken
        report = (latest or {}).get("report")
        later = any(i["kind"] != "policy-changed" and ms(i["at"]) > ms((report or {}).get("at")) for i in ticket["inputs"])
        return (report or {}).get("state") != "ready" or bool(ticket["undelivered"]) or later

    def _new_assignment(self, ticket: dict) -> dict:
        assignment = {"id": key("assignment", ticket["id"], str(len(ticket["assignments"]))), "state": "pending", "createdAt": self.now()}
        ticket["assignments"].append(assignment)
        ticket.pop("delivery", None)
        return assignment

    def _end_assignment(self, ticket: dict, assignment: dict, reason: str) -> None:
        assignment["state"] = "ended"
        assignment["endedAt"] = self.now()
        assignment["endedReason"] = reason
        self.emit("assignment.ended", ticketId=ticket["id"], assignmentId=assignment["id"], reason=reason)
