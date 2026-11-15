"""聊天软件（钉钉 / 飞书）准备好了没有：命令行装没装、登录没登录、群和 home 和审批人配没配对。

superops doctor 逐项列出；启动检查（preflight）和面板的“聊天渠道”用这里的登录状态。
两个命令行都查，但只有开着的那个缺了才算问题；没开的只说一声装没装。
"""
from __future__ import annotations

import json
import shutil
import subprocess

from .settings import CHAT_APPS

TITLES = {"dingtalk": "钉钉", "lark": "飞书"}
CLI = {"dingtalk": "dws", "lark": "lark-cli"}
LOGIN_FIX = {"dingtalk": "dws auth login --recommend", "lark": "lark-cli auth login --domain im（本人身份，7 天续一次）"}
# 本人身份必须有的权限（scope → 用来做什么）。只列实际踩过的：少了 im:message.send_as_user 时确认、问题卡片、结果都发不出去，
# 发件箱一直重试（2026-11-15）。auth status 不给权限列表时（旧版 lark-cli）不查。
LARK_SCOPES = {"im:message.send_as_user": "以你的身份往群里发消息（确认、问题卡片、结果）"}
INSTALL_FIX = {"dingtalk": "安装钉钉命令行 dws，再 dws auth login --recommend",
               "lark": "npm install -g @larksuite/cli，再 lark-cli config init --new 和 lark-cli auth login --domain im"}


def _walk(data, keys: tuple[str, ...]) -> list:
    """JSON 里任意层级、这些键的值（dws 的输出套几层不固定）。"""
    found = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and not isinstance(v, (dict, list)):
                found.append(v)
            found += _walk(v, keys)
    elif isinstance(data, list):
        for v in data:
            found += _walk(v, keys)
    return found


