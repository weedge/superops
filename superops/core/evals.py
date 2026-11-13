"""评估：线程级别对排队触发的判断，以及它们以什么决定结束。"""
from __future__ import annotations

from .errors import UserError
from .model import live_ticket, ms, shift, thread_key
from .part import EnginePart
from .reasons import key, reason_of

REQUEUES = 2  # 过期的评估连续重新排队几次之后记为未完成


def stronger(a: str, b: str) -> str:
    return "explicit" if "explicit" in (a, b) else "inferred"


class Evals(EnginePart):
    """把一个线程排队的触发并成一次评估，从准入取强度、发起人和依据，请研判方判断，
    暂停期间保住期限，最后记录决定：accept / stop / ignore / unavailable。"""

    def __init__(self, options, tickets):
        super().__init__(options)
        self.tickets = tickets

    def due(self, thread: dict, policy) -> None:
        """评估过了期限就过期；暂停期间不过期，恢复后期限顺延暂停的时长。候选还没送到研判方的不过期。"""
        evaluation = thread.get("evaluating")
        if not evaluation or (policy.paused and evaluation.get("pausedAt")):
            return
        now = self.now()
        if policy.paused:
            evaluation["pausedAt"] = now
        elif evaluation.get("pausedAt"):
            evaluation["deadline"] = shift(evaluation["deadline"], (ms(now) - ms(evaluation["pausedAt"])) / 1000)
            del evaluation["pausedAt"]
        elif evaluation.get("delivered") and evaluation["deadline"] <= now:
            self._expire(thread)
            return
        else:
            return
        self.repo.threads.save(thread)

    def delivered(self, eval_id: str, policy) -> None:
        """候选送到了研判方：期限从现在算。"""
        thread = next((t for t in self.repo.threads.active() if (t.get("evaluating") or {}).get("id") == eval_id), None)
        if not thread or thread["evaluating"].get("delivered"):
            return
        now = self.now()
        thread["evaluating"]["delivered"] = now
        thread["evaluating"]["deadline"] = shift(now, policy.eval_timeout_ms / 1000)
        self.repo.threads.save(thread)

    def _expire(self, thread: dict) -> None:
        expired = (thread.get("expired") or 0) + 1
        if expired > REQUEUES:
            self._finish(thread, thread["evaluating"], {"kind": "unavailable", "reason": [{"code": "timeout"}]})
            return
        evaluation = thread.pop("evaluating")
        thread["expired"] = expired
        ids = evaluation["triggerIds"]
        thread["pending"] = [*ids, *[i for i in thread["pending"] if i not in ids]]
        self.repo.threads.save(thread)
        self._release(evaluation["id"])

    def start(self, key_: str, policy) -> None:
        """对一个线程排队的触发开始一次评估：先过准入，再请研判方。"""
        thread = self.repo.threads.get(key_)
        at = self.now()

        def owned(trigger_id: str) -> bool:
            trigger = self.repo.triggers.get(trigger_id)
            return bool(trigger) and len(self.owners(trigger)) > 0

        followups = [i for i in thread["pending"] if owned(i)]
        if followups:  # 工单还在决定的跟进属于那张工单：离开队列，不产生决定
            thread["pending"] = [i for i in thread["pending"] if i not in followups]
            self.repo.threads.save(thread)
            for trigger_id in followups:
                self.emit("trigger.followup", triggerId=trigger_id)
            if not thread["pending"]:
                return
        # 已经开过工单的触发不能再给新的评估带来权限，也不能再开一张
        accepted = self._accepted(thread["key"])
        started = [i for i in thread["pending"] if i in accepted]
        if started:
            self._ignore(thread, started, [{"code": "startedTicket", "triggerId": i, "values": {"ticket": accepted[i]["ticketId"]}}
                                           for i in started], policy, at)
        triggers = [t for t in (self.repo.triggers.get(i) for i in thread["pending"]) if t]
        tickets = self.tickets_on_thread(thread)
        reasons: list[dict] = []
        strongest = None
        for trigger in triggers:
            gate = policy.gate(trigger, thread, tickets)
            if not gate["admissible"]:
                reasons.append({"code": gate["reason"], "triggerId": trigger["id"]})
                continue
            later = bool(strongest) and gate["strength"] == strongest["strength"] and trigger["at"] >= strongest["trigger"]["at"]
            weaker = not strongest or stronger(strongest["strength"], gate["strength"]) != strongest["strength"]
            if weaker or later:
                strongest = {"trigger": trigger, "strength": gate["strength"], "basis": gate["basis"]}
        if not strongest:
            if thread["pending"]:
                self._ignore(thread, list(thread["pending"]), reasons or [{"code": "noAdmissible"}], policy, at)
            thread.pop("holdUntil", None)
            self.repo.threads.save(thread)
            return
        eval_id = key("eval", thread["key"], policy.version, at, *thread["pending"])
        thread["evaluating"] = {
            "id": eval_id, "threadKey": thread["key"], "triggerIds": thread["pending"], "primary": strongest["trigger"]["id"],
            "strength": strongest["strength"], "actor": strongest["trigger"]["actor"], "basis": strongest["basis"],
            "policyVersion": policy.version, "startedAt": at, "deadline": shift(at, policy.eval_timeout_ms / 1000)}
        thread["pending"] = []
        thread.pop("holdUntil", None)
        self.repo.threads.save(thread)
        for trigger in triggers:
            disposition = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": at}
            self.repo.triggers.save_disposition(trigger["id"], {**disposition, "evalId": eval_id})
        self.emit("eval.started", evalId=eval_id, threadKey=thread["key"], triggerIds=thread["evaluating"]["triggerIds"],
                  strength=strongest["strength"])
        self.options.assessor.request(self._candidate_of(thread))

    def candidate(self, trigger_id: str) -> dict | None:
        thread = self._holding(trigger_id)
        return self._candidate_of(thread) if thread else None

    def decide(self, trigger_id: str, outcome: dict) -> dict:
        """决定一条触发：持有它的那次评估；没有评估持有它时，对它单独做一次只过准入的评估。
        outcome: {kind: accept|ignore|stop|unavailable, reason, title?, scope?, acknowledgement?, rules?, name?, note?,
                  ticketId?, audit?, evalId?}"""
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        held = self._holding(trigger_id)
        if outcome.get("evalId") and (held or {}).get("evaluating", {}).get("id") != outcome["evalId"]:
            raise UserError("evalFinished", {"eval": outcome["evalId"]})
        earlier = self._accepted(thread_key(trigger["threads"][0])).get(trigger_id) if outcome["kind"] == "accept" else None
        if earlier:  # 重放一条已经开过工单的触发，不会再开第二张
            others = [i for i in (held or {}).get("evaluating", {}).get("triggerIds", []) if i != trigger_id]
            if held and others:
                self._reassess(held, {"code": "acceptedBefore", "values": {"trigger": trigger_id}}, [trigger_id])
            elif held:
                self._finish(held, held["evaluating"], {"kind": "ignore", "reason": [
                    {"code": "startedTicket", "triggerId": trigger_id, "values": {"ticket": earlier["ticketId"]}}]})
            return earlier
        if outcome["kind"] != "stop":
            self.left_to_workers(trigger)
        thread = held or self.thread(trigger["threads"][0])
        evaluation = (held or {}).get("evaluating") or self._direct_eval(
            thread, trigger, outcome["kind"], outcome.get("ticketId") if outcome["kind"] == "stop" else None)
        if outcome["kind"] == "accept" and evaluation["strength"] != "explicit" and not self.policy().admits_inferred(outcome.get("rules") or []):
            raise UserError("notExplicit")
        return self._decide_eval(thread, evaluation, outcome)

    def reevaluate(self, trigger_id: str) -> bool:
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        if self._holding(trigger_id):
            return False
        self.queue_for_eval(trigger)
        disposition = self.repo.triggers.disposition(trigger_id) or {"fanout": [], "at": self.now()}
        self.repo.triggers.save_disposition(trigger_id, {**disposition, "requeued": True})
        self.emit("trigger.requeued", triggerId=trigger_id)
        return True

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        ticket = self.require(ticket_id)
        if not any(i["kind"] == "trigger" and i["triggerId"] == trigger_id for i in ticket["inputs"]):
            raise UserError("notReached")
        if any(h["ticketId"] == ticket_id for h in (self.repo.triggers.disposition(trigger_id) or {}).get("handbacks") or []):
            raise UserError("handedBack")
        return self.hand_back(ticket_id, self.repo.triggers.get(trigger_id), reason)

    def dismiss(self, decision_id: str) -> dict:
        decision = self.repo.decisions.get(decision_id)
        if not decision or decision["kind"] != "unavailable":
            raise UserError("unknownProblem")
        decision["dismissed"] = True
        self.repo.decisions.save(decision)
        return decision

    def withdraw(self, trigger: dict) -> None:
        """触发被手工路由了：从它线程的队列里拿掉。"""
        thread = self.repo.threads.get(thread_key(trigger["threads"][0]))
        if not thread or trigger["id"] not in thread["pending"]:
            return
        thread["pending"] = [i for i in thread["pending"] if i != trigger["id"]]
        self.repo.threads.save(thread)

    def routed(self, trigger_id: str, ticket_id: str) -> None:
        """触发转给了工单，持有它的评估随之结束；评估里还有别的触发的，其余重新排队。"""
        held = self._holding(trigger_id)
        reason = {"code": "routed", "values": {"ticket": ticket_id}}
        if held and len(held["evaluating"]["triggerIds"]) == 1:
            self._finish(held, held["evaluating"], {"kind": "ignore", "reason": [reason]})
        elif held:
            self._reassess(held, {**reason, "triggerId": trigger_id}, [trigger_id])

    def policy_changed(self, from_: str, to: str) -> None:
        for thread in self.repo.threads.active():
            if thread.get("evaluating"):
                self._reassess(thread, {"code": "policyChanged"})
        self.emit("policy.changed", **{"from": from_, "to": to})

    # ---- 内部

    def _release(self, eval_id: str) -> None:
        release = getattr(self.options.assessor, "release", None)
        if release:
            release(eval_id)

    def _reassess(self, thread: dict, reason: dict, without: list[str] | None = None) -> dict:
        """以未完成结束线程的评估，并把它的触发（除了 without）排到别的触发前面等新的评估。"""
        ids = [i for i in thread["evaluating"]["triggerIds"] if i not in (without or [])]
        decision = self._finish(thread, thread["evaluating"], {"kind": "unavailable", "reason": [reason]})
        current = self.repo.threads.get(thread["key"])
        current["pending"] = [*ids, *[i for i in current["pending"] if i not in ids]]
        self.repo.threads.save(current)
        return decision

    def _holding(self, trigger_id: str) -> dict | None:
        trigger = self.repo.triggers.get(trigger_id)
        thread = self.repo.threads.get(thread_key(trigger["threads"][0])) if trigger else None
        return thread if thread and trigger_id in ((thread.get("evaluating") or {}).get("triggerIds") or []) else None

    def _candidate_of(self, thread: dict) -> dict:
        evaluation = thread["evaluating"]
        triggers = [t for t in (self.repo.triggers.get(i) for i in evaluation["triggerIds"]) if t]
        return {"evalId": evaluation["id"], "thread": thread, "triggers": triggers, "strength": evaluation["strength"],
                "policyVersion": evaluation["policyVersion"], "tickets": self.tickets_on_thread(thread)}

    def _ignore(self, thread: dict, ids: list[str], reason: list[dict], policy, at: str) -> None:
        """记下线程排队的这些触发不开评估及其原因，并把它们移出队列。"""
        evaluation = {"id": key("eval", thread["key"], "ignored", policy.version, at, *ids), "threadKey": thread["key"],
                      "triggerIds": ids, "strength": "inferred", "actor": {"source": thread["ref"]["source"], "id": ""},
                      "basis": {"via": "followup"}, "policyVersion": policy.version, "startedAt": at, "deadline": at}
        self._record(thread, evaluation, {"kind": "ignore", "reason": reason})
        thread["pending"] = [i for i in thread["pending"] if i not in ids]
        thread.pop("expired", None)

    def _accepted(self, key_: str) -> dict[str, dict]:
        """主线程是 key_ 的触发各自开了哪张工单（按接单决定）。"""
        accepted: dict[str, dict] = {}
        for decision in self.repo.decisions.by_thread(key_):
            if decision["kind"] != "accept" or not decision.get("ticketId"):
                continue
            for trigger_id in decision["triggerIds"]:
                accepted.setdefault(trigger_id, decision)
        return accepted

    def _direct_eval(self, thread: dict, trigger: dict, kind: str, ticket_id: str | None = None) -> dict:
        """只过准入的单触发评估：用于不经排队直接作出的决定。立即决定，不影响线程上进行中的评估。"""
        policy = self.policy()
        gate = policy.gate(trigger, thread, self.tickets_on_thread(thread))
        if not gate["admissible"]:
            raise UserError("notAdmissible", {"reason": gate["reason"]})
        at = self.now()
        if trigger["id"] in thread["pending"]:
            thread["pending"] = [i for i in thread["pending"] if i != trigger["id"]]
            self.repo.threads.save(thread)
        eid = key("eval", thread["key"], "direct", trigger["id"], kind, *([ticket_id] if ticket_id else []), at)
        return {"id": eid, "threadKey": thread["key"], "triggerIds": [trigger["id"]], "primary": trigger["id"],
                "strength": gate["strength"], "actor": trigger["actor"], "basis": gate["basis"],
                "policyVersion": policy.version, "startedAt": at, "deadline": at}

    def _decide_eval(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        if self.policy().version != evaluation["policyVersion"]:  # 策略在没有 policy_changed 的情况下变了（升级、重启）
            if (thread.get("evaluating") or {}).get("id") == evaluation["id"]:
                return self._reassess(thread, {"code": "policyChanged"})
            return self._finish(thread, evaluation, {"kind": "unavailable", "reason": [{"code": "policyChanged"}], **audit})
        kind = outcome["kind"]
        if kind in ("ignore", "unavailable"):
            unassessed = [{"code": "ignoredFromCard"}] if kind == "ignore" and evaluation["strength"] != "explicit" and not outcome.get("audit") else []
            return self._finish(thread, evaluation, {"kind": kind, "reason": [*unassessed, *reason_of(outcome["reason"])], **audit})
        if kind == "stop":
            ticket = self.repo.tickets.get(outcome["ticketId"])
            if not ticket or not live_ticket(ticket):
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopUnknown"}], **audit})
            if evaluation["strength"] != "explicit":
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopNotExplicit"}], **audit})
            decision = self._finish(thread, evaluation, {"kind": "stop", "reason": reason_of(outcome["reason"]), "ticketId": ticket["id"], **audit})
            self.tickets.end(ticket["id"], "human", "stopped", self.now(), outcome["reason"] if isinstance(outcome["reason"], str) else None)
            return decision
        return self._accept(thread, evaluation, outcome)

    def _accept(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        at = self.now()
        basis = {**evaluation["basis"], **({"rules": outcome["rules"]} if outcome.get("rules") else {})}
        mandate = {"strength": evaluation["strength"], "policyVersion": evaluation["policyVersion"], "basis": basis,
                   "actor": evaluation["actor"], "scope": outcome.get("scope") or ""}
        ticket_id = key("ticket", evaluation["id"])
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        decision = self._finish(thread, evaluation, {"kind": "accept", "reason": reason_of(outcome["reason"]), **audit,
                                                     "mandate": mandate, "ticketId": ticket_id})
        self.tickets.create(ticket_id, decision["id"], mandate, evaluation, outcome, at)
        return decision

    def _finish(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        """记录评估的决定；线程自己的评估随之结束，只过准入的那种不影响线程上进行中的评估。"""
        decision = self._record(thread, evaluation, outcome)
        if (thread.get("evaluating") or {}).get("id") != evaluation["id"]:
            return decision
        thread.pop("evaluating", None)
        thread.pop("expired", None)
        self.repo.threads.save(thread)
        self._release(evaluation["id"])
        return decision

    def _record(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        at = self.now()
        decision = {"id": key("decision", evaluation["id"]), "evalId": evaluation["id"], "threadKey": thread["key"],
                    "triggerIds": evaluation["triggerIds"], "policyVersion": evaluation["policyVersion"], "at": at,
                    "kind": outcome["kind"], "reason": outcome["reason"], "strength": evaluation["strength"]}
        for field in ("ticketId", "mandate", "audit"):
            if outcome.get(field):
                decision[field] = outcome[field]
        self.repo.decisions.save(decision)
        for trigger_id in evaluation["triggerIds"]:
            disposition = self.repo.triggers.disposition(trigger_id) or {"fanout": [], "at": at}
            self.repo.triggers.save_disposition(trigger_id, {**disposition, "evalId": evaluation["id"], "decisionId": decision["id"]})
        self.emit("decision.recorded", decisionId=decision["id"], kind=decision["kind"], threadKey=thread["key"])
        return decision
