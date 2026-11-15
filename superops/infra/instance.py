"""这套部署叫什么：数据目录、钥匙串服务名、launchd 标签、积分计数器都从 agents.yaml 的 instance 一节来。

只认仓库里的 agents.yaml，不认 local.yaml：local.yaml 放在数据目录里，数据目录本身要先由这里定下来。

    instance:
      name: popto                      # 只用小写字母、数字和 -
      # 下面几项不写就按 name 推导（括号里是 name: popto 推出来的值）：
      # data_dir: ~/<name>-superops-data        （~/popto-superops-data）环境变量 SUPEROPS_DATA 优先
      # keychain_service: <name>-ops            （popto-ops）密钥存成 <服务名>/<密钥名>
      # launchd_label: cn.<name>.superops       （cn.popto.superops）
      credits: {counter: popto_credits, label: Popto 积分}   # 报告 credits_used 记到哪个计数器；预算是 budgets.<counter>

没写 instance（或没写 name）时用不带任何业务名的默认值：~/superops-data、superops、superops、credits / 积分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import paths

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COUNTER_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Instance:
    name: str | None
    data_dir: str
    keychain_service: str
    launchd_label: str
    credits_counter: str
    credits_label: str


_CACHE: dict[tuple[str, int], Instance] = {}


def from_config(section: dict | None) -> Instance:
    """把 agents.yaml 的 instance 一节换成 Instance；写错了抛 ValueError（superops check 会报出来）。"""
    sec = section or {}
    if not isinstance(sec, dict):
        raise ValueError("instance 要写成一个映射")
    name = sec.get("name")
    if name is not None and not (isinstance(name, str) and NAME_RE.match(name)):
        raise ValueError(f"instance.name 是 {name!r}：只用小写字母、数字和 -，并以字母或数字开头")
    credits = sec.get("credits") or {}
    if not isinstance(credits, dict):
        raise ValueError("instance.credits 要写成 {counter, label}")
    counter = str(credits.get("counter") or "credits")
    if not COUNTER_RE.match(counter):
        raise ValueError(f"instance.credits.counter 是 {counter!r}：只用小写字母、数字和 _")
    for key in ("data_dir", "keychain_service", "launchd_label"):
        if sec.get(key) is not None and not (isinstance(sec[key], str) and sec[key].strip()):
            raise ValueError(f"instance.{key} 要写成非空的字符串")
    return Instance(
        name=name,
        data_dir=sec.get("data_dir") or (f"~/{name}-superops-data" if name else "~/superops-data"),
        keychain_service=sec.get("keychain_service") or (f"{name}-ops" if name else "superops"),
        launchd_label=sec.get("launchd_label") or (f"cn.{name}.superops" if name else "superops"),
        credits_counter=counter,
        credits_label=str(credits.get("label") or "积分"),
    )


def current(home: Path | None = None) -> Instance:
    """读 <仓库>/agents.yaml 的 instance；按文件的修改时间缓存。"""
    p = (home or paths.ops_home()) / "agents.yaml"
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        key = (str(p), -1)
    if key not in _CACHE:
        section = None
        if key[1] != -1:
            section = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("instance")
        _CACHE.clear()
        _CACHE[key] = from_config(section)
    return _CACHE[key]
