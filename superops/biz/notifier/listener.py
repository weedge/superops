"""钉钉消息 / 卡片回调的解析与监听循环：dws event 长连接 + 群消息轮询兜底。本机不需要公网入口。

分两层：
- 纯函数：`classify_event(raw, approvers, ...)` 把一条 dws 原始事件解析成结构化结果并做权限判断
  （不发任何请求）；`parse_command`、`listen_chats`、`_walk` 是它用到的零件。钉钉来源（superops.sources）复用这一层；
- dws 循环：`consume`（事件流）/ `poll_group`（轮询群消息）。默认把每条原始事件交给 `handle_event`
  （旧行为：解析后经 HTTP 发给宿主的 /v1/events、/v1/pause、/v1/resume）；传了 `on_event` 就只把原始事件交给它。

支持的回复（只接受 notifier.dingtalk.approvers 里的人；card_approvers 里的人只能批卡片）：
  C12 1 [原因] / T12 1 [原因]   批卡片，选第 1 个选项（也可写选项 key）；T 开头是工单短号
  记下来：……              追加到 decisions.md
  需求：……                转成 demand.manual 进需求池
  已发 pk_xxx [链接]      半自动发布完成后补登记（视频号没有链接，只回「已发 pk_xxx」）
  暂停 <名字/平台/agent>  /  恢复 <…>
互动卡片按钮回调（user_card_action_triggered）里的 context 带 card_id / choice 时同样生效。
其余文字算普通消息（kind=message），由调用方决定理不理。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime

from ...infra.config import load_agents_config
from ...infra import paths
from ..client import HubClient
from ...infra.timeutil import TZ, iso

log = logging.getLogger("dingtalk-events")

# 卡片编号：C12（旧卡片序号）或 T12（工单短号），大小写都认
RE_CARD = re.compile(r"^\s*(?:批\s*)?([CcTt])(\d+)\s+(\S+)(?:\s+(.+))?\s*$", re.S)
RE_NOTE = re.compile(r"^\s*记下来\s*[:：]\s*(.+)$", re.S)
RE_DEMAND = re.compile(r"^\s*需求\s*[:：]\s*(.+)$", re.S)
RE_PUBLISHED = re.compile(r"^\s*已发\s+(pk_\S+)(?:\s+(\S+))?\s*$")
RE_PAUSE = re.compile(r"^\s*(暂停|恢复)\s+(\S+)\s*$")

TEXT_KEYS = ("text", "content", "msgContent", "message")
# 只认 ID，不认昵称（昵称可随意改，能冒充审批人）。dws --flatten 的群消息只带 sender_open_dingtalk_id。
SENDER_KEYS = ("senderId", "sender_id", "senderStaffId", "senderUserId", "userId", "user_id", "operatorUserId",
               "openDingTalkId", "senderOpenDingTalkId", "sender_open_dingtalk_id", "operator_open_dingtalk_id")


def _walk(obj, keys) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)):
                out.append(str(v))
            elif k in keys and isinstance(v, dict) and isinstance(v.get("content"), str):
                out.append(v["content"])
            else:
                out += _walk(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            out += _walk(v, keys)
    elif isinstance(obj, str) and obj.startswith("{"):
        try:
            out += _walk(json.loads(obj), keys)
        except json.JSONDecodeError:
            pass
    return out


def _walk_senders(obj) -> list[str]:
    """找发送人 ID，但绝不到消息正文里去找：正文是发消息的人自己写的，
    里面放一段 {"userId": "<审批人 ID>"} 不能让他变成审批人。正文字段（TEXT_KEYS）是字符串时整个跳过。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TEXT_KEYS and isinstance(v, str):
                continue
            if k in SENDER_KEYS and isinstance(v, (str, int)):
                out.append(str(v))
            else:
                out += _walk_senders(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _walk_senders(v)
    elif isinstance(obj, str) and obj.startswith("{"):
        try:
            out += _walk_senders(json.loads(obj))
        except json.JSONDecodeError:
            pass
    return out


def parse_command(text: str) -> dict | None:
    """把一条消息解析成事件（type / subject / summary / data，旧的宿主事件形状）；不认识的返回 None。
    暂停 / 恢复不是事件，返回 {"type": "_pause" | "_resume", "key": …}。"""
    text = (text or "").strip()
    text = re.sub(r"^@\S+\s*", "", text)  # 去掉开头的 @机器人
    if m := RE_CARD.match(text):
        card = f"{m.group(1).upper()}{m.group(2)}"
        return {"type": "decision.made", "subject": card, "summary": text[:200],
                "data": {"card": card, "choice": m.group(3), "reason": (m.group(4) or "").strip() or None}}
    if m := RE_NOTE.match(text):
        return {"type": "decision.note", "summary": m.group(1)[:200], "data": {"text": m.group(1).strip()}}
    if m := RE_DEMAND.match(text):
        return {"type": "demand.manual", "subject": "demand:manual", "summary": m.group(1)[:300],
                "data": {"text": m.group(1).strip()}}
    if m := RE_PUBLISHED.match(text):
        return {"type": "content.published.manual", "subject": f"pack:{m.group(1)}", "summary": text[:200],
                "data": {"pack_id": m.group(1), "url": m.group(2)} if m.group(2) else {"pack_id": m.group(1)}}
    if m := RE_PAUSE.match(text):
        return {"type": "_pause" if m.group(1) == "暂停" else "_resume", "key": m.group(2)}
    return None


DEFAULT_BOT_NAMES = ("兔管家",)
CHAT_KEYS = ("conversation_id", "conversationId", "openConversationId", "open_conversation_id", "chat_id", "chatId")
MESSAGE_ID_KEYS = ("message_id", "messageId", "msgId")
TIME_KEYS = ("create_time", "createTime", "createAt", "create_at")
AT_ME_KEYS = ("isInAtList", "is_in_at_list", "isAtMe", "is_at_me", "at_me")
# parse_command 的事件类型 → classify_event 的 kind
_KINDS = {"decision.made": "decide", "decision.note": "note", "demand.manual": "demand",
          "content.published.manual": "published", "_pause": "pause", "_resume": "resume"}


def _first(raw: dict, keys: tuple[str, ...]) -> str | None:
    """先看顶层字段，再往嵌套结构里找。"""
    for k in keys:
        if isinstance(raw.get(k), (str, int)) and not isinstance(raw.get(k), bool) and str(raw[k]):
            return str(raw[k])
    return next((v for v in _walk(raw, keys) if v), None)


def _event_time(raw: dict) -> str | None:
    """发送时间 → ISO（上海时区）。钉钉给的可能是 "2026-11-10 11:39:30" 或毫秒时间戳；认不出来原样返回。"""
    v = _first(raw, TIME_KEYS)
    if not v:
        return None
    try:
        if v.isdigit():
            n = int(v)
            return iso(datetime.fromtimestamp(n / 1000 if n > 10**11 else n, TZ))
        dt = datetime.fromisoformat(v.strip().replace(" ", "T", 1))
        return iso(dt if dt.tzinfo else dt.replace(tzinfo=TZ))
    except (ValueError, OverflowError, OSError):
        return v


def _truthy_flag(obj, keys: tuple[str, ...], depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(obj, dict):
        return any((k in keys and v in (True, 1, "true", "True", "1")) or _truthy_flag(v, keys, depth + 1) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_truthy_flag(v, keys, depth + 1) for v in obj)
    return False


def is_mentioned(text: str, raw: dict | None = None, bot_names: Iterable[str] | None = None) -> bool:
    """这条消息是不是冲着机器人说的：@ 了机器人（文字里有 @名字，或钉钉事件带“@ 了我”的标记），或以机器人名开头。"""
    t = (text or "").strip()
    for name in bot_names if bot_names is not None else DEFAULT_BOT_NAMES:
        if name and (f"@{name}" in t or t.startswith(name)):
            return True
    return _truthy_flag(raw or {}, AT_ME_KEYS)


def classify_event(raw: dict, approvers: list[str], allow_any: bool = False, card_approvers: list[str] | None = None,
                   *, bot_names: Iterable[str] | None = None) -> dict | None:
    """把一条 dws 原始事件（群消息 / 轮询到的消息 / 卡片回调）解析成结构化结果，并做权限判断。纯函数，不发请求。

    approvers：全权审批人的 ID。card_approvers：只能批卡片的人（能批哪类卡片由宿主再按卡片类型判断）。
    allow_any=True 时名单外的人也算有权限（调试用）。只认 ID，不认昵称。bot_names：机器人的名字（默认“兔管家”），判断 mentioned 用。

    返回 None：没有可处理的内容（没有文字的消息、没带卡片编号的回调、没权限的人点的卡片按钮）。
    否则返回 dict，公共字段：
      kind        见下
      by          发送人 ID（名单里的优先；一个 ID 都没找到是 None）
      role        "approver"（全权）| "card_approver"（只能批卡片）| "other"
      text        消息原文（卡片回调是 ""）
      mentioned   是否 @ 了机器人或以机器人名开头（卡片回调是 False）
      via         "message" | "card_action"
      chat        群 ID（事件里没带是 None）    message_id / event_id    钉钉的消息 ID / 事件 ID（没有是 None）
      at          发送时间，ISO；事件里没带是 None
    按 kind 另有：
      "decide"     card（"C12" / "T12"，卡片回调里是回调带的卡片 ID 原样）、choice、reason
      "note"       content（“记下来：”后面的话）
      "demand"     content（“需求：”后面的话）
      "published"  pack_id、url（没给链接是 None）
      "pause" / "resume"   key
      "message"    不匹配任何指令的普通消息；或者是指令但发送人没有权限——这时 denied 是那条指令的 kind
                   （否则 None），调用方可以据此回一句“没有权限”，但不能照指令执行。
    也就是说：返回的 kind 只要不是 "message"，发送人就一定有这个权限（批卡片：全权或限定审批人；其余指令：全权审批人）。
    """
    etype = raw.get("event_type") or raw.get("type") or ""
    senders = set(_walk_senders(raw))
    full = senders & set(map(str, approvers or []))
    limited = set() if full else senders & set(map(str, card_approvers or []))
    who = (sorted(full)[:1] or sorted(limited)[:1] or sorted(senders)[:1] or [None])[0]
    base = {"by": who, "role": "approver" if full else "card_approver" if limited else "other",
            "chat": _first(raw, CHAT_KEYS), "message_id": _first(raw, MESSAGE_ID_KEYS),
            "event_id": _first(raw, ("event_id", "eventId")), "at": _event_time(raw)}
    may_decide = bool(allow_any or full or limited)
    may_command = bool(full or (allow_any and not limited))   # 限定审批人只能批卡片，allow_any 也不放宽
    if "card_action" in etype:
        data = {}
        for c in _walk(raw, ("context",)):
            try:
                data = json.loads(c) if isinstance(c, str) else c
                break
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(data, dict) or not data:
            ids, choices = _walk(raw, ("card_id", "cardId")), _walk(raw, ("choice", "value"))
            data = {"card_id": ids[0] if ids else None, "choice": choices[0] if choices else None}
        card = data.get("card_id") or data.get("cardId")
        if not card or not may_decide:
            return None
        return {"kind": "decide", "card": card, "choice": data.get("choice"), "reason": data.get("reason"),
                **base, "text": "", "mentioned": False, "via": "card_action"}
    texts = [t for t in _walk(raw, TEXT_KEYS) if t and t.strip()]
    if not texts:
        return None
    text, evt = texts[0], None
    for t in texts:
        evt = parse_command(t)
        if evt:
            text = t
            break
    base.update(text=text, mentioned=is_mentioned(text, raw, bot_names), via="message")
    if not evt:
        return {"kind": "message", "denied": None, **base}
    kind = _KINDS[evt["type"]]
    if not (may_decide if kind == "decide" else may_command):
        return {"kind": "message", "denied": kind, **base}
    d = evt.get("data") or {}
    if kind == "decide":
        return {"kind": kind, "card": d["card"], "choice": d["choice"], "reason": d.get("reason"), **base}
    if kind in ("note", "demand"):
        return {"kind": kind, "content": d["text"], **base}
    if kind == "published":
        return {"kind": kind, "pack_id": d["pack_id"], "url": d.get("url"), **base}
    return {"kind": kind, "key": evt["key"], **base}


def handle_event(raw: dict, hub: HubClient, approvers: list[str], allow_any: bool = False,
                 card_approvers: list[str] | None = None) -> dict | None:
    """旧行为：classify_event 解析后经 HTTP 发给宿主（事件走 /v1/events，暂停 / 恢复走 /v1/pause、/v1/resume）。
    普通消息、没权限的指令一律忽略（返回 None）。approvers / card_approvers 含义同 classify_event。"""
    c = classify_event(raw, approvers, allow_any, card_approvers)
    if c is None:
        return None
    if c["kind"] == "message":
        if c["role"] == "other" and not allow_any:
            log.info("忽略非审批人的消息（发送者 %s）。如需允许，把其 userId 加进 notifier.dingtalk.approvers", c["by"] or "未知")
        elif c["denied"]:
            log.info("忽略限定审批人 %s 的非批红指令：%s", c["by"], c["denied"])
        return None
    by = c["by"] or "dingtalk"
    if c["kind"] == "pause":
        return hub.post("/v1/pause", {"key": c["key"], "reason": f"钉钉 {by}"})
    if c["kind"] == "resume":
        return hub.post("/v1/resume", {"key": c["key"]})
    if c["via"] == "card_action":
        evt = {"type": "decision.made", "subject": c["card"], "summary": f"卡片回调 {c['card']}",
               "data": {"card": c["card"], "choice": c["choice"], "reason": c["reason"]}}
    else:
        evt = parse_command(c["text"])
    evt["source"] = "dingtalk"
    evt.setdefault("data", {})["by"] = by
    mid = raw.get("message_id") or raw.get("messageId") or raw.get("msgId")
    # 事件流和轮询两条路都按消息 ID 去重：同一条回复只处理一次
    evt["dedup_key"] = (f"dingtalk:msg:{mid}" if mid else
                        f"dingtalk:{raw.get('event_id') or raw.get('eventId') or hash(json.dumps(raw, sort_keys=True, default=str))}")
    res = hub.emit(evt)
    if evt["type"] == "decision.made" and isinstance(res, dict) and res.get("ok") is False:
        # ref 带上卡片编号：分群时提示发回卡片所在的群
        hub.notify("message", "批红没成功", f"{evt['summary']}\n\n{res.get('error')}", priority=1,
                   ref=f"reply:{evt['data'].get('card')}")
    return res


CONNECT_GRACE = 180  # dws 启动后这么久还没连上就重启它（网络抖动时 dws 可能一直卡在 connecting）
_STATUS: dict[str, dict] = {}
_STATUS_LOCK = threading.Lock()


def status_path():
    return paths.ops_data() / "listener.status.json"


def read_status() -> dict:
    """各条监听的连接状态（_write_status 写的 listener.status.json）：{key: {connected, updated_at, last_error, …}}。"""
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(key: str, **kw) -> None:
    with _STATUS_LOCK:
        _STATUS.setdefault(key, {}).update(kw, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            p = status_path()
            p.write_text(json.dumps(_STATUS, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def _watch_stderr(proc: subprocess.Popen, state: dict, key: str) -> None:
    """转发 dws 的 stderr，并从 transport 行里读连接状态。"""
    for line in proc.stderr:  # type: ignore[union-attr]
        sys.stderr.write(line)
        low = line.lower()
        m = re.search(r"bus_pid=(\d+)", line)
        if m:
            state["bus_pid"] = int(m.group(1))
        if '"state":"connected"' in low.replace(" ", "") or "state: connected" in low:
            state["connected"] = True
            _write_status(key, connected=True, since=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        elif any(k in low.replace(" ", "") for k in ('"state":"connecting"', '"state":"reconnecting"', '"state":"disconnected"',
                                                     '"state":"degraded"', "state:degraded")):
            state["connected"] = False
            state["changed"] = time.monotonic()
            _write_status(key, connected=False, last_error=line.strip()[:300])


def consume(cmd: list[str], hub: HubClient | None, approvers: list[str], allow_any: bool, stop: threading.Event,
            card_approvers: list[str] | None = None, key: str | None = None,
            on_event: Callable[[dict], None] | None = None) -> None:
    """跑 dws event consume，断了自动重连。每条原始事件默认交给 handle_event；传了 on_event 就只交给它
    （这时 hub / approvers / allow_any / card_approvers 用不到，hub 可以传 None）。"""
    backoff = 5
    key = key or (cmd[3] if len(cmd) > 3 else "dws")
    while not stop.is_set():
        log.info("启动：%s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        state = {"connected": False, "changed": time.monotonic()}
        _write_status(key, connected=False, started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        threading.Thread(target=_watch_stderr, args=(proc, state, key), daemon=True).start()

        def _watchdog():
            while proc.poll() is None and not stop.is_set():
                if not state["connected"] and time.monotonic() - state["changed"] > CONNECT_GRACE:
                    # 多个订阅共用一个 dws event _bus 进程；bus 卡在 degraded 时只重启订阅没用，要连 bus 一起重启
                    bus = state.get("bus_pid")
                    log.warning("dws event %s 超过 %ds 没连上，重启订阅%s", key, CONNECT_GRACE,
                                f"和 bus（pid {bus}）" if bus else "")
                    _write_status(key, connected=False, last_error=f"{CONNECT_GRACE}s 未连上，已重启")
                    if bus:
                        try:
                            os.kill(bus, signal.SIGTERM)
                        except OSError:
                            pass
                    proc.terminate()
                    return
                stop.wait(5)
        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    raw = json.loads(line)
                    if on_event is not None:
                        on_event(raw)
                    else:
                        handle_event(raw, hub, approvers, allow_any, card_approvers)
                    backoff = 5
                except Exception:  # noqa: BLE001
                    log.exception("处理事件失败：%s", line[:300])
                if stop.is_set():
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()  # SIGTERM，让 dws 正常退订；不要 kill -9
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not stop.is_set():
            log.warning("dws event 退出（%s），%ds 后重连", proc.returncode, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)


POLL_INTERVAL = 15       # 秒；钉钉个人事件流实测会漏消息，用轮询群消息兜底
POLL_LOOKBACK = 600      # 启动时回看 10 分钟


def poll_state_path(tag: str = ""):
    return paths.ops_data() / (f"listener.poll.{tag}.json" if tag else "listener.poll.json")


def poll_group(chat_id: str, hub: HubClient | None, approvers: list[str], allow_any: bool, stop: threading.Event,
               dws: str = "dws", interval: float = POLL_INTERVAL, card_approvers: list[str] | None = None,
               tag: str = "", on_event: Callable[[dict], None] | None = None) -> None:
    """轮询群消息兜底，按消息 ID 去重。tag：默认群为空；分群时每个群一个标记，轮询进度和状态各记各的。
    每条消息包成 {"type": "group_message_poll", "conversation_id": 群 ID, **消息} 交给 handle_event；
    传了 on_event 就只交给它（同 consume）。"""
    from datetime import timedelta

    state_path = poll_state_path(tag)
    status_key = f"group_poll:{tag}" if tag else "group_poll"
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    seen: list[str] = list(st.get("seen") or [])
    since = datetime.now() - timedelta(seconds=POLL_LOOKBACK)
    if st.get("since"):
        since = max(since, datetime.fromisoformat(st["since"]))
    while not stop.is_set():
        started = datetime.now()
        query_from = (since - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")  # 留 1 分钟重叠，靠 seen 去重
        cmd = [dws, "chat", "+messages-list", "--group", chat_id, "--time", query_from, "--limit", "50", "--format", "json"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            data = json.loads(p.stdout or "{}")
            res = data.get("result", data) if isinstance(data, dict) else {}
            if p.returncode != 0 or not isinstance(res, dict) or "messages" not in res:
                raise RuntimeError((p.stderr or p.stdout or "无输出")[-300:])
            for m in sorted(res.get("messages") or [], key=lambda x: str(x.get("createTime"))):
                mid = m.get("messageId")
                if not mid or mid in seen:
                    continue
                seen.append(mid)
                raw = {"type": "group_message_poll", "conversation_id": chat_id, **m}
                try:
                    if on_event is not None:
                        on_event(raw)
                    else:
                        handle_event(raw, hub, approvers, allow_any, card_approvers)
                except Exception:  # noqa: BLE001
                    log.exception("处理轮询到的消息失败：%s", str(m)[:300])
            since = started
            seen = seen[-300:]
            try:
                state_path.write_text(json.dumps({"since": since.isoformat(timespec="seconds"), "seen": seen}),
                                      encoding="utf-8")
            except OSError:
                pass
            _write_status(status_key, connected=True, since=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        except Exception as e:  # noqa: BLE001
            log.warning("轮询群消息失败：%s", str(e)[:300])
            _write_status(status_key, connected=False, last_error=str(e)[:300])
        stop.wait(interval)


def listen_chats(dt: dict) -> list[tuple[str, str]]:
    """要监听回复的群：默认群 + card_chats 里分出去的群。返回 (群 ID, 标记)；默认群标记为空。"""
    import hashlib

    out: list[tuple[str, str]] = []
    if dt.get("chat_id"):
        out.append((str(dt["chat_id"]), ""))
    for chat in (dt.get("card_chats") or {}).values():
        chat = str(chat or "")
        if chat and chat not in [c for c, _ in out]:
            out.append((chat, hashlib.sha1(chat.encode()).hexdigest()[:6]))
    return out


def main() -> int:
    """独立进程方式运行（旧行为）：监听到的指令经 HTTP 发给宿主（agents.yaml 的 hub.host / port / token）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_agents_config()
    h = cfg.get("hub") or {}
    hub = HubClient(f"http://{h.get('host', '127.0.0.1')}:{h.get('port', 8787)}", h.get("token"))
    dt = (cfg.get("notifier") or {}).get("dingtalk") or {}
    approvers = [str(x) for x in dt.get("approvers") or []]
    card_approvers = sorted({str(x) for ids in (dt.get("card_approvers") or {}).values() for x in ids or []})
    allow_any = "--allow-any" in sys.argv
    if not approvers and not card_approvers and not allow_any:
        log.error("notifier.dingtalk.approvers 为空：为安全起见不接受任何人的批红。请在 local.yaml 配置后重启。")
    stop = threading.Event()
    threads = []
    chats = listen_chats(dt)
    for chat, tag in chats:
        cmd = ["dws", "event", "consume", "user_im_message_receive_group", "--group", chat, "--flatten", "-f", "ndjson"]
        threads.append(threading.Thread(target=consume, args=(cmd, hub, approvers, allow_any, stop, card_approvers),
                                        kwargs={"key": f"{cmd[3]}:{tag}" if tag else None}, daemon=True))
        threads.append(threading.Thread(target=poll_group, args=(chat, hub, approvers, allow_any, stop),
                                        kwargs={"card_approvers": card_approvers, "tag": tag}, daemon=True))
    if not chats:
        log.warning("未配置 notifier.dingtalk.chat_id，只监听互动卡片回调")
    cmd = ["dws", "event", "consume", "user_card_action_triggered", "--flatten", "-f", "ndjson"]
    threads.append(threading.Thread(target=consume, args=(cmd, hub, approvers, allow_any, stop, card_approvers), daemon=True))
    for t in threads:
        t.start()

    def _term(*_):
        raise KeyboardInterrupt

    import signal

    signal.signal(signal.SIGTERM, _term)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
