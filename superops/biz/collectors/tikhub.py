"""TikHub 最小客户端（抖音单条作品数据、评论）。Key 放钥匙串 popto-ops/tikhub。

注意（实测坑）：必须带非 Python 默认的 User-Agent，否则 Cloudflare 返回 403 / 1010。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..scriptkit import keychain

BASE = "https://api.tikhub.io"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) popto-ops/0.1"


class TikHub:
    def __init__(self, key: str | None = None, proxy: str | None = None):
        self.key = key or keychain("tikhub")
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {})]
        self.opener = urllib.request.build_opener(*handlers)
        self.requests = 0

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def get(self, path: str, **params) -> dict:
        if not self.key:
            raise RuntimeError("未配置 TikHub Key（钥匙串 popto-ops/tikhub）")
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}", "User-Agent": UA})
        self.requests += 1
        with self.opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def douyin_video_stats(self, aweme_id: str) -> dict:
        """公开接口（他人作品 / 创作者后台不可用时的兜底）。2026-11 起 app v3 返回 aweme_details 列表，
        部分作品为空，再退到 web 接口；web 接口播放数恒为 0（不公开），记成 None 而不是 0，免得被当成“播放为 0”。"""
        d = self.get("/api/v1/douyin/app/v3/fetch_one_video", aweme_id=aweme_id)
        data = d.get("data") or {}
        detail = data.get("aweme_detail") or next(iter(data.get("aweme_details") or []), None)
        source = "app"
        if not detail:
            d = self.get("/api/v1/douyin/web/fetch_one_video", aweme_id=aweme_id)
            detail, source = (d.get("data") or {}).get("aweme_detail"), "web"
        if not detail:
            raise LookupError("TikHub 查不到这条作品（可能已删除、私密或被限流）")
        s = detail.get("statistics") or {}
        views = s.get("play_count")
        if source == "web" and not views:
            views = None
        return {
            "views": views, "likes": s.get("digg_count"), "comments": s.get("comment_count"),
            "shares": s.get("share_count"), "favorites": s.get("collect_count"),
        }

    def douyin_comments(self, aweme_id: str, cursor: int = 0, count: int = 50) -> tuple[list[dict], int, bool]:
        d = self.get("/api/v1/douyin/app/v3/fetch_video_comments", aweme_id=aweme_id, cursor=cursor, count=count)
        data = d.get("data") or {}
        out = []
        for c in data.get("comments") or []:
            out.append({"id": str(c.get("cid")), "text": c.get("text") or "", "likes": c.get("digg_count") or 0,
                        "created_at": c.get("create_time"), "author": ((c.get("user") or {}).get("uid") or "")})
        return out, int(data.get("cursor") or 0), bool(data.get("has_more"))
