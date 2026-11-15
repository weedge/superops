"""创作者后台自动发布（Playwright + 本机 Chrome + 每个账号独立的登录态目录）。

- 账号：同一平台可以登多个号。每个号有一个账号键：主号就是平台名（douyin），后加的号是 douyin@2、douyin@3……
  登录态存在 ~/popto-superops-data/browser-profiles/<账号键>/。每个平台有一个默认账号（不指定账号的发布、
  评论引流、数据采集都用它），记在 browser-profiles/accounts.json 的 "_defaults" 里，没设过就是主号；
- 登录：`superops publish login <账号键>` 打开有界面的 Chrome，人扫码登录，之后发布复用；
- 发布：只由 executors/publish.py 在卡片批准后调用；每个发布包只发一次（ActionLedger）；
- 试运行（dry_run）：上传、填文案都做，停在“发布”按钮前截图，不点发布；
- 存草稿（draft）：上传、填文案都做，最后点平台的“保存草稿 / 暂存离开”，不点发布；人到草稿箱检查后自己发；
- 发布时间：发布包里有 scheduled_at 且还来得及，就用平台的“定时发布”；否则“立即发布”（见 publish_time）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ....infra import paths
from ....infra.timeutil import TZ, now, parse_iso

PLATFORMS = {
    "douyin": {"label": "抖音", "home": "https://creator.douyin.com/creator-micro/home",
               "login": "https://creator.douyin.com/", "logged_in": ["/creator-micro/"],
               "session_cookies": ["sessionid", "sessionid_ss"]},  # 抖音没登录时停在原网址、白屏，只能看 Cookie
    "wechat_channels": {"label": "视频号", "home": "https://channels.weixin.qq.com/platform",
                        "login": "https://channels.weixin.qq.com/login.html", "logged_in": ["/platform"]},
    "xiaohongshu": {"label": "小红书", "home": "https://creator.xiaohongshu.com/new/home",
                    "login": "https://creator.xiaohongshu.com/login", "logged_in": ["/new/", "/creator/"]},
}

# 只管账号（扫码登录、校验、读昵称头像、退出）、还没有自动发布器的平台：发布时走人工路径（官方发布页 upload）。
# 登录、是谁的判断都在 qrlogin.py（参照 Easel：快手走 passport 扫码、知乎 /api/v4/me、B站 nav 接口、公众号后台网址里的 token）
ACCOUNT_ONLY = {
    # 快手创作者中心首页的“立即登录”是个链接（先跳 /rest/infra/logout 再转 passport），这里直接开 passport 的登录页，不经过退出
    "kuaishou": {"label": "快手", "home": "https://cp.kuaishou.com/article/manage/video",
                 "login": "https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api&callback="
                          "https%3A%2F%2Fcp.kuaishou.com%2Frest%2Finfra%2Fsts%3FfollowUrl%3Dhttps%253A%252F%252Fcp.kuaishou.com%252Fprofile%26setRootDomain%3Dtrue",
                 "upload": "https://cp.kuaishou.com/article/publish/video", "app": "快手 App"},
    "bilibili": {"label": "B站", "home": "https://member.bilibili.com/platform/home", "login": "https://passport.bilibili.com/login",
                 "upload": "https://member.bilibili.com/platform/upload/video/frame", "app": "哔哩哔哩 App"},
    "zhihu": {"label": "知乎", "home": "https://www.zhihu.com/", "login": "https://www.zhihu.com/signin",
              "upload": "https://zhuanlan.zhihu.com/write", "app": "知乎 App"},
    "wechat_oa": {"label": "公众号", "home": "https://mp.weixin.qq.com/", "login": "https://mp.weixin.qq.com/",
                  "upload": "https://mp.weixin.qq.com/", "app": "公众号管理员的微信"},
}
# 账号页的全部平台：能自动发布的（PLATFORMS）+ 只管账号的
ACCOUNTS = {**{k: {**v, "publish": True, "app": {"wechat_channels": "微信"}.get(k, f"{v['label']} App")} for k, v in PLATFORMS.items()},
            **{k: {**v, "publish": False} for k, v in ACCOUNT_ONLY.items()}}


# 各平台“定时发布”要求的最少提前量（实测：抖音提示“支持 2 小时后及 14 天内”，小红书、视频号默认给的是 1 小时后）
MIN_LEAD = {"douyin": timedelta(hours=2), "xiaohongshu": timedelta(hours=1), "wechat_channels": timedelta(hours=1)}
MAX_AHEAD = timedelta(days=14)
SOON = timedelta(minutes=30)      # 离计划时间不到这么久：直接立即发布
TIME_FMT = "%Y-%m-%d %H:%M"


class PublishError(RuntimeError):
    pass


class NotLoggedIn(PublishError):
    pass


class NeedsVerification(PublishError):
    """平台在点发布之后要人做一次验证（短信验证码等）：自动程序做不了，要人来。"""


class ProfileBusy(PublishError):
    """同一平台的登录态目录同一时间只能开一个 Chrome（发布 / 数据采集 / 登录检查会抢）。"""


@dataclass
class PublishResult:
    ok: bool
    dry_run: bool
    url: str | None = None
    platform_post_id: str | None = None
    screenshots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    publish_at: str | None = None      # 定时发布：平台会在这个时刻发出（ISO）；立即发布为 None
    draft: bool = False                # 存了草稿，没有发布


DRAFT_SAVED = re.compile(r"保存成功|暂存成功|已保存|存草稿成功|草稿保存成功|已存入草稿|保存至草稿")
DIALOGS = "[role=dialog]:visible, .weui-desktop-dialog:visible, .semi-modal:visible, .d-modal:visible, .ant-modal:visible"


def save_draft(page, buttons: tuple[str, ...], *, shot, timeout_s: float = 60, click=None) -> str:
    """点平台的“保存草稿 / 暂存离开”，等平台确认（成功提示，或离开了发布页）。弹出确认框就点确认。
    click：按钮在封闭 shadow DOM 里时（小红书），由调用方给一个点按钮的函数，返回点到的文字，没点到返回 None。
    返回确认依据（写进结果）；等不到确认抛 PublishError（转人工，人去草稿箱看一眼）。"""
    start = page.url.split("?")[0]
    clicked = click() if click else None
    if not clicked:
        target = None
        for text in buttons:
            b = page.get_by_role("button", name=text, exact=True)
            if not b.count():
                b = page.get_by_text(text, exact=True)
            if b.count() and b.first.is_visible():
                target = b.first
                break
        if target is None:
            shot("no-draft-button")
            raise PublishError(f"页面上没找到「{' / '.join(buttons)}」按钮（页面可能改版了），没存成草稿")
        target.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        clicked = target.inner_text().strip()
        target.click()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        toast = page.get_by_text(DRAFT_SAVED)
        if toast.count() and toast.first.is_visible():
            seen = toast.first.inner_text().strip()[:30]
            shot("draft-saved")
            return f"平台提示「{seen}」"
        if page.url.split("?")[0] != start:
            page.wait_for_timeout(2000)
            shot("draft-saved")
            return "已离开发布页"
        dlg = page.locator(DIALOGS)
        if dlg.count():
            for text in ("确定", "确认", "暂存离开", "保存", "保存草稿"):
                b = dlg.first.get_by_role("button", name=text, exact=True)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    break
        page.wait_for_timeout(500)
    shot("draft-unconfirmed")
    raise PublishError(f"点了「{clicked}」，但没看到保存成功的提示，请到草稿箱确认")


def publish_time(pack: dict, platform: str, at: datetime | None = None) -> tuple[datetime | None, str]:
    """这条是立即发布还是定时发布。返回 (定时时刻或 None, 给人看的一句说明)。

    - 发布包写了 `publish_timing: immediate`，或没有 scheduled_at → 立即发布；
    - 计划时间已过，或离现在不到 30 分钟 → 立即发布（批得晚了，不再往后拖）；
    - 计划时间来得及 → 平台定时，时间就是 scheduled_at；
    - 计划时间在将来、但不够平台要求的提前量 → 定在平台允许的最早时间（比计划晚一点，不会比计划早）；
    - 超过 14 天 → 报错（平台不支持，转人工）。
    """
    at = at or now()
    raw = pack.get("scheduled_at")
    if pack.get("timing") == "immediate" or not raw:
        return None, "立即发布"
    try:
        planned = parse_iso(str(raw)).astimezone(TZ)
    except ValueError:
        return None, f"立即发布（发布包里的时间 {raw!r} 认不出来）"
    planned = planned.replace(second=0, microsecond=0)
    if planned <= at + SOON:
        return None, f"立即发布（计划 {planned.strftime('%H:%M')}，已到或不足 30 分钟）"
    if planned > at + MAX_AHEAD:
        raise PublishError(f"计划时间 {planned.strftime(TIME_FMT)} 超过平台定时发布的 14 天上限")
    earliest = at + MIN_LEAD.get(platform, timedelta(hours=2)) + timedelta(minutes=5)
    if planned >= earliest:
        return planned, f"定时发布 {planned.strftime('%m-%d %H:%M')}"
    earliest = earliest.replace(second=0, microsecond=0)
    earliest += timedelta(minutes=(-earliest.minute) % 5)      # 取整到 5 分钟
    lead = int(MIN_LEAD.get(platform, timedelta(hours=2)).total_seconds() // 3600)
    return earliest, (f"定时发布 {earliest.strftime('%m-%d %H:%M')}（平台要求至少提前 {lead} 小时，"
                      f"比计划的 {planned.strftime('%H:%M')} 晚）")


def horizontal_cover(cover: Path, out_dir: Path) -> Path:
    """由竖封面生成 4:3 的横封面：整宽裁出 4:3 的一块，在上、中、下几个位置里挑内容最多的那块
    （用裁出来的 JPEG 大小近似“内容多少”：大片留白的压得小）。
    不用“两侧模糊填充”：抖音会判“封面边框面积过大”。原图已经是横的就直接用；没有 ffmpeg 也直接用原图。"""
    out = out_dir / f"{cover.stem}-4x3.jpg"
    if out.is_file() and out.stat().st_mtime >= cover.stat().st_mtime:
        return out
    if not shutil.which("ffmpeg"):
        return cover
    best: tuple[int, Path] | None = None
    tries: list[Path] = []
    for i, offset in enumerate((0.0, 0.25, 0.5, 0.75, 1.0)):
        cand = out_dir / f".{cover.stem}-4x3-{i}.jpg"
        tries.append(cand)
        vf = f"crop=iw:min(ih\\,iw*3/4):0:(ih-min(ih\\,iw*3/4))*{offset},scale=1440:1080"
        try:
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(cover), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(cand)],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and cand.is_file() and (best is None or cand.stat().st_size > best[0]):
            best = (cand.stat().st_size, cand)
    if best:
        best[1].replace(out)
    for cand in tries:
        cand.unlink(missing_ok=True)
    return out if out.is_file() else cover


def video_codec(video: Path) -> str | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True, timeout=30)
        first = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
        return first.split(",")[0].strip() or None      # csv 输出有时带结尾逗号（“h264,”）
    except (OSError, subprocess.TimeoutExpired):
        return None


def ensure_h264(video: Path, out_dir: Path) -> tuple[Path, str | None]:
    """平台只收 H.264 / H.265 时（视频号），把别的编码（如 AV1）转成 H.264。返回 (要上传的文件, 说明或 None)。"""
    codec = video_codec(video)
    if codec in (None, "h264", "hevc"):
        return video, None
    out = out_dir / f"{video.stem}-h264.mp4"
    if out.is_file() and out.stat().st_mtime >= video.stat().st_mtime:
        return out, f"原片是 {codec} 编码，已转成 H.264 上传"
    if not shutil.which("ffmpeg"):
        raise PublishError(f"视频是 {codec} 编码，平台只收 H.264 / H.265，本机没有 ffmpeg 转不了")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not out.is_file():
        raise PublishError(f"视频是 {codec} 编码，转 H.264 失败：{r.stderr[-200:]}")
    return out, f"原片是 {codec} 编码，已转成 H.264 上传"


# ── 账号键与账号登记（同一平台多个号） ──
KEY_RE = re.compile(r"^([a-z_]+)(?:@([a-z0-9][a-z0-9-]{0,19}))?$")
DEFAULTS = "_defaults"


class AccountError(PublishError):
    """发布包指定的账号认不出来（不在已登记的账号里）：不猜，转人工。"""


def platform_of(key: str) -> str:
    """账号键 → 平台：douyin@2 → douyin。"""
    return str(key or "").split("@", 1)[0]


def is_account_key(key: str) -> bool:
    m = KEY_RE.match(str(key or ""))
    return bool(m and m.group(1) in ACCOUNTS)


def profiles_root() -> Path:
    return paths.ops_data() / "browser-profiles"


def registry_file() -> Path:
    return profiles_root() / "accounts.json"


def read_registry() -> dict:
    """accounts.json：{账号键: {status, name, account_id, followers, nickname, …}, "_defaults": {平台: 账号键}}。"""
    import json
    try:
        data = json.loads(registry_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_registry(data: dict) -> None:
    import json
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def write_account(key: str, **fields) -> None:
    data = read_registry()
    data[key] = {**(data.get(key) or {}), **fields}
    _save_registry(data)


def forget_account(key: str) -> None:
    """移除后加的号：登记删掉；它是默认账号的话，默认改回主号。"""
    data = read_registry()
    data.pop(key, None)
    defaults = data.get(DEFAULTS) or {}
    if defaults.get(platform_of(key)) == key:
        defaults.pop(platform_of(key))
    data[DEFAULTS] = defaults
    _save_registry(data)


def account_keys(platform: str) -> list[str]:
    """这个平台的全部账号：主号在前，后加的号按序号。后加的号以登记为准，也认登录目录（登记丢了不会丢号）。"""
    keys = {k for k in read_registry() if k != DEFAULTS and platform_of(k) == platform and is_account_key(k)}
    root = profiles_root()
    if root.is_dir():
        keys |= {d.name for d in root.iterdir() if d.is_dir() and d.name.startswith(platform + "@") and is_account_key(d.name)}
    keys.discard(platform)

    def order(k: str):
        slug = k.split("@", 1)[1]
        return (0, int(slug), "") if slug.isdigit() else (1, 0, slug)
    return [platform, *sorted(keys, key=order)]


def default_account(platform: str) -> str:
    key = (read_registry().get(DEFAULTS) or {}).get(platform)
    return key if key and platform_of(key) == platform and key in account_keys(platform) else platform


def set_default(key: str) -> None:
    data = read_registry()
    data[DEFAULTS] = {**(data.get(DEFAULTS) or {}), platform_of(key): key}
    _save_registry(data)


def next_account_key(platform: str) -> str:
    used = set(account_keys(platform))
    n = 2
    while f"{platform}@{n}" in used:
        n += 1
    return f"{platform}@{n}"


def account_label(key: str) -> str:
    """给人看的账号名：平台 + 账号昵称（有备注名用备注名），后加的号没登录时用“2号”。"""
    pf = platform_of(key)
    label = ACCOUNTS.get(pf, {}).get("label", pf)
    k = read_registry().get(key) or {}
    who = k.get("nickname") or k.get("name")
    if who:
        return f"{label}「{who}」"
    return label if key == pf else f"{label} {key.split('@', 1)[1]}号"


def resolve_account(platform: str, want: str | None) -> str:
    """发布包里写的账号 → 账号键。没写用默认账号；写的是账号键、账号名或备注名都认；认不出来抛 AccountError（不猜，转人工）。"""
    want = str(want or "").strip().lstrip("@")
    if not want:
        return default_account(platform)
    keys = account_keys(platform)
    if want in keys:
        return want
    reg = read_registry()
    hits = [k for k in keys if want in {str((reg.get(k) or {}).get(f) or "").strip() for f in ("name", "nickname", "account_id")} - {""}]
    if len(hits) == 1:
        return hits[0]
    label = ACCOUNTS.get(platform, {}).get("label", platform)
    raise AccountError(f"发布包指定的{label}账号「{want}」" + ("对上了不止一个号" if hits else "不在已登录的账号里") +
                       "，没有自动发；请在面板发布包上选好账号后重试，或手动发")


def pack_account(p: dict, meta: dict | None = None) -> str:
    """发布包发到哪个号：面板改过的（publish_packs.account）优先，其次 meta 里这个平台写的，都没有用默认账号。"""
    pm = {}
    if meta:
        from ..publish import _per_platform
        pm = _per_platform(meta, p["platform"])
    return resolve_account(p["platform"], p.get("account") or pm.get("account"))


def profile_dir(key: str) -> Path:
    d = profiles_root() / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def shots_dir(pack_id: str) -> Path:
    d = paths.ops_data() / "publish-shots" / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def profile_lock(key: str, wait_s: float = 600):
    """按账号排队用登录态目录（跨进程文件锁）：发布、数据采集、登录检查不会同时开同一个号的 Chrome。"""
    import fcntl

    lock = profiles_root() / f".{key}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        deadline = time.time() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    label = account_label(key)
                    raise ProfileBusy(f"{label}的浏览器正被别的任务占用（发布 / 数据采集 / 登录），等了 {int(wait_s)} 秒")
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def browser(key: str, *, headless: bool = True, error_shots: Path | None = None, lock_wait: float = 600):
    """本机 Chrome + 这个账号（账号键，主号就是平台名）的持久化登录态。出错时把当前页面截图到 error_shots。"""
    from playwright.sync_api import sync_playwright

    with profile_lock(key, lock_wait), sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir(key)), channel="chrome", headless=headless,
            viewport={"width": 1440, "height": 900}, locale="zh-CN", timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield ctx
        except Exception as e:
            if error_shots and ctx.pages:
                shot = error_shots / f"{key}-{stamp()}-error.png"
                try:
                    ctx.pages[-1].screenshot(path=str(shot))
                    e.screenshot = str(shot)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            ctx.close()


def open_until(page, url: str, ready, *, what: str, timeout_ms: int = 60000, retries: int = 1) -> None:
    """打开入口页并等关键元素出现；没等到就刷新重试（只用于点“发布”之前的步骤，重试不会重复发）。"""
    for attempt in range(retries + 1):
        try:
            if attempt == 0:
                page.goto(url, wait_until="commit", timeout=90000)
            else:
                page.reload(wait_until="commit", timeout=90000)
            ready().wait_for(timeout=timeout_ms)
            return
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise PublishError(f"页面 {timeout_ms // 1000} 秒没加载出「{what}」（已刷新重试 {retries} 次，多半是网络慢或页面改版）")


def shadow_button_center(page, host: str, texts: tuple[str, ...]) -> tuple[float, float, str] | None:
    """在“封闭”的 shadow DOM 组件（如小红书的 <xhs-publish-btn>）里找文字是 texts 之一的按钮，返回它的中心坐标和文字。
    Playwright 的定位器进不了封闭的 shadow root，Chrome 调试接口（DOM.getDocument pierce=true）可以。找不到返回 None。"""
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("DOM.enable")
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
        hits: list[tuple[str, int]] = []

        def walk(node, inside: bool) -> None:
            here = inside or node.get("localName") == host
            if here and node.get("nodeType") == 3 and node.get("nodeValue", "").strip() in texts:
                hits.append((node["nodeValue"].strip(), node["parentId"]))
            for k in ("children", "shadowRoots"):
                for child in node.get(k) or []:
                    walk(child, here)
        walk(doc, False)
        for want in texts:                      # 按 texts 的顺序挑
            for text, parent in hits:
                if text != want:
                    continue
                quad = cdp.send("DOM.getBoxModel", {"nodeId": parent})["model"]["border"]
                xs, ys = quad[0::2], quad[1::2]
                if max(xs) - min(xs) > 4 and max(ys) - min(ys) > 4:
                    return sum(xs) / 4, sum(ys) / 4, text
        return None
    finally:
        try:
            cdp.detach()
        except Exception:  # noqa: BLE001
            pass


UPLOADING = re.compile(r"取消上传|上传中|上传进度|作品上传中|\d{1,3}\s*%")
UPLOAD_WAIT_S = 20 * 60


def wait_uploaded(page, *, scope=None, markers=UPLOADING, timeout_s: float = UPLOAD_WAIT_S, what: str = "视频") -> None:
    """等视频真正传完：页面上（或 scope 里）不再有“取消上传 / 上传中 / xx%”这类进度。
    大文件要几分钟；没传完就点发布，平台要么不理，要么跳走后在后台继续传，一关浏览器就断了。"""
    area = scope or page.locator("body")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            text = area.inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            text = ""
        if not markers.search(text):
            return
        page.wait_for_timeout(3000)
    raise PublishError(f"等了 {int(timeout_s // 60)} 分钟，{what}还没传完（网络慢或文件太大）")


def friendly_error(e: Exception) -> str:
    """把 Playwright 的原始报错翻译成人能看懂的一句话。"""
    msg = str(e)
    if isinstance(e, PublishError):
        return msg
    m = re.search(r"waiting for (?:get_by_role\(\"\w+\", name=\"([^\"]+)\"|get_by_text\(\"([^\"]+)\"|get_by_placeholder\(\"([^\"]+)\")", msg)
    target = next((g for g in (m.groups() if m else ()) if g), None)
    if "Timeout" in msg:
        return f"等「{target}」超时（多半是网络慢或页面改版）" if target else "页面操作超时（多半是网络慢或页面改版）"
    return msg.splitlines()[0][:200]


def is_logged_in(platform: str, url: str, cookies: list[dict] | None = None) -> bool:
    cfg = PLATFORMS[platform]
    if not (any(k in url for k in cfg["logged_in"]) and "login" not in url):
        return False
    need = cfg.get("session_cookies")
    if need and cookies is not None:
        return any(c.get("name") in need and c.get("value") for c in cookies)
    return True


def login(key: str, timeout_s: int = 300) -> bool:
    """打开有界面的 Chrome 让人扫码；登录成功（跳到后台首页）后保存登录态并关闭。key 是账号键。"""
    from .qrlogin import logged_in
    platform = platform_of(key)
    cfg = ACCOUNTS[platform]
    with browser(key, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg["login"], wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if logged_in(platform, ctx):
                page.wait_for_timeout(3000)  # 让 Cookie 落盘
                return True
            page.wait_for_timeout(1500)
    return False


# 草稿在哪：视频号存在平台上（任何浏览器打开视频号助手都看得到）；小红书、抖音网页版的草稿只存在
# 本机专用 Chrome 的本地存储里（手机 App、别的浏览器都看不到），要用 open_drafts 打开这个 Chrome 才找得到。
DRAFTS = {
    "wechat_channels": {"url": "https://channels.weixin.qq.com/platform/post/draftListManager", "local": False,
                        "where": "视频号助手（网页，任何浏览器）→ 内容管理 → 草稿箱"},
    "xiaohongshu": {"url": "https://creator.xiaohongshu.com/publish/publish?source=official", "local": True, "click": "草稿箱",
                    "where": "本机专用 Chrome 里的小红书发布页 → 草稿箱（小红书网页草稿只存在这个浏览器里）"},
    "douyin": {"url": "https://creator.douyin.com/creator-micro/content/upload", "local": True,
               "where": "本机专用 Chrome 里的抖音发布页 → 顶部“继续编辑”（抖音网页只能暂存一条，只存在这个浏览器里）"},
}
DOUYIN_PENDING = "你还有上次未发布的视频"


def open_drafts(key: str, timeout_s: int = 3600) -> None:
    """打开这个号的有界面专用 Chrome，停在平台的草稿位置，人检查、点发布；人关掉窗口（或 1 小时）后结束。"""
    spot = DRAFTS[platform_of(key)]
    with browser(key, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(spot["url"], wait_until="domcontentloaded", timeout=90000)
        if spot.get("click"):
            try:
                page.get_by_text(spot["click"]).locator("visible=true").first.click(timeout=15000)
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + timeout_s
        while time.time() < deadline and not page.is_closed():
            try:
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001  人关了窗口
                break


def check_login(key: str) -> tuple[bool, str]:
    """无界面打开后台首页，看会不会被踢回登录页。key 是账号键（主号就是平台名）。"""
    platform = platform_of(key)
    cfg = PLATFORMS[platform]
    with browser(key) as ctx:
        page = ctx.new_page()
        page.goto(cfg["home"], wait_until="commit", timeout=90000)
        page.wait_for_timeout(6000)
        return is_logged_in(platform, page.url, ctx.cookies()), page.url


def stamp() -> str:
    return now().strftime("%H%M%S")


def payload_for(p: dict, meta: dict) -> dict:
    """发布包（publish_packs 行 + meta.json）→ 发布器要的字段，路径都转成绝对路径。"""
    from ..publish import _per_platform

    d = Path(p["pack_dir"]) if p.get("pack_dir") else None
    pm = _per_platform(meta, p["platform"])
    media = meta.get("media") or {}

    def f(name):
        return str(d / name) if d and name and (d / name).is_file() else None
    try:
        account, account_error = pack_account(p, meta), None
    except AccountError as e:
        account, account_error = None, str(e)
    return {
        "pack_id": p["id"], "platform": p["platform"], "account": account, "account_error": account_error,
        "video": f(media.get("video")), "cover": f(pm.get("cover") or media.get("cover")),
        "cover_horizontal": f(pm.get("cover_horizontal") or media.get("cover_horizontal")),
        "scheduled_at": pm.get("scheduled_at") or p.get("scheduled_at") or meta.get("scheduled_at"),
        "timing": pm.get("publish_timing") or meta.get("publish_timing"),
        "images": [x for x in (f(n) for n in media.get("images") or []) if x],
        "title": pm.get("title") or p.get("title") or meta.get("title") or "",
        "body": pm.get("body") or meta.get("body") or "",
        "topics": pm.get("topics") or meta.get("topics") or [],
        "ai_label": bool(pm.get("ai_label", meta.get("ai_label", True))),
        # 视频号默认声明原创（11-11 定）；转载、第三方解读等不是自己原创的，发布包里写 declare_original: false
        "declare_original": bool(pm.get("declare_original", meta.get("declare_original", p.get("platform") == "wechat_channels"))),
        "is_test": bool(meta.get("is_test")),
    }


def module_for(platform: str):
    import importlib

    name = {"douyin": "douyin", "wechat_channels": "channels", "xiaohongshu": "xiaohongshu"}.get(platform)
    if not name:
        return None
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError:
        return None


def run(p: dict, meta: dict, *, dry_run: bool, headless: bool = True, draft: bool = False) -> PublishResult:
    mod = module_for(p["platform"])
    if not mod:
        raise PublishError(f"{p['platform']} 还没有自动发布器")
    pl = payload_for(p, meta)
    if pl["account_error"]:
        raise AccountError(pl["account_error"])
    if pl["is_test"] and not dry_run:
        raise PublishError("测试内容不允许真发布（meta.is_test=true），只能试运行")
    if draft and not dry_run:
        pl["timing"] = "immediate"     # 存草稿不设定时：什么时候发由人发的时候定
        return mod.publish(pl, meta, dry_run=False, draft=True, shots=shots_dir(p["id"]), headless=headless)
    return mod.publish(pl, meta, dry_run=dry_run, shots=shots_dir(p["id"]), headless=headless)

