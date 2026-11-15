"""内容（选题 → 稿子）的公共小工具：计划日、稿子在哪是什么格式、能不能从面板直接发。日历、今日发布推荐和面板接口共用。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..infra import paths

ID_DATE = re.compile(r"c_(\d{4})(\d{2})(\d{2})_")
# 能自动出图（竖版 3:4 多图）的平台：小红书的“图文”就是多图笔记；知乎、公众号的“图文”是文章，不做成竖图
AUTO_IMAGE_PLATFORMS = {"xiaohongshu"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def planned_date(c: dict) -> str | None:
    """计划发布日：面板上挪过就用挪过的（planned_date），否则按内容 ID 里带的日期（c_20261111_01 → 2026-11-11）。"""
    if c.get("planned_date"):
        return str(c["planned_date"])[:10]
    m = ID_DATE.match(str(c.get("id") or ""))
    return "-".join(m.groups()) if m else None


def scripts(c: dict) -> list[dict]:
    """稿子文件：{path 绝对路径, platform 文件名即平台, exists, format 图文 / 视频（看稿子开头怎么写的）}。"""
    raw = c.get("script_path")
    try:
        items = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else ([raw] if raw else [])
    except ValueError:
        items = []
    out = []
    for sp in items:
        f = Path(sp) if Path(sp).is_absolute() else paths.ops_data() / sp
        head = f.read_text(encoding="utf-8")[:600] if f.is_file() else ""
        fmt = "图文" if "图文" in head else "视频" if head else None
        out.append({"path": str(f), "platform": f.stem, "exists": f.is_file(), "format": fmt,
                    "auto_images": fmt == "图文" and f.stem in AUTO_IMAGE_PLATFORMS})
    return out


def image_scripts(c: dict) -> list[dict]:
    """能走自动出图的稿子：写好了、是小红书的图文（多图笔记）。"""
    return [s for s in scripts(c) if s["exists"] and s["auto_images"]]
