"""网页里扫码登录各平台创作者后台（参照 Easel 的账号登录）：无界面打开登录页，把二维码截成图给面板显示，
人用手机 App 扫；抖音风控要短信验证时，面板弹输入框，人填的验证码经文件交给这里填进去。登录成功后顺手读出账号昵称和头像。

状态都落在 ~/popto-superops-data/publish-login/：
  <平台>.json   {state, message, at, phone?, name?}；state：starting 准备中 / qr_ready 请扫码 / scanned 已扫码 /
                sms_required 要短信验证码 / verifying 验证中 / success 登录成功 / expired 过期 / error 出错 / cancelled 取消
  <平台>.png    当前有效的二维码（抖音码一两分钟就过期，这里每几秒重截一次，过期就点刷新）
  <平台>.code   面板写进来的短信验证码，读走就删
  <平台>-avatar.<扩展名>  账号头像（下载到本地，面板不直接加载外网图片）

找码、判断已扫码 / 过期、短信验证的写法移植自 Easel（skills/shared/scripts 的 douyin_publish.py、xhs_publish.py、
web_publisher.py），只保留这三个平台、接到我们自己的登录目录和跨进程锁上（publishers.browser）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from ... import paths
from ...timeutil import iso
from . import ACCOUNTS, PLATFORMS, browser, is_logged_in

LOGIN_TIMEOUT_S = 240
TERMINAL = ("success", "expired", "error", "cancelled")
STATE_LABEL = {"starting": "准备中", "qr_ready": "请扫码", "scanned": "已扫码", "sms_required": "要短信验证码", "verifying": "验证中",
               "success": "登录成功", "expired": "二维码过期", "error": "出错", "cancelled": "已取消"}

# 抖音：创作者后台身份接口（Easel platform_readback.DOUYIN_PROFILE_API）、短信验证墙
DOUYIN_PROFILE_API = "https://creator.douyin.com/web/api/media/user/info/?aid=1128"
DOUYIN_WALL = ("身份验证", "接收短信", "短信验证", "验证方式", "短信已发送", "验证码已发送", "获取验证码", "验证并登录", "手机刷脸")
DOUYIN_SMS_MODAL = ("[class*='semi-modal-content']", "[class*='semi-modal']", "div[role='dialog']", "[class*='modal-content']",
                    "[class*='second_verify_panel']", "[class*='second-verify-panel']", "div[class*='uc_verification_component']")
DOUYIN_SMS_RECEIVE = ("text=接收短信验证码", "div:has-text('接收短信验证码')", "text=短信验证码")
DOUYIN_SMS_SEND = ("button:has-text('获取验证码')", "text=获取验证码", "button:has-text('发送验证码')", "text=重新发送")
DOUYIN_SMS_ERROR = ("验证码错误", "验证码填写错误", "验证码不正确", "验证码已过期", "验证码过期", "请重新获取", "验证失败", "已失效")
# 小红书：创作平台登录框和账号接口
XHS_LOGIN = "https://creator.xiaohongshu.com/login"
XHS_ACCOUNT_API = "https://creator.xiaohongshu.com/api/galaxy/user/info"
XHS_QR = (".sso-login-wrapper img, .sso-login-wrapper canvas, .qrcode-img, [class*='qrcode' i] img, [class*='qrcode' i] canvas, "
          "img[src*='qrcode' i], .login-container img[alt*='二维码']")
XHS_SWITCH = "button:has-text('扫码登录'), [role='tab']:has-text('扫码登录'), .login-type:has-text('扫码登录'), .sso-login-wrapper > div > div > img"
XHS_BOX = ".sso-login-wrapper, .login-container, .login-box"
# 视频号：二维码在 open.weixin.qq.com 的跨域 iframe 里（微信标准扫码组件），截 iframe 本身；身份看 auth_data 接口的响应
CHANNELS_QR_IFRAME = "iframe[src*='open.weixin.qq.com/connect/qrconnect']"
CHANNELS_AUTH = "/cgi-bin/mmfinderassistant-bin/auth/auth_data"
# 只管账号的四个平台（Easel 的 web_publisher.py / weixin_mp_stats.py / bili_login.py）：二维码选择器、登录判断用的接口
EXTRA_QR = {
    "kuaishou": "[class*=qrcode] img[src^='data:image'], [class*=qrcode] canvas, [class*=qrcode] img, [class*=qr-login] img, canvas",
    "bilibili": ".login-scan-box img, .login-scan-box canvas, [class*=qrcode] img, [class*=qrcode] canvas, img[src^='data:image'], canvas",
    # 知乎的 .Qrcode-img 容器在码加载出来之前是三个点的占位：只认里面真正的图片 / canvas
    "zhihu": ".Qrcode-img img, .Qrcode-img canvas, .SignFlow-qrcode img, .SignFlowQRCode img, [class*='qrcode' i] img, [class*='qrcode' i] canvas, img[src*='qr' i]",
    "wechat_oa": "img.login__type__container__scan__qrcode, .login__type__container__scan img, img[src*='qrcode']",
}
ZHIHU_SWITCH = "button:has-text('扫码登录'), [role='tab']:has-text('扫码登录'), .SignFlow-qrcodeTab, .SignFlow-tab--qrcode, .SignFlow-switch:has-text('扫码')"
KUAISHOU_USER_API = "https://cp.kuaishou.com/rest/cp/works/v2/common/pc/current/user"
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_STAT = "https://api.bilibili.com/x/web-interface/nav/stat"
ZHIHU_ME = "https://www.zhihu.com/api/v4/me"
MP_TOKEN = re.compile(r"[?&]token=(\d+)")
SCANNED_WORDS = ("扫码成功", "扫描成功", "已扫码", "请在手机上确认", "请在APP上确认", "请在微信中确认", "请在手机微信上确认")
EXPIRED_WORDS = ("二维码已失效", "二维码失效", "二维码已过期", "二维码过期", "二维码无效", "请刷新二维码", "二维码已超时")


# ───────────────────────── 状态文件 ─────────────────────────

def state_dir() -> Path:
    d = paths.ops_data() / "publish-login"
    d.mkdir(parents=True, exist_ok=True)
    return d


def status_file(pf: str) -> Path:
    return state_dir() / f"{pf}.json"


def qr_file(pf: str) -> Path:
    return state_dir() / f"{pf}.png"


def code_file(pf: str) -> Path:
    return state_dir() / f"{pf}.code"


def avatar_file(pf: str) -> Path | None:
    hits = sorted(state_dir().glob(f"{pf}-avatar.*"), key=lambda f: f.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_status(pf: str, state: str, message: str = "", **extra) -> None:
    _atomic(status_file(pf), json.dumps({"state": state, "message": message, "at": iso(), **extra}, ensure_ascii=False).encode())


def read_status(pf: str) -> dict:
    try:
        data = json.loads(status_file(pf).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "none", "message": ""}
    qr = qr_file(pf)
    if data.get("state") == "qr_ready" and qr.is_file():
        data["qr_ts"] = qr.stat().st_mtime_ns // 1_000_000     # 二维码换了，面板的图跟着换
    return data


def submit_code(pf: str, code: str) -> None:
    _atomic(code_file(pf), code.encode())


def take_code(pf: str) -> str:
    f = code_file(pf)
    try:
        code = f.read_text(encoding="utf-8").strip()
        f.unlink()
        return re.sub(r"\D", "", code)
    except OSError:
        return ""


def _clear(pf: str) -> None:
    for f in (qr_file(pf), code_file(pf)):
        f.unlink(missing_ok=True)


# ───────────────────────── 找码、截码 ─────────────────────────

def _box(el) -> dict | None:
    try:
        return el.bounding_box() if el.is_visible() else None
    except Exception:  # noqa: BLE001
        return None


def _white_part(el) -> tuple[float, float, float, float] | None:
    """微信扫码组件是深色主题，二维码图自带一圈深色边：在图片所在的 iframe 里把它画到 canvas 上（同源，读得到像素），
    找出白色方块占整张图的比例（左、上、右、下），截图时只裁白色那块。读不到就返回 None（照整张截）。"""
    try:
        r = el.evaluate("""(img) => { const w = img.naturalWidth, h = img.naturalHeight; if (!w || !h) return null;
            const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d'); g.drawImage(img, 0, 0);
            const d = g.getImageData(0, 0, w, h).data; let l = w, t = h, r = -1, b = -1;
            for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const i = (y * w + x) * 4;
              if (d[i] > 235 && d[i + 1] > 235 && d[i + 2] > 235 && d[i + 3] > 200) { if (x < l) l = x; if (x > r) r = x; if (y < t) t = y; if (y > b) b = y; } }
            return r < 0 ? null : [l / w, t / h, (r + 1) / w, (b + 1) / h]; }""")
        return tuple(r) if r and (r[2] - r[0]) > 0.3 and (r[3] - r[1]) > 0.3 else None
    except Exception:  # noqa: BLE001  画不了（跨域、没加载完）
        return None


EXPORT_JS = """(e) => { try {
    let w, h;
    if (e.tagName === 'IMG') { if (!e.complete || !e.naturalWidth) return null; w = e.naturalWidth; h = e.naturalHeight; }
    else if (e.tagName === 'CANVAS') { w = e.width; h = e.height; } else return null;
    if (w < 96 || h < 96) return null;
    const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, w, h); g.drawImage(e, 0, 0, w, h);
    const d = g.getImageData(0, 0, w, h).data;                       // 跨域图片在这里抛错 → 退回截图
    let l = w, t = h, r = -1, b = -1;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const i = (y * w + x) * 4;
      if (d[i] > 235 && d[i + 1] > 235 && d[i + 2] > 235) { if (x < l) l = x; if (x > r) r = x; if (y < t) t = y; if (y > b) b = y; } }
    if (r < 0) return null;
    let sx = l, sy = t, cw = r - l + 1, ch = b - t + 1;
    if (cw * ch > 0.9 * w * h || cw < w * 0.3) { sx = 0; sy = 0; cw = w; ch = h; }       // 没有深色边：用整张原图
    // 自带深色边的（视频号）只留白色那块；不论哪种都补一圈白边（静区）——B站的原图码贴着边，不留白手机扫不出
    const m = Math.round(Math.max(cw, ch) * 0.08), o = document.createElement('canvas');
    o.width = cw + m * 2; o.height = ch + m * 2; const og = o.getContext('2d'); og.fillStyle = '#fff'; og.fillRect(0, 0, o.width, o.height);
    og.imageSmoothingEnabled = false; og.drawImage(c, sx, sy, cw, ch, m, m, cw, ch); return o.toDataURL('image/png');
  } catch (err) { return null; } }"""


def _export(el, out: Path) -> bool:
    """把二维码按原始分辨率导出（不截屏）：页面上显示的码常是从大图缩小的（公众号 430px 的码显示成 122px），
    按屏幕像素截下来太糊、手机扫不出来。读不到像素（跨域）就返回 False，退回截图。不发新请求，不会换掉页面上的码。"""
    import base64
    try:
        url = el.evaluate(EXPORT_JS)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(url, str) or not url.startswith("data:image/png;base64,"):
        return False
    _atomic(out, base64.b64decode(url.split(",", 1)[1]))
    return True


def _shot(page, el, out: Path, pad: int = 6, part: tuple[float, float, float, float] | None = None) -> bool:
    """先按原始分辨率导出（_export）；不行再页面级截图裁到二维码区域（canvas 二维码直接元素截图在无界面下常是空白，Easel 实测），
    最后退回元素截图。part：只要元素里的这一块（按比例），用来去掉微信扫码组件自带的深色边。"""
    if _export(el, out):
        return True
    b = _box(el)
    if not b or b["width"] < 40:
        return False
    if part:
        l, t, r, bt = part
        b = {"x": b["x"] + b["width"] * l, "y": b["y"] + b["height"] * t, "width": b["width"] * (r - l), "height": b["height"] * (bt - t)}
        pad = 0
    tmp = out.with_name(f".{out.name}.tmp.png")
    try:
        page.screenshot(path=str(tmp), clip={"x": max(0, b["x"] - pad), "y": max(0, b["y"] - pad),
                                             "width": b["width"] + pad * 2, "height": b["height"] + pad * 2})
    except Exception:  # noqa: BLE001
        try:
            el.screenshot(path=str(tmp))
        except Exception:  # noqa: BLE001
            return False
    os.replace(tmp, out)
    return True


def _douyin_qr(page):
    """抖音登录页多版本：base64 方形 img（≈178px），或「扫码登录」下方的方形 canvas / svg。按尺寸定位，不靠 class。"""
    for img in page.query_selector_all("img"):
        try:
            if not (img.get_attribute("src") or "").startswith("data:image"):
                continue
            b = _box(img)
            if b and 100 <= b["width"] <= 320 and abs(b["width"] - b["height"]) < 40:
                return img
        except Exception:  # noqa: BLE001
            continue
    vh = (page.viewport_size or {}).get("height", 900)
    lab = page.query_selector("text=扫码登录")
    lb = _box(lab) if lab else None
    best = None
    for el in page.query_selector_all("div, svg, canvas"):
        b = _box(el)
        if not b or not (120 <= b["width"] <= 320 and abs(b["width"] - b["height"]) < 30):
            continue
        if b["y"] < 0 or b["y"] + b["height"] > vh - 5:
            continue
        if lb is not None:
            if not (b["y"] >= lb["y"] and abs((b["x"] + b["width"] / 2) - (lb["x"] + lb["width"] / 2)) < 160):
                continue
        elif b["x"] + b["width"] / 2 < 600:
            continue
        if best is None or b["y"] < best[1]:
            best = (el, b["y"])
    return best[0] if best else None


def _square_qr(page, selectors: str, minimum: int = 96):
    """给候选元素打“像不像二维码”的分：可见、接近正方形、尺寸合适；base64 图 / canvas 加权（Easel _score_qr_el）。"""
    best = None
    for sel in [s.strip() for s in selectors.split(",") if s.strip()]:
        try:
            els = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in els:
            b = _box(el)
            if not b:
                continue
            w, h = b["width"], b["height"]
            if w < minimum or h < minimum or w > 600 or h > 600 or not (0.75 <= w / h <= 1.34):
                continue
            score = min(w, h)
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
                src = el.get_attribute("src") or ""
                score += 10000 if tag == "img" and src.startswith("data:image") else 5000 if tag == "canvas" else 2000 if "qr" in src.lower() else 0
            except Exception:  # noqa: BLE001
                pass
            if best is None or score > best[0]:
                best = (score, el)
    return best[1] if best else None


def _channels_frame(page):
    for fr in page.frames:
        u = urlparse(fr.url or "")
        if u.hostname == "open.weixin.qq.com" and u.path == "/connect/qrconnect":
            return fr
    return None


def find_qr(pf: str, page):
    if pf == "douyin":
        return _douyin_qr(page)
    if pf == "xiaohongshu":
        return _square_qr(page, XHS_QR)
    if pf in EXTRA_QR:
        return _square_qr(page, EXTRA_QR[pf])
    # 视频号：截 iframe 里那张二维码图本身（坐标是相对主页面的，可以直接裁），不截整个 iframe（外面一圈深色底）
    fr = _channels_frame(page)
    if fr:
        try:
            ready = fr.evaluate("""() => { const i = document.querySelector("img.js_qrcode_img, img.web_qrcode_img, img[src*='/connect/qrcode/']");
                                           return !!(i && i.complete && i.naturalWidth >= 96); }""")
            # 组件里有好几张同样的二维码图，前面的是隐藏的（0×0），挑显示着的那张（实测 160×160）
            imgs = fr.query_selector_all("img.js_qrcode_img, img.web_qrcode_img, img[src*='/connect/qrcode/']") if ready else []
            img = next((i for i in imgs if (_box(i) or {}).get("width", 0) >= 96), None)
            if img:
                return img
        except Exception:  # noqa: BLE001
            pass
        el = page.query_selector(CHANNELS_QR_IFRAME)
        return el if el and _box(el) else None
    return None


def qr_state(pf: str, page) -> str:
    """二维码那块的文字：scanned 已扫码等确认 / expired 过期 / 空。"""
    try:
        if pf == "wechat_channels":
            fr = _channels_frame(page)
            text = fr.inner_text("body", timeout=500) if fr else ""
        elif pf == "xiaohongshu":
            text = page.inner_text(XHS_BOX, timeout=500)
        elif pf in EXTRA_QR:
            text = page.inner_text("body", timeout=500)
        else:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    compact = re.sub(r"\s+", "", text)
    if any(w in compact for w in SCANNED_WORDS):
        return "scanned"
    if any(w in compact for w in EXPIRED_WORDS):
        return "expired"
    return ""


def refresh_qr(pf: str, page) -> bool:
    """过期的码点“刷新”；视频号的扫码组件刷不动就重开登录页。"""
    scope = _channels_frame(page) if pf == "wechat_channels" else page
    for t in ("点击刷新", "刷新二维码", "二维码已失效", "已失效", "刷新"):
        try:
            el = scope.query_selector(f"text={t}") if scope else None
            if el and el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001
            continue
    if pf == "wechat_channels":
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


# ───────────────────────── 登录了没有、是谁 ─────────────────────────

def _get_json(ctx, url: str, **kw) -> dict:
    try:
        r = ctx.request.post(url, **kw) if kw.pop("post", False) else ctx.request.get(url, **kw)
        return r.json() if r.status == 200 else {}
    except Exception:  # noqa: BLE001
        return {}


def logged_in(pf: str, ctx) -> bool:
    """登上了没有。能自动发布的三个平台看网址和 Cookie（会不会被踢回登录页）；只管账号的四个平台问它们自己的账号接口
    （快手创作者中心 current/user、B站 nav、知乎 /api/v4/me），公众号看后台网址里有没有 token。"""
    try:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if pf in PLATFORMS:
            cookies = ctx.cookies()
            return any(is_logged_in(pf, p.url, cookies) for p in pages)
        if pf == "wechat_oa":
            return any(urlparse(p.url or "").hostname == "mp.weixin.qq.com" and MP_TOKEN.search(p.url or "") for p in pages)
        if pf == "bilibili":
            return bool((_get_json(ctx, BILI_NAV, timeout=8000).get("data") or {}).get("isLogin"))
        if pf == "zhihu":
            me = _get_json(ctx, ZHIHU_ME, timeout=8000)
            return bool(me.get("id")) and not me.get("error")
        if pf == "kuaishou":
            got = _get_json(ctx, KUAISHOU_USER_API, post=True, data="{}", headers={"Content-Type": "application/json", "Referer": "https://cp.kuaishou.com/"}, timeout=8000)
            return got.get("result") == 1
    except Exception:  # noqa: BLE001  登录成功那刻页面在跳转
        return False
    return False


def _clean_avatar(url) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        u = urlparse(url)
        return url if u.scheme in ("http", "https") and u.hostname and not u.username and not u.password else ""
    except ValueError:
        return ""


def _douyin_identity(page) -> dict | None:
    resp = page.evaluate("""async (url) => { try { const r = await fetch(url, {credentials: 'include'}); return await r.json(); }
                                             catch (e) { return null; } }""", DOUYIN_PROFILE_API)
    if not isinstance(resp, dict) or resp.get("status_code") != 0:
        return None
    user = resp.get("user_info") or resp.get("user") or {}
    if not isinstance(user, dict) or not (user.get("uid") or user.get("sec_uid")):
        return None
    avatar = ""
    for field in ("avatar_larger", "avatar_medium", "avatar_thumb"):
        urls = (user.get(field) or {}).get("url_list") if isinstance(user.get(field), dict) else None
        avatar = next((_clean_avatar(u) for u in (urls or []) if _clean_avatar(u)), "")
        if avatar:
            break
    return {"name": str(user.get("nickname") or "").strip()[:40], "avatar": avatar, "id": str(user.get("unique_id") or "")[:40] or None,
            "followers": user.get("follower_count")}


def _xhs_identity(ctx) -> dict | None:
    try:
        r = ctx.request.get(XHS_ACCOUNT_API, headers={"Referer": "https://creator.xiaohongshu.com/new/home"}, timeout=8000)
        body = r.json() if r.status == 200 else {}
    except Exception:  # noqa: BLE001
        return None
    data = body.get("data") if isinstance(body, dict) and body.get("success") is True else None
    if not isinstance(data, dict) or not str(data.get("userId") or "").strip():
        return None
    name = next((str(data[k]).strip()[:40] for k in ("userName", "nickname", "name") if data.get(k)), "")
    avatar = next((_clean_avatar(data[k]) for k in ("userAvatar", "avatar", "imageb", "images") if _clean_avatar(data.get(k))), "")
    return {"name": name, "avatar": avatar, "id": str(data.get("redId") or "")[:40] or None, "followers": data.get("fansCount")}


def _bili_identity(ctx) -> dict | None:
    nav = _get_json(ctx, BILI_NAV, timeout=8000).get("data") or {}
    if not nav.get("isLogin"):
        return None
    stat = _get_json(ctx, BILI_STAT, timeout=8000).get("data") or {}
    return {"name": str(nav.get("uname") or "")[:40], "avatar": _clean_avatar(nav.get("face")), "id": str(nav.get("mid") or "") or None,
            "followers": stat.get("follower")}


def _zhihu_identity(ctx) -> dict | None:
    me = _get_json(ctx, ZHIHU_ME, timeout=8000)
    if not me.get("id") or me.get("error"):
        return None
    return {"name": str(me.get("name") or "")[:40], "avatar": _clean_avatar(me.get("avatar_url")), "id": str(me.get("url_token") or "") or None,
            "followers": me.get("follower_count")}


def _page_identity(page, names: str, avatars: str) -> dict:
    """页面上的昵称和头像（快手创作者中心、公众号后台；选择器是 Easel 真机校准的）。"""
    got = {"name": "", "avatar": ""}
    for sel in names.split(","):
        for el in page.query_selector_all(sel.strip()):
            if _box(el) and (el.inner_text() or "").strip():
                got["name"] = el.inner_text().strip().splitlines()[0][:40]
                break
        if got["name"]:
            break
    for sel in avatars.split(","):
        for el in page.query_selector_all(sel.strip()):
            if _box(el) and _clean_avatar(el.get_attribute("src")):
                got["avatar"] = _clean_avatar(el.get_attribute("src"))
                break
        if got["avatar"]:
            break
    return got


def _channels_identity(payload) -> dict | None:
    if not isinstance(payload, dict) or payload.get("errCode") != 0:
        return None
    user = (payload.get("data") or {}).get("finderUser") if isinstance(payload.get("data"), dict) else None
    if not isinstance(user, dict) or not str(user.get("nickname") or "").strip():
        return None
    return {"name": str(user["nickname"]).strip()[:40], "avatar": _clean_avatar(user.get("headImgUrl")),
            "id": str(user.get("uniqId") or "")[:40] or None, "followers": user.get("fansCount")}


def _watch_channels(ctx) -> dict:
    """视频号的账号信息只在后台首页加载时的 auth_data 接口里，开页前先挂上监听。"""
    seen: dict = {}

    def on(resp):
        u = urlparse(resp.url)
        if u.hostname == "channels.weixin.qq.com" and u.path == CHANNELS_AUTH and 200 <= resp.status < 300:
            try:
                got = _channels_identity(resp.json())
                if got:
                    seen.update(got)
            except Exception:  # noqa: BLE001
                pass
    ctx.on("response", on)
    return seen


def identity(pf: str, ctx, page, seen: dict | None = None) -> dict:
    """{name, avatar, id, followers}：读不到的字段是空的（不影响“已登录”的结论）。"""
    got = None
    try:
        if pf == "douyin":
            if "creator.douyin.com" not in (page.url or ""):
                page.goto(ACCOUNTS[pf]["home"], wait_until="domcontentloaded", timeout=60000)
            got = _douyin_identity(page)
        elif pf == "xiaohongshu":
            got = _xhs_identity(ctx)
        elif pf == "bilibili":
            got = _bili_identity(ctx)
        elif pf == "zhihu":
            got = _zhihu_identity(ctx)
        elif pf in ("kuaishou", "wechat_oa"):
            want = "cp.kuaishou.com" if pf == "kuaishou" else "mp.weixin.qq.com"
            if want not in (page.url or "") or (pf == "wechat_oa" and not MP_TOKEN.search(page.url or "")):
                page.goto(ACCOUNTS[pf]["home"], wait_until="domcontentloaded", timeout=60000)
            for _ in range(12):           # 单页应用，昵称晚一点才出来
                got = _page_identity(page, ".user-info-name, .user-line .name, .username, .user-name" if pf == "kuaishou"
                                     else ".weui-desktop-account__nickname, .account_nickname",
                                     ".user-info-avatar img[src], .user-line img[src^='http']" if pf == "kuaishou"
                                     else ".weui-desktop-account__thumb img, img.weui-desktop-account__thumb, .weui-desktop-account__avatar img")
                if got["name"]:
                    break
                page.wait_for_timeout(500)
        else:
            if seen is not None and not seen:
                page.goto(ACCOUNTS[pf]["home"], wait_until="domcontentloaded", timeout=60000)
                for _ in range(20):
                    if seen:
                        break
                    page.wait_for_timeout(500)
            got = dict(seen or {}) or None
            if not got:      # 兜底：页面上的昵称和头像（Easel 真机校准的选择器）
                name = page.query_selector(".finder-info .finder-nickname")
                img = page.query_selector(".finder-info img.avatar[src]")
                got = {"name": (name.inner_text().strip()[:40] if name else ""), "avatar": _clean_avatar(img.get_attribute("src") if img else "")}
    except Exception:  # noqa: BLE001
        got = None
    got = {"name": "", "avatar": "", "id": None, "followers": None, **(got or {})}
    if got["avatar"]:
        _save_avatar(pf, ctx, got["avatar"])
    return got


def _save_avatar(pf: str, ctx, url: str) -> None:
    try:
        r = ctx.request.get(url, timeout=15000)
        kind = (r.headers.get("content-type") or "").split(";")[0].strip()
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(kind)
        body = r.body()
        if r.status == 200 and ext and 0 < len(body) < 2 << 20:
            for old in state_dir().glob(f"{pf}-avatar.*"):
                old.unlink(missing_ok=True)
            _atomic(state_dir() / f"{pf}-avatar.{ext}", body)
    except Exception:  # noqa: BLE001
        pass


def whoami(pf: str) -> dict:
    """无界面打开后台首页：登没登录（会不会被踢回登录页）+ 账号昵称、头像。给“校验账号”用。"""
    with browser(pf, lock_wait=60) as ctx:
        seen = _watch_channels(ctx) if pf == "wechat_channels" else None
        page = ctx.new_page()
        page.goto(ACCOUNTS[pf]["home"], wait_until="commit", timeout=90000)
        page.wait_for_timeout(6000)
        ok = logged_in(pf, ctx)
        who = identity(pf, ctx, page, seen) if ok else {"name": "", "avatar": "", "id": None, "followers": None}
        return {"logged_in": ok, "url": page.url, **who}


# ───────────────────────── 抖音的短信验证 ─────────────────────────

def _douyin_modal(page):
    best = None
    for sel in DOUYIN_SMS_MODAL:
        for el in page.query_selector_all(sel):
            try:
                if el.is_visible():
                    t = el.inner_text() or ""
                    if "验证" in t and (best is None or len(t) < best[0]):
                        best = (len(t), el)
            except Exception:  # noqa: BLE001
                continue
    return best[1] if best else None


def douyin_wall(page) -> bool:
    """真的出现了身份验证 / 短信墙（只认弹窗里的这些字，不认背景的“验证码登录”表单）。"""
    m = _douyin_modal(page)
    try:
        return bool(m) and any(k in (m.inner_text() or "") for k in DOUYIN_WALL)
    except Exception:  # noqa: BLE001
        return False


def _click_first(page, selectors) -> bool:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_code(page, code: str) -> bool:
    scope = _douyin_modal(page) or page
    cands = []
    for sel in ("input[placeholder*='验证码']", "input[maxlength='6']", "input[type='tel']", "input[type='text']", "input"):
        cands = [e for e in scope.query_selector_all(sel) if _box(e)]
        if cands:
            break
    if not cands:
        return False
    inp = cands[-1]
    try:
        inp.click()
        page.keyboard.press("Meta+a")
        page.keyboard.press("Delete")
        for ch in code:
            page.keyboard.type(ch)
            page.wait_for_timeout(40)
        if inp.input_value() != code:     # React 受控输入没收到：原生 setter + input 事件
            inp.evaluate("""(el, v) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
                                         el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""", code)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(700)
    for label in ("验证", "验证并登录", "确定", "提交"):      # 抖音的提交按钮是纯 div，按文字找最内层
        for el in page.query_selector_all(f":text-is('{label}')"):
            try:
                if el.is_visible() and not any((k.inner_text() or "").strip() == label for k in el.query_selector_all("*")):
                    el.click(timeout=3000)
                    return True
            except Exception:  # noqa: BLE001
                continue
    page.keyboard.press("Enter")
    return True


def douyin_sms(pf: str, ctx, page, deadline: float) -> bool:
    """选“接收短信验证码” → 点“获取验证码” → 等面板填码 → 填进去提交 → 成功 / 码错了让人重输 / 超时。"""
    take_code(pf)
    write_status(pf, "scanned", "扫码成功，平台要求短信验证，正在准备……")
    if _click_first(page, DOUYIN_SMS_RECEIVE):
        page.wait_for_timeout(1800)
    _click_first(page, DOUYIN_SMS_SEND)
    m = re.search(r"1\d{2}[*\s]{2,}\d{2,4}", page.inner_text("body") or "")
    phone = m.group(0) if m else ""
    write_status(pf, "sms_required", "验证码已发到绑定的手机" + (f"（{phone}）" if phone else "") + "，请在下面输入", phone=phone)
    while time.time() < deadline:
        if logged_in(pf, ctx):
            return True
        if read_status(pf).get("state") == "cancelled":
            return False
        code = take_code(pf)
        if not code:
            page.wait_for_timeout(1500)
            continue
        write_status(pf, "verifying", "正在验证……", phone=phone)
        _fill_code(page, code)
        until = min(deadline, time.time() + 30)
        while time.time() < until:
            if logged_in(pf, ctx):
                return True
            body = page.inner_text("body") or ""
            bad = next((w for w in DOUYIN_SMS_ERROR if w in body), "")
            if bad:
                break
            page.wait_for_timeout(1200)
        write_status(pf, "sms_required", f"{bad or '还没验证通过'}，请重新输入验证码（可以等一会儿让平台重发）", phone=phone)
    return False


# ───────────────────────── 扫码登录 ─────────────────────────

def _open_login(pf: str, page) -> None:
    if pf == "douyin":
        page.goto(ACCOUNTS[pf]["login"], wait_until="commit", timeout=30000)
        page.wait_for_timeout(2500)
        _click_first(page, ("text=扫码登录",))     # 抖音默认可能是“验证码登录”
    elif pf == "xiaohongshu":
        page.goto(XHS_LOGIN, wait_until="commit", timeout=30000)
        try:
            page.wait_for_selector(".sso-login-wrapper", state="visible", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        if not _square_qr(page, XHS_QR):
            for el in page.query_selector_all(XHS_SWITCH):
                if _box(el):
                    el.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    break
    elif pf == "kuaishou":          # passport 登录页默认可能是验证码登录：切到“扫码登录”
        page.goto(ACCOUNTS[pf]["login"], wait_until="commit", timeout=45000)
        page.wait_for_timeout(3500)
        _click_first(page, ("text=扫码登录",))
        page.wait_for_timeout(2500)
    elif pf == "zhihu":             # 知乎首屏资源多，等 domcontentloaded 常超时：开始响应就往下走
        page.goto(ACCOUNTS[pf]["login"], wait_until="commit", timeout=45000)
        page.wait_for_timeout(5000)
        if not _square_qr(page, EXTRA_QR[pf]):
            for el in page.query_selector_all(ZHIHU_SWITCH):
                if _box(el):
                    el.click(timeout=3000)
                    page.wait_for_timeout(2000)
                    break
    elif pf in ("bilibili", "wechat_oa"):
        page.goto(ACCOUNTS[pf]["login"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    else:
        page.goto(ACCOUNTS[pf]["login"], wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector(CHANNELS_QR_IFRAME, state="visible", timeout=30000)
        except Exception:  # noqa: BLE001
            pass


def _success(pf: str, ctx, page, seen) -> dict:
    page.wait_for_timeout(3000)          # 让 Cookie 落盘
    who = identity(pf, ctx, page, seen)
    _clear(pf)
    write_status(pf, "success", f"登录成功{('：' + who['name']) if who.get('name') else ''}", name=who.get("name") or "")
    return {"logged_in": True, **who}


def run(pf: str, timeout_s: int = LOGIN_TIMEOUT_S) -> dict:
    """无界面扫码登录。返回 {logged_in, name, avatar, ...}；过程中的每一步写进状态文件给面板轮询。"""
    if pf not in ACCOUNTS:
        raise ValueError(f"不认识的平台 {pf}")
    label, app = ACCOUNTS[pf]["label"], ACCOUNTS[pf]["app"]
    _clear(pf)
    write_status(pf, "starting", "正在打开登录页、准备二维码……")
    try:
        with browser(pf, lock_wait=15) as ctx:
            seen = _watch_channels(ctx) if pf == "wechat_channels" else None
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _open_login(pf, page)
            if logged_in(pf, ctx):
                return _success(pf, ctx, page, seen)
            qr, until = None, time.time() + 25
            while time.time() < until and not qr:
                qr = find_qr(pf, page)
                if qr or (pf == "douyin" and douyin_wall(page)) or logged_in(pf, ctx):
                    break
                page.wait_for_timeout(1000)
            if qr and _shot(page, qr, qr_file(pf), pad=8, part=_white_part(qr) if pf == "wechat_channels" else None):
                write_status(pf, "qr_ready", f"用{app}扫码登录{label}")
            elif not logged_in(pf, ctx) and not (pf == "douyin" and douyin_wall(page)):
                write_status(pf, "error", "登录页上没找到二维码（可能是网络慢、被平台拦截或页面改版），可以关掉重试，或在这台 Mac 上用窗口登录")
                return {"logged_in": False}
            deadline, last_shot, scanned = time.time() + timeout_s, time.time(), False
            while time.time() < deadline:
                if read_status(pf).get("state") == "cancelled":
                    return {"logged_in": False}
                if logged_in(pf, ctx):
                    return _success(pf, ctx, page, seen)
                if pf == "douyin" and douyin_wall(page):
                    if douyin_sms(pf, ctx, page, deadline):
                        return _success(pf, ctx, page, seen)
                    if read_status(pf).get("state") != "cancelled":
                        write_status(pf, "error", "短信验证超时没完成（没输入、多次输错或已过期），请重试")
                    return {"logged_in": False}
                st = qr_state(pf, page) or ("scanned" if pf == "douyin" and find_qr(pf, page) is None and time.time() - last_shot > 3 else "")
                if st == "scanned" and not scanned:
                    scanned = True
                    write_status(pf, "scanned", "扫码成功，请在手机上确认登录")
                if not scanned and (st == "expired" or time.time() - last_shot > 8):
                    if st == "expired":
                        refresh_qr(pf, page)
                    q = find_qr(pf, page)
                    if q and _shot(page, q, qr_file(pf), pad=8, part=_white_part(q) if pf == "wechat_channels" else None):
                        write_status(pf, "qr_ready", "二维码已刷新，请扫码" if st == "expired" else read_status(pf).get("message", ""))
                    last_shot = time.time()
                page.wait_for_timeout(1500)
            write_status(pf, "error" if scanned else "expired",
                         "已扫码，但没等到登录完成：请确认手机上点了同意，再重试" if scanned else "二维码超时没扫，关掉重新登录")
            return {"logged_in": False}
    except Exception as e:  # noqa: BLE001  任何异常都要落一个终态，面板才不会一直转圈
        from . import ProfileBusy
        msg = str(e) if isinstance(e, ProfileBusy) else f"登录中断：{type(e).__name__}：{str(e).splitlines()[0][:160] if str(e) else ''}"
        write_status(pf, "error", msg)
        return {"logged_in": False, "error": msg}
