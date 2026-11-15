"""数据保留期清理（08 §6 个人信息、§7 审计）：每天 04:30 由宿主拉起，也可以手动试运行。

python -m superops.biz.retention run [--dry-run]

- 脚本运行目录 runs/<编号>/ 和工单目录 work/<名字>/：保留 retention.runs_days 天（默认 90，见 08 §7）。
  runs 按目录名里的创建时间算（名字里没有就按目录修改时间），work 按目录里最新一个文件的修改时间算。
  删之前，ops.db 里还引用着的文件（脚本、发布包、PRD）先挪到 content/_archived_runs/<编号>/ 或
  content/_archived_work/<名字>/，引用路径一并改写；
- WorkBuddy 收发件箱、发布截图、人工登录时的页面快照：同样按 runs_days；
- 宿主状态库（superops.sqlite）里可能带评论原文的正文（工单 / 触发 / 卡片 / 通知）：这里不直接碰那个库，
  由调用方传 `store_cleanup(text_days, dry_run)` 回调去清，返回各类清空条数；没传就跳过（单独当脚本跑时就是这样）。
  ops.db 里需求信号的原话（signals.quote）超过 retention.text_days 天（默认 170）清空；
- 评论原文本身由评论采集按 purge_after 删除（180 天），这里每天再兜一次底；
- 两个天数上限都是 170：周报会引用上一周的评论，留 10 天余量，保证评论原文的所有副本都在 180 天内删掉；
- 日志超过 retention.log_max_mb（默认 20）就轮转，保留 retention.log_keep 份旧的（默认 2）；
  launchd 以追加方式写日志，可以就地截断；
- 没打过码的运行目录 / 工单目录（宿主中途被杀、来不及扫）补扫一遍。

还在用的目录不删也不打码（Agent 还在写文件）：调用方传了 `is_active(kind, name)` 就以它为准
（kind 是 "runs" 或 "work"，name 是目录名）；没传就看目录里最近 ACTIVE_QUIET 内有没有文件改动。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ..infra import config as cfgmod
from . import db
from ..infra import paths, redact
from .scriptkit import emit_result
from ..infra.timeutil import TZ, iso, now

MAX_DAYS = 170
ARCHIVE = {"runs": "_archived_runs", "work": "_archived_work"}   # content/ 下的归档目录，按来源分开，避免重名
RUN_NAME = re.compile(r"^t_(\d{8})_(\d{6})_")
OTHER_DIRS = ("workbuddy-inbox", "workbuddy-outbox", "publish-shots")
ACTIVE_QUIET = timedelta(minutes=30)   # 没有 is_active 回调时：这段时间内有文件改动的目录当作还在用

StoreCleanup = Callable[[int, bool], dict]      # (text_days, dry_run) -> {类别: 清空条数}
IsActive = Callable[[str, str], bool]           # (kind: "runs" | "work", 目录名) -> 是否还在用
# store_cleanup 返回的类别在汇总里的叫法；不认识的原样显示
STORE_NAMES = {"tickets": "工单", "tasks": "任务", "runs": "运行记录", "assignments": "派工", "triggers": "触发",
               "events": "事件", "evals": "评估", "messages": "消息", "outbox": "通知", "notices": "通知",
               "cards": "卡片", "replies": "回复", "threads": "会话"}


def settings(cfg: dict) -> dict:
    r = cfg.get("retention") or {}

    def days(key: str, default: int) -> int:
        try:
            v = int(r.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(1, min(v, MAX_DAYS))

    def num(key: str, default: float) -> float:
        try:
            return float(r.get(key, default))
        except (TypeError, ValueError):
            return default

    return {"enabled": bool(r.get("enabled", True)), "runs_days": days("runs_days", 90),
            "text_days": days("text_days", MAX_DAYS), "log_max_mb": max(1.0, num("log_max_mb", 20)),
            "log_keep": max(1, int(num("log_keep", 2)))}


# ---------------------------------------------------------------- 运行目录
def run_created(d: Path) -> datetime:
    m = RUN_NAME.match(d.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            pass
    return datetime.fromtimestamp(d.stat().st_mtime, TZ)


def _subdirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.is_symlink())


def run_dirs(data: Path) -> list[Path]:
    """脚本运行目录：<数据目录>/runs/<编号>/。"""
    return _subdirs(data / "runs")


def work_dirs(data: Path) -> list[Path]:
    """工单目录：<数据目录>/work/<名字>/。"""
    return _subdirs(data / "work")


def last_touched(d: Path) -> float:
    """目录里最新一个文件的修改时间（时间戳）。不算打码标记：补打码写标记不该让目录“变新”；
    一个文件都没有就用目录自己的修改时间。"""
    newest = 0.0
    for dirpath, _dirs, files in os.walk(d, followlinks=False):
        for name in files:
            if name == redact.MARKER:
                continue
            try:
                newest = max(newest, os.lstat(Path(dirpath) / name).st_mtime)
            except OSError:
                pass
    if newest:
        return newest
    try:
        return d.lstat().st_mtime
    except OSError:
        return 0.0


def last_used(kind: str, d: Path) -> datetime:
    """保留期从哪一刻起算：runs 按创建时间，work 按目录里最新文件的修改时间。"""
    return run_created(d) if kind == "runs" else datetime.fromtimestamp(last_touched(d), TZ)


def in_use(kind: str, d: Path, is_active: IsActive | None, at: datetime) -> bool:
    """这个目录是不是还有人在用（在用的不删、不打码）。"""
    if is_active is not None:
        try:
            return bool(is_active(kind, d.name))
        except Exception:  # noqa: BLE001
            return True   # 问不出来就当还在用，宁可晚删
    return last_touched(d) >= (at - ACTIVE_QUIET).timestamp()


def text_columns(conn) -> list[tuple[str, str]]:
    cols = []
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            if (r[2] or "").upper() in ("TEXT", ""):
                cols.append((t, r[1]))
    return cols


def _link_or_copy(src, dst) -> None:
    try:
        os.link(src, dst)  # 同一块盘：硬链接，不多占空间
    except OSError:
        shutil.copy2(src, dst)


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, copy_function=_link_or_copy, dirs_exist_ok=True)
    elif not dst.exists():
        _link_or_copy(src, dst)


def archive_refs(ops, data: Path, run_dir: Path, *, dry_run: bool, cols: list[tuple[str, str]] | None = None,
                 kind: str = "runs") -> int:
    """ops.db 里还引用着这个目录里的文件：复制到 content/_archived_<runs|work>/<目录名>/，引用改写过去。返回归档条目数。"""
    name = run_dir.name
    needle = f"{kind}/{name}/"
    target = f"content/{ARCHIVE[kind]}/{name}/"
    head = re.compile(r"(?<![\w.-])" + re.escape(needle))   # 前面不能紧挨别的字符：homework/x/ 不算 work/x/
    pat = re.compile(head.pattern + r"([^\s\"'\]\),;]+)")
    moved: set[str] = set()
    for table, col in cols if cols is not None else text_columns(ops):
        rows = ops.execute(f'SELECT rowid, "{col}" FROM "{table}" WHERE "{col}" LIKE ?', (f"%{needle}%",)).fetchall()
        for rowid, val in rows:
            for sub in {m.group(1).rstrip("/") for m in pat.finditer(str(val or ""))}:
                src = run_dir / sub
                if sub in moved or not src.exists():
                    continue
                if not dry_run:
                    _copy(src, data / target / sub)
                moved.add(sub)
            if not dry_run:
                ops.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', (head.sub(lambda _m: target, str(val)), rowid))
    return len(moved)


def purge_dirs(ops, data: Path, cutoff: datetime, *, dry_run: bool, kind: str = "runs",
               is_active: IsActive | None = None, at: datetime | None = None) -> dict:
    """删掉过期的 runs/ 或 work/ 子目录（kind 决定哪一个）；还在用的留着，ops.db 还引用的文件先归档。"""
    st = {"deleted": 0, "archived": 0, "kept_active": 0, "failed": 0}
    dirs = run_dirs(data) if kind == "runs" else work_dirs(data)
    old = [d for d in dirs if last_used(kind, d) < cutoff]
    if not old:
        return st
    at = at or now()
    # 只看真的存过这类目录路径的列（现在是脚本路径、发布包目录、PRD 路径），省得每个目录把全库扫一遍
    cols = [(t, c) for t, c in text_columns(ops)
            if ops.execute(f'SELECT 1 FROM "{t}" WHERE "{c}" LIKE ? LIMIT 1', (f"%{kind}/%",)).fetchone()]
    for d in old:
        if in_use(kind, d, is_active, at):
            st["kept_active"] += 1
            continue
        try:
            st["archived"] += archive_refs(ops, data, d, dry_run=dry_run, cols=cols, kind=kind)
            if not dry_run:
                shutil.rmtree(d)
            st["deleted"] += 1
        except OSError:
            st["failed"] += 1
    return st


def _newest_mtime(p: Path) -> float:
    try:
        newest = p.lstat().st_mtime
    except OSError:
        return 0.0
    if p.is_dir() and not p.is_symlink():
        for dirpath, _dirs, files in os.walk(p, followlinks=False):
            for name in files:
                try:
                    newest = max(newest, os.lstat(Path(dirpath) / name).st_mtime)
                except OSError:
                    pass
    return newest


def purge_old_entries(root: Path, cutoff: datetime, *, dry_run: bool) -> int:
    """目录下按条目（文件或子目录）删：最新的文件都早于 cutoff 才删。"""
    if not root.is_dir():
        return 0
    n = 0
    for p in root.iterdir():
        if p.is_symlink() or _newest_mtime(p) >= cutoff.timestamp():
            continue
        if not dry_run:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        n += 1
    return n


# ---------------------------------------------------------------- 数据库正文
def blank_ops_text(ops, cutoff_iso: str, *, dry_run: bool) -> dict[str, int]:
    sig_where = "created_at<? AND quote IS NOT NULL"
    com_where = "purge_after<?"
    if dry_run:
        sig = ops.execute(f"SELECT COUNT(*) FROM signals WHERE {sig_where}", (cutoff_iso,)).fetchone()[0]
        com = ops.execute(f"SELECT COUNT(*) FROM comments WHERE {com_where}", (iso(),)).fetchone()[0]
    else:
        sig = ops.execute(f"UPDATE signals SET quote=NULL WHERE {sig_where}", (cutoff_iso,)).rowcount
        com = ops.execute(f"DELETE FROM comments WHERE {com_where}", (iso(),)).rowcount
    return {"signals": sig, "comments": com}


# ---------------------------------------------------------------- 日志、打码
def rotate_logs(dirs: list[Path], max_bytes: int, keep: int, *, dry_run: bool) -> list[str]:
    rotated = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.log")):
            try:
                if p.is_symlink() or p.stat().st_size <= max_bytes:
                    continue
            except OSError:
                continue
            rotated.append(f"{d.name}/{p.name}")
            if dry_run:
                continue
            for i in range(keep, 1, -1):
                older = p.with_name(f"{p.name}.{i - 1}")
                if older.exists():
                    os.replace(older, p.with_name(f"{p.name}.{i}"))
            first = p.with_name(f"{p.name}.1")
            shutil.copy2(p, first)
            os.truncate(p, 0)  # 写日志的进程还开着这个文件（追加模式），不能换文件，只能截断
            redact.scrub_file(first)  # 轮转出来的旧日志不再有人写，可以打码
    return rotated


def scrub_pending(data: Path, *, dry_run: bool, is_active: IsActive | None = None, at: datetime | None = None) -> dict[str, int]:
    """没打过码、或打码后又有改动的运行目录 / 工单目录补扫；还在用的不碰（Agent 还在写文件）。"""
    at = at or now()
    out = {"dirs": 0, "files": 0, "hits": 0}
    for kind, dirs in (("runs", run_dirs(data)), ("work", work_dirs(data))):
        for d in dirs:
            if not redact.needs_scrub(d) or in_use(kind, d, is_active, at):
                continue
            r = redact.scrub_tree(d, dry_run=dry_run)
            out["dirs"] += 1
            out["files"] += r["files"]
            out["hits"] += r["hits"]
    for sub in ("workbuddy-inbox", "workbuddy-outbox", "logs/browser-login"):
        r = redact.scrub_tree(data / sub, mark=False, dry_run=dry_run)
        out["files"] += r["files"]
        out["hits"] += r["hits"]
    return out


def _purged_text(label: str, st: dict, days: int, kind: str) -> str | None:
    if not (st["deleted"] or st["kept_active"] or st["failed"]):
        return None
    p = f"删除 {days} 天前的{label} {st['deleted']} 个"
    if st["archived"]:
        p += f"（内容库还引用的 {st['archived']} 份文件已挪到 content/{ARCHIVE[kind]}/）"
    if st["kept_active"]:
        p += f"，{st['kept_active']} 个还在用先留着"
    if st["failed"]:
        p += f"，{st['failed']} 个删除失败"
    return p


# ---------------------------------------------------------------- 入口
def run(*, dry_run: bool = False, cfg: dict | None = None, store_cleanup: StoreCleanup | None = None,
        is_active: IsActive | None = None) -> dict:
    """跑一遍保留期清理，返回 {"summary": 一句话, "details": {...}}。

    store_cleanup(text_days, dry_run)：清宿主状态库里过期的正文，返回 {类别: 清空条数}，结果并进汇总
    （details["store_text"]）；不传就跳过。回调抛异常不影响其余清理，报错写进汇总。
    is_active(kind, name)：runs/<name> 或 work/<name> 是不是还在用；不传就按最近有没有文件改动判断。
    """
    s = settings(cfg if cfg is not None else cfgmod.load_agents_config())
    if not s["enabled"]:
        return {"summary": "数据保留清理已停用（retention.enabled=false），没有改动", "details": {}}
    data = paths.ops_data()
    t = now()
    runs_cut = t - timedelta(days=s["runs_days"])
    text_cut = iso(t - timedelta(days=s["text_days"]))
    store_n: dict | None = None
    store_err: str | None = None
    ops = db.open_ops()
    try:
        scrub = scrub_pending(data, dry_run=dry_run, is_active=is_active, at=t)  # 先打码再归档：归档出去的副本是干净的
        runs = purge_dirs(ops, data, runs_cut, dry_run=dry_run, kind="runs", is_active=is_active, at=t)
        work = purge_dirs(ops, data, runs_cut, dry_run=dry_run, kind="work", is_active=is_active, at=t)
        others = sum(purge_old_entries(data / sub, runs_cut, dry_run=dry_run) for sub in OTHER_DIRS)
        others += purge_old_entries(data / "logs" / "browser-login" / ".playwright-cli", runs_cut, dry_run=dry_run)
        if store_cleanup is not None:
            try:
                store_n = dict(store_cleanup(s["text_days"], dry_run) or {})
            except Exception as e:  # noqa: BLE001
                store_err = f"{type(e).__name__}: {e}"[:300]
        ops_n = blank_ops_text(ops, text_cut, dry_run=dry_run)
        rotated = rotate_logs([data / "logs", data / "notify"], int(s["log_max_mb"] * 1024 * 1024), s["log_keep"],
                              dry_run=dry_run)
    finally:
        ops.close()

    parts = [p for p in (_purged_text("运行目录", runs, s["runs_days"], "runs"),
                         _purged_text("工单目录", work, s["runs_days"], "work")) if p]
    if others:
        parts.append(f"收发件箱 / 发布截图 / 登录快照删 {others} 项")
    blanked = {k: v for k, v in (store_n or {}).items() if isinstance(v, int) and not isinstance(v, bool) and v}
    if blanked:
        parts.append(f"状态库清空 {s['text_days']} 天前的正文：" + "、".join(f"{STORE_NAMES.get(k, k)} {v}" for k, v in blanked.items()))
    if store_err:
        parts.append(f"状态库正文清理失败（{store_err}）")
    if ops_n["signals"]:
        parts.append(f"需求信号原话 {ops_n['signals']} 条")
    if ops_n["comments"]:
        parts.append(f"过期评论 {ops_n['comments']} 条")
    if rotated:
        parts.append(f"日志轮转 {len(rotated)} 个（{'、'.join(rotated)}）")
    if scrub["hits"]:
        parts.append(f"补打码：{scrub['files']} 个文件 {scrub['hits']} 处（Cookie / token / 手机号）")
    summary = "；".join(parts) if parts else "没有到期的数据"
    if dry_run:
        summary = "（试运行，没有改动）" + summary
    details = {"settings": s, "scrub": scrub, "runs": runs, "work": work, "other_entries": others, "store_text": store_n,
               "store_error": store_err, "ops_text": ops_n, "rotated_logs": rotated, "dry_run": dry_run}
    return {"summary": summary, "details": details}


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        r = run(dry_run="--dry-run" in sys.argv[2:])
        emit_result(r["summary"], retention=r["details"])
    else:
        print(__doc__)
        sys.exit(2)
