#!/usr/bin/env python3
"""假 Agent：从 stdin 读提示词，按里面的记号输出 JSON。测试用（agents.yaml 里 type: command）。

主 Session（提示词里有“待决定的候选”）：候选正文含「闲聊」→ ignore；含「转给 <ticket_id>」→ attach；含「停掉 <ticket_id>」→ stop；否则 accept。
执行器：按本轮输入里的记号——MOCK_ASK 提一个带选项的问题；MOCK_EMIT:<type>:<subject> 发事件；MOCK_BLOCK 报受阻；
MOCK_WAIT 报 waiting；MOCK_HANDBACK 把本轮人的消息交还；收到“人对你的问题作了决定”就 ready；默认 ready。
"""
import json
import os
import re
import sys

prompt = sys.stdin.read()
name = os.environ.get("MOCK_NAME", "mock")
log = os.environ.get("MOCK_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"agent": name, "prompt": prompt}, ensure_ascii=False) + "\n")


def tail(marker):
    return prompt.split(marker, 1)[1] if marker in prompt else ""


if "## 待决定的候选" in prompt:
    body = tail("## 待决定的候选").split("## 输出")[0]
    trigger_id = re.search(r"trigger_id: `([^`]+)`", body).group(1)
    decision = {"trigger_id": trigger_id, "kind": "accept", "reason": "名单内的人明确提出", "title": "假工单", "name": "mock-ticket",
                "scope": "按消息所说", "acknowledgement": "收到，这就处理"}
    m = re.search(r"转给 ([0-9a-f]{32})", body)
    s = re.search(r"停掉 ([0-9a-f]{32})", body)
    if "闲聊" in body:
        decision = {"trigger_id": trigger_id, "kind": "ignore", "reason": "普通讨论"}
    elif m:
        decision = {"trigger_id": trigger_id, "kind": "attach", "reason": "明确指向这张工单", "ticket_id": m.group(1), "note": "接着做"}
    elif s:
        decision = {"trigger_id": trigger_id, "kind": "stop", "reason": "人要求停止", "ticket_id": s.group(1)}
    print(json.dumps({"decisions": [decision]}, ensure_ascii=False))
    sys.exit(0)

inputs = tail("## 本轮输入").split("## 输出")[0]
first = "## 这张工单到目前为止" not in prompt          # 之后的各轮只看本轮输入，不再看起点
scope = inputs + tail("## 起点").split("## ")[0] if first else inputs
out = {"state": "ready", "summary": f"{name} 做完了"}
human = re.findall(r"trigger_id: `([^`]+)` · \S+ · 人", inputs)
if "人对你的问题作了决定" in inputs:
    out = {"state": "ready", "summary": f"{name} 按人的决定收尾", "message": "已按你的选择处理"}
elif "MOCK_HANDBACK" in inputs and human:
    out = {"state": "waiting", "summary": "这条不归我", "handback": [{"trigger_id": human[-1], "reason": "超出这张工单的范围"}]}
elif "MOCK_ASK" in scope:
    out = {"state": "waiting", "summary": "要人拍板", "question": {"text": "今晚发这条吗？", "card": {
        "title": "今晚发这条吗？", "proposal": "建议发布", "reasons": ["数据不错"],
        "options": [{"key": "approve", "label": "发", "emit": [{"type": "content.publish.approved", "subject": "content:c1", "data": {"pack": "pk1"}}]},
                    {"key": "reject", "label": "不发", "require_reason": True, "record": "not_doing"}],
        "on_expire": "reject"}}}
elif "MOCK_BLOCK" in scope:
    out = {"state": "blocked", "summary": "缺少素材，做不下去"}
elif "MOCK_WAIT" in scope:
    out = {"state": "waiting", "summary": "等后续事件"}
events = [{"type": t, "subject": s, "summary": f"{name} 发的 {t}"} for t, s in re.findall(r"MOCK_EMIT:([\w.]+):([\w:]+)", scope)]
if events:
    out["events"] = events
    if "MOCK_EMIT_THEN_WAIT" in scope:
        out["state"] = "waiting"
if "MOCK_SECRET" in scope:
    out = {"state": "ready", "summary": "登录手机号 13812345678，Authorization: Bearer sk-abcdef1234567890abcdef"}
asked = re.findall(r"trigger_id: `([^`]+)` · \S+ · 人", scope)
if "MOCK_ANSWER" in scope and asked:
    out = {"state": "ready", "summary": "答了", "answer": {"trigger_id": asked[-1], "text": "一共 2 条"}}
if human and out["state"] != "blocked" and "handback" not in out and "MOCK_ACK" in inputs:
    out["ack"] = [{"trigger_id": human[-1], "text": "收到"}]
print(json.dumps(out, ensure_ascii=False))
