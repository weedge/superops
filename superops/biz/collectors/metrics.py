"""发布后数据回收：T+1h / 24h / 72h / 7d 快照，异常（同平台同节点中位数 ×3 或 ×0.3）发事件（07）。

python -m superops.biz.collectors.metrics plan       # social.post.published → 登记 posts + 排采集计划
python -m superops.biz.collectors.metrics collect    # cron.collect_metrics → 采到点的快照
python -m superops.biz.collectors.metrics now [post_id …]   # 手动采一遍（面板“立即采集” / superops metrics collect）
python -m superops.biz.collectors.metrics removed <post_id …>   # 作品已在平台删掉：标记并停掉剩下的采集
python -m superops.biz.collectors.metrics import <csv>   # 创作者后台导出的数据手工导入
"""
from __future__ import annotations

import csv
import statistics
import sys
from datetime import timedelta
from pathlib import Path

from .. import db, platforms
from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from .tikhub import TikHub

CHECKPOINTS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "72h": timedelta(hours=72), "7d": timedelta(days=7)}
METRIC_COLS = ["views", "likes", "comments", "shares", "favorites", "follows_gained", "completion_rate",
               "avg_watch_sec", "profile_visits"]
GIVE_UP_AFTER = timedelta(hours=12)
LATE_TOLERANCE = timedelta(hours=2)   # 登记时某个节点已过点超过这么久，就不再采这个节点
MANUAL = "manual"                     # 手动采集 / 导入的快照节点：不占 1h…7d 节点，也不参与异常判断
MANUAL_WINDOW = timedelta(days=30)    # “全部采一遍”只采最近这么久发布的作品


def register_post(ops, d: dict) -> str:
    """作品 ID 可以先空着（视频号没有链接、短链解析不出）：采集时按标题在创作者后台对上再补。"""
    import hashlib

    from .creator import id_from_url

    platform = platforms.normalize(d["platform"])
    ppid = str(d.get("platform_post_id") or id_from_url(platform, d.get("url")) or "") or None   # 没有就存 NULL（唯一约束不拦多个 NULL）
    published = d.get("published_at") or iso()
    known = ppid and ops.execute("SELECT id FROM posts WHERE platform=? AND platform_post_id=?", (platform, ppid)).fetchone()
    if known:   # 同一条作品已经登记过（比如先按标题对上了 ID，人又回了带链接的“已发”）：沿用原编号
        pid = known[0]
    elif d.get("post_id"):
        pid = d["post_id"]
    elif ppid:
        pid = f"{platform}_{ppid}"[:80]
    else:   # 没有作品 ID：用内容 / 标题 + 发布时间生成稳定编号，免得多条都叫 “<平台>_”
        key = f"{d.get('content_id') or ''}|{d.get('title') or ''}|{d.get('url') or ''}|{published}"
        pid = f"{platform}_t{hashlib.sha1(key.encode()).hexdigest()[:12]}"
    ops.execute(
        "INSERT INTO posts(id,content_id,platform,account,platform_post_id,url,title,pillar,published_at,channel_code,publish_mode)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=COALESCE(excluded.url,url),"
        " title=COALESCE(excluded.title,title), platform_post_id=COALESCE(platform_post_id,excluded.platform_post_id)",
        (pid, d.get("content_id"), platform, d.get("account"), ppid, d.get("url"), d.get("title"), d.get("pillar"),
         published, d.get("channel_code"), d.get("publish_mode", "manual")),
    )
    base = parse_iso(published)
    late = False
    for cp, delta in CHECKPOINTS.items():
        due = base + delta
        if now() - due > LATE_TOLERANCE:
            # 补登记时早已过点：不拿现在的数冒充 1h / 24h 快照（会污染同节点中位数和异常判断）
            ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at,done_at,last_error) VALUES (?,?,?,?,?)",
                        (pid, cp, iso(due), "skipped:" + iso(), "登记时已过点，未采"))
            late = True
        else:
            ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at) VALUES (?,?,?)", (pid, cp, iso(due)))
    if late:  # 补一张“补登记时”的快照，让这条内容至少有一份真实数据
        ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at) VALUES (?,?,?)", (pid, "late", iso()))
    return pid


