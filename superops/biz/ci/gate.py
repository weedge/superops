"""合并闸门：确定性检查 MR 的 diff（04 §3、§4）。不调用大模型。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_LINES = 800
DESTRUCTIVE_SQL = re.compile(r"\b(DROP\s+(TABLE|COLUMN|INDEX|DATABASE)|TRUNCATE|RENAME\s+(TABLE|COLUMN)|"
                             r"ALTER\s+TABLE\s+\S+\s+(MODIFY|CHANGE|DROP|RENAME)|DELETE\s+FROM)\b", re.I)
BILLING = re.compile(r"(credit|billing|pricing|price|payment|charge|recharge|wallet|积分|扣费|计费|充值|价格)", re.I)
AUTH = re.compile(r"(auth|login|logout|session|permission|rbac|oauth|jwt|鉴权|登录)", re.I)
SECRETS = [
    ("疑似 API Key", re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,})")),
    ("私钥", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("疑似 Cookie / 会话", re.compile(r"(sessionid|passport_csrf_token|sid_tt|web_session)\s*[=:]\s*['\"]?[A-Za-z0-9%]{16,}", re.I)),
    ("疑似手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]


@dataclass
class GateResult:
    blockers: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)

    @property
    def auto_merge_ok(self) -> bool:
        return not self.blockers and not self.needs_human

    def as_dict(self) -> dict:
        return {"blockers": self.blockers, "needs_human": self.needs_human, "auto_merge_ok": self.auto_merge_ok}


def check(changes: list[dict], labels: list[str] | None = None, prd_direction: bool = False) -> GateResult:
    """changes: [{"new_path": ..., "diff": "unified diff"}]（GitLab /changes 接口格式）。"""
    r = GateResult()
    labels = labels or []
    added_total = removed_total = 0
    for ch in changes:
        path = ch.get("new_path") or ch.get("old_path") or ""
        diff = ch.get("diff") or ""
        added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
        added_total += len(added)
        removed_total += len(removed)
        if path.endswith(".sql") or "migration" in path.lower():
            for ln in added:
                if DESTRUCTIVE_SQL.search(ln):
                    r.needs_human.append(f"{path}：数据库迁移只允许新增，发现 `{ln.strip()[:80]}`")
                    break
        if BILLING.search(path):
            r.needs_human.append(f"{path}：涉及积分 / 计费")
        if AUTH.search(path):
            r.needs_human.append(f"{path}：涉及登录 / 鉴权")
        if ch.get("deleted_file"):
            r.needs_human.append(f"{path}：删除文件")
        for name, pat in SECRETS:
            for ln in added:
                if pat.search(ln):
                    r.blockers.append(f"{path}：{name}（`{ln.strip()[:60]}…`）")
                    break
    if added_total + removed_total > MAX_LINES:
        r.needs_human.append(f"改动 {added_total + removed_total} 行，超过 {MAX_LINES} 行")
    if "needs-human" in labels:
        r.needs_human.append("MR 带 needs-human 标签")
    if prd_direction:
        r.needs_human.append("PRD 标了“产品方向”")
    r.needs_human = list(dict.fromkeys(r.needs_human))
    return r
