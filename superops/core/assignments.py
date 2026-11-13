"""派工：执行器和它的 worker 报告的事实，以及人对工单问题的回答。"""
from __future__ import annotations

from .errors import UserError
from .model import latest_assignment, live_ticket, running_assignment
from .part import EnginePart
from .reasons import key
from .tickets import automatic

RECOVERY_NOTE = ("宿主在这张工单执行期间重启过。继续之前先检查当前工作目录、已产出的文件和外部状态（已发的事件、MR 等），"
                 "不要重复已经完成的对外操作。完成原来的派工并报告状态。")
SETTLED = ("waiting", "ready", "blocked", "failed")


class Assignments(EnginePart):
    """记录：派工开始、输入送达或送达失败、worker 取走了输入、一轮结束、宿主重启打断、worker 的报告及其问题。"""

    def __init__(self, options, tickets, replies):
        super().__init__(options)
        self.tickets = tickets
        self.replies = replies

    def started(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self.require(ticket_id)
        assignment = next((a for a in ticket["assignments"] if a["id"] == assignment_id), None)
        if not assignment or assignment["state"] == "ended":
            return
        assignment["state"] = "running"
        assignment.setdefault("startedAt", self.now())
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        if live_ticket(ticket) and ticket["undelivered"]:
            self.options.executor.deliver(ticket, assignment)
        self.emit("assignment.started", ticketId=ticket_id, assignmentId=assignment_id)

    def delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        ticket["undelivered"] = [i for i in ticket["undelivered"] if i not in input_ids]
        ticket["updatedAt"] = self.now()
        running_assignment(ticket)["deliveredAt"] = ticket["updatedAt"]
        for item in ticket["inputs"]:
            if item["kind"] == "trigger" and item["id"] in input_ids:
                item.setdefault("readAt", None)
                item["deliveredTo"] = assignment_id
        ticket.pop("delivery", None)
        self.repo.tickets.save(ticket)

    def read(self, ticket_id: str, input_ids: list[str], at: str | None = None) -> None:
        """worker 的会话把这些输入取进了一步，模型从这里读到它们。at 是取走的时刻（一轮开始时），缺省为现在。"""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        for item in ticket["inputs"]:
            if item["kind"] == "trigger" and item["id"] in input_ids and not item.get("readAt"):
                item["readAt"] = at or self.now()
        self.repo.tickets.save(ticket)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        running_assignment(ticket)["idleAt"] = self.now()
        self.repo.tickets.save(ticket)

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        ticket["delivery"] = {"error": error[:300], "at": self.now()}
        self.repo.tickets.save(ticket)

    def interrupted(self, ticket_id: str, assignment_id: str) -> None:
        """宿主重启时还在跑的派工：最近报告是 waiting / ready / blocked / failed 且之后没有输入送达的保持原样，
        其余的带一条恢复说明重新开始。"""
        ticket = self._running(ticket_id, assignment_id)
        running = running_assignment(ticket) if ticket else None
        if not running:
            return
        report = running.get("report")
        busy = bool(running.get("deliveredAt")) and bool(report) and running["deliveredAt"] >= report["at"]
        if not (report and report["state"] in SETTLED and not busy):
            self.tickets.restart(ticket, running, "hostRestarted", RECOVERY_NOTE)
        elif ticket["undelivered"]:
            self.options.executor.deliver(ticket, running)

    def report(self, ticket_id: str, report: dict) -> dict:
        """记录未结束工单当前派工的一次报告。
        report: {state, summary, question?: str|None, askedBy?, answers?, card?, expiresAt?}
        question 是字符串就提问，None 就清掉，缺省保持原样；ready 报告也会清掉问题。
        card 是随问题带的选项卡片（options 里写好人选中后执行的动作）。
        blocked / failed 会压住自动输入直到有人的消息。dropped 结束工单；ready 在没有跟随线程时以 completed 结束，
        否则等 follows_done。"""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        question = report.get("question")
        if question and (report["state"] == "ready" or report.get("answers")):
            raise UserError("questionWithResult")
        if report.get("askedBy") and not question:
            raise UserError("askerWithoutQuestion")

        def person(trigger_id: str, channel: bool) -> bool:
            trigger = self.repo.triggers.get(trigger_id)
            return bool(trigger) and trigger["human"] and (not channel or bool(trigger.get("channel"))) and self.reaches(ticket, trigger, False)

        if report.get("askedBy") and not person(report["askedBy"], True):
            raise UserError("notAsker")
        if report.get("answers") and not person(report["answers"], False):
            raise UserError("notAnswerable")
        assignment = running_assignment(ticket) or latest_assignment(ticket)
        if not assignment:
            raise UserError("unknownAssignment")
        if assignment["state"] == "ended":
            raise UserError("assignmentEnded")
        at = self.now()
        previous = ticket.get("attention")
        asked = previous
        if question and question != (previous or {}).get("question"):
            asked = {"id": key("attention", ticket_id, question, at), "question": question, "at": at}
            if report.get("askedBy"):
                asked["triggerId"] = report["askedBy"]
            if report.get("card"):
                asked["card"] = report["card"]
            if report.get("expiresAt"):
                asked["expiresAt"] = report["expiresAt"]
        cleared = "question" in report and question is None
        attention = None if cleared or report["state"] == "ready" else asked
        if attention:
            ticket["attention"] = attention
        else:
            ticket.pop("attention", None)
        if previous and previous["id"] != (attention or {}).get("id"):
            self.replies.withdraw_attention(previous["id"])
        assignment["report"] = {"state": report["state"], "summary": report["summary"], "at": at}
        ticket["updatedAt"] = at
        if report.get("answers"):
            if report["answers"] not in (ticket.get("answers") or []):
                ticket["answers"] = [*(ticket.get("answers") or []), report["answers"]]
            self.take_up(ticket_id, report["answers"])
        if report["state"] in ("blocked", "failed"):
            by_id = {i["id"]: i for i in ticket["inputs"]}
            ticket["undelivered"] = [i for i in ticket["undelivered"] if not automatic(by_id.get(i))]
        self.repo.tickets.save(ticket)
        if attention and attention["id"] != (previous or {}).get("id"):
            self.emit("attention.asked", ticketId=ticket_id, attentionId=attention["id"])
        # 跟随线程的工单随线程结束：ready 只有在 follows_done 成立时才结束它，并沿用那个结果
        done = self.tickets.follows_done(ticket)
        ready = "completed" if not ticket.get("follows") else (done or {}).get("outcome")
        if report["state"] == "ready" and ticket["undelivered"] and live_ticket(ticket):
            # 这一轮跑的时候又送来了新输入（执行器还没看到）：先不结束，再跑一轮处理它们，处理完的 ready 才结束
            self.options.executor.deliver(ticket, assignment)
            return self.require(ticket_id)
        outcome = None
        if report["state"] == "dropped":
            outcome = (done or {}).get("outcome") or "dropped"
        elif report["state"] == "ready":
            outcome = ready
        if outcome:
            self.tickets.end(ticket_id, "executor", outcome, at)
        return self.require(ticket_id)

    def answer(self, ticket_id: str, option: str | int, *, trigger_id: str | None = None, by: str | None = None,
               reason: str | None = None, expired: bool = False) -> dict:
        """人回答了工单当前的问题（批红）：记下选了哪个选项，清掉问题，把结果作为输入交给执行器，
        并发出 attention.answered，由应用层去执行选项里写好的动作。
        带 trigger_id 时，那条消息必须是人写的、到过这张工单、且发送人有审批权限；
        不带时是宿主自己的命令（面板 / 终端里的操作者，或到期按安全选项处理）。"""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        attention = ticket.get("attention")
        options = ((attention or {}).get("card") or {}).get("options") or []
        if not attention or not options:
            raise UserError("notAttention")
        chosen = None
        if isinstance(option, int) or (isinstance(option, str) and option.isdigit()):
            index = int(option) - 1
            chosen = options[index] if 0 <= index < len(options) else None
        if chosen is None:
            chosen = next((o for o in options if o.get("key") == option), None)
        if chosen is None:
            raise UserError("unknownOption", {"option": str(option)})
        if trigger_id:
            trigger = self.repo.triggers.get(trigger_id)
            if not trigger:
                raise UserError("unknownTrigger")
            who = by or trigger["actor"].get("id") or "?"
            if not trigger["human"] or not self.reaches(ticket, trigger, False) or not self.policy().may_answer(trigger, ticket):
                raise UserError("notApprover", {"by": who})
            by = who
        if chosen.get("require_reason") and not (reason or "").strip():
            raise UserError("reasonRequired", {"option": chosen.get("label") or chosen.get("key")})
        at = self.now()
        record = {"attentionId": attention["id"], "question": attention["question"], "option": chosen.get("key"),
                  "label": chosen.get("label"), "by": by or "operator", "at": at}
        if reason:
            record["reason"] = reason
        if trigger_id:
            record["triggerId"] = trigger_id
            self.take_up(ticket_id, trigger_id)
        if expired:
            record["expired"] = True
        ticket["answered"] = [*(ticket.get("answered") or []), record]
        ticket.pop("attention", None)
        ticket["updatedAt"] = at
        self.repo.tickets.save(ticket)
        self.replies.withdraw_attention(attention["id"])
        self.tickets.add_input(self.require(ticket_id), {"id": key("input", ticket_id, "answer", attention["id"]), "kind": "answer", "at": at,
                                                         **{k: record[k] for k in ("attentionId", "question", "option", "label", "by")},
                                                         **({"reason": reason} if reason else {}), **({"expired": True} if expired else {})})
        self.emit("attention.answered", ticketId=ticket_id, attentionId=attention["id"], option=chosen, by=record["by"],
                  reason=reason, triggerId=trigger_id, expired=expired)
        return chosen

    def _running(self, ticket_id: str, assignment_id: str) -> dict | None:
        """工单，前提是它未结束且正由这次派工执行。"""
        ticket = self.require(ticket_id)
        return ticket if live_ticket(ticket) and (running_assignment(ticket) or {}).get("id") == assignment_id else None