def cmd_plan() -> None:
    lease = load_task()
    ops = db.open_ops()
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if not d.get("platform"):
            continue
        ids.append(register_post(ops, d))
    late = [i for i in ids if ops.execute("SELECT 1 FROM metric_jobs WHERE post_id=? AND checkpoint='late'", (i,)).fetchone()]
    msg = f"登记 {len(ids)} 条已发布内容"
    if len(ids) - len(late):
        msg += f"，{len(ids) - len(late)} 条按 1h / 24h / 72h / 7d 排定采集"
    if late:
        msg += f"，{len(late)} 条登记时已过点（只补采一次当前数据）"
    emit_result(msg if ids else "事件里没有可登记的发布信息", posts=ids)


def _anomaly(ops, post: dict, cp: str, views: int | None) -> str | None:
    if not views or cp not in CHECKPOINTS:   # “late”补登记快照各条发布时长不同，互相比没有意义
        return None
    vals = [r[0] for r in ops.execute(
        "SELECT m.views FROM post_metrics m JOIN posts p ON p.id=m.post_id WHERE p.platform=? AND m.checkpoint=?"
        " AND m.post_id<>? AND m.views IS NOT NULL ORDER BY m.captured_at DESC LIMIT 60", (post["platform"], cp, post["id"]))]
    if len(vals) < 5:
        return None
    med = statistics.median(vals)
    if med <= 0:
        return None
    ratio = views / med
    if ratio >= 3:
        return f"高于同平台 {cp} 中位数 {ratio:.1f} 倍（{views} vs {med:.0f}）"
    if ratio <= 0.3:
        return f"低于同平台 {cp} 中位数（{ratio:.2f} 倍，{views} vs {med:.0f}）"
    return None


def save_snapshot(ops, post: dict, cp: str, m: dict, source: str, raw_ref: str | None = None) -> list[dict]:
    ts = iso()
    ops.execute(
        "INSERT OR REPLACE INTO post_metrics(post_id,captured_at,checkpoint,source," + ",".join(METRIC_COLS) + ",raw_ref)"
        " VALUES (?,?,?,?," + ",".join("?" * len(METRIC_COLS)) + ",?)",
        (post["id"], ts, cp, source, *[m.get(c) for c in METRIC_COLS], raw_ref),
    )
    ops.execute("UPDATE metric_jobs SET done_at=?, last_error=NULL WHERE post_id=? AND checkpoint=?", (ts, post["id"], cp))
    evs = [{"source": post["platform"], "type": "social.metrics.snapshot", "subject": f"post:{post['id']}",
            "summary": f"「{post.get('title') or post['id']}」{cp} 播放 {m.get('views')} 赞 {m.get('likes')} 评 {m.get('comments')}",
            "dedup_key": f"snapshot:{post['id']}:{cp}" + (f":{ts}" if cp not in CHECKPOINTS else ""), "payload_ref": f"ops.db://post_metrics/{post['id']}"}]
    why = _anomaly(ops, post, cp, m.get("views"))
    if why:
        evs.append({"source": post["platform"], "type": "social.metrics.anomaly", "subject": f"post:{post['id']}",
                    "summary": f"「{post.get('title') or post['id']}」{why}", "dedup_key": f"anomaly:{post['id']}:{cp}",
                    "payload_ref": f"ops.db://post_metrics/{post['id']}", "data": {"checkpoint": cp, "post": post}})
    return evs


