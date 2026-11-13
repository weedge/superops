"""仓库目录与数据目录。数据目录不进 git。"""
from __future__ import annotations

import os
from pathlib import Path


def ops_home() -> Path:
    """代码仓库根目录（policy.yaml、agents.yaml、skills/、prompts/ 所在处）。"""
    env = os.environ.get("SUPEROPS_HOME") or os.environ.get("OPS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent


def ops_data() -> Path:
    """数据目录：superops.sqlite、ops.db、work/、日志、登录态。"""
    return Path(os.environ.get("SUPEROPS_DATA") or os.environ.get("OPS_DATA") or "~/popto-superops-data").expanduser()


def ensure_data_dirs() -> Path:
    root = ops_data()
    for sub in ("work", "runs", "media", "exports", "logs", "browser-profiles",
                "workbuddy-inbox", "workbuddy-outbox", "notify", "dashboard", "content"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def expand(p: str | os.PathLike | None) -> Path | None:
    if p is None:
        return None
    return Path(os.path.expandvars(str(p))).expanduser()
