"""竞品调研的数据采集：竞品的作品、作品下的用户评论、竞品的社媒账号。数据给两条规则用：
- 竞品评论 → 需求评审（规则 competitor-review，只读）：从评论里归纳候选需求，评审卡片上每条都带来源作品；人批的进需求池。
- 竞品调研（规则 competitor-research，Claude 带网页搜索）：结合官网 / 定价页和这里采到的账号、作品，写竞品调研报告。

python -m superops.biz.collectors.competitors collect    # cron.competitor_review（每周四）：作品 + 评论；调研到期（默认 28 天）时顺带采账号、发调研事件
python -m superops.biz.collectors.competitors research   # cron.competitor_research（手动 superops fire）：只采作品和账号，立刻发调研事件

竞品名单就是监听意图词里类别为 competitor 的词（面板“业务 → 获客”改）。每个竞品每个平台搜一页：抖音按“最多点赞 + 最近半年”、
小红书按评论数；取评论最多的 POSTS_PER 条作品拉一页评论（抖音 50、小红书 10）。账号：抖音 / 小红书按竞品名搜用户，各留粉丝最多的
ACCOUNTS_PER 个（抖音带企业认证、小红书带官方认证标记），每天一份快照，看粉丝变化。
只存公开内容：评论原文和所在作品（链接、标题、作者），不存评论者身份；评论 RETAIN_DAYS 天后删除。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

from .. import db, paths
from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client
from ..timeutil import iso, now, parse_iso
from .listening import _clip, _post, _ts
from .tikhub import TikHub

POSTS_PER = 3
ACCOUNTS_PER = 3
MIN_LEN = 6
RETAIN_DAYS = 180
RESEARCH_EVERY = timedelta(days=28)


# ---------------------------------------------------------------------- 作品
def douyin_posts(th: TikHub, kw: str) -> list[dict]:
    d = _post(th, "/api/v1/douyin/search/fetch_video_search_v2", {
        "keyword": kw, "cursor": 0, "sort_type": "1", "publish_time": "180", "filter_duration": "0", "content_type": "0", "search_id": ""})
    out = []
    for b in ((d.get("data") or {}).get("business_data") or []):
        a = (b.get("data") or {}).get("aweme_info") or {}
        if a.get("aweme_id"):
            st = a.get("statistics") or {}
            out.append({"platform": "douyin", "post_id": str(a["aweme_id"]), "url": f"https://www.douyin.com/video/{a['aweme_id']}",
                        "title": _clip(a.get("desc"), 80), "author": (a.get("author") or {}).get("nickname"),
                        "likes": st.get("digg_count"), "n_comments": st.get("comment_count") or 0, "published_at": _ts(a.get("create_time"))})
    return out


def xhs_posts(th: TikHub, kw: str) -> list[dict]:
    d = th.get("/api/v1/xiaohongshu/app_v2/search_notes", keyword=kw, page=1, sort_type="comment_descending")
    out = []
    for it in (((d.get("data") or {}).get("data") or {}).get("items") or []):
        n = it.get("note") or {}
        if it.get("model_type") == "note" and n.get("id"):
            out.append({"platform": "xiaohongshu", "post_id": n["id"], "url": f"https://www.xiaohongshu.com/explore/{n['id']}",
                        "title": _clip(n.get("title") or n.get("desc"), 80), "author": (n.get("user") or {}).get("nickname"),
                        "likes": n.get("liked_count"), "n_comments": n.get("comments_count") or 0, "published_at": _ts(n.get("timestamp"))})
    return out


def douyin_comments(th: TikHub, post_id: str) -> list[dict]:
    rows, _, _ = th.douyin_comments(post_id, count=50)
    return [{"id": r["id"], "text": r["text"], "likes": r["likes"], "created_at": _ts(r["created_at"])} for r in rows]


def xhs_comments(th: TikHub, post_id: str) -> list[dict]:
    d = th.get("/api/v1/xiaohongshu/app_v2/get_note_comments", note_id=post_id)
    data = (d.get("data") or {}).get("data") or d.get("data") or {}
    return [{"id": c.get("id"), "text": c.get("content") or "", "likes": c.get("like_count") or 0, "created_at": _ts(c.get("time"))}
            for c in (data.get("comments") or []) if c.get("id")]


# ---------------------------------------------------------------------- 账号
def _fans_text(s: str | None) -> int | None:
    """小红书搜用户的 sub_title 形如 “Fans 40.4k” / “粉丝 1.2万”。"""
    m = re.search(r"([\d.]+)\s*([kKwW万]?)", s or "")
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"k": 1e3, "K": 1e3, "w": 1e4, "W": 1e4, "万": 1e4}.get(m.group(2), 1))


def douyin_accounts(th: TikHub, kw: str) -> list[dict]:
    d = _post(th, "/api/v1/douyin/search/fetch_user_search", {"keyword": kw, "cursor": 0, "douyin_user_fans": "", "douyin_user_type": "", "search_id": ""})
    out = []
    for u in ((d.get("data") or {}).get("user_list") or []):
        ui = u.get("user_info")
        if not ui:
            try:
                ui = json.loads(((u.get("dynamic_patch") or {}).get("raw_data")) or "{}").get("user_info") or {}
            except json.JSONDecodeError:
                ui = {}
        if ui.get("sec_uid"):
            out.append({"platform": "douyin", "account_id": ui["sec_uid"], "name": ui.get("nickname"),
                        "url": f"https://www.douyin.com/user/{ui['sec_uid']}", "fans": ui.get("follower_count"),
                        "verified": ui.get("enterprise_verify_reason") or ui.get("custom_verify") or ""})
    return sorted(out, key=lambda a: -(a["fans"] or 0))[:ACCOUNTS_PER]


def xhs_accounts(th: TikHub, kw: str) -> list[dict]:
    d = th.get("/api/v1/xiaohongshu/app_v2/search_users", keyword=kw, page=1)
    data = (d.get("data") or {}).get("data") or d.get("data") or {}
    out = []
    for u in (data.get("users") or []):
        if u.get("id"):
            out.append({"platform": "xiaohongshu", "account_id": u["id"], "name": u.get("name") or u.get("nickname"),
                        "url": f"https://www.xiaohongshu.com/user/profile/{u['id']}", "fans": _fans_text(u.get("sub_title")),
                        "verified": "官方认证" if u.get("red_official_verified") else ""})
    return sorted(out, key=lambda a: (not a["verified"], -(a["fans"] or 0)))[:ACCOUNTS_PER]


GENERIC = ("数字人", "AI", "ai", "Ai", "官方", "App", "APP", " ")


def core_name(term: str) -> str:
    """竞品词里的产品名：“蝉镜数字人”→“蝉镜”，“腾讯智影数字人”→“腾讯智影”。去掉通用词后太短就用原词。"""
    core = term
    for g in GENERIC:
        core = core.replace(g, "")
    return core if len(core) >= 2 else term


def relevant(text: str | None, term: str) -> bool:
    """账号名 / 作品标题或作者里得出现竞品的产品名，否则多半是搜索带出来的无关结果（2026-11-12 调研发现约四分之一无关）。"""
    return core_name(term).lower() in (text or "").lower()


POSTS = {"douyin": douyin_posts, "xiaohongshu": xhs_posts}
COMMENTS = {"douyin": douyin_comments, "xiaohongshu": xhs_comments}
ACCOUNTS = {"douyin": douyin_accounts, "xiaohongshu": xhs_accounts}


def _call(errors: list[str], what: str, fn, *a):
    """调接口：偶发 400 / 5xx 隔两秒重试一次，还不行就记下来、跳过。"""
    for attempt in (0, 1):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001
            if attempt:
                errors.append(f"{what}：{str(e)[:120]}")
                return None
            time.sleep(2)


def collect(competitors: list[str], th: TikHub, *, comments: bool = True, accounts: bool = False,
            budget_left: int | None = None) -> dict:
    """返回 {comments, posts, accounts, errors, requests}。同一条评论只留一次。"""
    out = {"comments": [], "posts": [], "accounts": [], "errors": [], "requests": 0}
    seen = set()
    for comp in competitors:
        for pf in POSTS:
            if budget_left is not None and th.requests >= budget_left:
                out["errors"].append("TikHub 今日请求预算用完，没采完")
                out["requests"] = th.requests
                return out
            if accounts:
                for a in _call(out["errors"], f"{pf} 搜账号「{comp}」", ACCOUNTS[pf], th, core_name(comp)) or []:
                    if relevant(a["name"], comp):
                        out["accounts"].append({**a, "competitor": comp})
            found = [p for p in _call(out["errors"], f"{pf} 搜「{comp}」", POSTS[pf], th, comp) or []
                     if relevant(f"{p['title']} {p.get('author') or ''}", comp)]
            for p in sorted(found, key=lambda x: -x["n_comments"])[:POSTS_PER]:
                out["posts"].append({**p, "competitor": comp})
                if not comments or not p["n_comments"]:
                    continue
                for c in _call(out["errors"], f"{pf} 拉评论 {p['post_id']}", COMMENTS[pf], th, p["post_id"]) or []:
                    text = " ".join((c["text"] or "").split())
                    key = f"{pf}:{c['id']}"
                    if len(text) < MIN_LEN or key in seen:
                        continue
                    seen.add(key)
                    out["comments"].append({"id": key, "platform": pf, "competitor": comp, "post_id": p["post_id"], "post_url": p["url"],
                                            "post_title": p["title"], "post_author": p["author"], "text": text[:300],
                                            "likes": c["likes"], "created_at": c["created_at"]})
    out["requests"] = th.requests
    return out


def store(ops, r: dict, at: str) -> int:
    """入库，返回新评论条数。作品按 平台:作品ID 去重（更新点赞评论数），账号每天一份快照。"""
    n = 0
    for c in r["comments"]:
        n += ops.execute("INSERT OR IGNORE INTO competitor_comments(id,platform,competitor,post_id,post_url,post_title,post_author,text,"
                         "likes,created_at,collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (c["id"], c["platform"], c["competitor"], c["post_id"], c["post_url"], c["post_title"], c["post_author"],
                          c["text"], c["likes"], c["created_at"], at)).rowcount
    for p in r["posts"]:
        ops.execute("INSERT INTO competitor_works(id,competitor,platform,post_id,url,title,author,likes,comments,published_at,collected_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET likes=excluded.likes, comments=excluded.comments,"
                    " collected_at=excluded.collected_at",
                    (f"{p['platform']}:{p['post_id']}", p["competitor"], p["platform"], p["post_id"], p["url"], p["title"], p["author"],
                     p.get("likes"), p["n_comments"], p.get("published_at"), at))
    for a in r["accounts"]:
        ops.execute("INSERT OR REPLACE INTO competitor_accounts(platform,account_id,day,competitor,name,url,fans,verified,collected_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (a["platform"], a["account_id"], at[:10], a["competitor"], a["name"], a["url"],
                                                     a["fans"], a["verified"], at))
    ops.execute("DELETE FROM competitor_comments WHERE collected_at<?", (iso(now() - timedelta(days=RETAIN_DAYS)),))
    return n


def research_due(force: bool = False) -> bool:
    mark = paths.ops_data() / "competitor-research.last"
    if force or not mark.exists():
        return True
    try:
        return now() - parse_iso(mark.read_text().strip()) >= RESEARCH_EVERY
    except ValueError:
        return True


def mark_research() -> None:
    (paths.ops_data() / "competitor-research.last").write_text(iso(), encoding="utf-8")


def report(comps: list[str], r: dict, new: int, seconds: float) -> str:
    lines = [f"# 竞品数据采集（{now().strftime('%Y-%m-%d %H:%M')}）", "",
             f"- 竞品：{'、'.join(comps)}；用时 {round(seconds)} 秒，TikHub 请求 {r['requests']} 次",
             f"- 作品 {len(r['posts'])} 条、评论 {len(r['comments'])} 条（新 {new} 条）、账号 {len(r['accounts'])} 个", ""]
    if r["accounts"]:
        lines += ["## 账号", "| 竞品 | 平台 | 账号 | 粉丝 | 认证 | 链接 |", "| --- | --- | --- | --- | --- | --- |",
                  *[f"| {a['competitor']} | {a['platform']} | {a['name']} | {a['fans'] if a['fans'] is not None else '?'} | {a['verified'] or '-'} | {a['url']} |"
                    for a in r["accounts"]], ""]
    lines += ["## 作品", "| 竞品 | 平台 | 作者 | 作品 | 赞 | 评论 | 链接 |", "| --- | --- | --- | --- | --- | --- | --- |",
              *[f"| {p['competitor']} | {p['platform']} | {p['author'] or '-'} | {p['title']} | {p.get('likes') or 0} | {p['n_comments']} | {p['url']} |"
                for p in r["posts"]]]
    if r["errors"]:
        lines += ["", "## 出错", *[f"- {e}" for e in r["errors"]]]
    return "\n".join(lines)


def run(mode: str) -> None:
    """mode=collect：每周（评论 + 作品；调研到期就带上账号并发调研事件）；mode=research：立刻调研（作品 + 账号）。"""
    cfg = load_agents_config()
    ops = db.open_ops()
    from ..ingest import sync_terms
    sync_terms(ops)
    comps = [r[0] for r in ops.execute("SELECT term FROM listening_terms WHERE industry='competitor' AND active=1 AND removed_at IS NULL ORDER BY term")]
    if not comps:
        emit_result("没有在用的竞品词（面板“业务 → 获客 → 监听的意图词”添加，类别选竞品词），跳过")
        return
    th = TikHub(proxy=None)
    if not th.configured:
        emit_result("没配 TikHub Key（钥匙串 popto-ops/tikhub），竞品数据采集跳过", status="failed")
        return
    hub = hub_client()
    budget = int((cfg.get("budgets") or {}).get("tikhub_requests", 0) or 0)
    try:
        used = int(hub.get("/v1/counter", name="tikhub_requests").get("value", 0))
    except Exception:  # noqa: BLE001
        used = 0
    research = research_due(force=mode == "research")
    t0 = time.time()
    r = collect(comps, th, comments=mode == "collect", accounts=research, budget_left=(budget - used) if budget else None)
    try:
        hub.post("/v1/counter", {"name": "tikhub_requests", "value": th.requests})
    except Exception:  # noqa: BLE001
        pass
    at = iso()
    new = store(ops, r, at)
    ops.commit()
    md = report(comps, r, new, time.time() - t0)
    artifacts = []
    if os.environ.get("OPS_RUN_DIR"):
        p = Path(os.environ["OPS_RUN_DIR"]) / "竞品数据.md"
        p.write_text(md, encoding="utf-8")
        artifacts.append(str(p))
    stamp = now().strftime("%Y%m%d-%H%M")
    events = []
    if new:
        per = {}
        for c in r["comments"]:
            per[c["competitor"]] = per.get(c["competitor"], 0) + 1
        events.append({"source": "collector", "type": "competitor.comments.batch", "subject": f"competitors:{stamp}",
                       "summary": f"竞品评论 {new} 条（{'、'.join(per)}），挖需求并评审",
                       "payload_ref": f"ops.db://competitor_comments?since={at}", "dedup_key": f"competitors:{at}",
                       "data": {"count": new, "competitors": comps, "posts": len(r["posts"])}})
    if research and (r["posts"] or r["accounts"]):
        events.append({"source": "collector", "type": "competitor.data.ready", "subject": f"competitor-research:{stamp}",
                       "summary": f"竞品调研：{'、'.join(comps)}（账号 {len(r['accounts'])} 个、作品 {len(r['posts'])} 条）",
                       "payload_ref": f"ops.db://competitor_works?since={at}", "dedup_key": f"competitor-research:{at}",
                       "data": {"competitors": comps, "accounts": r["accounts"]}})
        mark_research()
    what = f"{len(comps)} 个竞品、{len(r['posts'])} 条作品" + (f"、新评论 {new} 条" if mode == "collect" else "") + \
        (f"、账号 {len(r['accounts'])} 个，已发起竞品调研" if research else "")
    emit_result(f"竞品数据采集：{what}、{r['requests']} 次请求" + (f"；{len(r['errors'])} 个出错" if r["errors"] else ""),
                follow_up_events=events, message_markdown=md, artifacts=artifacts, errors=r["errors"],
                status="failed" if r["errors"] and not (r["posts"] or r["comments"]) else "done")


def cmd_collect() -> None:
    run("collect")


if __name__ == "__main__":
    if sys.argv[1:2] in (["collect"], ["research"]):
        run(sys.argv[1])
    else:
        print(__doc__)
        sys.exit(2)