def creator_pass(ops, due: list[dict]) -> tuple[dict[str, dict], dict[str, str], dict[str, str]]:
    """自有作品读创作者后台（有真实播放量），每个平台开一次浏览器取完。
    返回 ({post_id: 指标}, {平台: 没取到的原因}, {按标题对上的 post_id: 补登记的作品 ID})。"""
    from . import creator

    got: dict[str, dict] = {}
    errs: dict[str, str] = {}
    linked: dict[str, str] = {}
    for platform in creator.PLATFORMS:
        posts = {j["pid"]: j for j in due if j["platform"] == platform}
        if not posts:
            continue
        want = {creator.norm_id(platform, j["platform_post_id"]) for j in posts.values()
                if creator.looks_like_id(platform, j["platform_post_id"])}
        unknown = [j for j in posts.values() if not creator.looks_like_id(platform, j["platform_post_id"])]
        since = None
        if unknown:   # 只按标题找时，翻到比最早那条再早 3 天为止
            since = min(parse_iso(j["published_at"]) for j in unknown) - timedelta(days=3)
        try:
            works = creator.list_works(platform, want, since=since)
        except Exception as e:  # noqa: BLE001
            from ..executors.publishers import friendly_error
            errs[platform] = friendly_error(e)
            continue
        by_id = {w["id"]: w for w in works}
        taken = {creator.norm_id(platform, r[0]) for r in ops.execute(
            "SELECT platform_post_id FROM posts WHERE platform=? AND platform_post_id<>''", (platform,))}
        for j in unknown:
            w = creator.match_title(j["title"] or "", parse_iso(j["published_at"]) if j["published_at"] else None, works, taken)
            if not w:
                continue
            ops.execute("UPDATE posts SET platform_post_id=?, url=COALESCE(url, ?) WHERE id=?", (w["id"], w["url"], j["pid"]))
            taken.add(w["id"])
            linked[j["pid"]] = w["id"]
        for pid, j in posts.items():
            w = by_id.get(linked.get(pid) or creator.norm_id(platform, j["platform_post_id"]))
            if w:
                got[pid] = w["metrics"]
    return got, errs, linked


def _snapshot_one(ops, j: dict, cp: str, creator: dict, creator_errs: dict, linked: dict, th, budget: int, used: int) -> list[dict]:
    """采一条作品的一个快照：先用创作者后台取到的，抖音再用 TikHub 兜底；都没有就抛出原因。"""
    post = {"id": j["pid"], "platform": j["platform"], "title": j["title"]}
    ppid = str(linked.get(j["pid"]) or j["platform_post_id"] or "")
    if j["pid"] in creator:
        return save_snapshot(ops, post, cp, creator[j["pid"]], "creator")
    if j["platform"] == "douyin" and th.configured and ppid.isdigit():
        if budget and used + th.requests >= budget:
            raise RuntimeError("TikHub 今日请求预算已用完")
        return save_snapshot(ops, post, cp, th.douyin_video_stats(ppid), "tikhub")
    if j["platform"] in creator_errs:
        raise RuntimeError(f"创作者后台没取到：{creator_errs[j['platform']]}")
    if j["platform"] in ("douyin", "wechat_channels", "xiaohongshu"):
        raise LookupError("创作者后台作品列表里没找到这条（作品 ID、标题都没对上）")
    raise LookupError("该平台没有可用的自动采集源，需要创作者后台导出或截图识别")


def _tikhub(cfg, hub) -> tuple[TikHub, int, int]:
    th = TikHub(proxy=None)
    budget = int((cfg.get("budgets") or {}).get("tikhub_requests", 0) or 0)
    try:
        used = int(hub.get("/v1/counter", name="tikhub_requests").get("value", 0))
    except Exception:  # noqa: BLE001
        used = 0
    return th, budget, used


def _count_tikhub(hub, th) -> None:
    if th.requests:
        try:
            hub.post("/v1/counter", {"name": "tikhub_requests", "value": th.requests})
        except Exception:  # noqa: BLE001
            pass


