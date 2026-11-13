"""评论回复执行器：回复属于“对外发消息”，只执行批准过的草稿（05 §5）。

一期半自动：把批准的回复整理成清单推给人，由人在平台上回复；执行器按 comment_id 幂等。
python -m superops.biz.executors.reply run
"""
from __future__ import annotations

import re
import sys

from .. import db
from ..scriptkit import emit_result, hub_client, load_task
from . import ActionLedger

SUSPICIOUS = re.compile(r"(https?://|www\.|微信|vx|v信|加我|qq|QQ|电话|手机号|1[3-9]\d{9}|@\S+)")


def cmd_run() -> None:
    lease = load_task()
    ops = db.open_ops()
    ledger = ActionLedger(ops)
    ready, blocked, skipped = [], [], []
    for e in lease.get("events") or []:
        for r in (e.get("data") or {}).get("replies") or []:
            cid, text = str(r.get("comment_id")), (r.get("text") or "").strip()
            if not text:
                continue
            if SUSPICIOUS.search(text):  # 二次检查：草稿里不该有链接、联系方式、无关 @
                blocked.append(f"{cid}：含链接 / 联系方式 / @，已拦截")
                continue
            ok, why = ledger.begin(f"reply:{cid}", "reply", cid, e.get("received_at"))
            if not ok:
                skipped.append(f"{cid}（{why}）")
                continue
            row = ops.execute("SELECT c.text, p.platform, p.url, p.title FROM comments c LEFT JOIN posts p ON p.id=c.post_id"
                              " WHERE c.id=? OR c.id LIKE ?", (cid, f"%:{cid}")).fetchone()
            ops.execute("UPDATE comments SET reply_status='approved' WHERE id=? OR id LIKE ?", (cid, f"%:{cid}"))
            ledger.finish(f"reply:{cid}", "awaiting_manual")
            ctx = f"「{(row[0] or '')[:40]}」（{row[1]} · {row[3] or ''} {row[2] or ''}）" if row else cid
            ready.append(f"- {ctx}\n  回复：{text}")
    if ready:
        hub_client().notify("message", f"请回复 {len(ready)} 条评论（已批准）", "\n".join(ready), priority=2,
                            ref=f"replies:{lease['task']['id']}", dedup=True)
    emit_result(f"推送 {len(ready)} 条待人工回复，拦截 {len(blocked)} 条，跳过 {len(skipped)} 条", blocked=blocked, skipped=skipped)


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        cmd_run()
    else:
        print(__doc__)
        sys.exit(2)
