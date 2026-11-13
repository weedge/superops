"""评论采集：每条内容每小时最多一批，原文只进 ops.db（180 天后删除），昵称哈希化（08 §6）。

python -m superops.biz.collectors.comments collect
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta

from .. import db
from ..scriptkit import emit_result
from ..timeutil import TZ, iso, now
from .tikhub import TikHub

ACTIVE_DAYS = 14
RETENTION = timedelta(days=180)


def author_hash(s: str) -> str:
    return hashlib.sha256(("popto-ops:" + (s or "")).encode()).hexdigest()[:16]


def save_comments(ops, post_id: str, items: list[dict], collected_at: str | None = None) -> int:
    new = 0
    collected_at = collected_at or iso()
    for c in items:
        cid = f"{post_id}:{c['id']}"
        created = c.get("created_at")
        if isinstance(created, (int, float)):
            created = iso(datetime.fromtimestamp(created, TZ))
        cur = ops.execute(
            "INSERT OR IGNORE INTO comments(id,post_id,author_hash,text,likes,created_at,reply_status,purge_after,collected_at)"
            " VALUES (?,?,?,?,?,?, 'none', ?, ?)",
            (cid, post_id, author_hash(c.get("author", "")), (c.get("text") or "")[:2000], c.get("likes") or 0,
             created or iso(), iso(now() + RETENTION), collected_at))
        new += cur.rowcount
    return new


def cmd_collect(only_post: str | None = None) -> None:
    """默认采最近 ACTIVE_DAYS 天发布的内容；only_post 指定单条（补采 / 测试），不受天数限制。"""
    ops = db.open_ops()
    purged = ops.execute("DELETE FROM comments WHERE purge_after<?", (iso(),)).rowcount
    th = TikHub()
    if only_post:
        posts = db.rows(ops.execute("SELECT * FROM posts WHERE id=? OR platform_post_id=?", (only_post, only_post)))
    else:
        posts = db.rows(ops.execute("SELECT * FROM posts WHERE published_at>=?", (iso(now() - timedelta(days=ACTIVE_DAYS)),)))
    events, skipped = [], 0
    for p in posts:
        if p["platform"] != "douyin" or not th.configured or not str(p["platform_post_id"] or "").isdigit():
            skipped += 1
            continue
        try:
            items, _, _ = th.douyin_comments(p["platform_post_id"])
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        mark = iso()
        n = save_comments(ops, p["id"], items, collected_at=mark)
        if n:
            hour = now().strftime("%Y-%m-%dT%H")
            events.append({"source": p["platform"], "type": "social.comment.batch", "subject": f"post:{p['id']}",
                           "summary": f"「{p.get('title') or p['id']}」新增 {n} 条评论",
                           "dedup_key": f"{p['platform']}:comment:{p['id']}:{hour}",
                           "payload_ref": f"ops.db://comments?post_id={p['id']}&since={mark}",  # 按采集时间取这一批
                           "data": {"new": n, "collected_at": mark}})
    emit_result(f"检查 {len(posts)} 条内容，{len(events)} 条有新评论，{skipped} 条暂无自动采集源；清理过期评论 {purged} 条",
                follow_up_events=events)


def _post_from_task() -> str | None:
    """被宿主拉起时，定时事件 data.post 可指定单条补采（superops emit cron.comment_collect --data '{"post": "..."}'）。"""
    import os

    if not os.environ.get("OPS_TASK_FILE"):
        return None
    from ..scriptkit import load_task

    for e in load_task().get("events") or []:
        if (e.get("data") or {}).get("post"):
            return str(e["data"]["post"])
    return None


if __name__ == "__main__":
    if sys.argv[1:2] == ["collect"]:
        cmd_collect(sys.argv[3] if sys.argv[2:3] == ["--post"] and len(sys.argv) > 3 else _post_from_task())
    else:
        print(__doc__)
        sys.exit(2)
