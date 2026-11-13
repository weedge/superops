"""小红书创作服务平台发布（creator.xiaohongshu.com，视频笔记和图文笔记）。

页面要点（2026-11-10 实测）：
- /publish/publish?source=official 默认“上传视频”页签，有 input[type=file]（视频）；
- 标题输入框 placeholder “填写标题会有更多赞哦”（≤20 字）；正文是 tiptap / ProseMirror；
- 话题：输入 `#词` 后弹出联想（先“加载中”），回车选第一个；
- “添加内容类型声明”下拉 →“笔记含AI合成内容”；
- 定时发布：`.post-time-wrapper` 里一个开关（`.d-switch`），打开后出现时间输入框（`.date-picker-container input`，
  格式 YYYY-MM-DD HH:mm，可以直接填），默认给的是 1 小时后；
- 底部“暂存离开”“发布”（2026-11-11 起是自定义组件 `<xhs-publish-btn>`，按钮在封闭的 shadow DOM 里，Playwright 定位不到；
  组件属性 `submit-text` 是按钮文字、`submit-disabled` 表示能不能点。用 Chrome 调试接口取按钮坐标再点，见 `_click_submit`）；
  视频上传中时“发布”是灰的。
- 图文：同一个发布页切到“上传图文”页签（`div.creator-tab`，可能被引导弹层 `div.d-popover` 挡住，先移掉），
  第一张图用 `.upload-input`，后面的用 `input[type=file]`，每张等 `.img-preview-area .pr` 多出一个预览（最多 18 张）；
  之后的标题、正文、话题、声明、定时、暂存 / 发布和视频笔记是同一个表单（切页签和上传的写法移植自 Easel 的 xhs_publish.py）。
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from ...timeutil import now
import time

from . import (TIME_FMT, UPLOAD_WAIT_S, NeedsVerification, NotLoggedIn, PublishError, PublishResult, browser, is_logged_in,
               open_until, publish_time, save_draft, shadow_button_center, stamp)

URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
TITLE_PH = "填写标题会有更多赞哦"
VERIFY_TEXT = "扫码验证"
DONE_URL = re.compile(r".*/publish/success.*|.*/new/note-manager.*")
MAX_IMAGES = 18
IMAGE_TAB = "上传图文"


def _click_tab(page, name: str) -> None:
    """点“上传图文 / 上传视频”页签：被引导弹层挡住就移掉弹层再点，15 秒内找不到就报错（Easel mustClickPublishTab 的写法）。"""
    deadline = time.time() + 15
    while time.time() < deadline:
        for tab in page.query_selector_all("div.creator-tab"):
            try:
                if not tab.is_visible() or (tab.inner_text() or "").strip() != name:
                    continue
                blocked = tab.evaluate("""(el) => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return true;
                    const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !(t === el || el.contains(t)); }""")
            except Exception:  # noqa: BLE001
                continue
            if blocked:
                for cover in page.query_selector_all("div.d-popover"):
                    cover.evaluate("el => el.remove()")
                page.wait_for_timeout(300)
                continue
            tab.click()
            page.wait_for_timeout(1200)
            return
        page.wait_for_timeout(300)
    raise PublishError(f"发布页上没找到「{name}」页签（页面可能改版了）")


def _upload_images(page, images: list[str]) -> None:
    """按页序逐张上传，每张等预览出来（最多 60 秒一张）。"""
    for i, path in enumerate(images):
        field = page.locator(".upload-input").first if i == 0 and page.locator(".upload-input").count() else page.locator("input[type=file]").first
        field.set_input_files(path)
        deadline = time.time() + 60
        while len(page.query_selector_all(".img-preview-area .pr")) < i + 1:
            if time.time() > deadline:
                raise PublishError(f"第 {i + 1} 张图片上传超时（60 秒）：{Path(path).name}")
            page.wait_for_timeout(500)
        page.wait_for_timeout(800)


def _wait_published(page, shots: Path, res: PublishResult, *, headless: bool, retry_submit) -> None:
    """点了“发布”之后：等跳到成功页。中途弹出“扫码验证”（要用已登录的小红书 App 扫码）时，
    无人值守就报 NeedsVerification（还没发出去）；有界面运行时等人扫码（最多 10 分钟，二维码过期人自己点刷新），
    验证完平台没自己发出的话，再点一次“发布”。"""
    deadline, verifying = time.time() + 180, False
    while time.time() < deadline:
        if DONE_URL.match(page.url):
            if verifying:
                res.notes.append("发布前小红书要了扫码验证，人已验证")
            return
        box = page.get_by_text(VERIFY_TEXT, exact=True)
        showing = bool(box.count() and box.first.is_visible())
        if showing and not verifying:
            _shot(page, shots, "needs-verification", res)
            if headless:
                err = NeedsVerification("点了发布后小红书要求扫码验证（用已登录该账号的小红书 App 扫码），自动程序做不了，这条还没有发出去")
                err.screenshot = res.screenshots[-1] if res.screenshots else None  # type: ignore[attr-defined]
                raise err
            print("小红书要求扫码验证：请用已登录“该账号”的小红书 App 扫打开的 Chrome 窗口里的二维码（过期就点二维码刷新，最多等 10 分钟）。",
                  flush=True)
            verifying, deadline = True, time.time() + 600
        elif verifying and not showing:
            page.wait_for_timeout(5000)              # 验证完平台一般会自己接着发；没发就再点一次
            if not DONE_URL.match(page.url):
                retry_submit()
            verifying, deadline = False, time.time() + 180
            res.notes.append("发布前小红书要了扫码验证，人已验证")
        page.wait_for_timeout(1000)
    _shot(page, shots, "after-publish", res)
    raise PublishError("点了发布，但没有跳到成功页，请到小红书后台确认是否发出")


def _shot(page, shots: Path, name: str, res: PublishResult) -> None:
    p = shots / f"xhs-{stamp()}-{name}.png"
    try:
        page.screenshot(path=str(p))
        res.screenshots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def _click_submit(page, scheduled: bool) -> str:
    """点底部的“发布 / 定时发布”。先等组件的 submit-disabled 变成 false，再按真实坐标点；老版页面没有组件时按文字点。"""
    texts = ("定时发布", "发布") if scheduled else ("发布",)
    host = page.locator("xhs-publish-btn")
    if host.count():
        for _ in range(60):
            if host.first.get_attribute("submit-disabled") != "true":
                break
            page.wait_for_timeout(1000)
        else:
            raise PublishError("“发布”按钮一直是灰的（可能还有必填项没填、视频还在处理）")
        host.first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        hit = shadow_button_center(page, "xhs-publish-btn", texts)
        if not hit:
            raise PublishError(f"在发布组件里没找到「{texts[0]}」按钮（页面可能又改版了）")
        x, y, text = hit
        page.mouse.click(x, y)
        return text
    page.get_by_text(texts[0], exact=True).last.click(timeout=15000)
    return texts[0]


def _set_time(page, when) -> None:
    """发布时间：when 为 None 保持“立即发布”（开关关着），否则打开“定时发布”开关并填时间；填完核对。"""
    box = page.locator(".post-time-wrapper").first
    box.scroll_into_view_if_needed()
    switch = box.locator(".d-switch").first
    on = "unchecked" not in (box.locator(".d-switch-simulator").first.get_attribute("class") or "unchecked")
    if (when is not None) != on:
        switch.click(timeout=10000)
        page.wait_for_timeout(800)
    if when is None:
        return
    field = box.locator(".date-picker-container input").first
    field.wait_for(timeout=10000)
    text = when.strftime(TIME_FMT)
    field.click()
    field.fill(text)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    more = page.get_by_text("更多设置", exact=True)      # 日历面板不会自己收起：点一下旁边的标题让它关掉
    if more.count():
        more.first.click()
        page.wait_for_timeout(500)
    if field.input_value().strip() != text:
        raise PublishError(f"定时发布时间没设置成功：想填 {text}，页面上是 {field.input_value()!r}")


def publish(pack: dict, meta: dict, *, dry_run: bool, shots: Path, headless: bool = True, draft: bool = False) -> PublishResult:
    res = PublishResult(ok=False, dry_run=dry_run)
    video = Path(pack.get("video") or "")
    images = [str(x) for x in pack.get("images") or [] if Path(x).is_file()]
    if pack.get("video") and not video.is_file():
        raise PublishError(f"视频不存在：{video}")
    if not pack.get("video") and not images:
        raise PublishError("发布包里既没有视频也没有图片")
    if not pack.get("video") and len(images) > MAX_IMAGES:
        raise PublishError(f"小红书图文最多 {MAX_IMAGES} 张，这个发布包有 {len(images)} 张")
    with browser(pack.get("account") or "xiaohongshu", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        try:
            open_until(page, URL, lambda: page.get_by_text("拖拽视频到此或点击上传"), what="发布页")
        except PublishError:
            if "login" in page.url:
                raise NotLoggedIn("小红书登录已失效：运行 superops publish login xiaohongshu 重新扫码")
            raise
        if pack.get("video"):
            page.locator("input[type=file]").first.set_input_files(str(video))
        else:                                        # 图文：切到“上传图文”，按页序逐张传
            _click_tab(page, IMAGE_TAB)
            _upload_images(page, images)
            res.notes.append(f"图文：已按页序上传 {len(images)} 张图")
        page.get_by_placeholder(TITLE_PH).wait_for(timeout=120000)
        page.wait_for_timeout(2500)
        for t in ("我知道了",):
            k = page.get_by_text(t, exact=True)
            if k.count() and k.first.is_visible():
                k.first.click()

        # 标题（≤20 字）
        title = re.sub(r"\s+", " ", pack.get("title") or "").strip()[:20]
        page.get_by_placeholder(TITLE_PH).fill(title)

        # 正文 + 话题
        ed = page.locator(".tiptap.ProseMirror").first
        ed.click()
        body = (pack.get("body") or "").strip()
        for i, line in enumerate(body.splitlines()):
            if i:
                page.keyboard.press("Enter")
            if line:
                page.keyboard.insert_text(line)
        topics = [t.lstrip("#").strip() for t in pack.get("topics") or [] if t.strip("# ")][:5]
        if topics:
            page.keyboard.press("Enter")
        ok_topics = 0
        for t in topics:
            page.keyboard.type("#" + t, delay=60)
            try:   # 等联想列表加载完再回车选第一个
                page.get_by_text("加载中").first.wait_for(state="hidden", timeout=8000)
                page.wait_for_timeout(600)
                page.keyboard.press("Enter")
                ok_topics += 1
            except Exception:  # noqa: BLE001
                page.keyboard.press("Space")
            page.wait_for_timeout(500)
        res.notes.append(f"标题、正文已填，话题 {ok_topics}/{len(topics)} 个识别为话题")

        # 内容类型声明：笔记含 AI 合成内容
        if pack.get("ai_label"):
            try:
                page.get_by_text("添加内容类型声明").click(timeout=10000)
                page.get_by_text("笔记含AI合成内容", exact=True).click(timeout=10000)
                page.wait_for_timeout(800)
                res.notes.append("已声明：笔记含AI合成内容")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 声明没设置成功：{str(e)[:120]}")
                page.keyboard.press("Escape")
        res.notes.append("封面用视频第一帧（平台默认）" if pack.get("video") else "封面用第一张图（平台默认）")

        # 等视频上传完（“上传中”消失）
        deadline = time.time() + UPLOAD_WAIT_S
        while page.get_by_text(re.compile(r"上传中\s*\d+%")).count():
            if time.time() > deadline:
                _shot(page, shots, "uploading", res)
                raise PublishError(f"等了 {UPLOAD_WAIT_S // 60} 分钟视频还在上传")
            page.wait_for_timeout(3000)
        page.wait_for_timeout(2000)
        # 发布时间：立即 / 定时。定时没设置成功就报错转人工，绝不悄悄变成立即发布
        when, timing = publish_time(pack, "xiaohongshu")
        _set_time(page, when)
        res.notes.append(timing)
        res.publish_at = when.isoformat(timespec="seconds") if when else None
        page.get_by_placeholder(TITLE_PH).scroll_into_view_if_needed()
        _shot(page, shots, "filled-top", res)
        page.locator(".post-time-wrapper").first.scroll_into_view_if_needed()
        _shot(page, shots, "filled-bottom", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发布”前，没有发布")
            return res
        if draft:
            def click_draft():   # “暂存离开”和“发布”在同一个封闭的 shadow DOM 组件里，按坐标点
                if not page.locator("xhs-publish-btn").count():
                    return None
                page.locator("xhs-publish-btn").first.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                hit = shadow_button_center(page, "xhs-publish-btn", ("暂存离开",))
                if not hit:
                    return None
                page.mouse.click(hit[0], hit[1])
                return hit[2]
            how = save_draft(page, ("暂存离开",), shot=lambda name: _shot(page, shots, name, res), click=click_draft)
            res.ok = res.draft = True
            res.notes.append(f"已存草稿，没有发布（{how}）")
            return res

        clicked = now() - timedelta(minutes=10)   # 本机和平台时钟可能差几分钟；小红书笔记时间只到分钟
        _click_submit(page, scheduled=when is not None)
        _wait_published(page, shots, res, headless=headless, retry_submit=lambda: _click_submit(page, scheduled=when is not None))
        page.wait_for_timeout(2000)
        _shot(page, shots, "published", res)
        res.ok = True
        found = _find_posted(page, pack.get("title") or "", clicked)
        if found:
            res.platform_post_id, res.url = found["id"], found["url"]
            res.notes.append((f"已定时，{when.strftime('%m-%d %H:%M')} 由平台发出" if when else "已发布") + "，笔记 ID 已记下")
        else:
            res.notes.append((f"已定时，{when.strftime('%m-%d %H:%M')} 由平台发出" if when else "已发布")
                             + "；笔记管理里暂时没认出这条，数据采集时会按标题自动对上")
        return res


def _find_posted(page, title: str, after) -> dict | None:
    """发布后打开笔记管理，从页面自己拉的笔记列表里认出刚发的这条（审核中的也在“全部”里）。认不出不算失败。"""
    from ...collectors.creator import XHS_NOTES, pick_new_work, xhs_work

    notes: list[dict] = []

    def on_resp(r):
        if "/note/user/posted" in r.url:
            try:
                notes.extend(((r.json().get("data") or {}).get("notes")) or [])
            except Exception:  # noqa: BLE001
                pass
    try:
        page.on("response", on_resp)
        page.goto(XHS_NOTES, wait_until="commit", timeout=60000)
        for _ in range(20):
            page.wait_for_timeout(1000)
            found = pick_new_work(title, after, [xhs_work(n) for n in notes])
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass
    return None
