"""自有账号的指标：直接读创作者后台（复用自动发布的登录态），比公开接口准。

- 抖音：creator.douyin.com/janus/douyin/creator/pc/work_list（按 max_cursor 翻页，每页 12 条，无需签名）。
  公开接口拿不到自己作品的播放量（web 接口播放数是 0，app 接口对部分作品返回空），所以抖音也以这里为准。
- 视频号：视频号助手首页会发 post/post_list（POST，pageSize 5）。截下这个请求（网址和请求体里带账号参数），
  改 pageSize / currentPage 重放就能翻页（2026-11-10 实测不需要额外签名）。
- 小红书：笔记管理页 /new/note-manager 会发 api/galaxy/v2/creator/note/user/posted?tab=0&page=N。
  这个请求带 x-s 签名，自己拼会返回 406，所以只截页面自己的响应，不够再往下滚让页面加载下一页（data.page = -1 表示没有更多）。

列表都按发布时间从新到旧，只翻到要找的作品都找到为止（只按标题找时，翻到早于 since 为止）。

作品 ID 没登记上的（视频号没有公开链接、自动发布时没截到 ID、人工补登记给的是短链），用 `match_title`
按「标题 + 发布时间」在列表里找，找到后由调用方把 ID 补回 posts 表。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..timeutil import TZ

DOUYIN_WORK_LIST = ("https://creator.douyin.com/janus/douyin/creator/pc/work_list"
                    "?status=0&count=12&max_cursor={cursor}&scene=star_atlas&device_platform=android&aid=1128")
CHANNELS_HOME = "https://channels.weixin.qq.com/platform"
XHS_NOTES = "https://creator.xiaohongshu.com/new/note-manager"
PLATFORMS = ("douyin", "wechat_channels", "xiaohongshu")
LOCK_WAIT = 120   # 发布 / 登录正占着这个平台的浏览器时最多等多久（秒）；等不到就下一轮再采


# ---------------------------------------------------------------- 作品 ID

def norm_id(platform: str, s) -> str:
    s = str(s or "").strip()
    return s.removeprefix("export/") if platform == "wechat_channels" else s


def looks_like_id(platform: str, s) -> bool:
    s = norm_id(platform, s)
    if platform == "douyin":
        return bool(re.fullmatch(r"\d{6,21}", s))
    if platform == "wechat_channels":
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{30,}", s))
    if platform == "xiaohongshu":
        return bool(re.fullmatch(r"[0-9a-f]{24}", s))
    return bool(s)


def id_from_url(platform: str, url: str | None) -> str | None:
    """从人贴的链接里取作品 ID；短链（v.douyin.com、xhslink.com）取不到，返回 None，之后按标题匹配。"""
    u = url or ""
    pats = {"douyin": [r"douyin\.com/(?:video|note)/(\d+)", r"[?&]modal_id=(\d+)"],
            "xiaohongshu": [r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-f]{24})"],
            "wechat_channels": [r"export/([A-Za-z0-9_-]{30,})"]}.get(platform, [])
    for p in pats:
        if m := re.search(p, u):
            return m.group(1)
    return None


# ---------------------------------------------------------------- 各平台一条作品 → 统一结构

def _epoch(v) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v), TZ) if v else None
    except (TypeError, ValueError, OSError):
        return None


def douyin_work(it: dict) -> dict:
    st = it.get("statistics") or {}
    aid = str(it.get("aweme_id") or "")
    return {"id": aid, "titles": [it.get("item_title") or "", it.get("desc") or ""], "created": _epoch(it.get("create_time")),
            "url": f"https://www.douyin.com/video/{aid}" if aid else None,
            "metrics": {"views": st.get("play_count"), "likes": st.get("digg_count"), "comments": st.get("comment_count"),
                        "shares": st.get("share_count") if st.get("share_count") is not None else st.get("forward_count"),
                        "favorites": st.get("collect_count")}}


def channels_work(it: dict) -> dict:
    """视频号：likeCount 是「喜欢♡」；favCount 是「推荐👍」（不是收藏，视频号没有收藏数），没有对应列，不存。"""
    desc = it.get("desc") or {}
    short = next((x.get("shortTitle") for x in desc.get("shortTitle") or [] if isinstance(x, dict)), "") or ""
    return {"id": norm_id("wechat_channels", it.get("objectId") or it.get("exportId")), "titles": [short, desc.get("description") or ""],
            "created": _epoch(it.get("createTime")), "url": None,
            "metrics": {"views": it.get("readCount"), "likes": it.get("likeCount"), "comments": it.get("commentCount"),
                        "shares": it.get("forwardCount"), "follows_gained": it.get("followCount"),
                        "completion_rate": it.get("fullPlayRate"), "avg_watch_sec": it.get("avgPlayTimeSec")}}


def xhs_work(n: dict) -> dict:
    created = None
    try:
        created = datetime.strptime(str(n.get("time") or ""), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        pass
    nid = str(n.get("id") or "")
    return {"id": nid, "titles": [n.get("display_title") or ""], "created": created,
            "url": f"https://www.xiaohongshu.com/explore/{nid}" if nid else None,
            "metrics": {"views": n.get("view_count"), "likes": n.get("likes"), "comments": n.get("comments_count"),
                        "shares": n.get("shared_count"), "favorites": n.get("collected_count")}}


# ---------------------------------------------------------------- 按标题匹配

def norm_title(s: str) -> str:
    return re.sub(r"[\W_]+", "", s or "").lower()   # 去掉空格、标点、表情、#


def same_title(title: str, candidates: list[str]) -> bool:
    """平台会截断标题（抖音 30 字、小红书 20 字、视频号短标题 16 字并去掉符号），所以比前缀；描述里包含标题也算。"""
    t = norm_title(title)
    if len(t) < 2:
        return False
    for c in map(norm_title, candidates):
        if not c:
            continue
        if c == t:
            return True
        n = min(len(c), len(t), 16)
        if n >= 6 and c[:n] == t[:n]:
            return True
        if len(t) >= 8 and t in c:
            return True
    return False


def match_title(title: str, published: datetime | None, works: list[dict], taken: set[str] = frozenset(),
                window: tuple[timedelta, timedelta] = (timedelta(days=3), timedelta(days=1))) -> dict | None:
    """在作品列表里按标题找这条内容：发布时间在 [登记时间 - 3 天, + 1 天] 内、还没被别的记录占用，取时间最近的。
    人工补登记的登记时间可能比真实发布晚，所以往前多放几天。"""
    hits = []
    for w in works:
        if not w["id"] or w["id"] in taken or not same_title(title, w["titles"]):
            continue
        if published and w.get("created") and not (published - window[0] <= w["created"] <= published + window[1]):
            continue
        hits.append(w)
    if not hits:
        return None
    if published:
        return min(hits, key=lambda w: abs((w["created"] - published).total_seconds()) if w.get("created") else 1e12)
    return hits[0]


def pick_new_work(title: str, after: datetime, works: list[dict]) -> dict | None:
    """刚点完发布：在作品列表里找标题对得上、发布时间不早于 after 的最新一条（发布器拿作品 ID 用）。"""
    hits = [w for w in works if w["id"] and w.get("created") and w["created"] >= after and same_title(title, w["titles"])]
    return max(hits, key=lambda w: w["created"]) if hits else None


# ---------------------------------------------------------------- 读列表

def _need_more(platform: str, works: list[dict], want: set[str], since: datetime | None) -> bool:
    found = {w["id"] for w in works}
    if not want <= found:
        return True
    if since is None:
        return False
    oldest = min((w["created"] for w in works if w.get("created")), default=None)
    return oldest is not None and oldest > since


def _douyin(ctx, page, want, since, max_pages) -> list[dict]:
    from ..executors.publishers import NotLoggedIn

    works, cursor = [], 0
    for _ in range(max_pages):
        r = ctx.request.get(DOUYIN_WORK_LIST.format(cursor=cursor), timeout=30000)
        if r.status != 200:
            raise RuntimeError(f"抖音创作者后台作品列表 HTTP {r.status}")
        d = r.json()
        if d.get("status_code") not in (0, None):
            raise NotLoggedIn(f"抖音创作者后台返回 status_code={d.get('status_code')}（多半是登录失效）")
        works += [douyin_work(it) for it in d.get("aweme_list") or []]
        if not d.get("has_more") or not _need_more("douyin", works, want, since):
            break
        cursor = d.get("max_cursor") or 0
    return works


def _channels(ctx, page, want, since, max_pages) -> list[dict]:
    import json

    from ..executors.publishers import NotLoggedIn, is_logged_in

    seen: dict = {}

    def on_req(r):
        if r.url.split("?")[0].endswith("/post/post_list") and "req" not in seen:
            seen["req"] = (r.url, r.post_data)
    page.on("request", on_req)
    page.goto(CHANNELS_HOME, wait_until="commit", timeout=90000)
    for _ in range(40):
        if "req" in seen:
            break
        if "login" in page.url:
            break
        page.wait_for_timeout(1000)
    if "req" not in seen:
        if not is_logged_in("wechat_channels", page.url):
            raise NotLoggedIn("视频号登录已失效：运行 superops publish login wechat_channels 重新扫码")
        raise RuntimeError("视频号助手首页 40 秒没发出作品列表请求（多半是网络慢或页面改版）")
    url, body = seen["req"]
    base = json.loads(body or "{}")
    works = []
    for no in range(1, max_pages + 1):
        r = ctx.request.post(url, data=json.dumps({**base, "pageSize": 20, "currentPage": no}),
                             headers={"content-type": "application/json"}, timeout=30000)
        d = r.json()
        if d.get("errCode") not in (0, None):
            raise RuntimeError(f"视频号作品列表 errCode={d.get('errCode')} {d.get('errMsg') or ''}".strip())
        data = d.get("data") or {}
        works += [channels_work(it) for it in data.get("list") or []]
        if not data.get("continueFlag") or not data.get("list") or not _need_more("wechat_channels", works, want, since):
            break
    return works


def _xhs(ctx, page, want, since, max_pages) -> list[dict]:
    from ..executors.publishers import NotLoggedIn

    pages: list[dict] = []

    def on_resp(r):
        if "/note/user/posted" in r.url:
            try:
                pages.append(r.json())
            except Exception:  # noqa: BLE001
                pass
    page.on("response", on_resp)
    page.goto(XHS_NOTES, wait_until="commit", timeout=90000)
    for _ in range(40):
        if pages or "login" in page.url:
            break
        page.wait_for_timeout(1000)
    if not pages:
        if "login" in page.url:
            raise NotLoggedIn("小红书登录已失效：运行 superops publish login xiaohongshu 重新扫码")
        raise RuntimeError("小红书笔记管理页 40 秒没加载出笔记列表（多半是网络慢或页面改版）")
    works: list[dict] = []
    used = 0
    for _ in range(max_pages):
        for d in pages[used:]:
            if not d.get("success", d.get("code") == 0):
                raise RuntimeError(f"小红书笔记列表返回 code={d.get('code')} {d.get('msg') or ''}".strip())
            works += [xhs_work(n) for n in (d.get("data") or {}).get("notes") or []]
        used = len(pages)
        last_page = ((pages[-1].get("data") or {}).get("page")) if pages else -1
        if last_page == -1 or not _need_more("xiaohongshu", works, want, since):
            break
        page.mouse.move(800, 600)
        page.mouse.wheel(0, 6000)                    # 滚到底，页面自己加载下一页
        for _ in range(10):
            page.wait_for_timeout(800)
            if len(pages) > used:
                break
        else:
            break
    return works


def list_works(platform: str, want_ids: set[str] = frozenset(), *, since: datetime | None = None,
               max_pages: int = 15, lock_wait: float = LOCK_WAIT, account: str | None = None) -> list[dict]:
    """读自有账号的作品列表（新→旧），翻到 want_ids 都找到、且（给了 since 时）早于 since 为止。
    account 是账号键（同一平台多个号时），不给就是主号。没登录抛 NotLoggedIn，别的失败抛异常，调用方降级。"""
    from ..executors.publishers import NotLoggedIn, browser, profile_dir

    fn = {"douyin": _douyin, "wechat_channels": _channels, "xiaohongshu": _xhs}.get(platform)
    if not fn:
        raise LookupError(f"{platform} 没有创作者后台取数源")
    key = account or platform
    if not any(profile_dir(key).iterdir()):
        raise NotLoggedIn(f"{key} 创作者后台没登录过（superops publish login {key}）")
    want = {norm_id(platform, x) for x in want_ids if x}
    with browser(key, lock_wait=lock_wait) as ctx:
        page = ctx.new_page()
        works = fn(ctx, page, want, since, max_pages)
    seen, out = set(), []
    for w in works:                       # 视频号置顶、翻页重叠会有重复
        if w["id"] and w["id"] not in seen:
            seen.add(w["id"])
            out.append(w)
    return out
