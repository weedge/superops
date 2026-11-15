"""时间工具：统一用 Asia/Shanghai，ISO 字符串存库。"""
from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

_FAKE_NOW: datetime | None = None


def now() -> datetime:
    if _FAKE_NOW is not None:
        return _FAKE_NOW
    env = os.environ.get("OPS_FAKE_NOW")
    if env:
        return parse_iso(env)
    return datetime.now(TZ).replace(microsecond=0)


def set_fake_now(dt: datetime | None) -> None:
    """测试用：冻结时间。"""
    global _FAKE_NOW
    _FAKE_NOW = dt


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(TZ).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)?\s*$")


def parse_duration(s: str | int | float | None, default: timedelta | None = None) -> timedelta | None:
    """'10m' / '2h' / '30s' / '1d' / 90（秒）→ timedelta。"""
    if s is None or s == "":
        return default
    if isinstance(s, (int, float)):
        return timedelta(seconds=float(s))
    m = _DUR.match(str(s))
    if not m:
        raise ValueError(f"无法解析的时长: {s!r}")
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def in_window(dt: datetime, window: str | None) -> bool:
    """window 形如 '22:30-08:00'（可跨午夜）。"""
    if not window:
        return False
    a, b = (parse_hhmm(x) for x in window.split("-"))
    t = dt.astimezone(TZ).time()
    if a <= b:
        return a <= t < b
    return t >= a or t < b


def window_end(dt: datetime, window: str) -> datetime:
    """返回 dt 所在静默窗口的结束时刻（dt 不在窗口内则返回 dt）。"""
    if not in_window(dt, window):
        return dt
    _, b = (parse_hhmm(x) for x in window.split("-"))
    local = dt.astimezone(TZ)
    end = local.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end


def today_start(dt: datetime | None = None) -> datetime:
    d = (dt or now()).astimezone(TZ)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)
