"""社媒监听 · 接口优先（获客）：按“在用”的意图词，用 TikHub 搜各平台近 24 小时的作品，结构化入库，再交给 Agent 判断线索。

python -m superops.biz.collectors.listening collect   # cron.social_listening：搜 → 入库 mentions → 发 social.mention.new

为什么用接口：浏览器监听（Antigravity 开专用 Chrome）4 个平台要 35 分钟，小红书 / 快手卡在登录、视频号网页端没有搜索，
页面上读的粉丝数和发布时间常常缺。接口不用登录，几分钟搜完，直接给作品 ID、发布时间、作者（抖音带粉丝数）。
知乎、B站、搜狗微信没有可用的接口，仍由浏览器监听补（规则“社媒舆情监听（浏览器补位）”）。

接口（2026-11-12 实测）：
- 抖音    POST /api/v1/douyin/search/fetch_video_search_v2   publish_time=1（最近一天）；一页约 8 条，has_more 常为 0
- 小红书  GET  /api/v1/xiaohongshu/app_v2/search_notes       time_filter=一天内, sort_type=time_descending；一页 20 条，没有粉丝数
- 视频号  POST /api/v1/wechat_search/v2/fetch_search_videos   publish_time=day, sort=latest, raw=False；一页 10 条，只有号名
- 快手    GET  /api/v1/kuaishou/app/search_video_v2          没有时间筛选，返回大量旧作品：按 timestamp（毫秒）自己过滤
每个词每个平台只取第一页（成本可控：14 个词 × 4 个平台 ≈ 56 次请求）。TikHub 要带非 Python 默认的 User-Agent。

两处补强（2026-11-12 第一次对比后加的）：
- **先补粉丝数再判断**：小红书搜索不带粉丝数，按笔记互动从高到低给作者补查（GET /api/v1/xiaohongshu/app_v2/get_user_info，
  data.data.fans），每天最多 enrich_max 个；30 天内查过的作者直接用库里的数。
- **抖音按人群词找人**：类别是 persona（人群词，如“保险代理人”“律师”）的词不搜作品，而是搜用户
  （POST /api/v1/douyin/search/fetch_user_search，douyin_user_fans=10k_100k / 100k_1M；粉丝筛选不严，代码里再按
  min_followers–max_followers 过滤），再取这些人主页最近一条非置顶作品（GET /api/v1/douyin/app/v3/fetch_user_post_videos，
  is_top=1 是置顶）当评论对象，只要 7 天内发的；每天最多 user_post_max 个人。作品搜索按“最新”排，搜出来多是刚发帖的小号，
  按人找能直接落到画像里的中大号。
配置在 agents.yaml / local.yaml 的 `listening`：min_followers（默认 10000）、max_followers（500000）、enrich_max（60）、user_post_max（10）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from .. import db
from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client
from ..timeutil import TZ, iso, now
from .tikhub import BASE, UA, TikHub

PLATFORMS = ("douyin", "xiaohongshu", "wechat_channels", "kuaishou")
WINDOW = timedelta(hours=24)
PERSONA_WINDOW = timedelta(days=7)    # 按人找的：最近一条作品 7 天内发的才算活跃、才有可评论的对象
DEFAULTS = {"min_followers": 10000, "max_followers": 500000, "enrich_max": 60, "user_post_max": 10}


def settings(cfg: dict | None = None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("listening") or {})}

def _ts(sec: float | None) -> str | None:
    if not sec:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(float(sec), TZ).isoformat(timespec="seconds")


def _post(th: TikHub, path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {th.key}", "User-Agent": UA, "Content-Type": "application/json"})
    th.requests += 1
    with th.opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _clip(s, n=160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------- 各平台：搜一页 → 统一字段
def search_douyin(th: TikHub, kw: str) -> list[dict]:
    d = _post(th, "/api/v1/douyin/search/fetch_video_search_v2", {
        "keyword": kw, "cursor": 0, "sort_type": "0", "publish_time": "1", "filter_duration": "0", "content_type": "0", "search_id": ""})
    out = []
    for b in ((d.get("data") or {}).get("business_data") or []):
        a = (b.get("data") or {}).get("aweme_info") or {}
        if not a.get("aweme_id"):
            continue
        au, st = a.get("author") or {}, a.get("statistics") or {}
        out.append({"platform": "douyin", "post_id": str(a["aweme_id"]), "url": f"https://www.douyin.com/video/{a['aweme_id']}",
                    "title": _clip(a.get("desc")), "author": au.get("nickname"), "author_id": au.get("sec_uid") or au.get("uid"),
                    "profile_url": f"https://www.douyin.com/user/{au['sec_uid']}" if au.get("sec_uid") else None,
                    "fans": au.get("follower_count"), "likes": st.get("digg_count"), "comments": st.get("comment_count"),
                    "collects": st.get("collect_count"), "published_at": _ts(a.get("create_time"))})
    return out


def search_xiaohongshu(th: TikHub, kw: str) -> list[dict]:
    d = th.get("/api/v1/xiaohongshu/app_v2/search_notes", keyword=kw, page=1, sort_type="time_descending", time_filter="一天内")
    out = []
    for it in (((d.get("data") or {}).get("data") or {}).get("items") or []):
        n = it.get("note") or {}
        if it.get("model_type") != "note" or not n.get("id"):
            continue
        u = n.get("user") or {}
        out.append({"platform": "xiaohongshu", "post_id": n["id"], "url": f"https://www.xiaohongshu.com/explore/{n['id']}",
                    "title": _clip(f"{n.get('title') or ''} {n.get('desc') or ''}"), "author": u.get("nickname"), "author_id": u.get("userid"),
                    "profile_url": f"https://www.xiaohongshu.com/user/profile/{u['userid']}" if u.get("userid") else None,
                    "fans": None, "likes": n.get("liked_count"), "comments": n.get("comments_count"),
                    "collects": n.get("collected_count"), "published_at": _ts(n.get("timestamp"))})
    return out


def search_wechat_channels(th: TikHub, kw: str) -> list[dict]:
    d = _post(th, "/api/v1/wechat_search/v2/fetch_search_videos", {"keyword": kw, "sort": "latest", "publish_time": "day", "raw": False})
    out = []
    for it in ((d.get("data") or {}).get("items") or []):
        pid = it.get("exportId") or it.get("docID")
        if not pid:
            continue
        out.append({"platform": "wechat_channels", "post_id": pid, "url": None,   # 视频号作品没有网页链接，人在微信里搜作者名
                    "title": _clip(it.get("title")), "author": (it.get("source") or {}).get("title"), "author_id": None,
                    "profile_url": None, "fans": None, "likes": None, "comments": None, "collects": None,
                    "published_at": _ts(it.get("pubTime"))})
    return out


def search_kuaishou(th: TikHub, kw: str) -> list[dict]:
    d = th.get("/api/v1/kuaishou/app/search_video_v2", keyword=kw)
    out = []
    for f in ((d.get("data") or {}).get("mixFeeds") or []):
        fd = f.get("feed") or {}
        if not fd.get("photo_id"):
            continue
        share = {k: v[0] for k, v in parse_qs(fd.get("share_info") or "").items()}
        out.append({"platform": "kuaishou", "post_id": share.get("photoId") or str(fd["photo_id"]),
                    "url": f"https://www.kuaishou.com/short-video/{share['photoId']}" if share.get("photoId") else None,
                    "title": _clip(fd.get("caption")), "author": fd.get("user_name"), "author_id": share.get("userId") or str(fd.get("user_id") or ""),
                    "profile_url": f"https://www.kuaishou.com/profile/{share['userId']}" if share.get("userId") else None,
                    "fans": None, "likes": fd.get("like_count"), "comments": fd.get("comment_count"), "collects": None,
                    "published_at": _ts((fd.get("timestamp") or 0) / 1000)})
    return out


def douyin_users(th: TikHub, kw: str, lo: int, hi: int) -> list[dict]:
    """按人群词搜抖音用户，粉丝数在 [lo, hi] 的留下（接口的粉丝档位不严，自己再筛一遍）。"""
    seen, out = set(), []
    for band in ("10k_100k", "100k_1M"):
        d = _post(th, "/api/v1/douyin/search/fetch_user_search",
                  {"keyword": kw, "cursor": 0, "douyin_user_fans": band, "douyin_user_type": "", "search_id": ""})
        for u in ((d.get("data") or {}).get("user_list") or []):
            ui = u.get("user_info")
            if not ui:
                try:
                    ui = json.loads(((u.get("dynamic_patch") or {}).get("raw_data")) or "{}").get("user_info") or {}
                except json.JSONDecodeError:
                    ui = {}
            sec, fans = ui.get("sec_uid"), ui.get("follower_count") or 0
            if not sec or sec in seen or not lo <= fans <= hi:
                continue
            seen.add(sec)
            out.append({"author": ui.get("nickname"), "author_id": sec, "fans": fans,
                        "profile_url": f"https://www.douyin.com/user/{sec}"})
    return out


def douyin_latest(th: TikHub, sec_uid: str) -> dict | None:
    """主页最近一条非置顶作品。"""
    d = th.get("/api/v1/douyin/app/v3/fetch_user_post_videos", sec_user_id=sec_uid, max_cursor=0, count=6, sort_type=0)
    posts = [a for a in ((d.get("data") or {}).get("aweme_list") or []) if a.get("aweme_id") and not a.get("is_top")]
    if not posts:
        return None
    a = max(posts, key=lambda x: x.get("create_time") or 0)
    st = a.get("statistics") or {}
    return {"post_id": str(a["aweme_id"]), "url": f"https://www.douyin.com/video/{a['aweme_id']}", "title": _clip(a.get("desc")),
            "likes": st.get("digg_count"), "comments": st.get("comment_count"), "collects": st.get("collect_count"),
            "published_at": _ts(a.get("create_time"))}


SEARCH = {"douyin": search_douyin, "xiaohongshu": search_xiaohongshu, "wechat_channels": search_wechat_channels,
          "kuaishou": search_kuaishou}


def collect(terms: list[dict], th: TikHub, *, platforms=PLATFORMS, budget_left: int | None = None) -> dict:
    """搜一遍。返回 {hits: {id: hit}, per_term: {词: 条数}, per_platform: {平台: 条数}, errors: [..], requests, seconds}。
    只留近 24 小时发布的（快手没有时间筛选，抖音偶尔混进旧的）；同一条作品被几个词搜到只记一条、记下所有词。"""
    t0, cutoff = time.time(), now() - WINDOW
    hits: dict[str, dict] = {}
    per_term = {t["term"]: 0 for t in terms}
    per_platform = {p: 0 for p in platforms}
    errors: list[str] = []
    for t in [t for t in terms if t.get("industry") != "persona"]:
        for pf in platforms:
            if budget_left is not None and th.requests >= budget_left:
                errors.append("TikHub 今日请求预算用完，后面的词没搜")
                break
            try:
                try:
                    rows = SEARCH[pf](th, t["term"])
                except Exception:  # noqa: BLE001  接口偶尔回 400 / 5xx：隔两秒重试一次
                    time.sleep(2)
                    rows = SEARCH[pf](th, t["term"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{pf}「{t['term']}」：{str(e)[:120]}")
                continue
            for h in rows:
                if not h["published_at"] or h["published_at"] < iso(cutoff):
                    continue
                key = f"{pf}:{h['post_id']}"
                cur = hits.setdefault(key, {**h, "id": key, "terms": [], "categories": []})
                if t["term"] not in cur["terms"]:
                    cur["terms"].append(t["term"])
                    cur["categories"].append(t.get("industry") or "")
                    per_term[t["term"]] += 1
        else:
            continue
        break
    for h in hits.values():
        per_platform[h["platform"]] += 1
    return {"hits": hits, "per_term": per_term, "per_platform": per_platform, "errors": errors,
            "requests": th.requests, "seconds": round(time.time() - t0, 1)}


def collect_people(terms: list[dict], th: TikHub, s: dict, known: set[str], *, budget_left: int | None = None) -> dict:
    """抖音按人群词找人：搜用户 → 粉丝数在画像区间里、以前没见过的 → 取最近一条非置顶作品（7 天内）。"""
    hits: dict[str, dict] = {}
    per_term: dict[str, int] = {}
    errors: list[str] = []
    cands: list[tuple[dict, str]] = []
    for t in [t for t in terms if t.get("industry") == "persona"]:
        per_term[t["term"]] = 0
        if budget_left is not None and th.requests >= budget_left:
            errors.append("TikHub 今日请求预算用完，人群词没搜完")
            break
        try:
            for u in douyin_users(th, t["term"], s["min_followers"], s["max_followers"]):
                if u["author_id"] not in known and all(u["author_id"] != c[0]["author_id"] for c in cands):
                    cands.append((u, t["term"]))
        except Exception as e:  # noqa: BLE001
            errors.append(f"douyin 找人「{t['term']}」：{str(e)[:120]}")
    cands.sort(key=lambda c: -c[0]["fans"])
    cutoff = iso(now() - PERSONA_WINDOW)
    for u, term in cands[: int(s["user_post_max"])]:
        if budget_left is not None and th.requests >= budget_left:
            break
        try:
            p = douyin_latest(th, u["author_id"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"douyin 主页「{u['author']}」：{str(e)[:120]}")
            continue
        if not p or not p["published_at"] or p["published_at"] < cutoff:
            continue
        key = f"douyin:{p['post_id']}"
        hits[key] = {"platform": "douyin", **p, **u, "id": key, "terms": [term], "categories": ["persona"]}
        per_term[term] += 1
    return {"hits": hits, "per_term": per_term, "errors": errors, "candidates": len(cands)}


def enrich_fans(ops, hits: dict[str, dict], th: TikHub, limit: int, *, budget_left: int | None = None) -> int:
    """小红书作者补粉丝数：先用 30 天内库里记过的，再按笔记互动从高到低补查，最多 limit 个作者。"""
    since = iso(now() - timedelta(days=30))
    by_author: dict[str, list[dict]] = {}
    for h in hits.values():
        if h["platform"] == "xiaohongshu" and h.get("fans") is None and h.get("author_id"):
            by_author.setdefault(h["author_id"], []).append(h)
    for aid, hs in list(by_author.items()):
        r = ops.execute("SELECT fans FROM mentions WHERE platform='xiaohongshu' AND author_id=? AND fans IS NOT NULL AND collected_at>=?"
                        " ORDER BY collected_at DESC LIMIT 1", (aid, since)).fetchone()
        if r:
            for h in hs:
                h["fans"] = r[0]
            del by_author[aid]
    order = sorted(by_author, key=lambda a: -max((h.get("likes") or 0) + (h.get("collects") or 0) + (h.get("comments") or 0)
                                                 for h in by_author[a]))
    n = 0
    for aid in order[: int(limit)]:
        if budget_left is not None and th.requests >= budget_left:
            break
        try:
            fans = (((th.get("/api/v1/xiaohongshu/app_v2/get_user_info", user_id=aid).get("data") or {}).get("data") or {}).get("fans"))
        except Exception:  # noqa: BLE001
            continue
        if fans is not None:
            for h in by_author[aid]:
                h["fans"] = int(fans)
            n += 1
    return n


def store(ops, hits: dict[str, dict], collected_at: str) -> list[str]:
    """入库，返回这次新出现的作品（以前搜到过的不再交给 Agent 重判）。"""
    new = []
    for key, h in hits.items():
        n = ops.execute(
            "INSERT OR IGNORE INTO mentions(id,platform,post_id,url,title,author,author_id,profile_url,fans,likes,comments,collects,"
            "published_at,terms,categories,collected_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'tikhub')",
            (key, h["platform"], h["post_id"], h["url"], h["title"], h["author"], h["author_id"], h["profile_url"], h["fans"],
             h["likes"], h["comments"], h["collects"], h["published_at"], json.dumps(h["terms"], ensure_ascii=False),
             json.dumps(h["categories"], ensure_ascii=False), collected_at)).rowcount
        if n:
            new.append(key)
    return new


def update_term_hits(ops) -> None:
    """近 7 天命中数：按 mentions 里每条作品命中的词算（浏览器补位的命中由 Agent 的 term_stats 另记）。"""
    since = iso(now() - timedelta(days=7))
    for (term,) in ops.execute("SELECT term FROM listening_terms").fetchall():
        n = ops.execute("SELECT COUNT(*) FROM mentions WHERE collected_at>=? AND terms LIKE ?",
                        (since, f'%{json.dumps(term, ensure_ascii=False)}%')).fetchone()[0]
        ops.execute("UPDATE listening_terms SET hits_7d=? WHERE term=?", (n, term))


def report(r: dict, new: list[str]) -> str:
    from .. import platforms as pf_mod
    lines = [f"# 社媒监听 · 接口（{now().strftime('%Y-%m-%d %H:%M')}，近 24 小时）", "",
             f"- 用时 {r['seconds']} 秒，TikHub 请求 {r['requests']} 次；去重后 {len(r['hits'])} 条作品，其中新出现 {len(new)} 条",
             f"- 抖音按人群词找人：画像区间内的新作者 {r.get('people', {}).get('candidates', 0)} 个，取到 7 天内作品的 {r.get('people', {}).get('kept', 0)} 个；"
             f"小红书补查粉丝数 {r.get('enriched', 0)} 个作者",
             "- 各平台：" + "，".join(f"{pf_mod.label(p)} {n}" for p, n in r["per_platform"].items()),
             "- 各词：" + "，".join(f"{t} {n}" for t, n in sorted(r["per_term"].items(), key=lambda x: -x[1])), ""]
    zero = [t for t, n in r["per_term"].items() if not n]
    if zero:
        lines.append(f"- 零命中：{'、'.join(zero)}")
    if r["errors"]:
        lines += ["", "## 出错", *[f"- {e}" for e in r["errors"]]]
    return "\n".join(lines)


def cmd_collect() -> None:
    import os

    cfg = load_agents_config()
    ops = db.open_ops()
    from ..ingest import sync_terms
    sync_terms(ops)
    terms = [dict(r) for r in ops.execute("SELECT term, industry FROM listening_terms WHERE active=1 AND removed_at IS NULL ORDER BY term")]
    th = TikHub(proxy=None)
    if not th.configured:
        emit_result("没配 TikHub Key（钥匙串 popto-ops/tikhub），接口监听跳过", status="failed")
        return
    if not terms:
        emit_result("没有在用的监听意图词（面板“业务 → 获客”添加），跳过")
        return
    hub = hub_client()
    budget = int((cfg.get("budgets") or {}).get("tikhub_requests", 0) or 0)
    try:
        used = int(hub.get("/v1/counter", name="tikhub_requests").get("value", 0))
    except Exception:  # noqa: BLE001
        used = 0
    s = settings(cfg)
    left = (budget - used) if budget else None
    t_all = time.time()
    r = collect(terms, th, budget_left=left)
    known = {row[0] for row in ops.execute("SELECT author_id FROM mentions WHERE platform='douyin' AND author_id IS NOT NULL")}
    known |= {row[0].rsplit("/", 1)[-1] for row in ops.execute("SELECT profile_url FROM leads WHERE platform='douyin' AND profile_url IS NOT NULL")}
    people = collect_people(terms, th, s, known, budget_left=left)
    for k, h in people["hits"].items():
        if k not in r["hits"]:
            r["hits"][k] = h
            r["per_platform"]["douyin"] += 1
    r["per_term"].update(people["per_term"])
    r["errors"] += people["errors"]
    r["people"] = {"candidates": people["candidates"], "kept": len(people["hits"])}
    r["enriched"] = enrich_fans(ops, r["hits"], th, s["enrich_max"], budget_left=left)
    r["requests"], r["seconds"] = th.requests, round(time.time() - t_all, 1)
    try:
        hub.post("/v1/counter", {"name": "tikhub_requests", "value": th.requests})
    except Exception:  # noqa: BLE001
        pass
    at = iso()
    new = store(ops, r["hits"], at)
    update_term_hits(ops)
    ops.commit()
    md = report(r, new)
    run_dir = os.environ.get("OPS_RUN_DIR")
    artifacts = []
    if run_dir:
        p = Path(run_dir) / "接口监听.md"
        p.write_text(md, encoding="utf-8")
        artifacts.append(str(p))
    events = []
    if new:
        events.append({"source": "collector", "type": "social.mention.new", "subject": f"mentions:{now().strftime('%Y%m%d-%H%M')}",
                       "summary": f"接口监听：近 24 小时新作品 {len(new)} 条（{'，'.join(f'{k} {v}' for k, v in r['per_platform'].items() if v)}），判断线索",
                       "payload_ref": f"ops.db://mentions?since={at}", "dedup_key": f"mentions:{at}",
                       "data": {"count": len(new), "per_platform": r["per_platform"], "seconds": r["seconds"], "requests": r["requests"]}})
    summary = (f"接口监听：{r['seconds']} 秒、{r['requests']} 次请求，近 24 小时作品 {len(r['hits'])} 条（新 {len(new)} 条）；"
               + "，".join(f"{k} {v}" for k, v in r["per_platform"].items()) + (f"；{len(r['errors'])} 个出错" if r["errors"] else ""))
    emit_result(summary, follow_up_events=events, message_markdown=md, artifacts=artifacts, errors=r["errors"],
                status="failed" if r["errors"] and not r["hits"] else "done")


if __name__ == "__main__":
    if sys.argv[1:2] == ["collect"]:
        cmd_collect()
    else:
        print(__doc__)
        sys.exit(2)
