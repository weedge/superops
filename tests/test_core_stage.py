"""原型 tests/stage.spec.ts 的移植：工单阶段（ticket_stage）和能否重试（can_retry）。

表格按 docs/003 的行序；一张工单命中多行时取第一行。
"""
from __future__ import annotations

import pytest

from superops.core import can_retry, ticket_stage


def assignment(**overrides) -> dict:
    return {"id": "a", "state": "running", "createdAt": "", **overrides}


def reported(report_state: str, /, **overrides) -> dict:
    """报告了 report_state 的派工；overrides 里的 state 是派工自己的状态。"""
    return assignment(**{"report": {"state": report_state, "summary": "", "at": ""}, **overrides})


def ticket(**overrides) -> dict:
    return {"id": "t", "title": "x", "decisionId": "d",
            "mandate": {"strength": "explicit", "policyVersion": "v", "basis": {"via": "addressed", "addressed": "mention"},
                        "actor": {"source": "feishu", "id": "ou"}, "scope": ""},
            "channels": [], "assignments": [assignment()], "inputs": [], "undelivered": [], "createdAt": "", "updatedAt": "",
            "origin": {"triggerId": "x", "triggerIds": ["x"]}, **overrides}


ATTENTION = {"id": "q", "question": "?"}
DELIVERY = {"error": "offline", "at": ""}
# 人的一条消息，在报告之后到了工单
ANSWERED = {"inputs": [{"id": "i", "kind": "trigger", "triggerId": "e", "at": "later", "human": True}]}
ENDED = {"at": "", "by": "executor", "reason": "closed", "outcome": "closed"}


def reported_at(state: str) -> dict:
    return reported(state, report={"state": state, "summary": "", "at": "earlier"})


STAGES = [
    ("有输入送达失败", ticket(delivery=DELIVERY), {"stage": "person", "action": "delivery"}),
    ("送达失败先于待答和待审", ticket(delivery=DELIVERY, attention=ATTENTION, assignments=[reported("ready")]),
     {"stage": "person", "action": "delivery"}),
    ("有问题待人回答", ticket(attention=ATTENTION), {"stage": "person", "action": "answer"}),
    ("待答先于待审", ticket(attention=ATTENTION, assignments=[reported("ready")]), {"stage": "person", "action": "answer"}),
    ("待答先于失败", ticket(attention=ATTENTION, assignments=[reported("failed")]), {"stage": "person", "action": "answer"}),
    ("就绪报告", ticket(assignments=[reported("ready")]), {"stage": "person", "action": "review"}),
    ("已结束派工的就绪报告", ticket(assignments=[reported("ready", state="ended")]), {"stage": "person", "action": "review"}),
    ("受阻报告", ticket(assignments=[reported("blocked")]), {"stage": "person", "action": "blocked"}),
    ("失败报告", ticket(assignments=[reported("failed")]), {"stage": "person", "action": "failed"}),
    ("受阻报告之后人已经回复", ticket(**ANSWERED, assignments=[reported_at("blocked")]), {"stage": "working"}),
    ("没有派工", ticket(assignments=[]), {"stage": "working", "action": "queued"}),
    ("派工还没启动", ticket(assignments=[reported("failed", state="ended"), assignment(state="pending")]),
     {"stage": "working", "action": "queued"}),
    ("等待报告", ticket(assignments=[reported("waiting")]), {"stage": "idle"}),
    ("派工已结束", ticket(assignments=[assignment(state="ended")]), {"stage": "idle"}),
    ("派工在运行", ticket(), {"stage": "working"}),
    ("进展报告", ticket(assignments=[reported("progress")]), {"stage": "working"}),
    ("工单已结束", ticket(ended=ENDED, attention=ATTENTION, assignments=[reported("failed")]), {"stage": "ended"}),
]


# spec:16
@pytest.mark.parametrize("value, stage", [(value, stage) for _, value, stage in STAGES], ids=[name for name, _, _ in STAGES])
def test_工单阶段(value, stage):
    assert ticket_stage(value) == stage


RETRIES = [
    ("失败的未结束工单", True, ticket(assignments=[reported("failed")])),
    ("受阻的未结束工单", True, ticket(assignments=[reported("blocked")])),
    ("失败且还有问题待答的工单", True, ticket(attention=ATTENTION, assignments=[reported("failed")])),
    ("受阻且输入送达失败的工单", True, ticket(delivery=DELIVERY, assignments=[reported("blocked")])),
    ("失败后已结束的工单", False, ticket(ended=ENDED, assignments=[reported("failed")])),
    ("已经重试过_新派工还没启动的工单", False, ticket(assignments=[reported("failed", state="ended"), assignment(state="pending")])),
    ("就绪的工单", False, ticket(assignments=[reported("ready")])),
    ("在运行的工单", False, ticket()),
    ("没有派工的工单", False, ticket(assignments=[])),
    # worker 已经拿到人的消息（阶段也这么说），重试只会打断它
    ("失败之后人已经回复的工单", False, ticket(**ANSWERED, assignments=[reported_at("failed")])),
    # 除非那条消息送达失败：新派工重新启动 worker 并接走消息
    ("失败之后人的回复送达失败的工单", True, ticket(**ANSWERED, delivery=DELIVERY, assignments=[reported_at("failed")])),
    ("在工作但输入送不到的工单", True, ticket(delivery=DELIVERY)),
    ("已结束且输入送不到的工单", False, ticket(ended=ENDED, delivery=DELIVERY)),
]


# spec:38
@pytest.mark.parametrize("expected, value", [(expected, value) for _, expected, value in RETRIES], ids=[name for name, _, _ in RETRIES])
def test_能否重试(expected, value):
    assert can_retry(value) is expected


# spec:56
def test_新派工能补救的等人阶段都可以重试():
    delivery = ticket(**ANSWERED, delivery=DELIVERY, assignments=[reported_at("failed")])
    assert ticket_stage(delivery) == {"stage": "person", "action": "delivery"}
    assert can_retry(delivery) is True
