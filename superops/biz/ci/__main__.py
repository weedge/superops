"""CI 中台脚本（04）：审查结论写回 MR、合并闸门、线上核实。

python -m superops.biz.ci poll           # cron.ci_poll：发现 agent/* 分支的 MR，发 gitlab.mr.opened / updated
python -m superops.biz.ci review-post    # gitlab.review.done：把审查意见写成 MR 评论 + 标签，决定打回还是通过
python -m superops.biz.ci merge-gate     # gitlab.review.passed：跑合并闸门，能自动合就合，否则发卡片
python -m superops.biz.ci merge          # gitlab.mr.merge_approved：人批后合并
python -m superops.biz.ci verify <url> <sha> [--subject mr:repo!iid]   # 线上核实：version.json 是否包含该提交

合并后的线上核实：合并（卡片合并、自动合并、在 GitLab 上直接合并）时登记一条 deploy_checks；
ci.verify 里配了这个项目的线上版本地址，每次 poll 顺带去查，线上是这个提交或包含它就发 deploy.verified，
过了期限（默认 24 小时）还不是就发 deploy.mismatch（P0 告警）；没配地址的，合并时发 deploy.unverifiable 请人确认上线。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import timedelta

from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from . import gate
from .. import db, ingest
from .gitlab import GitLab

MAX_ROUNDS = 3


def _ci_cfg() -> dict:
    c = load_agents_config().get("ci") or {}
    c.setdefault("gitlab_url", "https://gitlab.example.com")
    c.setdefault("projects", [])
    c.setdefault("auto_merge", False)
    c.setdefault("branch_prefix", "agent/")
    c.setdefault("verify", {})   # {项目: {url: 线上版本地址（version.json 等，内容里有提交号）, within: 24h}}
    return c


# ---------------------------------------------------------------------- 合并后的线上核实
def _hours(spec, default: float = 24) -> float:
    s = str(spec or "").strip().lower()
    try:
        return float(s[:-1]) / 60 if s.endswith("m") else float(s[:-1]) * 24 if s.endswith("d") else float(s.rstrip("h"))
    except ValueError:
        return default


def verify_spec(c: dict, project: str) -> dict:
    v = (c.get("verify") or {})
    spec = v.get(project) or v.get("/" + project) or {}
    return spec if isinstance(spec, dict) else {"url": str(spec)}


def register_deploy_check(c: dict, project: str, iid: int, sha: str | None, web_url: str | None, title: str | None = None) -> list[dict]:
    """合并了：登记一条待核实。没配线上版本地址的，返回一条 deploy.unverifiable 请人确认上线。只登记一次。"""
    project = GitLab.normalize(project)
    spec = verify_spec(c, project)
    ops = db.open_ops()
    try:
        if ops.execute("SELECT 1 FROM deploy_checks WHERE project=? AND iid=?", (project, iid)).fetchone():
            return []
        t = now()
        status = "pending" if spec.get("url") and sha else "unconfigured"
        note = None if status == "pending" else ("没拿到合并提交号" if spec.get("url") else "ci.verify 没配这个项目的线上版本地址")
        ops.execute("INSERT INTO deploy_checks(project,iid,sha,url,merged_at,deadline,status,note,web_url) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project, iid, sha, spec.get("url"), iso(t), iso(t + timedelta(hours=_hours(spec.get("within")))), status, note, web_url))
        ops.commit()
    finally:
        ops.close()
    if status == "pending":
        return []
    name = f"{project}!{iid}" + (f"「{title[:40]}」" if title else "")
    return [{"source": "ci", "type": "deploy.unverifiable", "subject": f"mr:{project}!{iid}", "link": web_url,
             "summary": f"{name} 已合并，但没法自动核实是否上线（{note}）：请人上线后确认；要自动核实，在 local.yaml 的 ci.verify 配线上版本地址",
             "dedup_key": f"unverifiable:{project}:{iid}", "data": {"project": project, "iid": iid, "sha": sha}}]


def online_version(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=20) as r:
        body = r.read().decode(errors="replace")
    try:
        data = json.loads(body)
        return str(data.get("commit") or data.get("sha") or data.get("version") or "")
    except (json.JSONDecodeError, AttributeError):
        return body.strip()[:80]


def deploy_contains(gl: GitLab, project: str, sha: str, online: str) -> bool:
    """线上就是这个提交，或者线上的提交包含它（之后又有别的合并一起上了线）。"""
    if not online or not sha:
        return False
    if online.startswith(sha[:8]) or sha.startswith(online[:8]):
        return True
    if not all(ch in "0123456789abcdef" for ch in online.lower()) or len(online) < 7:
        return False
    try:
        base = gl.merge_base(project, sha, online)
    except Exception:  # noqa: BLE001
        return False
    return bool(base) and str(base).startswith(sha[:8])


def check_deploys(gl: GitLab) -> tuple[list[dict], list[str]]:
    """查一遍待核实的合并。返回 (事件, 说明)。"""
    ops = db.open_ops()
    events, notes = [], []
    try:
        for r in db.rows(ops.execute("SELECT * FROM deploy_checks WHERE status='pending' ORDER BY merged_at")):
            name, subject = f"{r['project']}!{r['iid']}", f"mr:{r['project']}!{r['iid']}"
            try:
                online = online_version(r["url"])
                ok = deploy_contains(gl, r["project"], r["sha"], online)
                seen = f"线上 {online[:12] or '未知'}，期望包含 {r['sha'][:12]}"
            except Exception as e:  # noqa: BLE001
                ok, seen = False, f"线上版本地址请求失败：{str(e)[:120]}"
            late = now() > parse_iso(r["deadline"])
            status = "verified" if ok else ("mismatch" if late else "pending")
            ops.execute("UPDATE deploy_checks SET status=?, checked_at=?, attempts=attempts+1, note=? WHERE project=? AND iid=?",
                        (status, iso(), seen, r["project"], r["iid"]))
            if status == "verified":
                events.append({"source": "ci", "type": "deploy.verified", "subject": subject, "link": r["web_url"],
                               "summary": f"{name} 已上线：{seen}", "dedup_key": f"verified:{r['project']}:{r['iid']}"})
                notes.append(f"{name} 已上线")
            elif status == "mismatch":
                events.append({"source": "ci", "type": "deploy.mismatch", "subject": subject, "link": r["web_url"],
                               "summary": f"{name} 合并超过期限还没上线：{seen}", "dedup_key": f"mismatch:{r['project']}:{r['iid']}"})
                notes.append(f"{name} 超过期限没上线")
        ops.commit()
    finally:
        ops.close()
    return events, notes


# ---------------------------------------------------------------------- 测试需求
def _demand(proj: str, iid: int) -> dict | None:
    ops = db.open_ops()
    try:
        return ingest.demand_for_mr(ops, GitLab.normalize(proj), iid)
    finally:
        ops.close()


def _producer(mr: dict) -> str:
    desc = mr.get("description") or ""
    for a in ("claude", "codex", "antigravity"):
        if f"producer: {a}" in desc or f"agent:{a}" in (mr.get("labels") or []):
            return a
    return "claude"


def cmd_poll() -> None:
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    missing = []
    if not c["projects"]:
        missing.append("local.yaml 的 ci.projects 为空")
    if not gl.configured:
        missing.append("钥匙串没有 popto-ops/gitlab（GitLab Token）")
    if missing:
        emit_result("CI 中台未接入：" + "；".join(missing) + "，跳过")
        return
    events = []
    for proj in [GitLab.normalize(p) for p in c["projects"]]:
        for mr in gl.open_mrs(proj):
            if not str(mr.get("source_branch", "")).startswith(c["branch_prefix"]):
                continue
            sha = mr.get("sha")
            labels = mr.get("labels") or []
            subject = f"mr:{proj}!{mr['iid']}"
            if "review:passed" in labels:
                events.append({"source": "ci", "type": "gitlab.review.passed", "subject": subject,
                               "summary": f"MR {proj}!{mr['iid']} 审查通过，待合并闸门",
                               "dedup_key": f"passed:{proj}:{mr['iid']}:{sha}",
                               "data": {"project": proj, "iid": mr["iid"], "sha": sha, "web_url": mr.get("web_url")}})
                continue
            typ = "gitlab.mr.updated" if any(l.startswith("review:round-") for l in labels) else "gitlab.mr.opened"
            events.append({"source": "ci", "type": typ, "subject": subject, "link": mr.get("web_url"),
                           "summary": f"MR {proj}!{mr['iid']}「{mr.get('title', '')[:40]}」有新提交",
                           "dedup_key": f"mr:{proj}:{mr['iid']}:{sha}",
                           "data": {"project": proj, "iid": mr["iid"], "sha": sha, "producer": _producer(mr),
                                    "web_url": mr.get("web_url"), "title": mr.get("title")}})
        # 在 GitLab 上直接合并（没走合并卡片）的 Agent MR：补一条留底，dedup 保证只记一次
        for mr in gl.merged_mrs(proj):
            if not str(mr.get("source_branch", "")).startswith(c["branch_prefix"]):
                continue
            ingest.demand_merged(proj, mr["iid"])  # 幂等：已经 done 的不再改
            events += register_deploy_check(c, proj, mr["iid"], mr.get("merge_commit_sha"), mr.get("web_url"), mr.get("title"))
            events.append({"source": "ci", "type": "gitlab.mr.merged", "subject": f"mr:{proj}!{mr['iid']}",
                           "link": mr.get("web_url"),
                           "summary": f"MR {proj}!{mr['iid']}「{mr.get('title', '')[:40]}」已合并"
                                      f"（{(mr.get('merged_by') or {}).get('username') or '?'} 在 GitLab 上合并）",
                           "dedup_key": f"merged:{proj}:{mr['iid']}",
                           "data": {"project": proj, "iid": mr["iid"], "merged_at": mr.get("merged_at"),
                                    "merged_by": (mr.get("merged_by") or {}).get("username"), "via": "gitlab"}})
    deploy_events, deploy_notes = check_deploys(gl)
    events += deploy_events
    n_open = sum(1 for e in events if e["type"].startswith(("gitlab.mr.opened", "gitlab.mr.updated", "gitlab.review")))
    emit_result(f"发现 {n_open} 个需要处理的 Agent MR" + (f"；线上核实：{'，'.join(deploy_notes)}" if deploy_notes else ""),
                follow_up_events=events)


def _fmt_findings(findings: list[dict], rnd: int) -> str:
    lines = [f"### Agent 审查 · 第 {rnd} 轮", ""]
    if not findings:
        lines.append("没有发现会出错的问题。")
    for f in findings:
        lines.append(f"- **{f.get('severity', 'minor')}** `{f.get('file', '')}:{f.get('line', '')}` {f.get('problem', '')}"
                     + (f"（触发条件：{f['trigger']}）" if f.get("trigger") else ""))
    lines.append("\n_只报真会出错的问题；minor 留档不阻塞。最多 3 轮。_")
    return "\n".join(lines)


def cmd_review_post() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    follow, posted = [], 0
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        proj, iid, rnd = d.get("project"), d.get("iid"), int(d.get("round") or 1)
        findings = d.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        labels = [f"review:round-{rnd}"]
        if not blocking or rnd >= MAX_ROUNDS:
            labels.append("review:passed")
        if gl.configured and proj and iid:
            gl.note(proj, iid, _fmt_findings(findings, rnd))
            gl.add_labels(proj, iid, labels)
            posted += 1
        subject = e.get("subject") or f"mr:{proj}!{iid}"
        if blocking and rnd < MAX_ROUNDS:
            follow.append({"source": "ci", "type": "gitlab.review.changes_requested", "subject": subject,
                           "summary": f"{subject} 第 {rnd} 轮审查有 {len(blocking)} 个需修复的问题",
                           "data": {**d, "findings": blocking}})
        else:
            follow.append({"source": "ci", "type": "gitlab.review.passed", "subject": subject,
                           "summary": f"{subject} 审查通过" + ("（已满 3 轮，剩余意见留档）" if blocking else ""),
                           "data": {"project": proj, "iid": iid, "sha": d.get("sha")}})
    emit_result(f"写回 {posted} 个 MR 的审查结论" if posted else "审查结论未写回 GitLab（未配置 Token），已转后续事件",
                follow_up_events=follow)


def cmd_merge_gate() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    if not gl.configured:
        emit_result("未配置 GitLab Token，无法跑合并闸门")
        return
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        proj, iid = d["project"], d["iid"]
        mr = gl.mr(proj, iid)
        g = gate.check(gl.changes(proj, iid), mr.get("labels"), prd_direction="产品方向" in (mr.get("description") or ""))
        subject = f"mr:{proj}!{iid}"
        if g.blockers:
            gl.note(proj, iid, "### 合并闸门：不能合并\n" + "\n".join(f"- {b}" for b in g.blockers))
            emit_result(f"{subject} 有阻断问题：{'；'.join(g.blockers)[:300]}", follow_up_events=[
                {"source": "ci", "type": "gitlab.review.changes_requested", "subject": subject,
                 "summary": f"{subject} 合并闸门发现阻断问题", "data": {**d, "findings": [{"severity": "blocker", "problem": b} for b in g.blockers]}}])
            return
        demand = _demand(proj, iid)
        is_test = bool(demand and demand["is_test"]) or ingest.looks_like_test(mr.get("title"))
        if g.auto_merge_ok and c["auto_merge"] and not is_test and (mr.get("head_pipeline") or mr.get("pipeline") or {}).get("status") == "success":
            res = gl.merge(proj, iid, mr.get("sha")) or {}
            ingest.demand_merged(GitLab.normalize(proj), iid)
            emit_result(f"{subject} 已自动合并", follow_up_events=[
                {"source": "ci", "type": "gitlab.mr.merged", "subject": subject, "summary": f"{subject} 已合并", "data": d},
                *register_deploy_check(c, proj, iid, res.get("merge_commit_sha"), mr.get("web_url"), mr.get("title"))])
            return
        why = g.needs_human or (["shadow 模式：ci.auto_merge=false"] if not c["auto_merge"] else ["流水线未通过"])
        reasons = [f"MR：{mr.get('web_url')}", f"标题：{mr.get('title')}"]
        if demand:
            reasons.append(f"需求：{demand['title']}")
            if demand.get("quote"):
                reasons.append(f"需求原话：{demand['quote'][:160]}")
        if is_test:   # 测试需求：标题、建议、风险第一条都写明，免得当成正常需求顺手合了
            hold = "原话里写了不合并" if "不合并" in ((demand or {}).get("quote") or "") else "确认确实要合进主干再选合并"
            why = [f"这是测试需求（{hold}）", *why]
        verify = verify_spec(c, GitLab.normalize(proj)).get("url")
        why.append("合并后会自动核实线上版本" if verify else "这个仓库没配线上版本地址（ci.verify），合并后没法自动核实是否上线")
        card = {"kind": "release", "title": f"{'【测试】' if is_test else ''}合并 {proj}!{iid}？",
                "proposal": "先不合（这是测试需求）" if is_test else "合并（审查已通过）",
                "reasons": reasons, "risks": why,
                "options": [{"key": "merge", "label": "合并",
                             "emit": [{"type": "gitlab.mr.merge_approved", "subject": subject, "data": d}]},
                            {"key": "hold", "label": "先不合", "require_reason": True}],
                "on_expire": "hold", "links": [mr.get("web_url") or ""]}
        emit_result(f"{subject} 需要人批：{'；'.join(why)[:200]}", status="needs_decision", needs_decision=True, decision_card=card)
        return
    emit_result("没有待处理的 MR")


def cmd_merge() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    merged = []
    extra: list[dict] = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if gl.configured:
            res = gl.merge(d["project"], d["iid"], d.get("sha")) or {}
            p = GitLab.normalize(d["project"])
            merged.append((p, d["iid"]))
            ingest.demand_merged(p, d["iid"])
            extra += register_deploy_check(c, p, d["iid"], res.get("merge_commit_sha"), res.get("web_url") or d.get("web_url"), res.get("title"))
    pending = [f"{p}!{i}" for p, i in merged if verify_spec(c, p).get("url")]
    emit_result((f"已合并 {', '.join(f'{p}!{i}' for p, i in merged)}"
                 + ("；上线后每 30 分钟核实一次线上版本" if pending else "；这个仓库没配线上版本地址，没法自动核实上线，已请人确认" if merged else ""))
                if merged else "未合并（未配置 Token）",
                follow_up_events=[{"source": "ci", "type": "gitlab.mr.merged", "subject": f"mr:{p}!{i}",
                                   "summary": f"{p}!{i} 已合并（人批红后由 CI 中台合并）",
                                   "dedup_key": f"merged:{p}:{i}",  # 与 poll 补记的同一把键，避免重复留底
                                   "data": {"project": p, "iid": i, "via": "card"}} for p, i in merged] + extra)


def verify(url: str, sha: str) -> tuple[bool, str]:
    try:
        online = online_version(url)
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败：{e}"
    ok = bool(online) and (online.startswith(sha[:8]) or sha.startswith(online[:8]))
    return ok, f"线上 {online[:12] or '未知'}，期望 {sha[:12]}"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "verify" and len(argv) >= 3:
        ok, msg = verify(argv[1], argv[2])
        subject = argv[argv.index("--subject") + 1] if "--subject" in argv else f"deploy:{argv[2][:8]}"
        hub_client().emit({"source": "ci", "type": "deploy.verified" if ok else "deploy.mismatch", "subject": subject,
                           "summary": msg, "link": argv[1], "dedup_key": f"verify:{argv[1]}:{argv[2]}"})
        print(("通过：" if ok else "不一致：") + msg)
        return 0 if ok else 1
    fn = {"poll": cmd_poll, "review-post": cmd_review_post, "merge-gate": cmd_merge_gate, "merge": cmd_merge}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
