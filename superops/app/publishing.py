"""面板的发布台：社媒账号（各平台创作者后台的登录态）和发布包的操作。

- 账号：每个平台一个专用 Chrome 登录目录（~/popto-superops-data/browser-profiles/<平台>/），一个平台一个账号。
  面板能做的：检查登录（无界面）、扫码登录 / 换号（在这台 Mac 上打开有界面的 Chrome，人扫码）、
  打开草稿箱（有界面）、退出登录（登录目录挪到回收处，不直接删）。
- 发布包：批准发布（等于在群里批了发布卡片：发出带人工批准凭据的 content.publish.approved，按平台模式执行）、
  预演（只试运行，不点发布）、重试（自动发布失败转人工的，用原来的批准再发一次）、人工发完补登记。
- 浏览器操作都很慢（登录最多 5 分钟、打开草稿箱最多 1 小时），放到独立子进程里跑，状态记在内存里给面板轮询；
  同一平台同一时间只跑一个（登录目录本来也只能开一个 Chrome）。
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
    # 动作: (标签, 作用对象, 有没有界面, 子进程参数)
    "check": ("检查登录", "platform", False, ["publish", "check"]),
    "login": ("扫码登录", "platform", True, ["publish", "login"]),
    "open": ("打开草稿箱", "platform", True, ["publish", "open"]),
    "try": ("预演（不发布）", "pack", False, ["publish", "try"]),
    "retry": ("重试发布", "pack", False, ["publish", "retry"]),
}
# 各平台的字数限制：标题按发布器实际截断的长度（抖音 30、小红书 20、视频号短标题 6–16），正文按平台输入框上限，话题发布器最多填 5 个
LIMITS = {"douyin": {"title": 30, "body": 1000, "topics": 5},
          "xiaohongshu": {"title": 20, "body": 1000, "topics": 5},
          "wechat_channels": {"title": 16, "title_min": 6, "body": 1000, "topics": 5}}
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
    return paths.ops_data() / "browser-profiles" / "accounts.json"


def _read_accounts() -> dict:
    try:
        return json.loads(accounts_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_account(platform: str, **fields) -> None:
    data = _read_accounts()
    data[platform] = {**(data.get(platform) or {}), **fields}
    f = accounts_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


class PublishDesk:
    def __init__(self, cfg_loader, now_iso, cli: list[str] | None = None):
        self._cfg = cfg_loader
        self._now = now_iso
        self._cli = cli or [sys.executable, "-c", "import sys; from superops.cli import main; main(sys.argv[1:])"]
        self._lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    # ── 查询 ──
    def _running(self, target: str) -> dict | None:
        """target 是平台或发布包 ID：发布包的动作也占着它那个平台的登录目录。"""
        return next((j for j in self.jobs.values() if j["state"] == "running" and target in (j["target"], j["platform"])), None)

    def accounts(self) -> list[dict]:
        pubs = _pubs()
        cfg = self._cfg()
        known = _read_accounts()
        out = []
        for pf, meta in pubs.PLATFORMS.items():
            prof = pubs.profile_dir(pf) if hasattr(pubs, "profile_dir") else paths.ops_data() / "browser-profiles" / pf
            mode = str(((cfg.get("publishers") or {}).get(pf) or {}).get("mode") or "off")
            k = known.get(pf) or {}
            job = self._running(pf)
            out.append({"platform": pf, "label": meta["label"], "mode": mode, "profile": prof.exists() and any(prof.iterdir()),
                        "status": k.get("status") or ("unknown" if prof.exists() else "none"), "checked_at": k.get("checked_at"),
                        "logged_in_at": k.get("logged_in_at"), "url": k.get("url"), "note": k.get("note"),
                        "nickname": (cfg.get("publish_accounts") or {}).get(pf) or k.get("nickname"),
                        "drafts": (pubs.DRAFTS.get(pf) or {}).get("where"), "home": meta.get("home"),
                        "upload": UPLOAD_URL.get(pf), "limits": LIMITS.get(pf, {}),
                        "busy": job and {"id": job["id"], "action": job["action"], "label": job["label"], "started_at": job["started_at"]}})
        return out

    def job_list(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j["started_at"], reverse=True)[:JOB_KEEP]

    # ── 子进程动作 ──
    def start(self, action: str, target: str, *, by: str = "panel") -> dict:
        if action not in ACTIONS:
            raise PublishError(400, f"不认识的动作 {action}")
        label, kind, headed, args = ACTIONS[action]
        pubs = _pubs()
        platform = target
        if kind == "platform":
            if target not in pubs.PLATFORMS:
                raise PublishError(400, f"不认识的平台 {target}")
            if action == "open" and target not in pubs.DRAFTS:
                raise PublishError(400, f"{pubs.PLATFORMS[target]['label']}没有可打开的草稿箱")
        else:
            pack = self.pack_row(target)
            platform = pack["platform"]
        with self._lock:
            busy = self._running(platform) or self._running(target)
            if busy:
                raise PublishError(409, f"{pubs.PLATFORMS.get(platform, {}).get('label', platform)}正在{busy['label']}，等它结束再试")
            job = {"id": uuid.uuid4().hex[:10], "action": action, "label": label, "target": target, "platform": platform,
                   "headed": headed, "by": by, "state": "running", "started_at": self._now(), "finished_at": None, "output": ""}
            # 平台级动作按平台占位；发布包动作也占住它的平台（同一个登录目录）
            job["target"] = platform if kind == "platform" else target
            self.jobs[job["id"]] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["started_at"])[:-JOB_KEEP]:
                if old["state"] != "running":
                    self.jobs.pop(old["id"], None)
        cmd = [*self._cli, *args, target]
        if action == "retry":
            cmd.append("--headed")      # 平台要短信验证码时，人在弹出的窗口里完成
        threading.Thread(target=self._run, args=(job, cmd), daemon=True, name=f"publish-{action}-{target}").start()
        return job

    def _run(self, job: dict, cmd: list[str]) -> None:
        log = paths.ops_data() / "logs" / "publish-desk"
        log.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2 * 3600, cwd=str(paths.ops_home()))
            out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.returncode and proc.stderr else "")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = "超过 2 小时没结束，已停止", False
        except OSError as e:
            out, ok = f"启动失败：{e}", False
        out = out.strip()
        if out.startswith("✗"):        # 命令行把“没成功”打成 ✗ 开头但退出码是 0（如 publish try）
            ok = False
        (log / f"{job['started_at'][:19].replace(':', '')}-{job['action']}-{job['target']}.log").write_text(out, encoding="utf-8")
        if job["action"] == "check":
            ok = "✓" in out.splitlines()[0] if out else False
            _write_account(job["platform"], status="ok" if ok else "expired", checked_at=self._now(),
                           url=(out.split()[-1] if out and out.split()[-1].startswith("http") else None),
                           note=None if ok else "登录失效或没登录，扫码重新登录")
        elif job["action"] == "login":
            ok = ok and "登录成功" in out
            _write_account(job["platform"], status="ok" if ok else "expired", checked_at=self._now(),
                           **({"logged_in_at": self._now(), "note": None} if ok else {"note": "扫码超时或没登录成功"}))
        job.update(state="done" if ok else "failed", finished_at=self._now(), output=out[-4000:])

    def logout(self, platform: str) -> dict:
        """退出登录：把登录目录挪到 browser-profiles/.removed/<平台>-<时间>/（不直接删，误点还能挪回来）。"""
        pubs = _pubs()
        if platform not in pubs.PLATFORMS:
            raise PublishError(400, f"不认识的平台 {platform}")
        if self._running(platform):
            raise PublishError(409, "这个平台正在执行别的操作，等它结束再退出")
        prof = paths.ops_data() / "browser-profiles" / platform
        if not prof.exists():
            return {"ok": True, "moved": None}
        dest = prof.parent / ".removed" / f"{platform}-{self._now()[:19].replace(':', '')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with pubs.profile_lock(platform, 0):
                shutil.move(str(prof), str(dest))
        except pubs.ProfileBusy as e:
            raise PublishError(409, str(e)) from e
        _write_account(platform, status="none", checked_at=self._now(), note="已退出登录", logged_in_at=None)
        return {"ok": True, "moved": str(dest)}

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

    def pack(self, pack_id: str) -> dict:
        """发布包详情：给面板做预览、一键复制和人工发布用。"""
        from ..biz.executors.publish import _pack_meta, _per_platform, _publisher_mode
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
        return {**{k: p.get(k) for k in ("id", "content_id", "platform", "status", "action_status", "approved_at", "scheduled_at", "updated_at")},
                "mode": _publisher_mode(self._cfg(), p["platform"], meta), "is_test": bool(meta.get("is_test")), "test_note": meta.get("test_note"),
                "title": pm.get("title") or p.get("title") or meta.get("title"), "body": pm.get("body") or meta.get("body") or "",
                "topics": topics, "pinned_comment": pm.get("pinned_comment"), "ai_label": bool(pm.get("ai_label") or meta.get("ai_label")),
                "account": pm.get("account") or p.get("account"), "channel_code": pm.get("channel_code") or meta.get("channel_code"),
                "cover": pm.get("cover") or media.get("cover"), "video": media.get("video"), "files": files, "shots": shots,
                "duration_sec": (meta.get("media_info") or {}).get("duration_sec"), "orientation": (meta.get("media_info") or {}).get("orientation")}

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


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
