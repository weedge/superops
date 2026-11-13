"""把卡片 JSON（附录 D）渲染成钉钉 / 飞书都能显示的 Markdown。"""
from __future__ import annotations

from ..timeutil import parse_iso

KIND_LABEL = {
    "publish_batch": "待发布",
    "topic_calendar": "选题",
    "demand_top": "需求",
    "demand": "需求",
    "prd": "PRD",
    "comment_reply": "评论回复",
    "compliance": "合规",
    "video_qa": "质检",
    "release": "生产发布",
    "incident": "线上报错",
    "lead": "线索",
    "cleanup": "大扫除",
    "alert": "告警",
}


def short_code(seq: int) -> str:
    return f"C{seq}"


def _hhmm(s: str | None) -> str:
    if not s:
        return ""
    try:
        d = parse_iso(s)
        return d.strftime("%m-%d %H:%M")
    except ValueError:
        return s


def render_card(card: dict, seq: int, expires_at: str | None, bot_name: str = "兔管家") -> tuple[str, str]:
    code = short_code(seq)
    label = KIND_LABEL.get(card.get("kind", ""), "待批")
    title = f"【{label}】{card.get('title', '待决定事项')}（{code}）"
    lines = [f"### {title}", ""]
    if card.get("proposal"):
        lines.append(f"**建议**：{card['proposal']}")
    for name, key in (("理由", "reasons"), ("风险", "risks")):
        vals = card.get(key) or []
        if vals:
            lines.append(f"**{name}**：")
            lines += [f"- {v}" for v in vals]
    items = card.get("items") or []
    if items:
        lines.append("**明细**：")
        for i, it in enumerate(items[:20], 1):
            parts = [str(it.get(k)) for k in ("platform", "time", "title") if it.get(k)]
            extra = f" · {it['note']}" if it.get("note") else ""
            lines.append(f"{i}. {' '.join(parts) or it}{extra}")
        if len(items) > 20:
            lines.append(f"…… 另有 {len(items) - 20} 条，见详情链接")
    opts = card.get("options") or []
    lines.append("")
    lines.append("**选项**：")
    for i, o in enumerate(opts, 1):
        need = "（需写原因）" if o.get("require_reason") else ""
        lines.append(f"{i}. {o.get('label', o.get('key'))}{need}")
    lines.append("")
    lines.append(f"回复「{code} 序号」即可，例如「{code} 1」；需要原因的选项在序号后接原因，如「{code} {len(opts) or 2} 先不做，原因……」。")
    partial = next((i for i, o in enumerate(opts, 1) if items and len(o.get("emit") or []) == len(items) > 1), None)
    if partial:
        lines.append(f"只批其中几条：「{code} {partial} 3,5」= 只做明细第 3、5 条。")
    on_expire = card.get("on_expire")
    exp_label = next((o.get("label") for o in opts if o.get("key") == on_expire), None) or "不做任何对外动作"
    if expires_at:
        lines.append(f"超时：{_hhmm(expires_at)} 前未处理则「{exp_label}」")
    for ln in card.get("links") or []:
        if str(ln).startswith("http"):
            lines.append(f"[详情]({ln})")
        elif (lp := local_file(ln)) is not None:
            lines.append(f"详情：见下一条文件「{lp.name}」")
        # 本机目录在手机上打不开：不显示
    lines.append(f"\n—— {bot_name}")
    return title, "\n".join(lines)


def render_card_batch(cards: list[tuple[dict, int, str | None]], bot_name: str = "兔管家") -> tuple[str, str]:
    title = f"【待批清单】{len(cards)} 件事等你拍板"
    lines = [f"### {title}", "", "昨天超过卡片上限或在静默时段产生的事项，合并在这里，每件仍可单独回复。", ""]
    for card, seq, exp in cards:
        code = short_code(seq)
        opts = " / ".join(f"{i}.{o.get('label', o.get('key'))}" for i, o in enumerate(card.get("options") or [], 1))
        lines.append(f"**{code}** {card.get('title', '')}")
        if card.get("proposal"):
            lines.append(f"- 建议：{card['proposal']}")
        lines.append(f"- 选项：{opts}；超时 {_hhmm(exp)}")
        lines.append("")
    lines.append("回复「编号 序号」，例如「C12 1」。")
    lines.append(f"\n—— {bot_name}")
    return title, "\n".join(lines)


def local_file(ln) -> "Path | None":
    """卡片里的本机文件链接（相对路径按数据目录解析）；可作为文件发进群的才返回。"""
    from pathlib import Path

    from .. import paths

    if not ln or str(ln).startswith(("http://", "https://")):
        return None
    p = Path(str(ln)).expanduser()
    if not p.is_absolute():
        p = paths.ops_data() / p
    try:
        return p if p.is_file() and p.stat().st_size <= 20 * 1024 * 1024 else None
    except OSError:
        return None
