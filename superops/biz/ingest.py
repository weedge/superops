"""Agent 产出 → ops.db（确定性写入，由宿主在输出通过校验后调用）。

Agent 自己不写业务库：它只输出结构化 JSON，这里按 Skill 名把结果落到对应的表。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import yaml

from . import db
from ..infra import paths
from ..infra.timeutil import iso

log = logging.getLogger("ingest")


def _sid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


TERM_CATEGORIES = {"pain": "痛点词", "solution": "方案词", "competitor": "竞品词", "brand": "品牌词",
                   "persona": "人群词"}   # 人群词：不搜作品，按它在抖音找粉丝数在画像区间里的人（collectors/listening.py）


def edit_term(ops, action: str, term: str, category: str | None = None) -> dict:
    """面板 / 命令行改监听词：add（新增或恢复）、enable、disable、remove。
    删掉的词留一行 removed_at，keywords.yaml 下次同步时不会再加回来。"""
    term = (term or "").strip()
    if not term or len(term) > 40:
        raise ValueError("词不能为空，也不要超过 40 个字")
    t = iso()
    row = ops.execute("SELECT * FROM listening_terms WHERE term=?", (term,)).fetchone()
    if action == "add":
        if category and category not in TERM_CATEGORIES:
            raise ValueError(f"类别只能是 {'、'.join(TERM_CATEGORIES)}")
        if row:
            ops.execute("UPDATE listening_terms SET active=1, removed_at=NULL, industry=COALESCE(?, industry), updated_at=? WHERE term=?",
                        (category, t, term))
        else:
            ops.execute("INSERT INTO listening_terms(term, industry, active, hits_7d, created_at, updated_at) VALUES (?,?,1,0,?,?)",
                        (term, category or "pain", t, t))
    elif not row:
        raise ValueError(f"没有这个词：{term}")
    elif action in ("enable", "disable"):
        ops.execute("UPDATE listening_terms SET active=?, removed_at=NULL, updated_at=? WHERE term=?", (int(action == "enable"), t, term))
    elif action == "remove":
        ops.execute("UPDATE listening_terms SET active=0, removed_at=?, updated_at=? WHERE term=?", (t, t, term))
    else:
        raise ValueError("action 只能是 add / enable / disable / remove")
    ops.commit()
    return dict(ops.execute("SELECT term, industry category, active, hits_7d, removed_at FROM listening_terms WHERE term=?", (term,)).fetchone())


def sync_terms(ops) -> int:
    p = paths.ops_home() / "listening" / "keywords.yaml"
    if not p.exists():
        return 0
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    n = 0
    for cat, terms in data.items():
        for t in terms or []:
            n += ops.execute("INSERT OR IGNORE INTO listening_terms(term, industry, active, created_at, updated_at)"
                             " VALUES (?,?,1,?,?)", (str(t), cat, iso(), iso())).rowcount
    return n


def _signals(ops, items: list[dict], source_type: str, source_ref: str) -> int:
    n = 0
    for s in items or []:
        if not isinstance(s, dict) or not s.get("quote"):
            continue
        sid = "sg_" + _sid(source_ref, s.get("quote", ""))
        n += ops.execute("INSERT OR IGNORE INTO signals(id,source_type,source_ref,quote,kind,module,persona,intensity,created_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?)",
                         (sid, source_type, s.get("source_ref") or source_ref, s["quote"][:300], s.get("kind"), s.get("module"),
                          s.get("persona"), s.get("intensity"), iso())).rowcount
    return n


def comment_triage(ops, out: dict, lease: dict) -> None:
    for c in out.get("comments") or []:
        cid = str(c.get("id"))
        # 兼容 Agent 只给平台评论 ID 的情况：按后缀匹配
        row = ops.execute("SELECT id FROM comments WHERE id=? OR id LIKE ?", (cid, f"%:{cid}")).fetchone()
        if not row:
            continue
        ops.execute("UPDATE comments SET category=?, sentiment=?, reply_draft=?, reply_status=CASE WHEN ? IS NOT NULL"
                    " THEN 'drafted' ELSE reply_status END WHERE id=?",
                    (c.get("category"), c.get("sentiment"), c.get("reply_draft"), c.get("reply_draft"), row[0]))
    _signals(ops, out.get("signals"), "comment", lease["task"]["id"])


def score_demand(ops, did: str, scoring: dict | None = None, value_hint: str | None = None) -> float:
    """按信号汇总初始分与人群分布：需求分 = Σ(意图强度 × 人群权重) × 价值系数 ÷ 成本。"""
    if scoring is None:
        from ..infra.config import load_agents_config

        scoring = load_agents_config().get("demand_scoring") or {}
    pw = scoring.get("persona_weights") or {}
    dflt = float(scoring.get("default_persona_weight", 1.0))
    cost = scoring.get("cost") or {"S": 1, "M": 2, "L": 4}
    row = ops.execute("SELECT title, kind, cost_size, value_factor FROM demands WHERE id=?", (did,)).fetchone()
    if not row:
        return 0.0
    title, kind, size, vf = row
    total, dist = 0.0, {}
    for persona, intensity in ops.execute("SELECT persona, intensity FROM signals WHERE demand_id=?", (did,)):
        persona = persona or "未知"
        w = max([float(v) for k, v in pw.items() if k in persona] or [dflt])
        total += float(intensity or 1) * w
        dist[persona] = dist.get(persona, 0) + 1
    if vf is None:
        text = f"{value_hint or ''} {kind or ''} {title or ''}"
        vf = max([float(v) for k, v in (scoring.get("value_factors") or {}).items() if k in text] or [1.0])
    score = round(total * float(vf) / float(cost.get(size or "S", 1)), 2)
    ops.execute("UPDATE demands SET score=?, value_factor=?, persona_dist=?, updated_at=? WHERE id=?",
                (score, vf, json.dumps(dist, ensure_ascii=False), iso(), did))
    return score


TEST_MARK = "【测试】"
_TEST_RE = __import__("re").compile(r"【测试】|\[测试\]|这是测试")   # 只认明确标注，别把“A/B 测试需求”这类正经需求当成测试


def looks_like_test(*texts: str | None) -> bool:
    """人写需求时标了【测试】（或说明是测试链路用的）：这条需求是测试，卡片、MR 上都要标出来。"""
    return any(t and _TEST_RE.search(t) for t in texts)


def demand_mining(ops, out: dict, lease: dict) -> None:
    for a in out.get("assignments") or []:
        did = a.get("demand_id")
        nd = a.get("new_demand") or None
        if not did and nd and nd.get("title"):
            did = "dm_" + _sid(nd["title"])
            ops.execute("INSERT OR IGNORE INTO demands(id,title,kind,module,signal_count,cost_size,score,status,updated_at)"
                        " VALUES (?,?,?,?,0,?,0,'open',?)", (did, nd["title"], nd.get("kind"), nd.get("module"), nd.get("size"), iso()))
        if not did:
            continue
        if looks_like_test(a.get("quote"), (nd or {}).get("title")):   # 归并时标题常把【测试】丢掉：按原话补回来
            ops.execute("UPDATE demands SET is_test=1, title=CASE WHEN title LIKE ? THEN title ELSE ? || title END WHERE id=?",
                        (f"{TEST_MARK}%", TEST_MARK, did))
        sid = "sg_" + _sid(lease["task"]["id"], a.get("quote", ""))
        ops.execute("INSERT OR IGNORE INTO signals(id,source_type,source_ref,quote,kind,module,persona,intensity,demand_id,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, "mining", a.get("signal_ref") or lease["task"]["id"], a.get("quote", "")[:300], a.get("kind"),
                     a.get("module"), a.get("persona"), a.get("intensity"), did, iso()))
        ops.execute("UPDATE signals SET demand_id=? WHERE quote=? AND demand_id IS NULL", (did, a.get("quote", "")))
        cnt = ops.execute("SELECT COUNT(*) FROM signals WHERE demand_id=?", (did,)).fetchone()[0]
        ops.execute("UPDATE demands SET signal_count=?, updated_at=? WHERE id=?", (cnt, iso(), did))
        score_demand(ops, did, value_hint=(nd or {}).get("value"))


def weekly_review(ops, out: dict, lease: dict) -> None:
    for d in out.get("top_demands") or []:
        if d.get("demand_id"):
            ops.execute("UPDATE demands SET score=?, cost_size=COALESCE(?, cost_size), updated_at=? WHERE id=?",
                        (d.get("score"), d.get("size"), iso(), d["demand_id"]))


def social_listening(ops, out: dict, lease: dict) -> None:
    from . import platforms

    for ld in out.get("leads") or []:
        ld["platform"] = platforms.normalize(ld.get("platform")) or ""  # “B站”和 bilibili 算同一个平台，不重复入库
        if isinstance(ld.get("score"), (int, float)) and 0 < ld["score"] <= 1:   # 评分统一 0–100（有的 Agent 写成 0–1）
            ld["score"] = round(ld["score"] * 100, 1)
        lid = "ld_" + _sid(ld.get("platform", ""), ld.get("profile_url", ""))
        ops.execute("INSERT INTO leads(id,platform,profile_url,nickname,industry,lead_type,followers,score,stage,why,outreach_draft,"
                    "target_url,comment_text,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?, '发现', ?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET score=excluded.score, why=excluded.why,"
                    " outreach_draft=excluded.outreach_draft, updated_at=excluded.updated_at,"
                    # 已经发过评论的线索不换评论对象和原文（卡片上批的是哪段字，记录里就留哪段）
                    " target_url=CASE WHEN comment_status IS NULL THEN COALESCE(excluded.target_url, target_url) ELSE target_url END,"
                    " comment_text=CASE WHEN comment_status IS NULL THEN COALESCE(excluded.comment_text, comment_text) ELSE comment_text END",
                    (lid, ld.get("platform"), ld.get("profile_url"), ld.get("nickname"), ld.get("industry"), ld.get("lead_type"),
                     ld.get("followers"), ld.get("score"), ld.get("why"), ld.get("suggestion"), ld.get("target_url"),
                     (ld.get("comment") or "").strip() or None, iso()))
    for t in out.get("term_stats") or []:
        ops.execute("UPDATE listening_terms SET hits_7d=?, updated_at=? WHERE term=?", (t.get("hits", 0), iso(), t.get("term")))
    _signals(ops, out.get("signals"), "listening", lease["task"]["id"])
    # 有还没发过卡片的新线索：接着触发“今日线索卡片”（确定性脚本按库里的线索生成，不靠 Agent 写卡片）
    if ops.execute("SELECT 1 FROM leads WHERE stage='发现' LIMIT 1").fetchone():
        out.setdefault("follow_up_events", []).append({
            "type": "lead.batch.ready", "subject": f"leads:{lease['task']['id']}", "summary": "监听发现新线索，生成今日线索卡片"})


def topic_planning(ops, out: dict, lease: dict) -> None:
    """选题入库（proposed）。没批就不做的选题标成 shelved（已搁置），不删：
    - 报告的 output.shelved 点名的；
    - 工单结束（ready）时，这张工单排过、还是 proposed、又不在最后一版 calendar 里的（人“改方向”后被换掉的那些）。"""
    task = (lease.get("task") or {}).get("id")
    final = []
    for c in out.get("calendar") or []:
        final.append(c["content_id"])
        ops.execute("INSERT OR IGNORE INTO contents(id,parent_id,pillar,persona,topic,popto_features,status,created_at,task)"
                    " VALUES (?,?,?,?,?,?, 'proposed', ?, ?)",
                    (c["content_id"], c.get("parent_id"), c.get("pillar"), c.get("persona"), c.get("title"),
                     json.dumps(c.get("popto_features") or [], ensure_ascii=False), iso(), task))
    shelve = [str(c) for c in out.get("shelved") or []]
    if out.get("report_state") == "ready" and task:
        shelve += [r[0] for r in ops.execute("SELECT id FROM contents WHERE task=? AND status='proposed'", (task,))
                   if r[0] not in final]
    for cid in dict.fromkeys(shelve):
        ops.execute("UPDATE contents SET status='shelved' WHERE id=? AND status='proposed'", (cid,))


def script_writing(ops, out: dict, lease: dict) -> None:
    cid = out.get("content_id")
    if cid:
        paths_ = [s.get("path") for s in out.get("scripts") or []]
        ops.execute("UPDATE contents SET script_path=?, status='scripted' WHERE id=?", (json.dumps(paths_, ensure_ascii=False), cid))


def incident_triage(ops, out: dict, lease: dict) -> None:
    _signals(ops, out.get("signals"), "incident", lease["task"]["id"])


def _demand_ids(ops, lease: dict) -> list[str]:
    """这一轮关联的需求：事件里带的需求 ID / 主题，或修复 MR 的 MR 号；都没有时（开发工单的后续几轮，输入是人对 PRD 的回答）
    按工单目录名找回第一轮登记的需求。"""
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if d.get("demand_id"):
            ids.append(str(d["demand_id"]))
        elif str(e.get("subject") or "").startswith("demand:"):
            ids.append(str(e["subject"]).split(":", 1)[1])
        elif d.get("iid"):  # 修复 MR：按 MR / PR 链接反查需求
            ids += [r["id"] for r in _demands_with_mr(ops, d.get("project"), d["iid"])]
    task = (lease.get("task") or {}).get("id")
    if not ids and task:
        ids = [r[0] for r in ops.execute("SELECT id FROM demands WHERE task=?", (task,))]
    return list(dict.fromkeys(ids))


def prd_writing(ops, out: dict, lease: dict) -> None:
    """需求 → PRD / 开发：把 PRD 路径、MR 链接、状态写回需求池（approved → prd_review / in_dev）。"""
    for did in _demand_ids(ops, lease):
        row = ops.execute("SELECT mr_links FROM demands WHERE id=?", (did,)).fetchone()
        if not row:
            continue
        links = [x for x in (row[0] or "").split(",") if x]
        if out.get("mr_url") and out["mr_url"] not in links:
            links.append(out["mr_url"])
        status = "in_dev" if links else ("prd_review" if out.get("needs_decision") else "approved")
        ops.execute("UPDATE demands SET status=?, prd_path=COALESCE(?, prd_path), mr_links=?, task=COALESCE(task, ?), updated_at=? WHERE id=?",
                    (status, out.get("prd_path"), ",".join(links) or None, (lease.get("task") or {}).get("id"), iso(), did))


def mr_link_pattern(project: str | None, iid) -> re.Pattern:
    """需求池里记的 MR / PR 链接：GitLab …/<项目>[/-]/merge_requests/<号>，GitHub …/<owner/repo>/pull/<号>；
    号后面不能再跟数字（!1 不能匹配到 !12）。"""
    proj = re.escape(project.strip("/")) + r"(?:/-)?" if project else r"[^,]*?"
    return re.compile(rf"{proj}/(?:merge_requests|pull)/{int(iid)}(?![0-9])")


def _demands_with_mr(ops, project: str | None, iid, extra: str = "") -> list[dict]:
    pat = mr_link_pattern(project, iid)
    rows = ops.execute(f"SELECT * FROM demands WHERE mr_links LIKE ? {extra} ORDER BY updated_at DESC", (f"%/{int(iid)}%",))
    return [dict(r) for r in rows if pat.search(r["mr_links"] or "")]


def demand_for_mr(ops, project: str, iid: int) -> dict | None:
    """MR / PR 对应的需求（按需求池里记的链接）+ 人最早说这件事的原话。"""
    found = _demands_with_mr(ops, project, iid)
    if not found:
        return None
    d = found[0]
    q = ops.execute("SELECT quote FROM signals WHERE demand_id=? ORDER BY created_at LIMIT 1", (d["id"],)).fetchone()
    d["quote"] = q[0] if q else ""
    d["is_test"] = bool(d.get("is_test")) or looks_like_test(d.get("title"), d["quote"])
    return d


def demand_merged(project: str, iid: int, ops_db=None) -> list[str]:
    """MR / PR 合并（卡片合并或在 GitLab / GitHub 上直接合并）：关联的需求标为 done。"""
    ops = db.open_ops(ops_db)
    try:
        ids = [r["id"] for r in _demands_with_mr(ops, project, iid, "AND status<>'done'")]
        for did in ids:
            ops.execute("UPDATE demands SET status='done', updated_at=? WHERE id=?", (iso(), did))
        ops.commit()
        return ids
    finally:
        ops.close()


PLATFORM_LABEL = {"douyin": "抖音", "xiaohongshu": "小红书"}


def _src(q: dict) -> str:
    pf = PLATFORM_LABEL.get(q.get("platform"), q.get("platform") or "")
    return f"{pf} @{q.get('author') or '?'}《{q.get('title') or '?'}》{q.get('url') or ''}".strip()


def competitor_review(ops, out: dict, lease: dict) -> None:
    """竞品评论评审卡片发出前补齐来源：每条明细都带一条来源作品（平台、作者、标题、链接），卡片链接列出全部来源作品，
    工单目录写一份“来源作品.md”（竞品 | 平台 | 作者 | 作品 | 链接 | 用到的原话）。不靠 Agent 自觉：人要看得出证据来自哪个竞品的哪条作品。"""
    cands = out.get("candidates") or []
    card = out.get("decision_card") if isinstance(out.get("decision_card"), dict) else None
    sources: dict[str, dict] = {}
    for i, c in enumerate(cands):
        quotes = [q for q in ((c.get("evidence") or {}).get("quotes") or []) if isinstance(q, dict) and q.get("url")]
        for q in quotes:
            src = sources.setdefault(q["url"], {**q, "used": []})
            src["used"].append(f"{i + 1}. {q.get('text', '')[:60]}")
        items = (card or {}).get("items") or []
        if quotes and i < len(items) and not any(q["url"] in (items[i].get("note") or "") for q in quotes):
            items[i]["note"] = f"{items[i].get('note') or ''}；来源：{_src(quotes[0])}".lstrip("；")
    if card is not None and sources:
        card["links"] = list(dict.fromkeys([*(card.get("links") or []), *sources]))[:20]
        by: dict[str, dict[str, int]] = {}
        for q in sources.values():
            pf = PLATFORM_LABEL.get(q.get("platform"), q.get("platform") or "")
            by.setdefault(q.get("competitor") or "?", {}).setdefault(pf, 0)
            by[q.get("competitor") or "?"][pf] += 1
        card.setdefault("reasons", []).append(
            f"证据来自 {len(sources)} 条竞品作品：" + "；".join(f"{k}（{'、'.join(f'{p} {n}' for p, n in v.items())}）" for k, v in by.items())
            + "。每条明细后面是来源作品，全部链接附在卡片末尾。")
    task = (lease.get("task") or {}).get("id")
    d = paths.ops_data() / "work" / str(task) if task else None
    if d and d.is_dir() and sources:
        rows = ["# 竞品评论评审 · 来源作品", "", "| 竞品 | 平台 | 作者 | 作品 | 链接 | 用到的原话（序号是候选需求） |", "| --- | --- | --- | --- | --- | --- |"]
        rows += [f"| {q.get('competitor') or '-'} | {PLATFORM_LABEL.get(q.get('platform'), q.get('platform') or '-')} | {q.get('author') or '-'} | "
                 f"{q.get('title') or '-'} | {u} | {'<br>'.join(q['used'])} |" for u, q in sources.items()]
        (d / "来源作品.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


HANDLERS = {
    "ops-comment-triage": comment_triage, "ops-demand-mining": demand_mining, "ops-weekly-review": weekly_review,
    "ops-social-listening": social_listening, "ops-competitor-review": competitor_review, "ops-topic-planning": topic_planning, "ops-script-writing": script_writing,
    "ops-incident-triage": incident_triage, "ops-prd-writing": prd_writing,
}


def apply(skill: str | None, out: dict, lease: dict, ops_db=None) -> bool:
    fn = HANDLERS.get(skill or "")
    if not fn:
        return False
    ops = db.open_ops(ops_db)
    try:
        ops.execute("BEGIN")
        fn(ops, out, lease)
        ops.execute("COMMIT")
        return True
    except Exception:  # noqa: BLE001
        ops.execute("ROLLBACK")
        log.exception("入库失败：%s", skill)
        return False
    finally:
        ops.close()
