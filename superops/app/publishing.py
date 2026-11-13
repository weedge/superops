"""面板的发布台：社媒账号（各平台创作者后台的登录态）和发布包的操作。

- 账号：每个号一个专用 Chrome 登录目录（~/popto-superops-data/browser-profiles/<账号键>/）。同一平台可以登多个号：
  主号的账号键就是平台名（douyin），“添加账号”加的号是 douyin@2、douyin@3……每个平台有一个默认账号（发布包没指定账号、
  评论引流、登录检查都用它），没设过就是主号。
  抖音、小红书、视频号能自动发布；快手、B站、知乎、公众号只管账号（登录、校验、读身份、退出），发布走人工路径。
  面板能做的（参照 Easel 的账号页）：扫码登录 / 换号（无界面打开登录页，二维码截图在面板里显示，手机扫；抖音要短信验证码时
  面板里填）、校验账号（无界面，顺带读出昵称、头像、粉丝数）、在这台 Mac 上开窗口登录（备用）、打开草稿箱（有界面）、
  添加账号、设为默认、退出登录（登录目录挪到回收处，不直接删；后加的号退出就是移除）。
- 发布包：批准发布（等于在群里批了发布卡片：发出带人工批准凭据的 content.publish.approved，按平台模式执行）、
  预演（只试运行，不点发布）、重试（自动发布失败转人工的，用原来的批准再发一次）、人工发完补登记。
- 浏览器操作都很慢（登录最多 5 分钟、打开草稿箱最多 1 小时），放到独立子进程里跑，状态记在内存里给面板轮询；
  同一个号同一时间只跑一个（登录目录本来也只能开一个 Chrome）。
- 发布包：发到哪个号可以在面板上改（publish_packs.account 存账号键，空着就用默认账号）。
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from ..biz import paths

ACTIONS = {
    # 动作: (标签, 作用对象（account 账号键 / pack 发布包 ID）, 有没有界面, 子进程参数)
    "check": ("校验账号", "account", False, ["publish", "check"]),
    "qrlogin": ("扫码登录", "account", False, ["publish", "qrlogin"]),
    "login": ("开窗口登录", "account", True, ["publish", "login"]),
    "open": ("打开草稿箱", "account", True, ["publish", "open"]),
    "try": ("预演（不发布）", "pack", False, ["publish", "try"]),
    "retry": ("重试发布", "pack", False, ["publish", "retry"]),
}
# 各平台的字数限制：标题按发布器实际截断的长度（抖音 30、小红书 20、视频号短标题 6–16），正文按平台输入框上限，话题发布器最多填 5 个
LIMITS = {"douyin": {"title": 30, "body": 1000, "topics": 5},
          "xiaohongshu": {"title": 20, "body": 1000, "topics": 5},
          "wechat_channels": {"title": 16, "title_min": 6, "body": 1000, "topics": 5}}
# 发布包还能改账号的状态：发出去、存了草稿、不发的都不能改了
ACCOUNT_EDITABLE = {"ready", "carded", "failed", "approved"}
# 人工发布时打开的官方发布页（用你自己浏览器的登录态，和专用 Chrome 的登录目录无关）
UPLOAD_URL = {"douyin": "https://creator.douyin.com/creator-micro/content/upload",
              "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish?source=official",
              "wechat_channels": "https://channels.weixin.qq.com/platform/post/create"}
MEDIA_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v"}
JOB_KEEP = 40


class PublishError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _pubs():
    from ..biz.executors import publishers
    return publishers


def accounts_file() -> Path:
    return _pubs().registry_file()


def _read_accounts() -> dict:
    return _pubs().read_registry()


def _write_account(key: str, **fields) -> None:
    _pubs().write_account(key, **fields)


def _key(target: str) -> str:
    pubs = _pubs()
    if not pubs.is_account_key(target):
        raise PublishError(400, f"不认识的账号 {target}")
    return target


class PublishDesk:
    def __init__(self, cfg_loader, now_iso, cli: list[str] | None = None):
        self._cfg = cfg_loader
        self._now = now_iso
        self._cli = cli or [sys.executable, "-c", "import sys; from superops.cli import main; main(sys.argv[1:])"]
        self._lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    # ── 查询 ──
    def _running(self, target: str) -> dict | None:
        """target 是账号键或发布包 ID：发布包的动作也占着它要发的那个号的登录目录。"""
        return next((j for j in self.jobs.values() if j["state"] == "running" and target in (j["target"], j.get("account") or j.get("platform"))), None)

    def accounts(self) -> list[dict]:
        """每个号一行（同一平台的号挨着，主号在前）：key 是账号键，platform 是平台，default 是不是这个平台的默认账号。"""
        pubs = _pubs()
        cfg = self._cfg()
        known = _read_accounts()
        out = []
        for pf, meta in pubs.ACCOUNTS.items():
            can = bool(meta.get("publish"))           # 只管账号的平台（快手、B站、知乎、公众号）还没有自动发布器
            mode = str(((cfg.get("publishers") or {}).get(pf) or {}).get("mode") or "off") if can else "none"
            keys = pubs.account_keys(pf)
            default = pubs.default_account(pf)
            for key in keys:
                prof = pubs.profiles_root() / key
                k = known.get(key) or {}
                job = self._running(key)
                av = self._qr().avatar_file(key)
                remark = ((cfg.get("publish_accounts") or {}).get(pf) if key == pf else None) or k.get("nickname")
                out.append({"key": key, "platform": pf, "label": meta["label"], "main": key == pf, "default": key == default,
                            "slug": key.split("@", 1)[1] if "@" in key else "", "accounts": len(keys),
                            "mode": mode, "publish": can, "app": meta.get("app"),
                            "profile": prof.exists() and any(prof.iterdir()),
                            "status": k.get("status") or ("unknown" if prof.exists() else "none"), "checked_at": k.get("checked_at"),
                            "logged_in_at": k.get("logged_in_at"), "url": k.get("url"), "note": k.get("note"),
                            "nickname": remark, "name": k.get("name"), "account_id": k.get("account_id"), "followers": k.get("followers"),
                            "avatar": av.stat().st_mtime_ns // 1_000_000 if av and k.get("status") == "ok" else None,
                            "drafts": (pubs.DRAFTS.get(pf) or {}).get("where"), "home": meta.get("home"),
                            "upload": UPLOAD_URL.get(pf) or meta.get("upload"), "limits": LIMITS.get(pf, {}),
                            "busy": job and {"id": job["id"], "action": job["action"], "label": job["label"], "started_at": job["started_at"]}})
        return out

    def add_account(self, platform: str, *, by: str = "panel") -> dict:
        """给一个平台再加一个号：分配下一个账号键（douyin@2…），直接开始扫码登录。没登录成功的号留在列表里，可以重试或移除。"""
        pubs = _pubs()
        if platform not in pubs.ACCOUNTS:
            raise PublishError(400, f"不认识的平台 {platform}")
        with self._lock:
            pending = [k for k in pubs.account_keys(platform)[1:] if (_read_accounts().get(k) or {}).get("status") in (None, "none")
                       and not self._running(k)]
            key = pending[0] if pending else pubs.next_account_key(platform)    # 上次加了没登上的号先复用，不越加越多
            if not pending:
                _write_account(key, status="none", added_at=self._now(), note=None)
        job = self.start("qrlogin", key, by=by)
        return {**job, "key": key}

    def set_default(self, key: str) -> dict:
        """设为这个平台的默认账号：发布包没指定账号的、评论引流、登录检查都用它。只能设登录着的号。"""
        pubs = _pubs()
        key = _key(key)
        if (_read_accounts().get(key) or {}).get("status") != "ok":
            raise PublishError(409, f"{pubs.account_label(key)}还没登录（或登录失效），先扫码登录再设为默认")
        pubs.set_default(key)
        return {"ok": True, "default": key}

    @staticmethod
    def _qr():
        from ..biz.executors.publishers import qrlogin
        return qrlogin

    def login_status(self, key: str) -> dict:
        """扫码登录的实时状态（面板每秒拉一次）：状态、提示、二维码有没有更新、是否还在跑。"""
        key = _key(key)
        st = self._qr().read_status(key)
        job = self._running(key)
        running = bool(job and job["action"] == "qrlogin")
        if not running and st.get("state") not in (*self._qr().TERMINAL, "none"):
            st = {**st, "state": "error", "message": "登录进程已经结束：" + (st.get("message") or "没写原因")}
        return {**st, "running": running}

    def submit_code(self, key: str, code: str) -> dict:
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        if not 4 <= len(code) <= 8:
            raise PublishError(400, "验证码是 4–8 位数字")
        if self.login_status(key).get("state") not in ("sms_required", "verifying"):
            raise PublishError(409, "现在不需要验证码")
        self._qr().submit_code(key, code)
        return {"ok": True}

    def cancel(self, key: str) -> dict:
        """关掉扫码弹窗：停掉登录进程，放开登录目录。"""
        job = self._running(_key(key))
        if not job or job["action"] != "qrlogin":
            return {"ok": True, "stopped": False}
        self._qr().write_status(key, "cancelled", "已取消")
        proc = job.get("_proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return {"ok": True, "stopped": True}

    def job_list(self) -> list[dict]:
        return [{k: v for k, v in j.items() if not k.startswith("_")} for j in sorted(self.jobs.values(), key=lambda j: j["started_at"], reverse=True)[:JOB_KEEP]]

    # ── 子进程动作 ──
    def start(self, action: str, target: str, *, by: str = "panel") -> dict:
        if action not in ACTIONS:
            raise PublishError(400, f"不认识的动作 {action}")
        label, kind, headed, args = ACTIONS[action]
        pubs = _pubs()
        if kind == "account":
            account = _key(target)
            if action == "open" and pubs.platform_of(account) not in pubs.DRAFTS:
                raise PublishError(400, f"{pubs.account_label(account)}没有可打开的草稿箱")
        else:
            pack = self.pack_row(target)
            try:
                account = pubs.pack_account(pack, self._pack_meta(pack))
            except pubs.AccountError:
                account = pack["platform"]        # 认不出账号：照样跑（发布器会报出来、转人工），按主号占位
        platform = pubs.platform_of(account)
        with self._lock:
            busy = self._running(account) or self._running(target)
            if busy:
                raise PublishError(409, f"{pubs.account_label(account)}正在{busy['label']}，等它结束再试")
            # 账号动作按账号占位；发布包动作也占住它要发的那个号（同一个登录目录）
            job = {"id": uuid.uuid4().hex[:10], "action": action, "label": label, "target": target, "account": account, "platform": platform,
                   "headed": headed, "by": by, "state": "running", "started_at": self._now(), "finished_at": None, "output": ""}
            self.jobs[job["id"]] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["started_at"])[:-JOB_KEEP]:
                if old["state"] != "running":
                    self.jobs.pop(old["id"], None)
        cmd = [*self._cli, *args, target]
        if action == "qrlogin":
            self._qr().write_status(account, "starting", "正在打开登录页、准备二维码……")
            if str((_read_accounts().get(account) or {}).get("note") or "").startswith("上次扫码"):
                _write_account(account, note=None)          # 重新扫码：上一次失败的提示不再挂着
        if action == "retry":
            cmd.append("--headed")      # 平台要短信验证码时，人在弹出的窗口里完成
        threading.Thread(target=self._run, args=(job, cmd), daemon=True, name=f"publish-{action}-{target}").start()
        return job

    def _run(self, job: dict, cmd: list[str]) -> None:
        log = paths.ops_data() / "logs" / "publish-desk"
        log.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(paths.ops_home()))
            job["_proc"] = proc                       # 扫码弹窗关掉时 cancel() 用它停掉进程
            try:
                stdout, stderr = proc.communicate(timeout=2 * 3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                stderr = "超过 2 小时没结束，已停止\n" + (stderr or "")
            out = (stdout or "") + (("\n" + stderr) if proc.returncode and stderr else "")
            ok = proc.returncode == 0
        except OSError as e:
            out, ok = f"启动失败：{e}", False
        job.pop("_proc", None)
        out = out.strip()
        acct = _account_line(out)
        if out.startswith("✗"):        # 命令行把“没成功”打成 ✗ 开头但退出码是 0（如 publish try）
            ok = False
        (log / f"{job['started_at'][:19].replace(':', '')}-{job['action']}-{job['target']}.log").write_text(out, encoding="utf-8")
        who = {"name": acct.get("name") or None, "account_id": acct.get("id"), "followers": acct.get("followers")} if acct.get("name") else {}
        key = job["account"]
        if job["action"] == "check":
            first = out.splitlines()[0] if out else ""
            ok = "✓" in first
            url = next((w for w in reversed(first.split()) if w.startswith("http")), None)
            _write_account(key, status="ok" if ok else "expired", checked_at=self._now(), url=url,
                           note=(self._same_as(key, who) if ok else "登录失效或没登录，扫码重新登录"), **who)
        elif job["action"] == "qrlogin":
            ok = bool(acct.get("logged_in")) if acct else ok and "✓" in out
            if ok:
                _write_account(key, status="ok", checked_at=self._now(), logged_in_at=self._now(), note=self._same_as(key, who), **who)
            elif self._qr().read_status(key).get("state") != "cancelled":
                _write_account(key, note="上次扫码没登录成功：" + (self._qr().read_status(key).get("message") or ""))
        elif job["action"] == "login":
            ok = ok and "登录成功" in out
            _write_account(key, status="ok" if ok else "expired", checked_at=self._now(),
                           **({"logged_in_at": self._now(), "note": None} if ok else {"note": "扫码超时或没登录成功"}))
        job.update(state="done" if ok else "failed", finished_at=self._now(), output=out[-4000:])

    def _same_as(self, key: str, who: dict) -> str | None:
        """同一平台的另一个号登的是同一个账号（同 ID，或没有 ID 时同名）：提醒一句，免得以为是两个号。"""
        pubs = _pubs()
        if not who.get("name"):
            return None
        known = _read_accounts()
        for other in pubs.account_keys(pubs.platform_of(key)):
            o = known.get(other) or {}
            if other == key or o.get("status") != "ok":
                continue
            if (who.get("account_id") and o.get("account_id") == who["account_id"]) or \
                    (not (who.get("account_id") and o.get("account_id")) and o.get("name") == who["name"]):
                return f"和 {pubs.account_label(other)}（{other}）是同一个号，可以退出其中一个"
        return None

    def logout(self, key: str) -> dict:
        """退出登录：把登录目录挪到 browser-profiles/.removed/<账号键>-<时间>/（不直接删，误点还能挪回来）。
        主号退出后还留在列表里（没登录）；后加的号退出就是移除：登记删掉，它是默认账号的话默认改回主号。"""
        pubs = _pubs()
        key = _key(key)
        if self._running(key):
            raise PublishError(409, "这个号正在执行别的操作，等它结束再退出")
        prof = pubs.profiles_root() / key
        dest = None
        if prof.exists():
            dest = prof.parent / ".removed" / f"{key}-{self._now()[:19].replace(':', '')}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with pubs.profile_lock(key, 0):
                    shutil.move(str(prof), str(dest))
            except pubs.ProfileBusy as e:
                raise PublishError(409, str(e)) from e
        removed = key != pubs.platform_of(key)
        if removed:
            pubs.forget_account(key)
        elif dest:
            _write_account(key, status="none", checked_at=self._now(), note="已退出登录", logged_in_at=None, name=None, account_id=None, followers=None)
        for f in self._qr().account_files(key):      # 头像、二维码、扫码状态（按名字精确匹配，不碰同平台别的号）
            f.unlink(missing_ok=True)
        return {"ok": True, "moved": str(dest) if dest else None, "removed": removed}

    # ── 发布包 ──
    def pack_row(self, pack_id: str) -> dict:
        from ..biz import db
        ops = db.open_ops()
        try:
            row = ops.execute("SELECT p.*, a.status action_status, a.approved_at FROM publish_packs p"
                              " LEFT JOIN actions a ON a.action_id='publish:'||p.id WHERE p.id=?", (pack_id,)).fetchone()
        finally:
            ops.close()
        if not row:
            raise PublishError(404, f"找不到发布包 {pack_id}")
        return dict(row)

    @staticmethod
    def _pack_meta(p: dict) -> dict:
        from ..biz.executors.publish import _pack_meta
        return _pack_meta(p.get("pack_dir"))

    def set_pack_account(self, pack_id: str, key: str | None) -> dict:
        """改发布包发到哪个号。key 空着 = 用这个平台的默认账号。发出去、存了草稿、不发的不能改。"""
        from ..biz import db
        pubs = _pubs()
        p = self.pack_row(pack_id)
        if p["status"] not in ACCOUNT_EDITABLE:
            raise PublishError(409, f"{pack_id} 现在是「{p['status']}」，不能再改账号")
        key = str(key or "").strip() or None
        if key and (not pubs.is_account_key(key) or pubs.platform_of(key) != p["platform"] or key not in pubs.account_keys(p["platform"])):
            raise PublishError(400, f"{key} 不是这个平台的账号")
        ops = db.open_ops()
        try:
            ops.execute("UPDATE publish_packs SET account=?, updated_at=? WHERE id=?", (key, self._now(), pack_id))
            ops.commit()
        finally:
            ops.close()
        return {"ok": True, "account": key or pubs.default_account(p["platform"])}

    def pack(self, pack_id: str) -> dict:
        """发布包详情：给面板做预览、一键复制和人工发布用。"""
        from ..biz.executors.publish import _pack_meta, _per_platform, _publisher_mode
        pubs = _pubs()
        p = self.pack_row(pack_id)
        meta = _pack_meta(p.get("pack_dir"))
        pm = _per_platform(meta, p["platform"])
        media = meta.get("media") or {}
        d = Path(p["pack_dir"]).expanduser() if p.get("pack_dir") else None
        files = []
        if d and d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in MEDIA_TYPES:
                    files.append({"name": f.name, "size": f.stat().st_size, "kind": "video" if f.suffix.lower() in {".mp4", ".mov", ".m4v"} else "image"})
        shots = []
        sd = paths.ops_data() / "publish-shots" / pack_id
        if sd.is_dir():
            shots = [f.name for f in sorted(sd.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True) if f.suffix == ".png"][:6]
        topics = pm.get("topics") or meta.get("topics") or []
        try:
            acct, acct_err = pubs.pack_account(p, meta), None
        except pubs.AccountError as e:
            acct, acct_err = None, str(e)
        return {**{k: p.get(k) for k in ("id", "content_id", "platform", "status", "action_status", "approved_at", "scheduled_at", "updated_at")},
                "mode": _publisher_mode(self._cfg(), p["platform"], meta), "is_test": bool(meta.get("is_test")), "test_note": meta.get("test_note"),
                "title": pm.get("title") or p.get("title") or meta.get("title"), "body": pm.get("body") or meta.get("body") or "",
                "topics": topics, "pinned_comment": pm.get("pinned_comment"), "ai_label": bool(pm.get("ai_label") or meta.get("ai_label")),
                "account": p.get("account") or pm.get("account"), "account_key": acct, "account_error": acct_err,
                "account_label": pubs.account_label(acct) if acct else None, "account_editable": p["status"] in ACCOUNT_EDITABLE,
                "account_choices": [{"key": k, "label": pubs.account_label(k), "default": k == pubs.default_account(p["platform"]),
                                     "status": (_read_accounts().get(k) or {}).get("status") or "none"} for k in pubs.account_keys(p["platform"])],
                "channel_code": pm.get("channel_code") or meta.get("channel_code"),
                "cover": pm.get("cover") or media.get("cover"), "video": media.get("video"), "files": files, "shots": shots,
                "duration_sec": (meta.get("media_info") or {}).get("duration_sec"), "orientation": (meta.get("media_info") or {}).get("orientation")}

    def image_path(self, key: str, kind: str) -> Path:
        """扫码登录的二维码（只在等扫码时给）和账号头像。"""
        key = _key(key)
        q = self._qr()
        f = q.qr_file(key) if kind == "qr" else q.avatar_file(key) if kind == "avatar" else None
        if not f or not f.is_file() or (kind == "qr" and q.read_status(key).get("state") != "qr_ready"):
            raise PublishError(404, "没有这张图")
        return f

    def media_path(self, pack_id: str, name: str, shot: bool = False) -> Path:
        """只给发布包目录（或它的截图目录）里的媒体文件，不接受子路径。"""
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise PublishError(400, "文件名不对")
        if shot:
            base = paths.ops_data() / "publish-shots" / pack_id
        else:
            p = self.pack_row(pack_id)
            if not p.get("pack_dir"):
                raise PublishError(404, "这个发布包没有素材目录")
            base = Path(p["pack_dir"]).expanduser()
        f = (base / name)
        if f.suffix.lower() not in MEDIA_TYPES | {".png"} or not f.is_file() or f.resolve().parent != base.resolve():
            raise PublishError(404, "没有这个文件")
        return f


def _account_line(out: str) -> dict:
    """命令行 check / qrlogin 最后一行的 {"account": {...}}：登没登录、昵称、账号 ID、粉丝数。"""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith('{"account"'):
            try:
                return json.loads(line).get("account") or {}
            except ValueError:
                return {}
    return {}


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
