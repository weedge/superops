"""决策档案：decisions.md（定的事）与 not-doing.md（定了不做的事）。只追加，不改写（08 §3）。"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from . import paths
from .timeutil import now

_LOCK = threading.Lock()


def _next_id(text: str, prefix: str, day: str) -> str:
    n = len(re.findall(rf"^## {prefix}-{day}-\d+", text, flags=re.M))
    return f"{prefix}-{day}-{n + 1:02d}"


def _append(path: Path, prefix: str, title: str, lines: list[str]) -> str:
    with _LOCK:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        day = now().strftime("%Y%m%d")
        entry_id = _next_id(text, prefix, day)
        block = "\n".join([f"## {entry_id} {title}", *[f"- {ln}" for ln in lines]])
        sep = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
        path.write_text(text + sep + block + "\n", encoding="utf-8")
        return entry_id


def append_decision(text: str, *, source: str, applies: str | None = None, home: Path | None = None) -> str:
    home = home or paths.ops_home()
    title = text.strip().splitlines()[0][:40]
    lines = [f"原话：“{text.strip()}”", f"日期：{now().date().isoformat()}"]
    if applies:
        lines.append(f"适用：{applies}")
    lines.append(f"来源：{source}")
    return _append(home / "decisions.md", "D", title, lines)


def append_not_doing(title: str, reason: str, *, source: str, reply: str | None = None,
                     review_after_days: int = 90, home: Path | None = None) -> str:
    from datetime import timedelta

    home = home or paths.ops_home()
    lines = [f"原话：“{reason.strip()}”", f"日期：{now().date().isoformat()}"]
    lines.append(f"回复口径：{reply or '目前暂不考虑，后续有变化会第一时间通知。'}")
    lines.append(f"复查：{(now() + timedelta(days=review_after_days)).date().isoformat()}")
    lines.append(f"来源：{source}")
    return _append(home / "not-doing.md", "N", title, lines)


def relevant_entries(keywords: list[str], home: Path | None = None, limit: int = 12) -> str:
    """给提示词的“相关决定”一节用：按关键词挑出相关的决定；没有关键词时给最近的几条。"""
    home = home or paths.ops_home()
    out: list[str] = []
    for name in ("decisions.md", "not-doing.md"):
        p = home / name
        if not p.exists():
            continue
        blocks = re.split(r"(?m)^(?=## [DN]-)", p.read_text(encoding="utf-8"))
        blocks = [b.strip() for b in blocks if b.strip().startswith("## ")]
        hits = [b for b in blocks if any(k and k in b for k in keywords)] if keywords else []
        chosen = hits or blocks[-limit // 2:]
        out.extend(chosen[:limit])
    return "\n\n".join(out[:limit]) if out else "（暂无记录）"
