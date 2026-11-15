"""仓库目录与数据目录。数据目录不进 git。"""
from __future__ import annotations

import os
from pathlib import Path


def code_root() -> Path:
    """superops 代码所在的仓库（pyproject.toml、launchd/ 模板所在处）。不随 SUPEROPS_HOME 变。"""
    return Path(__file__).resolve().parent.parent.parent


def ops_home() -> Path:
    """配置目录（policy.yaml、agents.yaml、skills/、prompts/ 所在处）：SUPEROPS_HOME，没设时就是代码仓库。
    另起一套部署时把它指到别的目录，代码仍用 code_root()。"""
    env = os.environ.get("SUPEROPS_HOME") or os.environ.get("OPS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return code_root()


def ops_data() -> Path:
    """数据目录：superops.sqlite、ops.db、work/、日志、登录态。

    环境变量 SUPEROPS_DATA 优先，否则按 agents.yaml 的 instance（见 instance.py）。"""
    env = os.environ.get("SUPEROPS_DATA") or os.environ.get("OPS_DATA")
    if env:
        return Path(env).expanduser()
    from .instance import current
    return Path(current().data_dir).expanduser()


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
