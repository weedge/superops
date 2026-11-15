"""Assessor：引擎的研判端口。评估的第二层（第一层是确定性的准入 gate）。

两条路：
- 规则直配：系统事件（定时、流水线）命中了写明 `when` 的规则，就不需要模型来“判断”——规则本身就是团队写下的授权。
  这里只做确定性的检查：规则是否暂停、同线程是否已有这条规则的工单在跑、是否到了轮数上限、是否超预算、接单模式。
- 主 Session：人的明确请求（接单 / 转给已有工单 / 停止 / 忽略），以及没有 `when` 的语义规则的研判，交给模型。
  主 Session 只路由，不做技术决定、不改写请求。

observe 模式下，本来会接的主动候选记为 ignore 并标注“本来会接单”，什么都不启动：先看一阵记录，再切到 auto。
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..infra import paths
from ..infra.instance import current as current_instance
from ..infra.timeutil import TZ
from ..core.errors import UserError
from ..core.model import live_ticket
from . import prompts
from .settings import matching_rules, trigger_event

log = logging.getLogger("superops.assessor")


def semantic_rules(settings: dict) -> list[dict]:
    """需要主 Session 研判的规则：启用、没有 when、且没有被单独暂停。"""
    paused = settings.get("paused_keys") or {}
    return [r for r in settings["policy"]["rules"] if r["enabled"] and not r.get("when") and f"rule:{r['id']}" not in paused]


class Assessor:
    def __init__(self, rt):
        self.rt = rt
        rt.effects.handle("assessment", self._assess, limit=lambda: 0 if rt.settings.current()["paused"] else 4, retry=(2.0, 60.0))

    # ---- 引擎端口

    def request(self, candidate: dict) -> None:
        self.rt.effects.enqueue("assessment", candidate["evalId"], {"triggerId": candidate["triggers"][-1]["id"]})

    def release(self, eval_id: str) -> None:
        self.rt.effects.cancel("assessment", eval_id)

    # ---- 处理

    def _assess(self, payload: dict, eval_id: str) -> None:
        rt = self.rt
        candidate = rt.engine.candidate(payload["triggerId"])
        if not candidate or candidate["evalId"] != eval_id:
            return
        rt.engine.candidate_delivered(eval_id)
        outcome = self.direct(candidate)
        if outcome is None:
            self.route(candidate)
            return
        notice = outcome.pop("_notice", None)
        attach_to = outcome.pop("_attach", None)
        try:
            rt.engine.decide(payload["triggerId"], {**outcome, "evalId": eval_id})
        except UserError as error:
            log.info("评估 %s 的直配决定被拒绝：%s", eval_id[:8], error)
            return
        for trig in candidate["triggers"] if attach_to else []:
            try:
                rt.engine.attach(attach_to, trig["id"])
            except UserError as error:
                log.info("触发 %s 没能交给进行中的工单：%s", trig["id"][:8], error)
        if notice:
            rt.notify(notice["kind"], notice["title"], notice["body"], priority=notice["priority"], ref=f"record:{payload['triggerId']}", dedup=True)

    def direct(self, candidate: dict) -> dict | None:
        """确定性的研判结果；需要模型时返回 None。"""
        rt = self.rt
        settings = rt.settings.current()
        machine = sorted((t for t in candidate["triggers"] if not t["human"]), key=lambda t: t["at"])
        approved = [t for t in machine if (t.get("meta") or {}).get("approved")]
        if candidate["strength"] == "explicit" and not approved:
            return None
        semantic = semantic_rules(settings)
        if not machine:
            return None if semantic else {"kind": "ignore", "reason": {"code": "noRuleMatched"}, "audit": {"direct": True}}
        trigger = approved[-1] if approved else machine[-1]
        type_, origin, subject = trigger_event(trigger)
        rules = matching_rules(settings, trigger)
        if not rules:
            if semantic and len(machine) < len(candidate["triggers"]):
                return None  # 批里还有人的消息，交给主 Session
            return {"kind": "ignore", "reason": {"code": "noRuleMatched"}, "audit": {"direct": True, "event": type_}}
        rule = rules[0]
        audit = {"direct": True, "event": type_, "origin": origin, "matches": [{"rule_id": rule["id"], "reason": "事件类型直接命中规则的 when"}]}
        named = {"rule": rule["name"]}
        if rt.settings.is_paused(f"rule:{rule['id']}"):
            return {"kind": "ignore", "reason": {"code": "rulePaused", "values": named}, "audit": audit}
        if rule["run"]["executor"] == "none":
            out = {"kind": "ignore", "reason": {"code": "recordOnly", "values": named}, "audit": audit}
            if rule["tell"] == "always":
                out["_notice"] = {"kind": "alert" if rule["priority"] == 0 else "message", "title": f"{rule['name']}：{trigger.get('text') or type_}",
                                  "body": f"**{type_}** · {origin}\n\n{trigger.get('text') or ''}"
                                          + (f"\n\n对象：`{subject}`" if subject else "") + (f"\n\n链接：{trigger['url']}" if trigger.get("url") else ""),
                                  "priority": rule["priority"]}
            return out
        same = [t for t in candidate["tickets"] if rule["id"] in ((t["mandate"].get("basis") or {}).get("rules") or [])]
        owning = {rule["id"], *rule["unless_live"]}
        live = [t for t in candidate["tickets"] if live_ticket(t) and owning & set((t["mandate"].get("basis") or {}).get("rules") or [])]
        if live:
            # 不只是记一句“已送达”：触发到达时这张工单可能还没开出来（同一批事件晚到几毫秒），扇出时没送到它，
            # 这里把触发真正交给它（2026-11-12 竞品评审批的 10 条需求，晚到的 2 条就这样丢了）
            return {"kind": "ignore", "reason": {"code": "continuesTicket", "values": {**named, "ticket": f"T{live[0].get('no', '?')}"}},
                    "audit": audit, "_attach": live[0]["id"]}
        if rule.get("max_rounds") and len(same) >= int(rule["max_rounds"]):
            return {"kind": "ignore", "reason": {"code": "maxRounds", "values": {**named, "rounds": str(rule["max_rounds"])}}, "audit": audit}
        if rule.get("credits_per_run"):
            inst = current_instance()
            budget = float((rt.cfg.get("budgets") or {}).get(inst.credits_counter, 0) or 0)
            if budget and rt.counter(inst.credits_counter) + float(rule["credits_per_run"]) > budget:
                return {"kind": "ignore", "reason": {"code": "overBudget", "values": {**named, "budget": inst.credits_label}}, "audit": audit}
        title = rule["name"] if not subject or subject == type_ else f"{rule['name']} · {subject}"
        if settings["eval"]["mode"] != "auto" and not approved:  # 人批准的动作不受接单模式限制
            return {"kind": "ignore", "reason": {"code": "observed", "values": {"text": rule["name"]}},
                    "audit": {**audit, "would_accept": True, "proposal": {"title": title, "rule": rule["id"]}}}
        return {"kind": "accept", "title": title[:80], "name": rule["id"], "scope": rule["instruction"],
                "reason": {"code": "ruleMatched", "values": named}, "rules": [rule["id"]], "audit": audit}

    # ---- 主 Session

    def route(self, candidate: dict) -> None:
        rt = self.rt
        settings = rt.settings.current()
        trigger_ids = [t["id"] for t in candidate["triggers"]]
        handbacks = {i: (rt.store.triggers.disposition(i) or {}).get("handbacks") or [] for i in trigger_ids}
        related = sorted(candidate["tickets"], key=lambda t: (not live_ticket(t), t["createdAt"]))[:8]
        card = prompts.candidate_card(candidate, related=related, owners={}, handbacks=handbacks)
        open_tickets = [t for t in rt.store.tickets.open() if t["mandate"]["strength"] == "explicit" or t.get("attention")]
        run_dir = paths.ops_data() / "work" / "_router" / datetime.now(TZ).strftime("%Y%m%d") / candidate["evalId"][:12]

        def check(out: dict) -> list[str]:
            unknown = [d["trigger_id"] for d in out["decisions"] if d["trigger_id"] not in trigger_ids]
            return [f"decisions 里有不属于这个候选的 trigger_id：{unknown}"] if unknown else ([] if out["decisions"] else ["decisions 不能为空"])

        outcome = rt.agents.run(
            role=settings["eval"]["role"], schema=prompts.ROUTE_SCHEMA, run_dir=run_dir, tools="readonly", effort=settings["eval"].get("effort"),
            prompt=lambda agent, errors, fallback: prompts.router_turn(rt.home, settings=settings, cards=[card], open_tickets=open_tickets, errors=errors),
            timeout=max(60.0, settings["eval"]["timeout_seconds"] - 30.0), priority=1, check=check)
        primary = candidate["triggers"][-1]["id"]
        if not outcome.ok:
            self._decide(primary, {"kind": "unavailable", "reason": {"code": "assessFailed", "values": {"text": outcome.error_detail[:200]}},
                                   "evalId": candidate["evalId"]})
            return
        decision = outcome.output["decisions"][0]  # 一个候选一个决定：它决定整批
        self.apply(candidate, decision)

    def apply(self, candidate: dict, decision: dict) -> dict | None:
        """把主 Session 的一条决定落到引擎上；被引擎拒绝的记为未完成，等下一条触发或人来重新评估。"""
        rt = self.rt
        trigger_id, kind, eval_id = decision["trigger_id"], decision["kind"], candidate["evalId"]
        reason = decision.get("reason") or "（没有写原因）"
        try:
            if kind == "attach":
                return rt.engine.attach(decision["ticket_id"], trigger_id, decision.get("note"))
            if kind == "stop":
                return rt.engine.decide(trigger_id, {"kind": "stop", "ticketId": decision.get("ticket_id") or "", "reason": reason, "evalId": eval_id})
            if kind in ("ignore", "unavailable"):
                audit = {"rule_assessments": decision["rule_assessments"]} if decision.get("rule_assessments") else None
                return rt.engine.decide(trigger_id, {"kind": kind, "reason": reason, "evalId": eval_id, **({"audit": audit} if audit else {})})
            outcome = {"kind": "accept", "title": (decision.get("title") or reason)[:80], "scope": decision.get("scope") or "", "reason": reason,
                       "evalId": eval_id}
            for src, dst in (("name", "name"), ("note", "note"), ("acknowledgement", "acknowledgement")):
                if decision.get(src):
                    outcome[dst] = decision[src]
            if candidate["strength"] != "explicit":
                outcome = self._inferred(decision, outcome)
            return rt.engine.decide(trigger_id, outcome)
        except UserError as error:
            log.info("主 Session 对 %s 的决定被拒绝：%s", trigger_id[:8], error)
            return self._decide(trigger_id, {"kind": "unavailable", "evalId": eval_id,
                                             "reason": {"code": "assessFailed", "values": {"text": f"{kind} 被拒绝：{error}"[:200]}}})

    def _inferred(self, decision: dict, outcome: dict) -> dict:
        """主动候选的接单：每条已启用的语义规则恰好判一次，至少命中一条，方向已定；不在 auto 模式只记录。"""
        settings = self.rt.settings.current()
        semantic = [r["id"] for r in semantic_rules(settings)]
        judged = decision.get("rule_assessments") or []
        ids = [j["rule_id"] for j in judged]
        audit = {"rule_assessments": judged, "direction": decision.get("direction"),
                 "matches": [{"rule_id": j["rule_id"], "reason": j["reason"]} for j in judged if j["matched"]]}
        base = {"evalId": outcome["evalId"], "audit": audit}
        if sorted(ids) != sorted(semantic):
            return {**base, "kind": "unavailable", "reason": {"code": "assessFailed", "values": {"text": "rule_assessments 必须每条已启用的语义规则恰好一项"}}}
        direction = decision.get("direction") or {}
        if not (direction.get("requested") and direction.get("settled")):
            return {**base, "kind": "ignore", "reason": {"code": "directionUnsettled"}}
        if not audit["matches"]:
            return {**base, "kind": "ignore", "reason": {"code": "noRuleMatched"}}
        if settings["eval"]["mode"] != "auto":
            audit.update({"would_accept": True, "proposal": {"title": outcome["title"], "scope": outcome["scope"]}})
            return {**base, "kind": "ignore", "reason": {"code": "observed", "values": {"text": outcome["reason"]}}}
        return {**outcome, "rules": [m["rule_id"] for m in audit["matches"]], "audit": audit}

    def _decide(self, trigger_id: str, outcome: dict) -> dict | None:
        try:
            return self.rt.engine.decide(trigger_id, outcome)
        except UserError as error:
            log.info("记录研判结果失败：%s", error)
            return None
