"""抖音创作者中心发布（creator.douyin.com）。

页面要点（2026-11-10 实测）：
- 上传页 /content/upload 有 input[type=file]；上传后跳到 /content/post/video 或 /content/publish（灰度不同），
  所以以“作品标题”输入框出现为准，不看网址；
- 简介是富文本编辑器（.zone-container[contenteditable]），话题输入 `#词` 后按空格变成话题；
- 自主声明：点“请选择自主声明” → 单选“内容由AI生成”（点 label）→ 确定；
- 封面：“选择封面”弹窗顶上有“设置竖封面 / 设置横封面”两个页签，竖屏视频先打开竖封面、横屏视频先打开横封面
  （以“竖封面预览 / 横封面预览”哪个在显示为准）；两个都传，最后“完成”。不传横封面，发文助手会提示“横封面缺失”；
- 发布时间：`[class*=schedule-part]` 里两个 label“立即发布 / 定时发布”；选定时后出现时间输入框（semi 日期选择器，
  格式 yyyy-MM-dd HH:mm，可以直接填），平台提示“支持 2 小时后及 14 天内的定时发布”；
- 上传完会冒出“视频预览功能”之类的提示气泡（按钮“我知道了”），不关会挡住截图；
- 底部“发布”“暂存离开”；
- 点“发布”后抖音可能风控，弹“接收短信验证码”（要输入绑定手机收到的验证码）：无人值守时做不了，报 NeedsVerification；
  有界面运行（superops publish retry <包> --headed）时等人在窗口里验证完，最多 10 分钟。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import (TIME_FMT, UPLOAD_WAIT_S, NeedsVerification, NotLoggedIn, PublishError, PublishResult, browser, horizontal_cover,
               DOUYIN_PENDING, is_logged_in, open_until, publish_time, save_draft, stamp)

UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
TITLE_PH = "填写作品标题"


def _shot(page, shots: Path, name: str, result: PublishResult) -> None:
    p = shots / f"douyin-{stamp()}-{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        result.screenshots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def _dismiss(page) -> None:
    """关掉引导类的提示气泡（“视频预览功能 · 我知道了”等）。它们出现的时机不固定，所以填表过程中要调几次。"""
    for name in ("我知道了", "知道了"):
        for loc in (page.get_by_role("button", name=name), page.get_by_text(name, exact=True)):
            for i in range(min(loc.count(), 3)):
                try:
                    if loc.nth(i).is_visible():
                        loc.nth(i).click(timeout=2000)
                        page.wait_for_timeout(300)
                except Exception:  # noqa: BLE001
                    pass


def _set_time(page, when) -> None:
    """发布时间：when 为 None 选“立即发布”，否则选“定时发布”并填时间；填完核对输入框里的值。"""
    part = page.locator("[class*=schedule-part]").first
    part.scroll_into_view_if_needed()
    if when is None:
        part.locator("label", has_text="立即发布").click(timeout=10000)
        return
    part.locator("label", has_text="定时发布").click(timeout=10000)
    box = part.locator("input.semi-input").first
    box.wait_for(timeout=10000)
    text = when.strftime(TIME_FMT)
    box.click()
    box.fill(text)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    heading = page.get_by_text("发布设置", exact=True)      # 日期面板不一定自己收起：点一下小节标题让它关掉
    if heading.count():
        heading.first.click()
        page.wait_for_timeout(500)
    if box.input_value().strip() != text:
        raise PublishError(f"定时发布时间没设置成功：想填 {text}，页面上是 {box.input_value()!r}")


def publish(pack: dict, meta: dict, *, dry_run: bool, shots: Path, headless: bool = True, draft: bool = False) -> PublishResult:
    """pack: video / cover / title / body / topics / ai_label。dry_run=True 时停在“发布”前。"""
    res = PublishResult(ok=False, dry_run=dry_run)
    video = Path(pack["video"])
    if not video.is_file():
        raise PublishError(f"视频不存在：{video}")
    with browser(pack.get("account") or "douyin", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        page.goto(UPLOAD, wait_until="commit", timeout=90000)
        page.wait_for_timeout(3000)
        if not is_logged_in("douyin", page.url, ctx.cookies()):
            raise NotLoggedIn("抖音登录已失效：运行 superops publish login douyin 重新扫码")
        open_until(page, UPLOAD, lambda: page.get_by_text("点击上传").first, what="点击上传")
        if draft:       # 抖音网页只能暂存一条：上一条还没发就再存，会把它覆盖掉
            pending = page.get_by_text(DOUYIN_PENDING)
            if pending.count() and pending.first.is_visible():
                _shot(page, shots, "draft-slot-taken", res)
                raise PublishError("抖音网页只能暂存一条草稿，上一条暂存的还没发（发布页顶部“你还有上次未发布的视频”）。"
                                   "为了不覆盖它，这条没存；先运行 superops publish open douyin 把上一条发掉或放弃，再重试")
        page.set_input_files("input[type=file]", str(video))
        page.get_by_placeholder(TITLE_PH).wait_for(timeout=180000)
        page.wait_for_timeout(2500)
        _dismiss(page)

        # 标题（≤30 字）
        title = (pack.get("title") or "")[:30]
        page.get_by_placeholder(TITLE_PH).fill(title)

        # 简介 + 话题
        ed = page.locator(".zone-container[contenteditable=true]").first
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
            page.keyboard.type("#" + t, delay=60)
            page.wait_for_timeout(1800)          # 等联想出来，空格确认为话题
            page.keyboard.press("Space")
            page.wait_for_timeout(500)
        res.notes.append(f"标题、简介已填，话题 {len(topics)} 个")

        # 自主声明：内容由 AI 生成
        if pack.get("ai_label"):
            try:
                page.get_by_text("请选择自主声明").click(timeout=10000)
                page.locator("label.semi-radio", has_text="内容由AI生成").click(timeout=10000)
                page.get_by_role("button", name="确定").click(timeout=10000)
                page.wait_for_timeout(1000)
                res.notes.append("已声明：内容由AI生成")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 声明没设置成功：{str(e)[:120]}")
                page.keyboard.press("Escape")

        # 先等视频传完再设封面（大文件要几分钟）：进度条、“取消上传”、“上传中”都没了才算。
        # 没传完就开封面弹窗，“完成”点了弹窗关不掉（11-11 讲解片实测，传到 98% 时）
        deadline = time.time() + UPLOAD_WAIT_S
        while time.time() < deadline:
            txt = page.inner_text("body")
            if "上传失败" in txt or not re.search(r"上传中|取消上传|上传进度|\d{1,3}%\s*$", txt, re.M):
                break
            page.wait_for_timeout(3000)
        else:
            _shot(page, shots, "still-uploading", res)
            raise PublishError(f"等了 {UPLOAD_WAIT_S // 60} 分钟视频还没传完（网络慢或文件太大）")
        if "上传失败" in page.inner_text("body"):
            _shot(page, shots, "upload-failed", res)
            raise PublishError("视频上传失败")

        # 封面（失败不阻断：平台会用默认帧）。横封面：发布包给了就用，没给就由竖封面生成一张 4:3 的
        cover = pack.get("cover")
        if cover and Path(cover).is_file():
            wide = Path(pack["cover_horizontal"]) if pack.get("cover_horizontal") else horizontal_cover(Path(cover), shots)
            try:
                both = _set_cover(page, Path(cover), wide)
                res.notes.append("竖封面、横封面已设置" if both else "竖封面已设置（横封面没设置成功，不影响发布）")
            except Exception as e:  # noqa: BLE001
                _shot(page, shots, "cover-failed", res)
                res.notes.append(f"封面没设置成功（用平台默认帧）：{str(e)[:120]}")
                _close_cover_modal(page)

        # 发布时间：立即 / 定时。定时没设置成功就报错转人工，绝不悄悄变成立即发布
        when, timing = publish_time(pack, "douyin")
        _set_time(page, when)
        res.notes.append(timing)
        res.publish_at = when.isoformat(timespec="seconds") if when else None
        _dismiss(page)
        for _ in range(20):          # 等封面检测跑完，截图里才看得到平台对封面的结论（最多等一分钟，不影响发布）
            if "封面检测中" not in page.inner_text("body"):
                break
            page.wait_for_timeout(3000)
        page.get_by_role("button", name="发布", exact=True).scroll_into_view_if_needed()
        page.wait_for_timeout(800)
        _shot(page, shots, "filled", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发布”前，没有发布")
            return res
        if draft:
            how = save_draft(page, ("暂存离开",), shot=lambda name: _shot(page, shots, name, res))
            res.ok = res.draft = True
            res.notes.append(f"已存草稿，没有发布（{how}）")
            return res

        created: dict = {}

        def on_resp(r):
            if "aweme" in r.url and "create" in r.url and r.request.method == "POST":
                try:
                    created.update(r.json())
                except Exception:  # noqa: BLE001
                    pass
        page.on("response", on_resp)
        from ...timeutil import now as _now
        from datetime import timedelta as _td
        clicked = _now() - _td(minutes=10)            # 本机和平台时钟可能差几分钟
        page.get_by_role("button", name="发布", exact=True).click()
        _wait_published(page, shots, res, headless=headless)
        page.wait_for_timeout(3000)
        _shot(page, shots, "published", res)
        aweme = created.get("aweme") if isinstance(created.get("aweme"), dict) else {}
        aid = str(created.get("aweme_id") or created.get("item_id") or aweme.get("aweme_id") or "")
        if not aid:                                   # 接口没给 ID（大文件后台传完再发时常见）：到作品列表里按标题确认
            aid = str(_confirm_in_list(pack.get("title") or "", clicked, pack.get("account")) or "")
            if aid:
                res.notes.append("作品 ID 是到作品列表里按标题对上的")
        res.platform_post_id = aid or None
        res.url = f"https://www.douyin.com/video/{aid}" if aid else None
        res.ok = True
        done = f"已定时，{when.strftime('%m-%d %H:%M')} 由平台发出" if when else "已发布"
        res.notes.append(done + ("" if aid else "（没截到作品 ID，数据采集时会按标题在后台自动对上）"))
        if not aid:
            res.notes.append("发布接口返回：" + json.dumps(created, ensure_ascii=False)[:200])
        return res


def _upload_cover(page, image: Path) -> None:
    with page.expect_file_chooser(timeout=15000) as fc:
        page.locator("text=上传封面").first.click(force=True)   # 按钮里带“+”图标，精确文字匹配不到
    fc.value.set_files(str(image))
    page.wait_for_timeout(4000)
    for name in ("确定", "确认"):           # 有的版本上传后先弹裁剪确认
        b = page.get_by_role("button", name=name, exact=True)
        if b.count() and b.first.is_visible():
            b.first.click()
            page.wait_for_timeout(1500)


VERIFY_TEXT = "接收短信验证码"
BACKGROUND_UPLOAD = "作品上传中"


def _wait_background_upload(page, shots: Path, res: PublishResult) -> None:
    """跳到作品管理页之后，抖音可能还在后台传视频（右下角“作品上传中，请勿关闭页面 xx%，上传完成后将自动发布”）。
    这时关浏览器会把上传打断、作品发不出去，所以要等这个提示消失，才算发出。"""
    deadline = time.time() + UPLOAD_WAIT_S
    seen = False
    while time.time() < deadline:
        box = page.get_by_text(BACKGROUND_UPLOAD)
        if not (box.count() and box.first.is_visible()):
            if seen:
                res.notes.append("视频在作品管理页后台传完了")
                page.wait_for_timeout(5000)      # 传完到作品出现在列表里还要几秒
            return
        seen = True
        page.wait_for_timeout(3000)
    _shot(page, shots, "still-uploading", res)
    raise PublishError("点了发布，但视频一直没在后台传完（作品管理页上还显示“作品上传中”），请到后台确认是否发出")


def _wait_published(page, shots: Path, res: PublishResult, *, headless: bool) -> None:
    """点了“发布”之后：等跳到作品管理页。中途弹出短信验证时，无人值守就报 NeedsVerification（还没发出去）；
    有界面运行时等人在窗口里验证完（最多 10 分钟），验证完平台没自己继续的话，人再点一次“发布”。"""
    manage = re.compile(r".*/content/manage.*")
    deadline, verifying = time.time() + 180, False
    while time.time() < deadline:
        if manage.match(page.url):
            if verifying:
                res.notes.append("发布前抖音要了短信验证，人已验证")
            _wait_background_upload(page, shots, res)
            return
        if not verifying and page.get_by_text(VERIFY_TEXT).count() and page.get_by_text(VERIFY_TEXT).first.is_visible():
            _shot(page, shots, "needs-verification", res)
            if headless:
                err = NeedsVerification("点了发布后抖音要求短信验证（输入绑定手机收到的验证码），自动程序做不了，这条还没有发出去")
                err.screenshot = res.screenshots[-1] if res.screenshots else None  # type: ignore[attr-defined]
                raise err
            print("抖音要求短信验证：请在打开的 Chrome 窗口里点“获取验证码”、输入验证码并点“验证”（最多等 10 分钟）。", flush=True)
            verifying, deadline = True, time.time() + 600
        page.wait_for_timeout(1000)
    _shot(page, shots, "after-publish", res)
    raise PublishError("点了发布，但没有跳到作品管理页，请到后台确认是否发出")


def _confirm_in_list(title: str, after, account: str | None = None) -> str | None:
    """发布接口没截到作品 ID 时，到作品列表（接口）里按标题找刚发的这条；找到返回作品 ID。浏览器已关，单独开一次。"""
    try:
        from ...collectors.creator import list_works, pick_new_work
        for _ in range(3):
            found = pick_new_work(title, after, list_works("douyin", since=after, max_pages=1, account=account))
            if found:
                return found["id"]
            time.sleep(10)
    except Exception:  # noqa: BLE001
        pass
    return None


def _set_cover(page, cover: Path, wide: Path | None = None) -> bool:
    """封面弹窗有“设置竖封面 / 设置横封面”两个页签，先打开哪个看视频方向（竖屏先竖、横屏先横）。
    按页签各传各的：竖封面传 cover（3:4），横封面传 wide（4:3），最后“完成”。
    返回两张是否都设置了；只设置了一张也照样往下走（另一张平台会用默认帧）。"""
    page.get_by_text("选择封面").first.click(timeout=10000)
    page.get_by_text(re.compile(r"[横竖]封面预览")).first.wait_for(timeout=15000)
    done = []
    for side, image in (("竖", cover), ("横", wide)):
        if not image or not Path(image).is_file():
            continue
        try:
            if not page.get_by_text(f"{side}封面预览").first.is_visible():
                page.get_by_text(f"设置{side}封面", exact=True).first.click(timeout=8000)
                page.get_by_text(f"{side}封面预览").first.wait_for(timeout=8000)
            _upload_cover(page, Path(image))
            done.append(side)
        except Exception:  # noqa: BLE001  其中一张没设置成功不阻断，另一张照常
            continue
    if not done:
        raise PublishError("竖封面、横封面都没设置成功")
    finish = page.get_by_role("button", name="完成", exact=True)
    for _ in range(40):                         # 封面图上传、处理时“完成”是灰的，点了没用：等它能点
        if finish.count() and finish.first.is_enabled() and "disabled" not in (finish.first.get_attribute("class") or ""):
            break
        page.wait_for_timeout(1500)
    finish.click(timeout=15000)
    page.wait_for_timeout(1500)
    later = page.get_by_role("button", name="暂不设置", exact=True)   # “设置横 / 竖封面获更多流量”提示
    if later.count() and later.first.is_visible():
        later.first.click()
        page.wait_for_timeout(1500)
    if page.get_by_text(re.compile(r"[横竖]封面预览")).first.is_visible():
        raise PublishError("点了“完成”，封面弹窗还在")
    return len(done) == 2


def _close_cover_modal(page) -> None:
    for sel in ("[class*=close]", "[aria-label=close]", "[aria-label=关闭]"):
        try:
            loc = page.locator(sel).filter(has=page.locator("svg"))
            if page.get_by_text("设置竖封面").is_visible() and loc.count():
                loc.last.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
