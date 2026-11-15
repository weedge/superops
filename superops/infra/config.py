"""加载 agents.yaml（叠加数据目录的 local.yaml）/ schedules.yaml，找 Skill 目录。

路由规则不在这里：新架构由 superops.app 的策略决定谁接单，旧 rules.json 只留作参考（config/ops-rules.reference.json）。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from . import paths

# 取值范围（宿主校验策略 / 工单参数时用）：通知方式、工具档位、思考强度。
# 上下文提供者是业务的，名单在 superops.biz.context.CONTEXT_PROVIDERS
TELL = {"never", "needed", "always", "digest"}
TOOLS = {"readonly", "workspace", "browser", "dev"}
EFFORT = {"low", "medium", "high", "xhigh", "max"}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(p: Path) -> Any:
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_agents_config(home: Path | None = None) -> dict:
    home = home or paths.ops_home()
    base = _read_yaml(home / "agents.yaml") or {}
    local = _read_yaml(paths.ops_data() / "local.yaml") or {}
    return deep_merge(base, local)


def load_schedules(home: Path | None = None) -> list[dict]:
    home = home or paths.ops_home()
    return _read_yaml(home / "schedules.yaml") or []


def find_skill(name: str, cfg: dict, home: Path | None = None) -> Path | None:
    """返回 Skill 目录：先找仓库的 skills/，再找 external_skill_dirs。"""
    home = home or paths.ops_home()
    cands = [home / "skills" / name]
    for d in cfg.get("external_skill_dirs", []) or []:
        p = Path(d).expanduser()
        if not p.is_absolute():
            p = (home / p).resolve()
        cands.append(p / name)
    for c in cands:
        if (c / "SKILL.md").exists():
            return c
    return None
