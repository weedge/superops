"""面板“设置 → 聊天渠道”：钉钉和飞书二选一，各自的群、审批人、登录状态；在面板上切换和修改。

改动存在库里的运行时覆盖（settings-overrides.chat），叠在 local.yaml 上，记进策略的变更历史；local.yaml 不动。
- 切换渠道：来源的监听线程只在启动时开（钉钉的事件长连接停不干净），所以切换后宿主优雅退出、由 launchd 拉起来，
  用新设置启动（Runtime.request_restart；不是 launchd 起的就提示人手动重启）。
- 改群：飞书每轮轮询都重新读群列表，不用重启；钉钉按群起监听，改了正在用的钉钉群要重启。
- 改审批人：每条消息都现读名单，立即生效。
"""
from __future__ import annotations

import shutil
import time

from ..core.errors import UserError
from .settings import CHAT_APPS

TITLES = {"dingtalk": "钉钉", "lark": "飞书"}
NAMES_KEY = "chat:names"                 # 群 ID → 群名（面板上按群名设置时记下的）
_auth_cache: dict = {"at": 0.0, "value": None}


def _lark_auth() -> dict:
    """飞书登录状态，缓存 60 秒（面板每几秒刷新一次，别每次都起一个 lark-cli）。"""
    if time.time() - _auth_cache["at"] > 60 or _auth_cache["value"] is None:
        from ..biz.notifier import lark
        _auth_cache.update(at=time.time(), value=lark.auth_status() if shutil.which("lark-cli") else
                           {"ok": False, "detail": "PATH 里没有 lark-cli"})
    return _auth_cache["value"]


def view(rt) -> dict:
    s = rt.settings.current()
    names = rt.store.get(NAMES_KEY) or {}
    states = {app: (rt.health.get(f"source:{app}") or {}) for app in CHAT_APPS}
    apps = {}
    for app in CHAT_APPS:
        src = s["sources"].get(app) or {"enabled": False, "config": {}}
        if app == "lark":
            auth = _lark_auth()
            login = {"ok": bool(auth.get("ok")), "detail": f"{auth.get('user') or ''} {auth.get('detail') or ''}".strip(),
                     "fix": "lark-cli auth login（本人身份，7 天续一次）"}
        else:
            ok = bool(shutil.which("dws"))
            login = {"ok": ok, "detail": "dws 已安装" if ok else "PATH 里没有 dws", "fix": "安装 dws 并 dws auth login"}
        apps[app] = {"title": TITLES[app], "enabled": bool(src["enabled"]),
                     "chats": [{"id": c, "name": names.get(c)} for c in (src["config"].get("chats") or [])],
                     "approvers": (s["policy"]["sources"].get(app) or {}).get("allowed") or [],
                     "login": login, "state": states[app].get("state") or ("off" if not src["enabled"] else "ready"),
                     "state_detail": states[app].get("detail") or ""}
    active = next((a for a in CHAT_APPS if apps[a]["enabled"]), None)
    return {"active": active, "apps": apps, "home": s["home"], "restartable": rt.restartable()}


def _resolve_group(app: str, value: str) -> tuple[str, str | None]:
    """群 ID 或群名 → (群 ID, 群名)。群名要唯一、完全一样，否则报错让人填 ID。"""
    value = value.strip()
    if app == "lark":
        if value.startswith("oc_"):
            return value, None
        from ..biz.notifier import lark
        data = lark.run(["im", "+chat-search", "--query", value, "--format", "json"])
        hits = [c for c in ((data.get("data") or {}).get("chats") or []) if c.get("name") == value]
    else:
        if value.startswith("cid"):
            return value, None
        from .autoreply import _dws
        data = _dws(["chat", "+chat-search", "--query", value])
        hits = [{"chat_id": c.get("openConversationId"), "name": c.get("name") or c.get("title")} for c in data.get("chats") or []
                if (c.get("name") or c.get("title")) == value]
    if len(hits) != 1 or not hits[0].get("chat_id"):
        raise UserError("invalidSettings", {"error": f"按群名「{value}」找到 {len(hits)} 个群，请直接填群 ID"})
    return str(hits[0]["chat_id"]), value


def act(rt, body: dict) -> dict:
    action, app = str(body.get("action") or ""), str(body.get("app") or "")
    if app not in CHAT_APPS:
        raise UserError("invalidSettings", {"error": f"聊天渠道只能是 {', '.join(CHAT_APPS)}"})
    current = view(rt)["apps"][app]
    restart = False
    if action == "switch":
        if not current["chats"]:
            raise UserError("invalidSettings", {"error": f"{TITLES[app]}还没配群，先设置群再切换"})
        if app == "lark" and not current["login"]["ok"]:
            raise UserError("invalidSettings", {"error": "飞书本人身份没登录：先在终端运行 lark-cli auth login"})
        if current["enabled"]:
            return {**view(rt), "restart": False, "note": f"现在用的就是{TITLES[app]}"}
        rt.settings.set_chat(app=app, origin="panel")
        restart = True
    elif action == "set_group":
        gid, name = _resolve_group(app, str(body.get("group") or ""))
        if name:
            rt.store.set(NAMES_KEY, {**(rt.store.get(NAMES_KEY) or {}), gid: name})
        rt.settings.set_chat(chats={app: [gid]}, origin="panel")
        restart = current["enabled"] and app == "dingtalk"
    elif action in ("add_approver", "remove_approver"):
        pid, name = str(body.get("id") or "").strip(), str(body.get("name") or "").strip()
        if not pid:
            raise UserError("invalidSettings", {"error": "要填审批人的 ID"})
        if app == "lark" and action == "add_approver" and not pid.startswith("ou_"):
            raise UserError("invalidSettings", {"error": "飞书审批人要填 open_id（ou_ 开头），不认昵称"})
        people = [p for p in current["approvers"] if p["id"] != pid]
        if action == "add_approver":
            people.append({"id": pid, **({"name": name} if name else {})})
        rt.settings.set_chat(approvers={app: people}, origin="panel")
    else:
        raise UserError("invalidSettings", {"error": f"不认识的操作：{action}"})
    restarted = rt.request_restart(f"面板改了聊天渠道（{action}）") if restart else False
    return {**view(rt), "restart": restarted, "restart_needed": restart and not restarted}
