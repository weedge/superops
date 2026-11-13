"""决定、被过滤的触发、结束的派工“为什么”：原因码 + 模板。准入和来源也可以给出自己的原因码。"""
from __future__ import annotations

import hashlib
import json
import re

REASONS = {
    "said": "{text}",
    "timeout": "研判超时",
    "policyChanged": "研判期间策略变了",
    "noAdmissible": "没有准入的触发",
    "startedTicket": "已经开过工单 {ticket}",
    "acceptedBefore": "触发 {trigger} 之前已被接单",
    "routed": "已转给工单 {ticket}",
    "humanStop": "人要求停止",
    "retried": "人工重试",
    "hostRestarted": "superops 重启",
    "stopUnknown": "要停的工单不存在或已结束",
    "stopNotExplicit": "停止需要名单内的人明确提出",
    "directionUnsettled": "没有确立要做的意图或目标方向，或者还需要产品 / 设计决定",
    "noRuleMatched": "没有命中已启用的规则",
    "ignoredFromCard": "直接从卡片忽略，没有按规则研判",
    "observed": "观察模式：本来会接单（{text}）",
    "ruleMatched": "事件直接命中规则「{rule}」",
    "continuesTicket": "同一线程上规则「{rule}」的工单 {ticket} 还在进行，事件已送达它",
    "maxRounds": "规则「{rule}」在这个线程上已到 {rounds} 轮上限",
    "overBudget": "规则「{rule}」会超出今日 {budget} 预算",
    "rulePaused": "规则「{rule}」已暂停",
    "recordOnly": "规则「{rule}」只留底，不开工单",
    "assessFailed": "研判没有完成：{text}",
    # 准入给出的原因码
    "not_human": "不是人发的，也不在来源的机器名单上",
    "not_a_member": "发送人不在来源的名单上",
    "admission_ignored": "准入表设为忽略",
    "no_mention": "没有 @ 机器人",
    # 来源给出的过滤原因
    "needs_approval": "这类事件要人批准后才能发，Agent 自己发的不收",
    "origin_paused": "这个事件来源已暂停",
    "source_off": "来源没有启用",
    "chat_not_listened": "不是监听的群",
}


def key(*values: str) -> str:
    """由内容导出的稳定标识：输入相同，标识相同。"""
    return hashlib.sha256(json.dumps(list(values), ensure_ascii=False).encode()).hexdigest()[:32]


def said(text: str) -> dict:
    return {"code": "said", "values": {"text": text}}


def reason_of(reason) -> list[dict]:
    """结果里的原因：带原因码的直接用，否则是研判方或人写的话。"""
    return [said(reason) if isinstance(reason, str) else reason]


def reason_text(reasons) -> str:
    """把原因列表写成一句中文（批量里的某条触发会带上它的 id）。"""
    if not reasons:
        return ""
    items = reasons if isinstance(reasons, list) else [reasons]
    out = []
    for r in items:
        template = REASONS.get(r["code"], r["code"])
        text = re.sub(r"\{(\w+)\}", lambda m: str((r.get("values") or {}).get(m.group(1), m.group(0))), template)
        out.append(f"{r['triggerId']}: {text}" if r.get("triggerId") else text)
    return "；".join(out)
