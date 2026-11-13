"""按 stderr / 退出码 / 输出识别失败类型（03 §3.2）。"""
from __future__ import annotations

import re
from datetime import timedelta

from ..biz.timeutil import iso, now


def classify(text: str, *, timed_out: bool, cb: dict) -> str:
    if timed_out:
        return "timeout"
    low = (text or "").lower()
    for kind, key in (("quota", "quota_patterns"), ("auth", "auth_patterns"), ("network", "network_patterns")):
        for p in cb.get(key) or []:
            if p.lower() in low:
                return kind
    return "error"


_IN = re.compile(r"(?:try again|retry|resets?)\s+in\s+(\d+)\s*(minute|min|hour|hr|h|m)", re.I)


def parse_retry_at(text: str) -> str | None:
    m = _IN.search(text or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
    return iso(now() + delta)