def cmd_collect() -> None:
    cfg = load_agents_config()
    ops = db.open_ops()
    hub = hub_client()
    th, budget, used = _tikhub(cfg, hub)
    due = db.rows(ops.execute(
        "SELECT j.*, p.platform, p.platform_post_id, p.title, p.published_at, p.id AS pid FROM metric_jobs j"
        " JOIN posts p ON p.id=j.post_id WHERE j.done_at IS NULL AND j.due_at<=? AND p.removed_at IS NULL"
        " ORDER BY j.due_at LIMIT 200", (iso(),)))
    events, done, manual, failed = [], 0, [], []
    creator, creator_errs, linked = creator_pass(ops, due) if due else ({}, {}, {})
    for j in due:
        try:
            events += _snapshot_one(ops, j, j["checkpoint"], creator, creator_errs, linked, th, budget, used)
            done += 1
        except Exception as e:  # noqa: BLE001
            ops.execute("UPDATE metric_jobs SET attempts=attempts+1, last_error=? WHERE post_id=? AND checkpoint=?",
                        (str(e)[:300], j["pid"], j["checkpoint"]))
            if now() - parse_iso(j["due_at"]) > GIVE_UP_AFTER:
                ops.execute("UPDATE metric_jobs SET done_at=? WHERE post_id=? AND checkpoint=?", ("skipped:" + iso(), j["pid"], j["checkpoint"]))
            (manual if isinstance(e, LookupError) else failed).append(f"{j['pid']}@{j['checkpoint']}")
    _count_tikhub(hub, th)
    parts = [f"采到 {done} 个快照"]
    if linked:
        parts.append(f"{len(linked)} 条按标题在后台对上了作品，已补登记作品 ID")
    for pf, err in creator_errs.items():
        parts.append(f"{platforms.label(pf)}创作者后台没取到（{err}）" + ("，已用 TikHub 兜底" if pf == "douyin" and th.configured else ""))
    if manual:
        parts.append(f"{len(manual)} 个需人工导出（superops metrics import）")
    if failed:
        parts.append(f"{len(failed)} 个采集失败")
    anomalies = [e for e in events if e["type"] == "social.metrics.anomaly"]
    if anomalies:
        parts.append(f"{len(anomalies)} 条异常已转单条复盘")
    emit_result("；".join(parts) if due else "没有到点的采集任务", follow_up_events=events,
                manual=manual, failed=failed)


