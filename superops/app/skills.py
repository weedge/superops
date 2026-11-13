"""面板“Skill”页：看有哪些 Skill、被哪些规则用、最近跑得怎么样；看和改 SKILL.md，改之前留一份历史版本，可以恢复。

- 来源两处：本仓库的 skills/（可以在面板上改），agents.yaml 的 external_skill_dirs（Popto 产品仓库里的 Skill，只读，
  去那个仓库改）。同名时仓库的优先，和执行器找 Skill 的顺序一致（biz.config.find_skill）。
- 改 SKILL.md 不用重启：执行器每一轮都现读 Skill 目录。正在跑的那一轮用的是改之前的版本。
- 只改 SKILL.md。schema.json 决定报告怎么校验、入库程序怎么读，改它要连代码一起改、跑测试，不在面板上改。
- 历史版本放在数据目录 skill-history/<名字>/<时间>.md（本仓库不在 git 里，这是唯一的回退手段），每个 Skill 留最近 50 份。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..biz import paths
from ..core.errors import UserError
from ..core.model import latest_report

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
KEEP = 50
MAX_BYTES = 200_000


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def front(text: str) -> dict:
    """SKILL.md 开头 --- 之间的 name / description（只认单行的 key: value）。"""
    m = FRONT_RE.match(text)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("name", "description"):
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def dirs(rt) -> list[tuple[Path, bool]]:
    """(目录, 能不能在面板上改)：仓库的在前。"""
    home = Path(rt.home)
    out = [(home / "skills", True)]
    for d in rt.cfg.get("external_skill_dirs") or []:
        p = Path(d).expanduser()
        out.append(((p if p.is_absolute() else (home / p)).resolve(), False))
    return out


def _find(rt, name: str) -> tuple[Path, bool] | None:
    if not NAME_RE.match(name or ""):
        return None
    for base, editable in dirs(rt):
        if (base / name / "SKILL.md").is_file():
            return base / name, editable
    return None


def _history_dir(name: str) -> Path:
    return paths.ops_data() / "skill-history" / name


def _rules(rt) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    settings = rt.settings.current()
    for r in settings["policy"]["rules"]:
        skill = (r.get("run") or {}).get("skill")
        if skill:
            by.setdefault(skill, []).append({"id": r["id"], "name": r["name"], "enabled": r["enabled"],
                                             "paused": f"rule:{r['id']}" in settings["paused_keys"],
                                             "agent": (r.get("run") or {}).get("agent"), "tools": (r.get("run") or {}).get("tools")})
    return by


def _stats(rt, days: int = 30) -> dict[str, dict]:
    """按规则统计近 days 天的工单：总数、最后一份报告是 failed 的、最近一次。"""
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    out: dict[str, dict] = {}
    for t in rt.store.tickets.recent(3000, since=since):
        rid = ((t["mandate"].get("basis") or {}).get("rules") or [None])[0]
        if not rid:
            continue
        s = out.setdefault(rid, {"runs": 0, "failed": 0, "last_at": None, "last_ticket": None})
        s["runs"] += 1
        report = latest_report(t)
        if report and report["state"] == "failed":
            s["failed"] += 1
        if not s["last_at"] or t["createdAt"] > s["last_at"]:
            s["last_at"], s["last_ticket"] = t["createdAt"], f"T{t.get('no')}"
    return out


def catalog(rt) -> dict:
    rules, stats = _rules(rt), _stats(rt)
    seen, items = set(), []
    for base, editable in dirs(rt):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            md = d / "SKILL.md"
            if d.name in seen or not md.is_file():
                continue
            seen.add(d.name)
            fm = front(md.read_text(encoding="utf-8", errors="replace"))
            used = rules.get(d.name, [])
            agg = {"runs": 0, "failed": 0, "last_at": None, "last_ticket": None}
            for r in used:
                s = stats.get(r["id"]) or {}
                agg["runs"] += s.get("runs", 0)
                agg["failed"] += s.get("failed", 0)
                if s.get("last_at") and (not agg["last_at"] or s["last_at"] > agg["last_at"]):
                    agg["last_at"], agg["last_ticket"] = s["last_at"], s["last_ticket"]
            hist = _history_dir(d.name)
            items.append({"name": d.name, "description": fm.get("description") or "", "editable": editable,
                          "origin": "仓库" if editable else "外部", "dir": str(d), "has_schema": (d / "schema.json").is_file(),
                          "updated_at": _mtime(md), "rules": used, "stats": agg,
                          "versions": len(list(hist.glob("*.md"))) if hist.is_dir() else 0})
    missing = sorted(set(rules) - seen)
    return {"skills": items, "missing": [{"name": n, "rules": rules[n]} for n in missing],
            "dirs": [{"path": str(b), "editable": e, "exists": b.is_dir()} for b, e in dirs(rt)]}


def detail(rt, name: str) -> dict:
    found = _find(rt, name)
    if not found:
        raise UserError("invalidSettings", {"error": f"没有这个 Skill：{name}"})
    d, editable = found
    md = d / "SKILL.md"
    schema = None
    if (d / "schema.json").is_file():
        schema = dumps_schema((d / "schema.json").read_text(encoding="utf-8", errors="replace"))
    hist = _history_dir(name)
    versions = sorted(hist.glob("*.md"), reverse=True) if hist.is_dir() else []
    files = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts)[:200]
    return {"name": name, "editable": editable, "dir": str(d), "markdown": md.read_text(encoding="utf-8", errors="replace"),
            "updated_at": _mtime(md), "schema": schema, "files": files, "rules": _rules(rt).get(name, []),
            "versions": [{"id": v.stem, "at": _mtime(v), "size": v.stat().st_size} for v in versions]}


def validate(name: str, text: str) -> None:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise UserError("invalidSettings", {"error": f"SKILL.md 太大（超过 {MAX_BYTES // 1000} KB）"})
    fm = front(text)
    if fm.get("name") != name:
        raise UserError("invalidSettings", {"error": f"开头的 --- 里要有 name: {name}（和目录名一致）"})
    if not fm.get("description"):
        raise UserError("invalidSettings", {"error": "开头的 --- 里要有一行 description: …（一句话说这个 Skill 干什么）"})
    if len(text[FRONT_RE.match(text).end():].strip()) < 20:
        raise UserError("invalidSettings", {"error": "正文是空的"})


def _backup(name: str, md: Path, tag: str) -> str:
    hist = _history_dir(name)
    hist.mkdir(parents=True, exist_ok=True)
    vid = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{tag}"
    n, target = 1, hist / f"{vid}.md"
    while target.exists():
        n += 1
        target = hist / f"{vid}-{n}.md"
    target.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
    for old in sorted(hist.glob("*.md"), reverse=True)[KEEP:]:
        old.unlink()
    return target.stem


def save(rt, name: str, text: str, *, base_updated_at: str | None = None, origin: str = "panel") -> dict:
    found = _find(rt, name)
    if not found:
        raise UserError("invalidSettings", {"error": f"没有这个 Skill：{name}"})
    d, editable = found
    if not editable:
        raise UserError("invalidSettings", {"error": f"{name} 是外部目录的 Skill（{d}），去它自己的仓库改"})
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    validate(name, text)
    md = d / "SKILL.md"
    if base_updated_at and _mtime(md) != base_updated_at:
        raise UserError("invalidSettings", {"error": "打开之后文件被改过了（别处改的）：刷新再改，免得覆盖"})
    if md.read_text(encoding="utf-8") == text:
        return {**detail(rt, name), "changed": False}
    backup = _backup(name, md, "before-" + re.sub(r"[^a-z]", "", origin)[:10])
    md.write_text(text, encoding="utf-8")
    rt.store.set("skill:last-edit", {"name": name, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                     "origin": origin, "backup": backup})
    return {**detail(rt, name), "changed": True, "backup": backup}


def version(rt, name: str, vid: str) -> dict:
    if not _find(rt, name) or not re.fullmatch(r"[0-9A-Za-z-]{6,60}", vid or ""):
        raise UserError("invalidSettings", {"error": "没有这个版本"})
    p = _history_dir(name) / f"{vid}.md"
    if not p.is_file():
        raise UserError("invalidSettings", {"error": "没有这个版本"})
    return {"name": name, "id": vid, "at": _mtime(p), "markdown": p.read_text(encoding="utf-8")}


def restore(rt, name: str, vid: str, origin: str = "panel") -> dict:
    old = version(rt, name, vid)
    return save(rt, name, old["markdown"], origin=origin)


def act(rt, body: dict) -> dict:
    action, name = str(body.get("action") or ""), str(body.get("name") or "")
    if action == "save":
        return save(rt, name, str(body.get("markdown") or ""), base_updated_at=body.get("base_updated_at"),
                    origin=str(body.get("origin") or "panel"))
    if action == "restore":
        return restore(rt, name, str(body.get("version") or ""), origin=str(body.get("origin") or "panel"))
    raise UserError("invalidSettings", {"error": f"不认识的操作：{action}"})


def dumps_schema(schema: str | None) -> str | None:
    if not schema:
        return None
    try:
        return json.dumps(json.loads(schema), ensure_ascii=False, indent=2)
    except ValueError:
        return schema
