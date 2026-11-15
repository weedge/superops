"""飞书（lark-cli）的最小封装：以“我本人”（--as user）收发消息。

为什么是轮询：飞书的消息事件（im.message.receive_v1 等）只能以机器人身份订阅，没有“别人发给我本人”的事件，
所以运营群用 `im +chat-messages-list` 按时间拉新消息，单聊和 @我 用 `im +messages-search --chat-type p2p / --is-at-me` 拉。
发：`im +messages-send --markdown`（转成飞书 post），回复：`im +messages-reply`。

lark-cli 的 JSON 字段名文档里没写全，normalize 对几种常见写法都认；只认 ID（open_id），不认昵称。
用户登录 7 天要续一次：`lark-cli auth login`（启动检查会提示）。
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime

from ..timeutil import TZ, iso

log = logging.getLogger("superops.lark")
BIN = "lark-cli"


class LarkError(RuntimeError):
    pass


def run(args: list[str], *, timeout: float = 60) -> dict | list:
    """跑一条 lark-cli（本人身份），返回 JSON。失败抛 LarkError，带 stderr 的最后一段。"""
    cmd = [BIN, *args, "--as", "user"]
    if "--format" not in args and len(args) > 1 and args[1] in ("+chat-messages-list", "+messages-search"):
        cmd += ["--format", "json"]          # 只有读消息的命令认 --format；发 / 回复不认
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise LarkError("本机没有 lark-cli") from e
    except subprocess.TimeoutExpired as e:
        raise LarkError(f"lark-cli 超时：{' '.join(args[:2])}") from e
    if p.returncode != 0:
        raise LarkError((p.stderr or p.stdout or f"exit {p.returncode}").strip()[-400:])
    try:
        return json.loads(p.stdout or "null") or {}
    except json.JSONDecodeError as e:
        raise LarkError(f"lark-cli 输出不是 JSON：{(p.stdout or '')[:200]}") from e


def find_chat(name: str) -> str:
    """群名 → 群 ID（oc_ 开头）。只认完全一样且唯一的群名，否则抛 LarkError 让人填 ID。"""
    data = run(["im", "+chat-search", "--query", name, "--format", "json"])
    chats = ((data.get("data") or {}).get("chats") if isinstance(data, dict) else None) or []
    hits = [c for c in chats if c.get("name") == name and c.get("chat_id")]
    if len(hits) != 1:
        raise LarkError(f"按群名「{name}」找到 {len(hits)} 个群，请直接填群 ID（oc_ 开头）")
    return str(hits[0]["chat_id"])


def _items(data) -> list[dict]:
    """从各种包法里取消息列表：直接是列表、data.items / data.messages / items / messages。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for k in ("items", "messages", "data"):
        v = data.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _items(v)
            if inner:
                return inner
    return []


def _ts(v) -> str | None:
    """毫秒 / 秒时间戳或 ISO → 上海时区 ISO。"""
    if v in (None, ""):
        return None
    s = str(v)
    if s.isdigit():
        n = int(s)
        return iso(datetime.fromtimestamp(n / 1000 if n > 10**11 else n, TZ))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))   # +chat-messages-list 给的是本地时间到分钟："2026-11-12 19:34"
    except ValueError:
        return None
    return iso(dt if dt.tzinfo else dt.replace(tzinfo=TZ))


def normalize(m: dict) -> dict | None:
    """一条飞书消息 → 统一字段（和钉钉来源、自动回复吃的字段一致）：
    message_id、chat_id / conversation_id、content（正文文字）、sender_id（open_id）、sender（显示名）、
    create_time（ISO）、chat_type（p2p / group）、is_at_me。没有消息 ID 或正文的返回 None。"""
    if (m.get("msg_type") or m.get("message_type")) == "system" or m.get("deleted"):
        return None                            # 系统消息（“某某 started the group chat.”）、已撤回的不算
    mid = m.get("message_id") or m.get("messageId") or m.get("id")
    sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
    sid = sender.get("id") or sender.get("open_id") or m.get("sender_id") or m.get("sender_open_id")
    if not sid and isinstance(sender.get("sender_id"), dict):
        sid = sender["sender_id"].get("open_id")
    if isinstance(sid, dict):
        sid = sid.get("open_id")
    text = m.get("content")
    if not isinstance(text, str) and isinstance(m.get("body"), dict):
        text = m["body"].get("content")
    if not isinstance(text, str):
        text = m.get("text")
    if isinstance(text, str) and text.startswith("{"):
        try:                                   # 原始 OAPI 的 content 是 JSON 字符串：取里面的 text
            text = json.loads(text).get("text") or text
        except (json.JSONDecodeError, AttributeError):
            pass
    if not mid or not isinstance(text, str) or not text.strip():
        return None
    chat_obj = m.get("chat") if isinstance(m.get("chat"), dict) else {}
    chat = m.get("chat_id") or m.get("chatId") or chat_obj.get("chat_id")
    return {"message_id": str(mid), "chat_id": chat, "conversation_id": chat, "content": text.strip(),
            "sender_id": str(sid) if sid else None,
            "sender": sender.get("name") or m.get("sender_name") or (str(sid) if sid else "对方"),
            "create_time": _ts(m.get("create_time") or m.get("createTime") or m.get("send_time")),
            "chat_type": m.get("chat_type") or chat_obj.get("chat_type"),
            "is_at_me": bool(m.get("is_at_me"))}