def manual_targets(ops, post_ids: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """手动采集要采哪些作品。给了 post_id 就采这些（已标记删除的跳过）；没给就采最近 30 天发布、没删的全部。
    返回 (要采的, 跳过说明)。"""
    cols = "p.id AS pid, p.platform, p.platform_post_id, p.title, p.published_at, p.removed_at"
    skipped: list[str] = []
    if post_ids:
        rows = []
        for pid in post_ids:
            r = ops.execute(f"SELECT {cols} FROM posts p WHERE p.id=?", (pid,)).fetchone()
            if not r:
                skipped.append(f"{pid}：没有这条作品")
            elif r["removed_at"]:
                skipped.append(f"「{r['title'] or pid}」已在平台删除，不采")
            else:
                rows.append(dict(r))
        return rows, skipped
    since = iso(now() - MANUAL_WINDOW)
    return db.rows(ops.execute(f"SELECT {cols} FROM posts p WHERE p.removed_at IS NULL AND p.published_at>=?"
                               " ORDER BY p.published_at DESC LIMIT 100", (since,))), skipped


def collect_now(ops, post_ids: list[str] | None, *, th, budget: int = 0, used: int = 0) -> dict:
    """手动采一遍：不看采集节点，给每条作品存一份 checkpoint=manual 的当前快照。到点的 1h…7d 节点照旧由定时采集负责。"""
    targets, skipped = manual_targets(ops, post_ids)
    creator, creator_errs, linked = creator_pass(ops, targets) if targets else ({}, {}, {})
    got, failed = [], []
    for j in targets:
        label = f"{platforms.label(j['platform'])}「{j['title'] or j['pid']}」"
        try:
            _snapshot_one(ops, j, MANUAL, creator, creator_errs, linked, th, budget, used)
            m = ops.execute("SELECT views, likes, comments FROM post_metrics WHERE post_id=? ORDER BY captured_at DESC LIMIT 1",
                            (j["pid"],)).fetchone()
            got.append(f"{label} 播放 {m['views']} 赞 {m['likes']} 评 {m['comments']}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{label}：{str(e)[:160]}")
    return {"targets": len(targets), "got": got, "failed": failed, "skipped": skipped, "linked": list(linked)}


def cmd_now(post_ids: list[str]) -> None:
    lease = load_task()
    for e in lease.get("events") or []:   # 面板 / CLI 触发时，要采的作品放在事件 data.post_ids
        post_ids = post_ids or list((e.get("data") or {}).get("post_ids") or [])
    cfg = load_agents_config()
    ops = db.open_ops()
    hub = hub_client()
    th, budget, used = _tikhub(cfg, hub)
    r = collect_now(ops, post_ids or None, th=th, budget=budget, used=used)
    _count_tikhub(hub, th)
    if not r["targets"]:
        summary = "没有要采的作品" + (f"（{'；'.join(r['skipped'])}）" if r["skipped"] else "（最近 30 天没有发布、没删的作品）")
    else:
        summary = f"手动采集 {r['targets']} 条：采到 {len(r['got'])} 条" + (f"，{len(r['failed'])} 条没采到" if r["failed"] else "")
        lines = r["got"] + [f"没采到 {x}" for x in r["failed"]] + r["skipped"]
        summary += "\n" + "\n".join(f"- {x}" for x in lines)
    emit_result(summary, status="done" if r["got"] or not r["targets"] else "failed", **{k: r[k] for k in ("got", "failed", "skipped")})


def mark_removed(ops, post_id: str, reason: str = "作品已在平台删除") -> bool:
    """作品在平台上删掉了：记下删除时间，剩下没采的节点标成跳过，以后不再采、手动“全部采一遍”也不包括它。"""
    if not ops.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
        return False
    t = iso()
    ops.execute("UPDATE posts SET removed_at=COALESCE(removed_at, ?) WHERE id=?", (t, post_id))
    ops.execute("UPDATE metric_jobs SET done_at=?, last_error=? WHERE post_id=? AND done_at IS NULL", ("skipped:" + t, f"{reason}，不再采集", post_id))
    return True


def import_csv(path: Path) -> tuple[int, list[dict]]:
    ops = db.open_ops()
    n, events = 0, []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pid = row.get("post_id")
            if not pid and row.get("platform") and row.get("platform_post_id"):
                r = ops.execute("SELECT id FROM posts WHERE platform=? AND platform_post_id=?",
                                (row["platform"], row["platform_post_id"])).fetchone()
                pid = r[0] if r else None
            if not pid:
                continue
            p = ops.execute("SELECT id, platform, title FROM posts WHERE id=?", (pid,)).fetchone()
            if not p:
                continue
            m = {}
            for c in METRIC_COLS:
                v = (row.get(c) or "").strip()
                if v:
                    m[c] = float(v) if c in ("completion_rate", "avg_watch_sec") else int(float(v))
            events += save_snapshot(ops, dict(p), row.get("checkpoint") or MANUAL, m, "export", raw_ref=str(path))
            n += 1
    return n, events


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "plan":
        cmd_plan()
    elif cmd == "collect":
        cmd_collect()
    elif cmd == "now":
        cmd_now(argv[1:])
    elif cmd == "removed" and len(argv) > 1:
        ops = db.open_ops()
        for pid in argv[1:]:
            print(f"{pid}：{'已标记删除，剩下的采集停掉了' if mark_removed(ops, pid) else '没有这条作品'}")
    elif cmd == "import" and len(argv) > 1:
        n, events = import_csv(Path(argv[1]))
        hub = hub_client()
        for e in events:
            try:
                hub.emit(e)
            except Exception:  # noqa: BLE001
                pass
        print(f"导入 {n} 行，发出 {len(events)} 个事件")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
