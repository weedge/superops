"""发布执行器（05 §4）。

python -m superops.biz.executors.publish register     # content.pack.ready → 登记发布包
python -m superops.biz.executors.publish batch-card   # 17:00 → 今晚待发布 N 条，一张卡片
python -m superops.biz.executors.publish run          # content.publish.approved → 执行（按平台 off / dry_run / draft / live）
python -m superops.biz.executors.publish record       # content.published.manual → 人发完后补登记

一期所有平台按“半自动”执行：执行器把发布包整理好推给人，人在手机 / 后台发完后回复
「已发 <发布包ID> <链接>」（或 superops published），执行器补登记并排定数据采集。没有链接（视频号）或给的是短链时
只回「已发 <发布包ID>」也行：数据采集时按标题在创作者后台对上作品，再补上作品 ID。
自动发布：executors/publishers/（Playwright 驱动创作者后台），按 agents.yaml 的 publishers.<平台>.mode 启用（off / dry_run / draft / live）。
draft：自动填好后存进平台草稿箱、不点发布，人到草稿箱检查后自己发，发完照样回「已发 <发布包ID>」。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from .. import db
from .. import platforms as platforms_mod
from ..collectors.metrics import register_post
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from . import ActionLedger

PLATFORM_LABEL = platforms_mod.LABELS


def _pack_meta(pack_dir: str | None) -> dict:
    if not pack_dir:
        return {}
    p = Path(pack_dir).expanduser() / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def cmd_register() -> None:
    lease = load_task()
    ops = db.open_ops()
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        meta = _pack_meta(d.get("pack_dir"))
        platforms = d.get("platforms") or ([d["platform"]] if d.get("platform") else meta.get("platforms") or [])
        content = d.get("content_id") or (e.get("subject") or "").removeprefix("content:")
        for raw_pf in platforms:
            pf = platforms_mod.normalize(raw_pf)  # Agent 常写“抖音”：库里统一存 douyin
            pid = f"pk_{content}_{pf}"
            per = meta.get("per_platform") or {}
            pm = per.get(raw_pf) or per.get(pf) or {}
            ops.execute(
                "INSERT INTO publish_packs(id,content_id,platform,account,title,scheduled_at,pack_dir,publish_mode,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'ready', ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                " scheduled_at=excluded.scheduled_at, pack_dir=excluded.pack_dir, status=CASE WHEN status IN ('rejected','failed') THEN 'ready' ELSE status END,"
                " updated_at=excluded.updated_at",
                (pid, content, pf, pm.get("account") or d.get("account"), pm.get("title") or d.get("title") or meta.get("title"),
                 pm.get("scheduled_at") or d.get("scheduled_at"), d.get("pack_dir"), pm.get("publish_mode") or "semi_auto",
                 iso(), iso()))
            ids.append(pid)
    # 17:00 的批量卡片已经发过、这批里又有今天要发的（人上午批了“今天发”，出图和质检拖到了傍晚）：
    # 现在就补一张待发布卡片，不等明天的 17:00
    t = now()
    today = t.date().isoformat()
    due = [pid for pid in ids if (lambda r: r and str(r[0] or today)[:10] <= today)(
        ops.execute("SELECT scheduled_at FROM publish_packs WHERE id=?", (pid,)).fetchone())]
    if due and t.hour >= BATCH_CARD_HOUR:
        packs, card = build_batch_card(ops)
        if card:
            ops.commit()
            emit_result(f"登记 {len(ids)} 个发布包；今天的 17:00 卡片已经发过，现在补发待发布卡片（{len(packs)} 条）", packs=ids,
                        status="needs_decision", needs_decision=True, decision_card=card)
            return
    emit_result(f"登记 {len(ids)} 个发布包，等 17:00 统一发卡片" if ids else "事件里没有发布包信息", packs=ids)


BATCH_CARD_HOUR = 17        # schedules.yaml 的 cron.publish_batch_card


def cmd_batch_card() -> None:
    ops = db.open_ops()
    packs, card = build_batch_card(ops)
    if not card:
        emit_result("今晚没有待发布内容")
        return
    emit_result(f"今晚 {len(packs)} 条待发布，已生成批量卡片", status="needs_decision", needs_decision=True, decision_card=card)


def build_batch_card(ops) -> tuple[list[dict], dict | None]:
    """待批准（status=ready）的发布包做成一张批量卡片，并把它们标成 carded。没有待发布的返回 ([], None)。"""
    packs = db.rows(ops.execute("SELECT * FROM publish_packs WHERE status='ready' ORDER BY scheduled_at, platform"))
    if not packs:
        return [], None
    from ..config import load_agents_config

    cfg = load_agents_config()
    mode_label = {"live": "自动发布", "dry_run": "自动发布·试运行（仍需人工发）", "draft": "自动存草稿（你在草稿箱检查后发）",
                  "off": "半自动（人工发）"}
    items, modes, extra_risks = [], set(), []
    from . import publishers as pubs

    for p in packs:
        meta = _pack_meta(p["pack_dir"])
        mode = _publisher_mode(cfg, p["platform"], meta)
        modes.add(mode)
        for risk in meta.get("card_risks") or []:          # 发布包自己写明的风险（比如成片没走系统质检）
            if risk not in extra_risks:
                extra_risks.append(str(risk))
        timing, when = "", None
        if mode in ("dry_run", "live") and pubs.module_for(p["platform"]):
            try:   # 按现在的时间估一下：批得越晚，定时越可能顺延或变成立即发布
                when, note = pubs.publish_time(pubs.payload_for(p, meta), p["platform"])
                timing = " · " + note
            except pubs.PublishError as ex:
                timing = f" · ⚠️ {ex}"
        t = ""
        if mode in ("dry_run", "live") and pubs.module_for(p["platform"]) and when is None and not timing.startswith(" · ⚠️"):
            t = "立即"                                       # 一批准就发：时间栏不再显示用不上的计划时间
        elif p["scheduled_at"]:
            try:
                t = parse_iso(p["scheduled_at"]).strftime("%H:%M")
            except ValueError:
                t = p["scheduled_at"]
        if p["platform"] == "wechat_channels" and mode in ("dry_run", "live", "draft"):
            timing += " · " + ("声明原创" if pubs.payload_for(p, meta).get("declare_original") else "不声明原创")
        items.append({"platform": PLATFORM_LABEL.get(p["platform"], p["platform"]), "time": t,
                      "title": p["title"] or p["content_id"], "note": mode_label.get(mode, "半自动（人工发）") + timing,
                      "pack_id": p["id"]})
    expires = now().replace(hour=22, minute=0, second=0)
    if expires <= now():
        expires = now() + timedelta(hours=2)
    all_draft = modes == {"draft"}
    card = {
        "kind": "publish_batch",
        "title": f"今晚 {len(packs)} 条内容待发布",
        "proposal": "全部存草稿" if all_draft else "全部发布",
        "reasons": ["发布包目录里有标题、正文、话题、封面和渠道码，可在详情里逐条查看"],
        "risks": extra_risks + (["自动发布会直接发到正式账号；失败或登录失效时自动转人工，群里会提醒"] if "live" in modes else []) +
                 (["存草稿的平台不会直接发出：要你到草稿箱检查后自己点发布，发完回复「已发 发布包ID」以便回收数据"]
                  if "draft" in modes else []) +
                 (["人工发布的平台需要你在手机上发，发完回复「已发 发布包ID 链接」以便回收数据（视频号没有链接，只回「已发 发布包ID」）"]
                  if modes - {"live", "draft"} else []),
        "items": items,
        "options": [
            {"key": "approve_all", "label": "全部存草稿" if all_draft else "全部发布",
             "emit": [{"type": "content.publish.approved", "subject": f"pack:{p['id']}", "data": {"pack_id": p["id"]}} for p in packs]},
            {"key": "reject_all", "label": "全部不发", "require_reason": True},
        ],
        "expires_at": iso(expires),
        "on_expire": "reject_all",
        "links": sorted({p["pack_dir"] for p in packs if p["pack_dir"]})[:5],
    }
    ops.executemany("UPDATE publish_packs SET status='carded', updated_at=? WHERE id=?", [(iso(), p["id"]) for p in packs])
    return packs, card


def _per_platform(meta: dict, platform: str) -> dict:
    """meta.json 里平台键可能是中文（Agent 写的“抖音”），按归一后的代码匹配。"""
    for k, v in (meta.get("per_platform") or {}).items():
        if platforms_mod.normalize(k) == platform:
            return v or {}
    return {}


def _pack_files(p: dict, meta: dict) -> list[Path]:
    """要随发布消息一起发进群的文件：封面、视频（手机上保存后直接发）。"""
    if not p.get("pack_dir"):
        return []
    d = Path(p["pack_dir"])
    pm = _per_platform(meta, p["platform"])
    media = meta.get("media") or {}
    names = [pm.get("cover") or media.get("cover"), media.get("video"), *(media.get("images") or [])]
    out = []
    for n in names:
        if n and (d / n).is_file() and (d / n) not in out:
            out.append(d / n)
    return out


IMAGE_POST_PLATFORMS = {"xiaohongshu"}     # 自动发布器会发图文的平台；别的平台的图文直接走人工


def image_only(meta: dict, platform: str | None = None) -> bool:
    """只有图片、没有视频的发布包，而且这个平台的自动发布器还不会发图文（目前只有小红书会）：直接走人工。
    platform 不给时只看是不是图文。"""
    media = meta.get("media") or {}
    images = not media.get("video") and bool(media.get("images"))
    return images and (platform is None or platform not in IMAGE_POST_PLATFORMS)


def _manual_instructions(p: dict, meta: dict) -> str:
    pm = _per_platform(meta, p["platform"])
    lines = []
    if meta.get("is_test"):
        lines.append(f"> ⚠️ **测试内容，不要真的发布**：{meta.get('test_note') or ''}")
    lines.append(f"**{PLATFORM_LABEL.get(p['platform'], p['platform'])} · {p['title'] or p['content_id']}**（`{p['id']}`）")
    if image_only(meta):
        n = len((meta.get("media") or {}).get("images") or [])
        lines.append(f"- 图文（{n} 张图）：按顺序上传下面的图片、复制正文，到官方发布页发")
    for k, label in (("account", "账号"), ("scheduled_at", "时间"), ("title", "标题")):
        v = pm.get(k) or meta.get(k)
        if v:
            lines.append(f"- {label}：{v[11:16] if k == 'scheduled_at' and len(str(v)) > 16 else v}")
    body = pm.get("body") or meta.get("body")
    topics = pm.get("topics") or meta.get("topics")
    if body or topics:
        lines += ["", "**正文（整段复制）**", "", str(body or "") + ("\n" + " ".join(topics) if topics else "")]
    if pm.get("pinned_comment"):
        lines += ["", f"**置顶评论**：{pm['pinned_comment']}"]
    if pm.get("ai_label") or meta.get("ai_label"):
        lines.append("- 发布时勾选「AI 生成内容」声明")
    files = _pack_files(p, meta)
    if files:
        lines.append("- 素材：见下方文件 " + "、".join(f"「{f.name}」" for f in files))
    if p["platform"] == "wechat_channels":
        lines += ["", f"发完回复：`已发 {p['id']}`（视频号没有链接，数据采集时按标题自动对上作品）"]
    else:
        lines += ["", f"发完回复：`已发 {p['id']} <链接>`（没有链接只回 `已发 {p['id']}` 也行，按标题自动对上作品）"]
    return "\n".join(lines)


def _publisher_mode(cfg: dict, platform: str, meta: dict) -> str:
    """off / dry_run / draft / live；测试内容最多试运行。"""
    mode = str(((cfg.get("publishers") or {}).get(platform) or {}).get("mode") or "off")
    if mode in ("live", "draft") and meta.get("is_test"):
        return "dry_run"
    return mode if mode in ("off", "dry_run", "draft", "live") else "off"


def _draft_where(platform: str) -> str:
    from .publishers import DRAFTS
    spot = DRAFTS.get(platform)
    if not spot:
        return "平台草稿箱"
    return spot["where"] + (f"。在这台 Mac 上运行 `superops publish open {platform}` 直接打开" if spot["local"] else "")


def publish_one(ops, ledger, hub, cfg, pack_id: str, approved_at: str | None, *, headless: bool = True,
                retry: bool = False) -> tuple[str, object]:
    """发一个已批准的发布包。返回 ("done", 作品记录) / ("manual", (发布包, meta)) / ("dry_run", 说明) / ("skipped", 原因)。
    retry=True：动作已由 ledger.reopen 重新打开，不再走 begin。"""
    from . import publishers as pubs

    p = ops.execute("SELECT * FROM publish_packs WHERE id=?", (pack_id,)).fetchone()
    if not p:
        return "skipped", f"{pack_id}（不存在）"
    p = dict(p)
    if not retry:
        ok, why = ledger.begin(f"publish:{pack_id}", "publish", pack_id, approved_at)
        if not ok:
            return "skipped", f"{pack_id}（{why}）"
    meta = _pack_meta(p["pack_dir"])
    mode = _publisher_mode(cfg, p["platform"], meta)
    label = PLATFORM_LABEL.get(p["platform"], p["platform"])
    if mode in ("dry_run", "live", "draft") and pubs.module_for(p["platform"]) and not image_only(meta, p["platform"]):
        r = None
        doing = {"dry_run": "发布试运行", "draft": "存草稿"}.get(mode, "发布")
        try:
            r = pubs.run(p, meta, dry_run=(mode == "dry_run"), draft=(mode == "draft"), headless=headless)
        except pubs.NotLoggedIn as ex:
            hub.notify("alert", f"{label}登录失效，改为人工发布", f"{ex}\n\n发布包 `{pack_id}` 已转人工发布。",
                       priority=1, ref=f"login:{p['platform']}", dedup=False)
        except pubs.NeedsVerification as ex:
            hub.notify("alert", f"{label}要身份验证，自动发布停在了验证这一步",
                       f"`{pack_id}`：{ex}\n\n这条还没有发出去。要继续自动发：在本机运行 `superops publish retry {pack_id} --headed`，"
                       "会打开有界面的 Chrome 重新填好并点发布，弹出验证（短信验证码或扫码）时你在窗口里完成；也可以按下面的说明手动发。",
                       priority=1, ref=f"verify:{pack_id}:{iso()[:16]}", dedup=True)
            if getattr(ex, "screenshot", None):
                hub.notify("file", Path(ex.screenshot).name, ex.screenshot, priority=1, ref=f"verify-shot:{pack_id}:{Path(ex.screenshot).name}", dedup=True)
        except Exception as ex:  # noqa: BLE001
            hub.notify("message", f"{label}自动{doing}失败，改为人工发布",
                       f"`{pack_id}`：{pubs.friendly_error(ex)}" + ("\n\n出错时的页面截图见下一条。" if getattr(ex, "screenshot", None) else ""),
                       priority=1, ref=f"pubfail:{pack_id}:{iso()[:16]}", dedup=True)
            if getattr(ex, "screenshot", None):
                hub.notify("file", Path(ex.screenshot).name, ex.screenshot, priority=1,
                           ref=f"pubfail-shot:{pack_id}:{Path(ex.screenshot).name}", dedup=True)
        if r and r.dry_run:
            hub.notify("message", f"{label}自动发布试运行（没有发布）",
                       f"`{pack_id}` 已在{label}后台自动填好并停在发布前，截图见下方。\n\n" +
                       "\n".join(f"- {n}" for n in r.notes) + "\n\n试运行期间仍需人工发布（见下一条）。",
                       priority=1, ref=f"dryrun:{pack_id}", dedup=True)
            for sp in r.screenshots:
                hub.notify("file", Path(sp).name, sp, priority=1, ref=f"dryrun-shot:{pack_id}:{Path(sp).name}", dedup=True)
            ops.execute("UPDATE publish_packs SET status='approved', updated_at=? WHERE id=?", (iso(), pack_id))
            ledger.finish(f"publish:{pack_id}", "awaiting_manual")
            return "dry_run", (p, meta, f"{label}试运行：" + "；".join(r.notes))
        if r and r.ok and r.draft:
            ops.execute("UPDATE publish_packs SET status='drafted', updated_at=? WHERE id=?", (iso(), pack_id))
            ledger.finish(f"publish:{pack_id}", "awaiting_manual", {"draft": True, "notes": r.notes})
            notes = [n for n in r.notes if not n.startswith(("立即发布", "立即发表"))]
            reply = f"`已发 {pack_id}`" + ("" if p["platform"] == "wechat_channels" else f"（或 `已发 {pack_id} <链接>`）")
            hub.notify("message", f"📝 {label}已存草稿「{p['title'] or p['content_id']}」",
                       f"`{pack_id}` 已自动填好并存进草稿，**没有发布**。\n\n草稿在：{_draft_where(p['platform'])}。检查后点发布，"
                       f"发完回复 {reply}，以便登记和回收数据。\n\n" + "\n".join(f"- {n}" for n in notes),
                       priority=1, ref=f"drafted:{pack_id}", dedup=True)
            for sp in r.screenshots[-2:]:
                hub.notify("file", Path(sp).name, sp, priority=1, ref=f"draft-shot:{pack_id}:{Path(sp).name}", dedup=True)
            return "draft", (p, meta, f"{label}已存草稿")
        if r and r.ok:
            # 没截到作品 ID 也照样登记（ID 先空着）：数据采集时按标题在创作者后台对上作品，再补上 ID
            rec = {"platform": p["platform"], "platform_post_id": r.platform_post_id, "url": r.url,
                   "content_id": p["content_id"], "title": p["title"], "publish_mode": "auto",
                   "channel_code": _per_platform(meta, p["platform"]).get("channel_code") or meta.get("channel_code"),
                   "published_at": r.publish_at or iso(),
                   "post_id": f"{p['platform']}_{r.platform_post_id or pack_id}"[:80]}
            rec["post_id"] = register_post(ops, {k: v for k, v in rec.items() if v is not None})
            ops.execute("UPDATE publish_packs SET status='published', updated_at=? WHERE id=?", (iso(), pack_id))
            ledger.finish(f"publish:{pack_id}", "done", {"url": r.url, "notes": r.notes, "post_id": rec["post_id"]})
            where = ("链接：" + r.url if r.url else
                     f"作品 ID：{r.platform_post_id}" if r.platform_post_id else
                     "没截到作品 ID，数据采集时会按标题在后台自动对上，不用补登记")
            verb = f"已定时发布（{r.publish_at[5:16].replace('T', ' ')} 由平台发出）" if r.publish_at else "已自动发布"
            hub.notify("message", f"✅ {label}{verb}「{p['title'] or p['content_id']}」",
                       where + "\n\n" + "\n".join(f"- {n}" for n in r.notes), priority=1, ref=f"published:{pack_id}", dedup=True)
            return "done", rec
    ops.execute("UPDATE publish_packs SET status='approved', updated_at=? WHERE id=?", (iso(), pack_id))
    ledger.finish(f"publish:{pack_id}", "awaiting_manual")
    return "manual", (p, meta)


def _announce_manual(hub, manual: list) -> None:
    """每个发布包一条消息，紧跟它的封面 / 视频文件（宿主按入队顺序发）。"""
    for i, (p, meta) in enumerate(manual, 1):
        hub.notify("message", f"请手动发布（{i}/{len(manual)}）{p['title'] or p['content_id']}", _manual_instructions(p, meta),
                   priority=1, ref=f"manual-publish:{p['id']}", dedup=True)
        for f in _pack_files(p, meta):
            hub.notify("file", f.name, str(f), priority=1, ref=f"manual-publish-file:{p['id']}:{f.name}", dedup=True)


def cmd_run() -> None:
    from ..config import load_agents_config

    lease = load_task()
    ops = db.open_ops()
    ledger = ActionLedger(ops)
    hub = hub_client()
    cfg = load_agents_config()
    done, manual, skipped, notes, drafted = [], [], [], [], []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        pack_id = d.get("pack_id") or (e.get("subject") or "").removeprefix("pack:")
        kind, value = publish_one(ops, ledger, hub, cfg, pack_id, e.get("received_at") or e.get("occurred_at"))
        if kind == "done":
            done.append(value)
        elif kind == "skipped":
            skipped.append(value)
        elif kind == "draft":
            drafted.append(value[2])
        elif kind == "dry_run":
            p, meta, note = value
            notes.append(note)
            manual.append((p, meta))
        else:
            manual.append(value)
    _announce_manual(hub, manual)
    follow = [{"type": "social.post.published", "subject": f"post:{r['post_id']}", "summary": f"{r['platform']} 已发布「{r['title']}」",
               "data": r} for r in done]
    emit_result(f"自动发布 {len(done)} 条，存草稿 {len(drafted)} 条，推送人工发布 {len(manual)} 条（含素材文件），跳过 {len(skipped)} 条"
                + ("；" + "；".join(notes) if notes else ""), follow_up_events=follow, skipped=skipped)


def retry(pack_id: str, *, headed: bool = False) -> dict:
    """已批准、自动发布失败转了人工的发布包，修好后用原来的批准再发一次（批准须在 24 小时内）。
    headed=True 打开有界面的 Chrome：平台要验证（短信验证码、扫码）时人在窗口里完成，程序等验证完再继续。"""
    from ..config import load_agents_config

    ops = db.open_ops()
    ledger = ActionLedger(ops)
    approved_at, why = ledger.reopen(f"publish:{pack_id}")
    if not approved_at:
        return {"ok": False, "error": why}
    hub = hub_client()
    kind, value = publish_one(ops, ledger, hub, load_agents_config(), pack_id, approved_at, headless=not headed, retry=True)
    if kind == "done":
        hub.emit({"source": "publish", "type": "social.post.published", "subject": f"post:{value['post_id']}",
                  "summary": f"{value['platform']} 已发布「{value['title']}」", "data": value,
                  "dedup_key": f"published:{value['post_id']}"})
        return {"ok": True, "post": value}
    if kind == "draft":
        return {"ok": True, "draft": True, "note": value[2]}
    if kind == "manual":
        return {"ok": False, "error": "又没发成，已转人工（群里有原因和截图）"}
    return {"ok": False, "error": str(value)}


def record_manual(pack_id: str, url: str | None = None, platform_post_id: str | None = None) -> dict:
    """人发完后补登记。链接里取不到作品 ID（短链、视频号没有链接）时 ID 先空着，数据采集时按标题对上。"""
    from ..collectors.creator import id_from_url

    ops = db.open_ops()
    p = ops.execute("SELECT * FROM publish_packs WHERE id=?", (pack_id,)).fetchone()
    if not p:
        raise SystemExit(f"找不到发布包 {pack_id}")
    p = dict(p)
    meta = _pack_meta(p["pack_dir"])
    ppid = platform_post_id or id_from_url(p["platform"], url)
    rec = {"platform": p["platform"], "platform_post_id": ppid, "url": url, "content_id": p["content_id"],
           "title": p["title"], "publish_mode": "semi_auto", "channel_code": _per_platform(meta, p["platform"]).get("channel_code") or meta.get("channel_code"),
           "post_id": f"{p['platform']}_{ppid or pack_id}"[:80], "published_at": iso()}
    ops.execute("UPDATE publish_packs SET status='published', updated_at=? WHERE id=?", (iso(), pack_id))
    ActionLedger(ops).finish(f"publish:{pack_id}", "done", rec)
    return rec


def cmd_record() -> None:
    lease = load_task()
    recs = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if d.get("pack_id"):
            recs.append(record_manual(d["pack_id"], d.get("url"), d.get("platform_post_id")))
    follow = [{"type": "social.post.published", "subject": f"post:{r['post_id']}", "summary": f"{r['platform']} 已发布「{r['title']}」",
               "data": r} for r in recs]
    emit_result(f"补登记 {len(recs)} 条人工发布", follow_up_events=follow)


if __name__ == "__main__":
    cmds = {"register": cmd_register, "batch-card": cmd_batch_card, "run": cmd_run, "record": cmd_record}
    fn = cmds.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not fn:
        print(__doc__)
        sys.exit(2)
    fn()
