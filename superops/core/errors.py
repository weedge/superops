"""人或 Agent 能引发的拒绝：固定的错误码 + 中文说明。missing → 404，其余 → 400。"""
from __future__ import annotations

import re

USER_ERRORS: dict[str, tuple[str, str]] = {
    "unknownTicket": ("missing", "工单不存在"),
    "unknownProblem": ("missing", "没有这条未完成的评估"),
    "unknownTrigger": ("missing", "触发不存在"),
    "unknownAssignment": ("missing", "派工不存在"),
    "unknownBlock": ("missing", "没有这个设置块：{block}"),
    "unknownOption": ("refused", "没有这个选项：{option}"),
    "ticketEnded": ("refused", "工单已结束"),
    "assignmentEnded": ("refused", "这次派工已结束，工单由更新的派工负责"),
    "evalFinished": ("refused", "评估 {eval} 已结束或被后来的评估取代，请对触发当前的评估作决定"),
    "notAdmissible": ("refused", "这条触发不准入：{reason}"),
    "notExplicit": ("refused", "直接接单只适用于名单内的人明确提出的请求；主动候选要先按规则研判，且只在 auto 模式下、命中已启用的规则时接单"),
    "ticketStopped": ("refused", "这张工单是被人停掉的，新的工作需要重新接单"),
    "mandateRevoked": ("refused", "当前策略已不再支持这张工单的授权，新的工作需要重新接单"),
    "notRetryable": ("refused", "只有未结束、且执行器最近报告失败或受阻（之后没有人的消息）或输入送达失败的工单才能重试"),
    "sourceThread": ("refused", "这是工单开始的线程，订阅不能取消"),
    "followedThread": ("refused", "这是工单自己的线程（例如它的 MR），订阅不能取消"),
    "notFollowing": ("refused", "工单没有订阅这个线程"),
    "questionWithResult": ("refused", "问人的问题不能同时是就绪的结果或答复，请分开报告"),
    "askerWithoutQuestion": ("refused", "指明了提问来源的消息，就必须带上问题"),
    "notAsker": ("refused", "问题必须指明引出它的那条人的消息，且该消息到过这张工单"),
    "notAnswerable": ("refused", "答复必须指明所回答的那条人的提问，且该消息到过这张工单"),
    "ackEnded": ("refused", "工单已结束，不再确认历史请求"),
    "notReached": ("refused", "这条触发不属于这张工单"),
    "handedBack": ("refused", "这张工单已经交还过这条触发"),
    "handedBackHere": ("refused", "这张工单的执行器已把这条触发作为超出范围交还：请作为新工作接单、转给别的工单或忽略"),
    "workerDecides": ("refused", "这条消息已在线程上送达工单 {tickets}：由它的执行器判断是否属于该工单，不属于的会交还并作为候选再来。主 Session 对它只能停工"),
    "ackNoChannel": ("refused", "确认需要一条有回复渠道的人的消息"),
    "ackNotPerson": ("refused", "请用原始的人的消息，而不是机器、CI 或执行器报告"),
    "notReady": ("refused", "工单还没有可交付的结果。进展留在内部，等核实过的就绪报告"),
    "notAttention": ("refused", "工单当前没有这个待人决定的问题"),
    "notQuestion": ("refused", "答复必须针对人的原始提问"),
    "noAnswer": ("refused", "执行器还没有给出这条提问的答复"),
    "noChannel": ("refused", "这张工单或这条触发没有可回复的渠道"),
    "notApprover": ("refused", "{by} 没有这张工单的审批权限"),
    "reasonRequired": ("refused", "选项「{option}」需要写明原因"),
    "sourceNotInstalled": ("refused", "来源 {source} 没有安装"),
    "invalidSettings": ("refused", "设置不合法：{error}"),
    "staleRevision": ("refused", "设置已被别处修改（当前修订 {revision}），请重新读取后再保存"),
}


class UserError(Exception):
    """领域各部分和应用层在请求不合法或对象不存在时抛出；HTTP 和 CLI 按 code 给出状态与文案。"""

    def __init__(self, code: str, values: dict | None = None):
        self.code = code
        self.values = values or {}
        kind, text = USER_ERRORS[code]
        self.missing = kind == "missing"
        super().__init__(re.sub(r"\{(\w+)\}", lambda m: str(self.values.get(m.group(1), m.group(0))), text))
