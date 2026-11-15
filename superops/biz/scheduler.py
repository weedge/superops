"""schedules.yaml → 最近一次应触发时刻（用于补发睡眠期间错过的定时事件）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..infra.timeutil import TZ, parse_duration, parse_hhmm

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def last_due(spec: dict, at: datetime) -> datetime:
    """返回 <= at 的最近一次触发时刻。"""
    local = at.astimezone(TZ)
    if "every" in spec:
        step = parse_duration(spec["every"])
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        n = int((local - midnight) / step)
        return midnight + n * step
    at_s = str(spec["at"]).strip()
    parts = at_s.split()
    if len(parts) == 2:
        wd = WEEKDAYS[parts[0].upper()[:3]]
        t = parse_hhmm(parts[1])
        cand = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        cand -= timedelta(days=(cand.weekday() - wd) % 7)
        if cand > local:
            cand -= timedelta(days=7)
        return cand
    t = parse_hhmm(at_s)
    cand = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if cand > local:
        cand -= timedelta(days=1)
    return cand


def next_due(spec: dict, at: datetime) -> datetime:
    """返回 > at 的下一次触发时刻（看板用）。"""
    last = last_due(spec, at)
    if "every" in spec:
        return last + parse_duration(spec["every"])
    return last + timedelta(days=7 if len(str(spec["at"]).split()) == 2 else 1)


def max_late(spec: dict) -> timedelta:
    return parse_duration(spec.get("max_late"), timedelta(hours=12))
