"""运营侧清理（人在大扫除卡片上批准后执行）：停用监听词、过期选题。不删数据，只改状态。
python -m superops.biz.executors.cleanup run
"""
from __future__ import annotations

import sys

from .. import db
from ..scriptkit import emit_result, load_task
from ...infra.timeutil import iso


def cmd_run() -> None:
    lease = load_task()
    ops = db.open_ops()
    terms = topics = 0
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        for t in d.get("deactivate_terms") or []:
            terms += ops.execute("UPDATE listening_terms SET active=0, updated_at=? WHERE term=?", (iso(), t)).rowcount
        for c in d.get("expire_topics") or []:
            topics += ops.execute("UPDATE contents SET status='expired' WHERE id=? AND status IN ('proposed','approved')", (c,)).rowcount
    emit_result(f"停用监听词 {terms} 个，过期选题 {topics} 条（keywords.yaml 里的词请在下次 MR 中同步删除）")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        cmd_run()
    else:
        print(__doc__)
        sys.exit(2)
