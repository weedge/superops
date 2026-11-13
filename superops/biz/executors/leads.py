"""线索卡片与跟进登记（获客流水线：监听 → 线索入库 → 卡片 → 人批 → 待跟进）。

python -m superops.biz.executors.leads card   监听跑完后：把还没处理的新线索（stage=发现）做成一张卡片
python -m superops.biz.executors.leads run    人批红后：把线索标成 待跟进 / 不跟进
python -m superops.biz.executors.leads comment  人批了“跟进并发评论”：用品牌号在线索的作品下发卡片上写的那条评论

卡片三个选项：跟进并发评论（发卡片上写明的原文，只在人批准后发，可只批几条）/ 只记待跟进（人自己去互动）/ 都不跟进。
系统不私信、不加微信；评论只发卡片上人看过的原文，每天最多 DAILY_COMMENTS 条，两条之间隔一会儿。
线索阶段：发现 → 已发卡 → 已评论 / 待跟进 / 不跟进。
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

from .. import db, platforms
from ..scriptkit import emit_result, load_task
from ..timeutil import iso, now

MAX_PER_CARD = 10  # 当日建议互动的线索不超过 10 条（ops-social-listening §2.6）
DAILY_COMMENTS = 10   # 品牌号一天最多自动发这么多条评论（多了容易被平台当成营销号限流）
GAP_S = (25, 50)      # 两条评论之间随机隔这么多秒
TYPE_LABEL = {"team_lead": "团队长", "mcn": "MCN", "creator": "创作者", "other": "其他"}


def _fans(n) -> str:
    if not n:
        return "粉丝数未知"
    return f"{n / 10000:.1f} 万粉" if n >= 10000 else f"{n} 粉"


def pending(ops, limit: int = MAX_PER_CARD) -> list[dict]:
    """还没发过卡片的新线索：团队长 / MCN 优先，再按分数。"""
    return db.rows(ops.execute(
        "SELECT * FROM leads WHERE stage='发现' ORDER BY CASE WHEN lead_type IN ('team_lead','mcn') THEN 0 ELSE 1 END,"
        " score DESC, updated_at DESC LIMIT ?", (limit,)))


def can_comment(ld: dict) -> str | None:
    """这条线索能不能自动发评论；不能就返回原因（卡片上写明，批了也只记待跟进）。"""
    from .comment import SUPPORTED, check_text

    if ld.get("platform") not in SUPPORTED:
        return f"{platforms.label(ld.get('platform'))}还不能自动发评论，批了记待跟进、人去发"
    if not ld.get("target_url"):
        return "没有具体作品链接，批了记待跟进、人去发"
    bad = check_text(ld.get("comment_text") or "")
    return f"{bad}，批了记待跟进、人去发" if bad else None


def build_card(leads: list[dict], detail: Path | None = None) -> dict:
    n_pri = sum(1 for ld in leads if ld.get("lead_type") in ("team_lead", "mcn"))
    items, comment, approve = [], [], []
    batch = now().strftime("%Y%m%d-%H%M")
    n_auto = 0
    for ld in leads:
        head = f"{ld.get('nickname') or '(未知昵称)'}（{TYPE_LABEL.get(ld.get('lead_type') or '', '线索')} · {ld.get('industry') or '行业未知'} · {_fans(ld.get('followers'))}）"
        why_not = can_comment(ld)
        if ld.get("comment_text"):   # 卡片上写的就是要发的原文，人批的就是这段字
            note = f"评论：「{ld['comment_text']}」 作品：{ld.get('target_url') or '（没有作品链接）'}" + (f"（{why_not}）" if why_not else "")
        else:
            note = f"{(ld.get('outreach_draft') or ld.get('why') or '')[:90]} {ld.get('profile_url') or ''}".strip() + "（没有评论原文，批了记待跟进）"
        n_auto += 0 if why_not else 1
        items.append({"platform": platforms.label(ld.get("platform")), "title": head, "note": note})
        comment.append({"type": "lead.comment.approved", "subject": f"lead-comments:{batch}",   # 一张卡片批的评论进同一张工单，按顺序发
                        "summary": f"跟进线索 {ld.get('nickname')} 并发评论", "data": {"lead_id": ld["id"]}})
        approve.append({"type": "lead.followup.approved", "subject": f"lead:{ld['id']}",
                        "summary": f"跟进线索 {ld.get('nickname')}", "data": {"lead_id": ld["id"]}})
    ids = [ld["id"] for ld in leads]
    return {
        "kind": "lead_batch",
        "title": f"今日线索 {len(leads)} 条" + (f"（团队长 / MCN {n_pri} 条）" if n_pri else "") + "：要跟进哪些？",
        "proposal": (f"跟进并发评论：用品牌号在作品下发明细里写的评论原文（{n_auto} 条能自动发）" if n_auto
                     else "跟进：记为“待跟进”，由人用品牌号去互动"),
        "reasons": ["来自今天的社媒监听：命中痛点词 / 方案词，且作者符合目标画像（详情见附件）。"],
        "risks": ["评论会以品牌号的名义公开发出，原文就是明细里「」中的字，请逐条看一遍；不带链接、不引导私信。",
                  f"不能自动发的（平台不支持、没有作品链接）批了只记“待跟进”；品牌号每天最多自动发 {DAILY_COMMENTS} 条评论。"],
        "items": items,
        "options": [
            {"key": "comment", "label": "跟进并发评论（可只选几条）", "emit": comment},
            {"key": "approve", "label": "只记待跟进（我自己去发）", "emit": approve},
            {"key": "skip", "label": "这批都不跟进", "emit": [{
                "type": "lead.followup.dismissed", "subject": f"lead-batch:{now().strftime('%Y%m%d-%H%M')}",
                "summary": f"{len(ids)} 条线索不跟进", "data": {"lead_ids": ids}}]},
        ],
        "on_expire": "skip",
        "links": [str(detail)] if detail else [],
    }


def write_detail(leads: list[dict], path: Path) -> Path:
    lines = [f"# 今日线索（{now().strftime('%Y-%m-%d')}）", ""]
    for i, ld in enumerate(leads, 1):
        lines += [f"## {i}. {ld.get('nickname')} · {platforms.label(ld.get('platform'))} · {_fans(ld.get('followers'))}",
                  f"- 主页：{ld.get('profile_url')}",
                  f"- 类型：{TYPE_LABEL.get(ld.get('lead_type') or '', ld.get('lead_type') or '-')} · 行业：{ld.get('industry') or '-'} · 评分：{ld.get('score') or '-'}",
                  f"- 为什么需要 Popto：{ld.get('why') or '-'}",
                  f"- 建议怎么互动：{ld.get('outreach_draft') or '-'}",
                  f"- 评论原文：{ld.get('comment_text') or '-'}",
                  f"- 评论哪条作品：{ld.get('target_url') or '-'}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def enrich_followers(ops, leads: list[dict]) -> int:
    """小红书搜索接口不带粉丝数：出卡片前给缺粉丝数的小红书线索各查一次（最多 MAX_PER_CARD 次请求）。查不到就留空。"""
    from ..collectors.tikhub import TikHub

    todo = [ld for ld in leads if ld.get("platform") == "xiaohongshu" and not ld.get("followers")
            and "/user/profile/" in (ld.get("profile_url") or "")]
    if not todo:
        return 0
    th = TikHub(proxy=None)
    if not th.configured:
        return 0
    n = 0
    for ld in todo:
        uid = ld["profile_url"].split("/user/profile/")[1].split("?")[0].strip("/")
        try:
            fans = int((((th.get("/api/v1/xiaohongshu/app_v2/get_user_info", user_id=uid).get("data") or {}).get("data") or {}).get("fans") or 0))
        except Exception:  # noqa: BLE001
            continue
        if fans:
            ld["followers"] = fans
            ops.execute("UPDATE leads SET followers=? WHERE id=?", (fans, ld["id"]))
            n += 1
    return n


def below_min(leads: list[dict], min_followers: int) -> list[dict]:
    """粉丝数已知且低于画像门槛的（团队长 / MCN 除外：他们的价值在带的那批人，不在自己账号的粉丝数）。"""
    return [ld for ld in leads if ld.get("followers") is not None and ld["followers"] < min_followers
            and ld.get("lead_type") not in ("team_lead", "mcn")]


def cmd_card() -> None:
    from ..collectors.listening import settings
    from ..config import load_agents_config

    ops = db.open_ops()
    leads = pending(ops, limit=MAX_PER_CARD * 3)
    enrich_followers(ops, leads)
    small = below_min(leads, int(settings(load_agents_config())["min_followers"]))
    for ld in small:   # 不上卡片，记下来（明细文件里列出），以后粉丝涨了再被监听到会重新判断
        ops.execute("UPDATE leads SET stage='粉丝不足', updated_at=? WHERE id=? AND stage='发现'", (iso(), ld["id"]))
    ops.commit()
    leads = [ld for ld in leads if ld not in small][:MAX_PER_CARD]
    if not leads:
        emit_result("没有待处理的新线索，不发卡片" + (f"（{len(small)} 条粉丝数低于门槛，没上卡片）" if small else ""))
        return
    run_dir = os.environ.get("OPS_RUN_DIR")
    detail = write_detail(leads, Path(run_dir) / "今日线索.md") if run_dir else None
    card = build_card(leads, detail)
    for ld in leads:
        ops.execute("UPDATE leads SET stage='已发卡', updated_at=? WHERE id=? AND stage='发现'", (iso(), ld["id"]))
    ops.commit()
    emit_result(f"今日线索卡片：{len(leads)} 条，等人选要跟进的" + (f"（另有 {len(small)} 条粉丝数低于门槛，没上卡片）" if small else ""),
                status="needs_decision", needs_decision=True,
                decision_card=card, artifacts=[str(detail)] if detail else [])


def cmd_run() -> None:
    lease = load_task()
    ops = db.open_ops()
    follow = skip = 0
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        ids = [d["lead_id"]] if d.get("lead_id") else list(d.get("lead_ids") or [])
        stage = "待跟进" if e.get("type") == "lead.followup.approved" else "不跟进"
        for lid in ids:
            n = ops.execute("UPDATE leads SET stage=?, updated_at=? WHERE id=? AND stage IN ('发现','已发卡')",
                            (stage, iso(), lid)).rowcount
            follow, skip = follow + (n if stage == "待跟进" else 0), skip + (n if stage == "不跟进" else 0)
    ops.commit()
    emit_result(f"线索登记：待跟进 {follow} 条，不跟进 {skip} 条")


def wait_gap(ops) -> float:
    """离上一条自动评论（不管是哪张工单发的）不到 GAP_S 就等到够。返回等了几秒。"""
    from ..timeutil import parse_iso
    last = ops.execute("SELECT MAX(commented_at) FROM leads WHERE comment_status IN ('sent','unconfirmed')").fetchone()[0]
    if not last:
        return 0.0
    need = random.uniform(*GAP_S) - (now() - parse_iso(last)).total_seconds()
    if need > 0:
        time.sleep(need)
    return max(need, 0.0)


@__import__("contextlib").contextmanager
def comment_lock():
    """跨进程排队发评论：同时开的几张评论工单一条一条来，间隔从上一条算（2026-11-12 七张工单各发一条，只隔了 11–14 秒）。"""
    import fcntl

    from .. import paths
    lock = paths.ops_data() / "browser-profiles" / ".comment.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def comments_today(ops) -> int:
    return ops.execute("SELECT COUNT(*) FROM leads WHERE comment_status='sent' AND commented_at>=?",
                       (now().strftime("%Y-%m-%dT00:00:00"),)).fetchone()[0]


def cmd_comment() -> None:
    """人批了“跟进并发评论”：逐条发。发不了的（平台不支持、没原文、超额、验证码、出错）记“待跟进”，写明原因，人去发。
    已经发过的不重发；点了发送但没确认到的也不重试（宁可漏，不可重复发）。"""
    from . import comment as cm

    lease = load_task()
    ops = db.open_ops()
    sent, manual = [], []
    failed = 0
    for e in lease.get("events") or []:
        lid = (e.get("data") or {}).get("lead_id")
        row = ops.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone() if lid else None
        if not row:
            continue
        ld = dict(row)
        name = f"{platforms.label(ld.get('platform'))} {ld.get('nickname') or lid}"
        if ld.get("comment_status") in ("sent", "unconfirmed"):
            continue
        why_not = can_comment(ld)
        if not why_not and comments_today(ops) >= DAILY_COMMENTS:
            why_not = f"今天已经自动发了 {DAILY_COMMENTS} 条评论，这条请人去发"
        if why_not:   # 卡片上已经写明发不了、批了记待跟进：这不算失败
            res = cm.CommentResult(False, supported=False, note=why_not)
        else:
            with comment_lock():
                wait_gap(ops)
                res = cm.post(ld["platform"], ld["target_url"], ld["comment_text"], lead_id=lid)
                failed += 0 if res.ok else 1
                unconfirmed = not res.ok and res.note.startswith("点了发送")
                ops.execute("UPDATE leads SET stage=?, comment_status=?, commented_at=?, comment_note=?, updated_at=? WHERE id=?",
                            ("已评论" if res.ok else "待跟进", "sent" if res.ok else "unconfirmed" if unconfirmed else "manual",
                             iso() if res.ok or unconfirmed else None, res.note[:300], iso(), lid))
                ops.commit()
        if why_not:
            ops.execute("UPDATE leads SET stage='待跟进', comment_status='manual', comment_note=?, updated_at=? WHERE id=?",
                        (res.note[:300], iso(), lid))
            ops.commit()
        (sent if res.ok else manual).append(f"{name}：{res.note}" + (f"（截图 {res.shots[-1]}）" if res.shots and not res.ok else ""))
    lines = [f"- ✅ {x}" for x in sent] + [f"- 👤 {x}" for x in manual]
    summary = f"线索评论：发出 {len(sent)} 条" + (f"，{len(manual)} 条要人去发 / 去看" if manual else "")
    emit_result(summary, status="failed" if failed and not sent else "done", sent=sent, manual=manual,
                message_markdown=summary + ("\n\n" + "\n".join(lines) if lines else ""))


if __name__ == "__main__":
    if sys.argv[1:2] == ["card"]:
        cmd_card()
    elif sys.argv[1:2] == ["run"]:
        cmd_run()
    elif sys.argv[1:2] == ["comment"]:
        cmd_comment()
    else:
        print(__doc__)
        sys.exit(2)