def list_messages(chat_id: str, since: str, *, page_size: int = 50) -> list[dict]:
    """群里 since（ISO）之后的消息，按时间正序。"""
    data = run(["im", "+chat-messages-list", "--chat-id", chat_id, "--start", since, "--sort", "asc", "--page-size", str(page_size)])
    return [n for n in (normalize(m) for m in _items(data)) if n]


def search(since: str, *, p2p: bool = False, at_me: bool = False) -> list[dict]:
    """since 之后别人发给我的单聊（p2p）或 @我 的消息（排除机器人发的）。"""
    args = ["im", "+messages-search", "--start", since, "--exclude-sender-type", "bot", "--page-size", "50"]
    if p2p:
        args += ["--chat-type", "p2p"]
    if at_me:
        args += ["--is-at-me"]
    out = []
    for m in _items(run(args)):
        n = normalize(m)
        if n:
            n["chat_type"] = n["chat_type"] or ("p2p" if p2p else "group")
            n["is_at_me"] = n["is_at_me"] or at_me
            out.append(n)
    return out


def send_markdown(chat_id: str, text: str, *, idempotency_key: str | None = None) -> str | None:
    args = ["im", "+messages-send", "--chat-id", chat_id, "--markdown", text]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key[:64]]
    return message_id_of(run(args))


def send_file(chat_id: str, path: str) -> str | None:
    return message_id_of(run(["im", "+messages-send", "--chat-id", chat_id, "--file", path], timeout=300))


def reply(message_id: str, text: str) -> str | None:
    return message_id_of(run(["im", "+messages-reply", "--message-id", message_id, "--text", text]))


def message_id_of(data) -> str | None:
    if isinstance(data, dict):
        for k in ("message_id", "messageId"):
            if data.get(k):
                return str(data[k])
        for v in data.values():
            if isinstance(v, dict):
                found = message_id_of(v)
                if found:
                    return found
    return None


def auth_status() -> dict:
    """本人登录状态：{ok, user, open_id, detail}。"""
    try:
        p = subprocess.run([BIN, "auth", "status"], capture_output=True, text=True, timeout=30)
        d = json.loads(p.stdout or "{}")
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"ok": False, "detail": f"lark-cli 不可用：{e}"}
    return parse_auth_status(d)


def parse_auth_status(d: dict) -> dict:
    """两种输出都认：1.0.100 起本人身份在 identities.user 里（status / available / openId / userName / tokenStatus），
    更早的版本是顶层的 identity / tokenStatus / userOpenId / userName。"""
    user = (d.get("identities") or {}).get("user") if isinstance(d.get("identities"), dict) else None
    if isinstance(user, dict):
        ok = bool(user.get("available")) and user.get("status") == "ready" and user.get("tokenStatus") != "expired"
        scopes = user.get("scope")
        return {"ok": ok, "user": user.get("userName"), "open_id": user.get("openId"),
                "scopes": str(scopes).split() if isinstance(scopes, str) else None,
                "detail": "已登录" if ok else (user.get("message") or "本人身份没登录或已过期")}
    ok = d.get("tokenStatus") not in ("expired", None) and d.get("identity") == "user"
    return {"ok": ok, "user": d.get("userName"), "open_id": d.get("userOpenId"), "scopes": None,
            "detail": "已登录" if ok else (d.get("note") or "本人身份没登录或已过期")}