def dws_status() -> dict:
    """钉钉本人登录状态：{installed, ok, user, detail}。认 `dws auth status --format json` 的 authenticated / token_valid。"""
    if not shutil.which(CLI["dingtalk"]):
        return {"installed": False, "ok": False, "detail": "PATH 里没有 dws"}
    try:
        p = subprocess.run([CLI["dingtalk"], "auth", "status", "--format", "json"], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as error:
        return {"installed": True, "ok": False, "detail": f"dws auth status 没查成：{str(error)[:120]}"}
    flags = _walk(data, ("authenticated", "token_valid", "tokenValid"))
    ok = p.returncode == 0 and bool(flags) and all(f is True or str(f).lower() == "true" for f in flags)
    user = next((str(v) for v in _walk(data, ("name", "nick", "userName", "user_name")) if v), None)
    return {"installed": True, "ok": ok, "user": user, "detail": "已登录" if ok else "没登录或登录已过期"}


def lark_status() -> dict:
    """飞书本人登录状态：{installed, ok, user, open_id, detail}。"""
    if not shutil.which(CLI["lark"]):
        return {"installed": False, "ok": False, "detail": "PATH 里没有 lark-cli"}
    from ..infra.notifier import lark
    return {"installed": True, **lark.auth_status()}


def login_status(app: str) -> dict:
    return lark_status() if app == "lark" else dws_status()


def _row(title: str, ok: bool, detail: str, fix: str | None = None) -> dict:
    return {"title": title, "ok": ok, "detail": detail, **({"fix": fix} if fix and not ok else {})}


def _groups(app: str, values: list[str], status: dict, find_chat) -> tuple[bool, list[str]]:
    """群能不能用：飞书的群名要登录后才能换成群 ID。返回 (都能用, 每个群一句)。"""
    ok, notes = True, []
    for value in values:
        if app != "lark" or value.startswith("oc_"):
            notes.append(value)
            continue
        if not status.get("ok"):
            ok = False
            notes.append(f"「{value}」（群名，要登录飞书后才能换成群 ID）")
            continue
        try:
            notes.append(f"「{value}」→ {find_chat(value)}")
        except Exception as error:  # noqa: BLE001  LarkError：没找到、重名
            ok = False
            notes.append(f"「{value}」：{error}")
    return ok, notes


def _group_fix(app: str, st: dict) -> str:
    if app == "lark" and not st.get("ok"):
        return LOGIN_FIX[app]                     # 没登录：登录后群名自己会换成群 ID
    return "改成群 ID（oc_ 开头）：lark-cli im +chat-search --query <群名> --as user"


def check(settings: dict, *, status=None, find_chat=None) -> list[dict]:
    """逐项检查，返回 [{title, ok, detail, fix?}]。status(app) / find_chat(name) 测试时可替换。"""
    status = status or login_status
    if find_chat is None:
        from ..infra.notifier.lark import find_chat
    sources = settings["sources"]
    enabled = [app for app in CHAT_APPS if (sources.get(app) or {}).get("enabled")]
    home = settings.get("home") or {}
    rows = [_row("聊天渠道", True, f"用{TITLES[enabled[0]]}（{'、'.join(TITLES[a] for a in CHAT_APPS if a not in enabled)}关着）" if enabled
                 else f"钉钉和飞书都没开：问题和结果落在本机收件箱（home 是 {home.get('source') or '未配置'}）")]
    for app in CHAT_APPS:
        title, cli = TITLES[app], CLI[app]
        path = shutil.which(cli)
        if app not in enabled:
            rows.append(_row(f"{title}命令行 {cli}", True, f"{path or '未安装'}（没开{title}，用不到）"))
            continue
        rows.append(_row(f"{title}命令行 {cli}", bool(path), path or "PATH 里没有", INSTALL_FIX[app]))
        st = status(app) if path else {"ok": False, "detail": "命令行没装"}
        who = " ".join(x for x in (st.get("user"), st.get("open_id")) if x)
        rows.append(_row(f"{title}登录（本人身份）", bool(st.get("ok")), f"{st.get('detail') or ''} {who}".strip(), LOGIN_FIX[app]))
        if app == "lark" and st.get("ok") and st.get("scopes") is not None:
            missing = [s for s in LARK_SCOPES if s not in st["scopes"]]
            rows.append(_row("飞书权限", not missing,
                             "；".join(f"缺 {s}：{LARK_SCOPES[s]}做不了" for s in missing) or f"{'、'.join(LARK_SCOPES)} 都有",
                             f'lark-cli auth login --scope "{" ".join(missing)}"（在终端里运行，按提示在浏览器里授权）'))

        config = (sources.get(app) or {}).get("config") or {}
        chats = [str(c) for c in config.get("chats") or []]
        rule_chats = {k: str(v) for k, v in (config.get("rule_chats") or {}).items() if v}
        if not chats:
            rows.append(_row(f"{title}群", False, f"没配群（sources.{app}.chats）", f"local.yaml 的 sources.{app}.chats 填群 ID"
                             + ("或完全一样的群名" if app == "lark" else "")))
        else:
            ok, notes = _groups(app, chats + [c for c in rule_chats.values() if c not in chats], st, find_chat)
            rows.append(_row(f"{title}群", ok, "、".join(notes), _group_fix(app, st)))

        target = (home.get("address") or {}).get("chat")
        if home.get("source") != app:
            rows.append(_row("home 渠道", False, f"指向 {home.get('source') or '未配置'}，不是开着的{title}：主动工单的结果发不出去",
                             f'local.yaml 写 home: {{source: {app}, thread: home, address: {{chat: "<群>"}}}}'))
        else:
            ok, notes = _groups(app, [target], st, find_chat) if target else (True, [f"没写群，用 sources.{app}.chats 的第一个"])
            rows.append(_row("home 渠道", ok, f"{title} {'、'.join(notes)}", _group_fix(app, st)))

        allowed = (settings["policy"]["sources"].get(app) or {}).get("allowed") or []
        ids = [str(p.get("id")) for p in allowed if p.get("id")]
        hint = (f'policy.sources.lark.allowed: [{{id: "{st["open_id"]}", name: "本人"}}]' if app == "lark" and st.get("open_id")
                else "policy.sources.lark.allowed 填 open_id（lark-cli auth status 的 userOpenId，ou_ 开头）" if app == "lark"
                else "policy.sources.dingtalk.allowed 填 openDingTalkId 和 userId（两种都填，不认昵称）")
        if not ids:
            rows.append(_row("审批人", False, "名单是空的：没有人能在群里提请求或批红", hint))
            continue
        bad = [i for i in ids if app == "lark" and not i.startswith("ou_")]
        note = f"{len(ids)} 人：" + "、".join(f"{p.get('name') or ''} {p['id']}".strip() for p in allowed if p.get("id"))
        if bad:
            rows.append(_row("审批人", False, note + f"；{'、'.join(bad)} 不是 open_id（ou_ 开头），不会生效", hint))
        elif app == "lark" and st.get("open_id") and st["open_id"] not in ids:
            rows.append(_row("审批人", True, note + "；你本人不在名单里：你在群里回 T12 1 不会生效"))
        else:
            rows.append(_row("审批人", True, note))
    return rows
