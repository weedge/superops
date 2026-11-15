"""视频号助手发布（channels.weixin.qq.com）。

页面要点（2026-11-10 实测）：
- 直接打开 /platform/post/create 会被跳回首页，要在首页点“发表视频”；
- 表单渲染在 wujie 微前端的 shadow DOM 里（Playwright 定位器能穿透）；
- 视频描述是 .input-editor（contenteditable），话题直接写 `#词 `；
- 短标题 6–16 字，只能用常规文字；
- “视频标注”下拉 → “含AI生成内容”；
- 只收 H.264 / H.265 的 MP4（AV1 等会提示“请使用 H264/H265 MP4视频文件进行上传”），上传前先转码；
- 定时发表：单选“不定时 / 定时”；选定时后多出一行“发表时间”，主输入框只读，点开是日历面板（`.weui-desktop-picker__panel`，
  月份用右箭头翻，日期是表格里的 a）和一个可以填的时间输入框（placeholder“请选择时间”）；
- 视频没处理完时“发表”按钮带 weui-desktop-btn_disabled；
- 声明原创：表单里有“声明原创”勾选框（`.declare-original-checkbox`），勾上弹“原创权益”窗，要先勾“我已阅读并同意…”，
  “声明原创”按钮才可点；点完窗关、勾选框保持勾上。默认声明（`declare_original` 默认 true，不是原创的写 false）；
- 已加入创作分成计划的账号，没声明原创时点“发表”会弹“声明原创的视频有机会获得广告分成”，选“直接发表”才会真正发出。
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from ....infra.timeutil import now
from . import (TIME_FMT, NotLoggedIn, PublishError, PublishResult, browser, ensure_h264, is_logged_in, open_until,
               publish_time, save_draft, stamp, wait_uploaded)

HOME = "https://channels.weixin.qq.com/platform"


def short_title(title: str) -> str | None:
    """视频号短标题：6–16 字。符号只支持书名号、引号、冒号、加号、问号、百分号、摄氏度，逗号用空格代替（平台提示原文）。"""
    t = re.sub(r"[，,、；;。.！!]", " ", title or "")
    t = re.sub(r"[^\w\u4e00-\u9fff《》“”\"：:+?？%° ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:16].strip()
    return t if len(t) >= 6 else None


def _shot(page, shots: Path, name: str, res: PublishResult) -> None:
    p = shots / f"channels-{stamp()}-{name}.png"
    try:
        page.screenshot(path=str(p))
        res.screenshots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def _set_time(page, when) -> None:
    """发表时间：when 为 None 选“不定时”，否则选“定时”，在日历面板里点日期、填时间；最后核对主输入框里的值。"""
    group = page.locator(".form-item").filter(has_text="定时发表").last
    group.scroll_into_view_if_needed()
    if when is None:
        group.get_by_text("不定时", exact=True).click(timeout=10000)
        return
    group.get_by_text("定时", exact=True).click(timeout=10000)
    item = page.locator(".form-item").filter(has_text="发表时间").last
    main = item.locator("input[placeholder='请选择发表时间']")
    main.wait_for(timeout=10000)
    main.click()
    panel = item.locator(".weui-desktop-picker__dd").first      # 第一个是日历，第二个（__dd__time）是时分列表
    panel.wait_for(timeout=10000)
    for _ in range(3):       # 翻到目标月份（定时最多 14 天后，最多翻一次）
        labels = [x.strip() for x in panel.locator(".weui-desktop-picker__panel__label").all_inner_texts()]
        if labels[:2] == [f"{when.year}年", f"{when.month}月"]:
            break
        panel.locator(".weui-desktop-btn__icon__right").first.click()
        page.wait_for_timeout(500)
    day = panel.locator("table a:not(.weui-desktop-picker__faded):not(.weui-desktop-picker__disabled)").filter(
        has_text=re.compile(rf"^\s*{when.day}\s*$"))
    if not day.count():
        raise PublishError(f"定时发表：日历里 {when.strftime('%m-%d')} 不能选（可能超出平台允许的范围）")
    day.first.click()
    page.wait_for_timeout(500)
    clock = item.locator("input[placeholder='请选择时间']")
    clock.click()
    clock.fill(when.strftime("%H:%M"))
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    item.locator(".label").first.click()       # 点一下标签，让面板收起
    page.wait_for_timeout(500)
    text = when.strftime(TIME_FMT)
    if main.input_value().strip() != text:
        raise PublishError(f"定时发表时间没设置成功：想设 {text}，页面上是 {main.input_value()!r}")


def _declare_original(page, res: PublishResult) -> bool:
    """在表单里声明原创：勾“声明原创” → “原创权益”窗里勾同意 → 点“声明原创”。最后核对勾选框确实勾上了。
    没成功不拦发布（照常发出，只是没有原创标记），在结果里写明。"""
    box = page.locator(".declare-original-checkbox").first
    try:
        box.scroll_into_view_if_needed(timeout=10000)
        checked = lambda: "ant-checkbox-wrapper-checked" in (box.locator("label").first.get_attribute("class") or "")  # noqa: E731
        if checked():
            res.notes.append("已声明原创")
            return True
        box.locator("label").first.click(timeout=8000)
        dlg = page.locator(".weui-desktop-dialog:visible").filter(has_text="原创权益").first
        dlg.wait_for(timeout=8000)
        dlg.locator(".original-proto-wrapper label").first.click(timeout=5000)
        ok = dlg.get_by_role("button", name="声明原创", exact=True)
        for _ in range(20):
            if "disabled" not in (ok.get_attribute("class") or ""):
                break
            page.wait_for_timeout(300)
        ok.click(timeout=5000)
        page.wait_for_timeout(1200)
        if checked():
            res.notes.append("已声明原创")
            return True
        reason = "点完“声明原创”勾选框没勾上"
    except Exception as e:  # noqa: BLE001
        reason = str(e).splitlines()[0][:120]
    for name in ("取消",):                       # 窗没关就关掉，别挡着“发表”
        b = page.locator(".weui-desktop-dialog:visible").get_by_role("button", name=name, exact=True)
        if b.count():
            b.first.click()
    res.notes.append(f"⚠️ 原创没声明成功（{reason}），照常发表，没有原创标记")
    return False


def _after_submit_dialogs(page, res: PublishResult, *, original: bool) -> None:
    """点“发表”之后可能弹出的确认框：没声明原创时会提示“声明原创的视频有机会获得广告分成”，选“直接发表”。
    表单里已经声明过原创的，一般不弹；弹了也选“直接发表”（原创已在表单里声明）。没弹就直接过。"""
    want = "直接发表"
    for _ in range(16):                       # 最多等 8 秒
        if "/platform/post/list" in page.url:
            return
        b = page.get_by_role("button", name=want, exact=True).or_(page.get_by_text(want, exact=True))
        if b.count() and b.first.is_visible():
            b.first.click()
            res.notes.append(f"原创声明提示：选了「{want}」")
            return
        page.wait_for_timeout(500)


def publish(pack: dict, meta: dict, *, dry_run: bool, shots: Path, headless: bool = True, draft: bool = False) -> PublishResult:
    res = PublishResult(ok=False, dry_run=dry_run)
    video = Path(pack["video"] or "")
    if not video.is_file():
        raise PublishError(f"视频不存在：{video}")
    video, converted = ensure_h264(video, shots)
    if converted:
        res.notes.append(converted)
    with browser(pack.get("account") or "wechat_channels", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        try:
            open_until(page, HOME, lambda: page.get_by_role("button", name="发表视频"), what="发表视频")
        except PublishError:
            if not is_logged_in("wechat_channels", page.url):
                raise NotLoggedIn("视频号登录已失效：运行 superops publish login wechat_channels 重新扫码")
            raise
        page.get_by_role("button", name="发表视频").click(timeout=30000)
        page.locator("input[type=file]").first.wait_for(state="attached", timeout=60000)
        page.locator("input[type=file]").first.set_input_files(str(video))
        page.locator(".input-editor").first.wait_for(timeout=60000)
        page.wait_for_timeout(3000)

        # 描述 + 话题
        ed = page.locator(".input-editor").first
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
            for t in topics:
                page.keyboard.type(f"#{t} ", delay=40)
                page.wait_for_timeout(400)
        res.notes.append(f"描述已填，话题 {len(topics)} 个")

        # 短标题
        st = short_title(pack.get("title") or "")
        if st:
            page.get_by_placeholder("填写短标题有机会获得更多流量").fill(st)
            page.wait_for_timeout(800)
            warn = page.get_by_text("标题包含特殊字符")
            if warn.count() and warn.first.is_visible():
                page.get_by_placeholder("填写短标题有机会获得更多流量").fill("")
                res.notes.append(f"⚠️ 短标题「{st}」被平台判为含特殊字符，已留空")
            else:
                res.notes.append(f"短标题：{st}")
        else:
            res.notes.append("标题不满 6 字或全是符号，短标题留空")

        # 视频标注：含 AI 生成内容
        if pack.get("ai_label"):
            try:
                page.get_by_text("选择视频标注").click(timeout=10000)
                page.get_by_text("含AI生成内容", exact=True).click(timeout=10000)
                page.wait_for_timeout(800)
                res.notes.append("视频标注：含AI生成内容")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 标注没设置成功：{str(e)[:120]}")
        res.notes.append("封面用平台自动生成的")

        # 等视频传完：上传时左边预览上有进度条和“取消上传”，这时“发表”看着能点，点了不生效（大文件实测）
        wait_uploaded(page, markers=re.compile(r"取消上传|上传中|\d{1,3}%"), what="视频")
        # 等视频处理完（发表按钮可点）
        btn = page.get_by_role("button", name="发表", exact=True)
        for _ in range(100):
            if "disabled" not in (btn.first.get_attribute("class") or ""):
                break
            page.wait_for_timeout(3000)
        else:
            _shot(page, shots, "not-ready", res)
            raise PublishError("等了 5 分钟，“发表”按钮还不能点（视频可能还在处理）")
        original = _declare_original(page, res) if pack.get("declare_original") else False
        if not pack.get("declare_original"):
            res.notes.append("不声明原创（发布包写了 declare_original: false）")
        # 发表时间：不定时 / 定时。定时没设置成功就报错转人工，绝不悄悄变成立即发表
        when, timing = publish_time(pack, "wechat_channels")
        _set_time(page, when)
        res.notes.append(timing.replace("发布", "发表"))
        res.publish_at = when.isoformat(timespec="seconds") if when else None
        page.locator(".input-editor").first.scroll_into_view_if_needed()
        _shot(page, shots, "filled-top", res)
        btn.first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        _shot(page, shots, "filled-bottom", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发表”前，没有发表")
            return res
        if draft:
            how = save_draft(page, ("保存草稿",), shot=lambda name: _shot(page, shots, name, res))
            res.ok = res.draft = True
            res.notes.append(f"已存草稿，没有发表（{how}）")
            return res

        from ...collectors.creator import channels_work, pick_new_work

        listed: list[dict] = []

        def on_resp(r):   # 发表后跳到内容管理页，页面会拉作品列表：从里面认出刚发的这条，记下作品 ID
            if r.url.split("?")[0].endswith("/post/post_list"):
                try:
                    listed.extend(((r.json().get("data") or {}).get("list")) or [])
                except Exception:  # noqa: BLE001
                    pass
        page.on("response", on_resp)
        clicked = now() - timedelta(minutes=10)   # 本机和平台时钟可能差几分钟
        btn.first.click()
        _after_submit_dialogs(page, res, original=original)
        try:
            page.wait_for_url(re.compile(r".*/platform/post/list.*"), timeout=180000)
        except Exception:  # noqa: BLE001
            _shot(page, shots, "after-publish", res)
            raise PublishError("点了发表，但没有跳到内容管理页，请到视频号助手确认是否发出")
        found = None
        for _ in range(15):
            page.wait_for_timeout(1000)
            found = pick_new_work(pack.get("title") or "", clicked, [channels_work(x) for x in listed])
            if found:
                break
        _shot(page, shots, "published", res)
        res.ok = True
        if found:
            res.platform_post_id = found["id"]
            res.notes.append((f"已定时，{when.strftime('%m-%d %H:%M')} 由平台发表" if when else "已发表")
                             + "，作品 ID 已记下（视频号没有公开链接，数据从视频号助手取）")
        else:
            res.notes.append((f"已定时，{when.strftime('%m-%d %H:%M')} 由平台发表" if when else "已发表")
                             + "；内容管理页里暂时没认出这条，数据采集时会按标题自动对上")
        return res
