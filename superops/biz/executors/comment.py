"""用品牌号在别人的作品下发一条评论（获客：人在“今日线索”卡片上批了“跟进并发评论”之后才会执行）。

复用发布用的登录态（browser-profiles/<平台>/），和发布、数据采集按平台排队用同一个 Chrome。
目前支持抖音网页版（www.douyin.com/video/<作品ID>）；别的平台返回 supported=False，由人去发。

抖音网页版视频页的评论区要点（2026-11-12 对真实页面核对）：
- 视频下方“全部评论”下面是评论框，未聚焦时只显示占位文字“留下你的精彩评论吧”；点一下才变成 DraftJS 编辑器
  （div.public-DraftEditor-content[contenteditable=true]）。没登录时这里是“登录”按钮或点了弹登录框。
- 输入文字后编辑器右侧出现红色的发送按钮；按回车同样发送。发出后编辑器清空，新评论出现在评论列表顶部。
- 频繁评论会触发滑块 / 验证码弹窗：自动程序不碰，转人工。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ...infra import paths
from ...infra.timeutil import now

SUPPORTED = {"douyin"}
MAX_LEN = 100                  # 抖音评论上限更长，这里按“短、具体”的口径卡在 100 字
PLACEHOLDER = "留下你的精彩评论吧"
VERIFY_HINTS = ("验证码", "安全验证", "滑块", "请完成验证", "拖动滑块")
LOGIN_HINTS = ("登录后评论", "请先登录", "扫码登录")


@dataclass
class CommentResult:
    ok: bool
    supported: bool = True
    note: str = ""
    shots: list[str] = field(default_factory=list)


def shots_dir(lead_id: str) -> Path:
    d = paths.ops_data() / "comment-shots" / lead_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_text(text: str) -> str | None:
    """发之前的硬检查：空、太长、带链接 / 联系方式的都不发。"""
    t = (text or "").strip()
    if not t:
        return "没有评论原文"
    if len(t) > MAX_LEN:
        return f"评论超过 {MAX_LEN} 字"
    low = t.lower()
    if any(k in low for k in ("http", "www.", ".com", ".cn", "vx", "v信", "微信", "加我", "私信我", "电话")):
        return "评论里带了链接或联系方式（口径是不带链接、不引导私信）"
    return None


def post(platform: str, url: str, text: str, *, lead_id: str, headless: bool = True) -> CommentResult:
    if platform not in SUPPORTED:
        return CommentResult(False, supported=False, note="这个平台还不能自动发评论，请人用品牌号去发")
    bad = check_text(text)
    if bad:
        return CommentResult(False, note=bad)
    if not url or "douyin.com/video/" not in url:
        return CommentResult(False, note="没有具体作品的链接（要 www.douyin.com/video/<作品ID>），不知道评论在哪条下面")
    return _douyin(url, text.strip(), shots_dir(lead_id), headless=headless)


def _shot(page, d: Path, name: str, res: CommentResult) -> None:
    p = d / f"douyin-{now().strftime('%H%M%S')}-{name}.png"
    try:
        page.screenshot(path=str(p))
        res.shots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def _body(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        return ""


def _douyin(url: str, text: str, d: Path, *, headless: bool) -> CommentResult:
    from .publishers import browser, default_account, friendly_error

    res = CommentResult(False)
    try:
        with browser(default_account("douyin"), headless=headless, error_shots=d) as ctx:   # 品牌号 = 抖音的默认账号
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            box = page.get_by_text(PLACEHOLDER).first
            try:
                box.wait_for(timeout=30000)
            except Exception:  # noqa: BLE001
                body = _body(page)
                _shot(page, d, "no-comment-box", res)
                if any(h in body for h in LOGIN_HINTS):
                    res.note = "品牌号在抖音网页版没登录（superops publish login douyin）"
                elif "作品已删除" in body or "视频不存在" in body or "该内容暂时无法" in body:
                    res.note = "作品已删除或不可见"
                else:
                    res.note = "30 秒没等到评论框（作者可能关了评论，或页面改版）"
                return res
            before = page.get_by_text(text, exact=False).count()
            box.click()
            editor = page.locator('[contenteditable="true"].public-DraftEditor-content').first
            editor.wait_for(timeout=10000)
            editor.click()
            page.keyboard.type(text, delay=60)
            time.sleep(1)
            _shot(page, d, "filled", res)
            body = _body(page)
            if any(h in body for h in LOGIN_HINTS):
                res.note = "品牌号在抖音网页版没登录（superops publish login douyin）"
                return res
            page.keyboard.press("Enter")
            # 发出去的标志：编辑器清空，评论列表里多出这段文字
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(1)
                body = _body(page)
                if any(h in body for h in VERIFY_HINTS):
                    _shot(page, d, "needs-verification", res)
                    res.note = "抖音要人做一次验证（滑块 / 验证码），没发出去：请人用品牌号手动发"
                    return res
                try:
                    cleared = not editor.inner_text(timeout=2000).strip()
                except Exception:  # noqa: BLE001
                    cleared = True
                if cleared and page.get_by_text(text, exact=False).count() > before:
                    _shot(page, d, "sent", res)
                    res.ok, res.note = True, "已发出（评论列表里看到了这条）"
                    return res
            _shot(page, d, "unconfirmed", res)
            res.note = "点了发送，但 20 秒内没在评论列表里看到这条：可能被平台拦了或在审核，请人去作品下看一眼"
            return res
    except Exception as e:  # noqa: BLE001
        if getattr(e, "screenshot", None):
            res.shots.append(e.screenshot)  # type: ignore[attr-defined]
        res.note = friendly_error(e)
        return res
