"""业务统计：只读 ops.db，给宿主面板 / 日报用。这里只取数，不做展示；状态值的中文名、颜色由展示方决定。

入口是 `business(days)`；系统自身的统计（工单、触发、卡片、Agent 运行）在宿主的状态库里，不归这里管。
`decisions()` 另外读仓库里的 decisions.md / not-doing.md（不属于 ops.db，所以不并进 business）。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from . import db, paths
from .config import load_agents_config
from .timeutil import iso, now, today_start

FUNNEL_COLS = ("clicks", "registrations", "first_done", "second_done", "paid")


def _load(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _clip(s, n: int = 240) -> str | None:
    if s is None:
        return None
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _dist(ops, table: str, col: str, empty: str = "未填", limit: int = 0) -> list[dict]:
    """某一列的取值分布：[{"label": 值, "n": 条数}]，按条数从多到少。table / col 只传本模块里写死的名字。"""
    sql = f"SELECT COALESCE({col}, ?) label, COUNT(*) n FROM {table} GROUP BY 1 ORDER BY n DESC, label"
    return db.rows(ops.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (empty,)))


def _count(ops, sql: str, args: tuple = ()) -> int:
    return ops.execute(sql, args).fetchone()[0] or 0


def _content_view(ops, cfg: dict) -> dict:
    contents = db.rows(ops.execute("SELECT id, parent_id, pillar, persona, topic, status, popto_features, created_at FROM contents ORDER BY id"))
    packs = db.rows(ops.execute(
        "SELECT p.id, p.content_id, p.platform, p.account, p.title, p.scheduled_at, p.publish_mode, p.status, p.updated_at,"
        " a.status action_status FROM publish_packs p LEFT JOIN actions a ON a.action_id='publish:'||p.id"
        " ORDER BY p.scheduled_at DESC, p.platform LIMIT 60"))
    packs_by_content: dict[str, list] = {}
    for p in packs:
        packs_by_content.setdefault(p["content_id"], []).append({"platform": p["platform"], "status": p["status"]})
    calendar: dict[str, list] = {}
    undated = 0
    for c in contents:
        c["features"] = _load(c.pop("popto_features"), []) or []
        c["packs"] = packs_by_content.get(c["id"], [])
        m = re.match(r"c_(\d{4})(\d{2})(\d{2})_", c["id"])  # 内容 ID 里带计划发布日
        if m:
            calendar.setdefault("-".join(m.groups()), []).append(c)
        else:
            undated += 1
    manual = db.rows(ops.execute(
        "SELECT a.ref pack_id, a.approved_at, p.platform, p.title, p.scheduled_at, p.account FROM actions a"
        " LEFT JOIN publish_packs p ON p.id=a.ref WHERE a.kind='publish' AND a.status='awaiting_manual' ORDER BY a.approved_at"))
    return {
        "total": len(contents), "undated": undated,
        "status": _dist(ops, "contents", "status"), "pillars": _dist(ops, "contents", "pillar"),
        "personas": _dist(ops, "contents", "persona"),
        "calendar": [{"date": d, "items": items} for d, items in sorted(calendar.items())],
        "packs": packs, "pack_status": _dist(ops, "publish_packs", "status"),
        "manual_publish": manual,
        "publishers": [{"platform": k, "mode": (v or {}).get("mode", "off")} for k, v in (cfg.get("publishers") or {}).items()],
    }


def _data_view(ops) -> dict:
    posts = db.rows(ops.execute(
        "SELECT p.id, p.platform, p.account, p.title, p.url, p.published_at, p.publish_mode, p.content_id, p.removed_at, m.checkpoint,"
        " m.captured_at, m.source, m.views, m.likes, m.comments, m.shares, m.favorites, m.follows_gained, m.completion_rate, m.avg_watch_sec"
        " FROM posts p LEFT JOIN post_metrics m ON m.post_id=p.id AND m.captured_at=(SELECT MAX(captured_at) FROM post_metrics WHERE post_id=p.id)"
        " ORDER BY p.published_at DESC LIMIT 50"))
    jobs: dict[str, list] = {}
    tnow = iso()
    for j in ops.execute("SELECT * FROM metric_jobs ORDER BY due_at"):
        done = j["done_at"] or ""
        st = "skipped" if done.startswith("skipped") else "done" if done else "overdue" if (j["due_at"] or "") < tnow else "pending"
        jobs.setdefault(j["post_id"], []).append({"checkpoint": j["checkpoint"], "state": st, "due_at": j["due_at"],
                                                  "attempts": j["attempts"], "error": _clip(j["last_error"], 120)})
    series: dict[str, list] = {}
    for m in ops.execute("SELECT post_id, captured_at, checkpoint, views, likes, comments, shares, favorites FROM post_metrics ORDER BY captured_at"):
        series.setdefault(m["post_id"], []).append({k: m[k] for k in m.keys() if k != "post_id"})
    ncom = {r[0]: r[1] for r in ops.execute("SELECT post_id, COUNT(*) FROM comments GROUP BY post_id")}
    for p in posts:
        p["jobs"] = jobs.get(p["id"], [])
        p["series"] = series.get(p["id"], [])
        p["comments_collected"] = ncom.get(p["id"], 0)
    all_jobs = [j for js in jobs.values() for j in js]
    upcoming = db.rows(ops.execute(
        "SELECT j.post_id, j.checkpoint, j.due_at, j.attempts, j.last_error, p.platform, p.title FROM metric_jobs j"
        " LEFT JOIN posts p ON p.id=j.post_id WHERE j.done_at IS NULL ORDER BY j.due_at LIMIT 12"))
    return {
        "posts": posts, "upcoming_jobs": upcoming,
        "recent_comments": [{**c, "text": _clip(c["text"], 160)} for c in db.rows(ops.execute(
            "SELECT c.post_id, c.text, c.likes, c.category, c.sentiment, c.reply_status, COALESCE(c.collected_at, c.created_at) AS at,"
            " p.platform, p.title FROM comments c LEFT JOIN posts p ON p.id=c.post_id ORDER BY at DESC LIMIT 20"))],
        "jobs": {s: sum(1 for j in all_jobs if j["state"] == s) for s in ("done", "pending", "overdue", "skipped")},
        "snapshots": _count(ops, "SELECT COUNT(*) FROM post_metrics"),
        "platforms": _dist(ops, "posts", "platform"),
        "comments": {"total": _count(ops, "SELECT COUNT(*) FROM comments"),
                     "category": _dist(ops, "comments", "category", "未分拣"),
                     "sentiment": _dist(ops, "comments", "sentiment", "未分拣"),
                     "reply": _dist(ops, "comments", "reply_status", "未分拣")},
    }


def _demand_view(ops) -> dict:
    demands = db.rows(ops.execute("SELECT id, title, kind, module, signal_count, persona_dist, value_factor, cost_size, score, status,"
                                  " prd_path, mr_links, decided_ref, task, is_test, updated_at FROM demands ORDER BY score DESC, updated_at DESC LIMIT 60"))
    for d in demands:
        d["persona_dist"] = _load(d["persona_dist"], {}) or {}
        d["mr_links"] = [x for x in (d["mr_links"] or "").split(",") if x]
        d["has_prd"] = bool(d.pop("prd_path"))
        ref = d.pop("decided_ref") or ""
        d["shelved_reason"] = ref.split("：", 1)[1] if d["status"] == "shelved" and "：" in ref else None
    kinds: dict[str, int] = {}
    for kind, n in ops.execute("SELECT kind, COUNT(*) FROM signals GROUP BY kind"):
        top = str(kind or "未分类").split("/")[0].strip() or "未分类"     # “体验优化 / 计费透明”归到“体验优化”
        kinds[top] = kinds.get(top, 0) + n
    sig_cols = "quote, kind, module, persona, intensity, source_type, source_ref, demand_id, created_at"
    return {
        "demands": demands,
        "total": _count(ops, "SELECT COUNT(*) FROM demands"),
        "status": _dist(ops, "demands", "status", "open"),
        "signals": {"total": _count(ops, "SELECT COUNT(*) FROM signals"),
                    "unassigned": _count(ops, "SELECT COUNT(*) FROM signals WHERE demand_id IS NULL"),
                    "source": _dist(ops, "signals", "source_type", limit=12), "kind": _dist(ops, "signals", "kind", limit=12),
                    "kind_group": [{"label": k, "n": n} for k, n in sorted(kinds.items(), key=lambda x: -x[1])][:8],
                    "module": _dist(ops, "signals", "module", "未标模块", limit=8),
                    "recent": [{**r, "quote": _clip(r["quote"], 240)} for r in db.rows(ops.execute(
                        f"SELECT {sig_cols} FROM signals ORDER BY created_at DESC, intensity DESC LIMIT 50"))],
                    "unassigned_recent": [{**r, "quote": _clip(r["quote"], 240)} for r in db.rows(ops.execute(
                        f"SELECT {sig_cols} FROM signals WHERE demand_id IS NULL ORDER BY intensity DESC, created_at DESC LIMIT 30"))]},
    }


def _growth_view(ops) -> dict:
    return {
        "total": _count(ops, "SELECT COUNT(*) FROM leads"),
        "stages": _dist(ops, "leads", "stage", limit=12), "types": _dist(ops, "leads", "lead_type", limit=12),
        "industries": _dist(ops, "leads", "industry", limit=12), "platforms": _dist(ops, "leads", "platform", limit=12),
        "leads": [{**r, "why": _clip(r["why"], 160), "outreach_draft": _clip(r["outreach_draft"], 200)} for r in db.rows(ops.execute(
            "SELECT id, platform, profile_url, nickname, industry, lead_type, followers, score, stage, why, outreach_draft,"
            " target_url, comment_text, comment_status, commented_at, comment_note, updated_at"
            " FROM leads ORDER BY score DESC, updated_at DESC LIMIT 40"))],
        "terms": db.rows(ops.execute("SELECT term, industry category, active, hits_7d, updated_at FROM listening_terms"
                                     " WHERE removed_at IS NULL ORDER BY active DESC, hits_7d DESC, term")),
    }


def _funnel_view(ops, since_day: str) -> dict:
    sums = ", ".join(f"COALESCE(SUM({c}),0) {c}" for c in FUNNEL_COLS)
    daily = db.rows(ops.execute(f"SELECT date, {sums} FROM funnel_daily WHERE date>=? GROUP BY date ORDER BY date DESC", (since_day,)))
    channels = db.rows(ops.execute(
        f"SELECT COALESCE(channel_code,'未带渠道码') channel, {sums} FROM funnel_daily WHERE date>=?"
        " GROUP BY 1 ORDER BY registrations DESC, clicks DESC LIMIT 20", (since_day,)))
    totals = dict(ops.execute(f"SELECT {sums} FROM funnel_daily WHERE date>=?", (since_day,)).fetchone())
    last = ops.execute("SELECT MAX(date) FROM funnel_daily").fetchone()[0]
    return {"daily": daily, "channels": channels, "totals": totals, "last_date": last}


def _daily(ops, since: str, day_list: list[str]) -> dict:
    """窗口内每天的量（下标和 days 对齐）：新内容、发布的作品、采到的评论、需求信号、线索、注册。"""
    def series(sql: str, arg: str = since) -> list:
        got = {r[0]: r[1] for r in ops.execute(sql, (arg,))}
        return [got.get(d) or 0 for d in day_list]

    return {
        "days": day_list,
        "contents": series("SELECT substr(created_at,1,10), COUNT(*) FROM contents WHERE created_at>=? GROUP BY 1"),
        "posts": series("SELECT substr(published_at,1,10), COUNT(*) FROM posts WHERE published_at>=? GROUP BY 1"),
        "comments": series("SELECT substr(COALESCE(collected_at, created_at),1,10), COUNT(*) FROM comments"
                           " WHERE COALESCE(collected_at, created_at)>=? GROUP BY 1"),
        "signals": series("SELECT substr(created_at,1,10), COUNT(*) FROM signals WHERE created_at>=? GROUP BY 1"),
        "leads": series("SELECT substr(updated_at,1,10), COUNT(*) FROM leads WHERE updated_at>=? GROUP BY 1"),
        "registrations": series("SELECT date, SUM(registrations) FROM funnel_daily WHERE date>=? GROUP BY 1", day_list[0]),
    }


def business(days: int = 7) -> dict:
    """ops.db 的业务统计，返回可以直接 json.dumps 的 dict（只读，不改库）。

    窗口按整天对齐：近 days 天 = 今天 + 往前 days-1 天（上海时间）。带“窗口内”字样的才受 days 影响，
    其余是当前存量（列表都有条数上限，见括号）。返回的键：

    - generated_at / days / since / window：生成时间、窗口天数、窗口起点（ISO）、“近 N 天”。
    - summary：顶部数字。窗口内：contents_new 新选题、posts_published 发布的作品、views 这些作品最新一次快照的
      播放合计、comments_new 采到的评论、signals_new 需求信号、leads_touched 新增或有更新的线索、clicks /
      registrations / paid 漏斗合计；存量：demands_open 状态不是 done 的需求、manual_publish_waiting 等人手动发布的发布包、
      metric_jobs_overdue 到点还没采的指标快照。
    - daily：窗口内每天的量，days 是日期列表，contents / posts / comments / signals / leads / registrations 与它等长。
    - content（内容）：total、undated（ID 里没带计划日的条数）、status / pillars / personas 分布、calendar
      （按计划发布日分组的内容，每条带 features 和各平台发布包状态 packs）、packs（最近 60 个发布包，带
      action_status）、pack_status 分布、manual_publish（已批准、等人手动发布的发布包）、publishers（各平台自动发布档位）。
    - data（数据回收 / 指标）：posts（最近 50 条作品 + 最新一次指标快照 + 各采集节点 jobs 的状态
      done / pending / overdue / skipped + series 全部快照按时间排 + comments_collected 采到的评论数）、
      recent_comments（最近 20 条评论，原文截到 160 字）、jobs（各状态的节点数）、upcoming_jobs（最近要采的 12 个节点）、
      snapshots（快照总数）、platforms 分布、comments（total 与 category / sentiment / reply 分布）。
    - demand（需求与开发）：demands（按分数排前 60，persona_dist 已解析、mr_links 是列表、has_prd、task 开发工单、
      is_test、shelved_reason 搁置原因）、total、status 分布、signals（total、unassigned 未归到需求的、source / kind 分布、
      kind_group 按大类（“体验优化 / 计费透明”算“体验优化”）、module 分布、recent 最近 50 条、unassigned_recent 没归到需求的
      30 条（强度高的在前），原话截到 240 字）。
    - growth（获客 / 线索）：total、stages / types / industries / platforms 分布、leads（按分数排前 40）、
      terms（监听意图词与近 7 天命中数）。
    - funnel（渠道漏斗，窗口内）：daily（按日期倒序，每天的 clicks / registrations / first_done / second_done / paid）、
      channels（按渠道码汇总，注册多的在前，最多 20 个）、totals、last_date（库里最新一天，用来看导出有没有断）。

    分布类的值都是 [{"label": 取值, "n": 条数}]。
    """
    days = max(1, int(days))
    cfg = load_agents_config()
    tnow = now()
    since_dt = today_start(tnow) - timedelta(days=days - 1)
    since, since_day = iso(since_dt), since_dt.date().isoformat()
    day_list = [(since_dt + timedelta(days=i)).date().isoformat() for i in range(days)]
    ops = db.open_ops()
    try:
        content, funnel, demand = _content_view(ops, cfg), _funnel_view(ops, since_day), _demand_view(ops)
        data = _data_view(ops)
        summary = {
            "contents_new": _count(ops, "SELECT COUNT(*) FROM contents WHERE created_at>=?", (since,)),
            "posts_published": _count(ops, "SELECT COUNT(*) FROM posts WHERE published_at>=?", (since,)),
            "views": _count(ops, "SELECT SUM(m.views) FROM posts p JOIN post_metrics m ON m.post_id=p.id AND m.captured_at="
                                 "(SELECT MAX(captured_at) FROM post_metrics WHERE post_id=p.id) WHERE p.published_at>=?", (since,)),
            "comments_new": _count(ops, "SELECT COUNT(*) FROM comments WHERE COALESCE(collected_at, created_at)>=?", (since,)),
            "signals_new": _count(ops, "SELECT COUNT(*) FROM signals WHERE created_at>=?", (since,)),
            "leads_touched": _count(ops, "SELECT COUNT(*) FROM leads WHERE updated_at>=?", (since,)),
            "clicks": funnel["totals"]["clicks"], "registrations": funnel["totals"]["registrations"], "paid": funnel["totals"]["paid"],
            "demands_open": _count(ops, "SELECT COUNT(*) FROM demands WHERE COALESCE(status,'open')<>'done'"),
            "manual_publish_waiting": len(content["manual_publish"]),
            "metric_jobs_overdue": data["jobs"]["overdue"],
        }
        return {
            "generated_at": iso(tnow), "days": days, "since": since, "window": f"近 {days} 天",
            "summary": summary, "daily": _daily(ops, since, day_list),
            "content": content, "data": data, "demand": demand, "growth": _growth_view(ops), "funnel": funnel,
        }
    finally:
        ops.close()


def decisions(home: Path | None = None) -> list[dict]:
    """decisions.md / not-doing.md 里的条目（## D-20261109-01 标题 + “- 日期：”一行），新的在前。
    每条：kind（定的事 / 不做的事）、id、title、date、scope（适用 / 回复口径）、review（复查日期）。"""
    home = home or paths.ops_home()
    out = []
    for fname, kind in (("decisions.md", "定的事"), ("not-doing.md", "不做的事")):
        try:
            text = (home / fname).read_text(encoding="utf-8")
        except OSError:
            continue
        for block in re.split(r"^## ", text, flags=re.M)[1:]:
            head, _, body = block.partition("\n")
            m = re.match(r"([A-Z]-\d{8}-\d+)\s*(.*)", head.strip())
            fields = dict(re.findall(r"^- (\S+?)：(.*)$", body, flags=re.M))
            out.append({"kind": kind, "id": m.group(1) if m else "", "title": (m.group(2) if m else head).strip(),
                        "date": fields.get("日期"), "scope": fields.get("适用") or fields.get("回复口径"),
                        "review": fields.get("复查")})
    return sorted(out, key=lambda d: d.get("date") or "", reverse=True)
