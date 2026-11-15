"""唯一出口：所有要找人的消息只经宿主的发件箱发出（02 §7）。"""
from __future__ import annotations

import json
import time
import re
import subprocess
import sys
from pathlib import Path

from .. import paths
from ..timeutil import iso, now


class Notifier:
    name = "base"
    supports_target = False  # True = send / send_file 接受 target（发到默认群以外的群）

    def send(self, kind: str, title: str, body: str, *, priority: int = 2, ref: str | None = None) -> dict:
        raise NotImplementedError

    def send_file(self, path: Path, *, title: str | None = None, priority: int = 2, ref: str | None = None) -> dict:
        """发一个本机文件（视频、封面、周报等）。默认实现只记一条文字，便于测试和影子运行。"""
        return self.send("file", title or path.name, f"📎 文件：`{path}`（{path.stat().st_size // 1024} KB）",
                         priority=priority, ref=ref)


class ConsoleNotifier(Notifier):
    """开发 / 影子运行：打印并写 notify/console.log，不对外发送。"""

    name = "console"

    def send(self, kind, title, body, *, priority=2, ref=None):
        line = f"[{iso()}] ({kind} P{priority}) {title}\n{body}\n"
        log = paths.ops_data() / "notify" / "console.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, file=sys.stderr)
        return {"ok": True, "message_id": None}


class FileNotifier(Notifier):
    """每条通知一个 Markdown 文件，便于测试和影子运行复盘。"""

    name = "file"
    supports_target = True

    def __init__(self, directory: Path | None = None):
        self.dir = directory or (paths.ops_data() / "notify" / "outbox")
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, kind, title, body, *, priority=2, ref=None, target=None):
        stamp = now().strftime("%Y%m%d-%H%M%S")
        n = len(list(self.dir.glob(f"{stamp}-*.md")))
        p = self.dir / f"{stamp}-{n:02d}-{kind}.md"
        head = f"<!-- priority={priority} ref={ref}" + (f" target={target}" if target else "") + " -->"
        p.write_text(f"{head}\n# {title}\n\n{body}\n", encoding="utf-8")
        return {"ok": True, "message_id": p.name}


_TRANSIENT = re.compile(r"i/o timeout|request_timeout|deadline exceeded|connection reset|no such host|"
                        r"temporary failure|TLS handshake timeout|EOF", re.I)


def _find_message_id(data, depth=0):
    """dws 回执结构不固定：在嵌套结构里找第一个消息 ID 字段。"""
    if depth > 5:
        return None
    if isinstance(data, dict):
        for k in ("messageId", "openMsgId", "msgId", "processQueryKey", "message_id"):
            if isinstance(data.get(k), str) and data[k]:
                return data[k]
        vals = data.values()
    elif isinstance(data, list):
        vals = data
    else:
        return None
    for v in vals:
        mid = _find_message_id(v, depth + 1)
        if mid:
            return mid
    return None


class DingTalkNotifier(Notifier):
    """通过 dws chat +messages-send 发 Markdown。dws 当前的卡片不支持按钮回调，
    批红改为在群里回复「C12 1」，由 notifier.listener 解析。"""

    name = "dingtalk"
    supports_target = True

    def __init__(self, cfg: dict, dws_bin: str = "dws"):
        self.cfg = cfg or {}
        self.dws = dws_bin

    def build_cmd(self, body: str | None = None, *, file: str | None = None, target: str | None = None) -> list[str]:
        """文字（--markdown）或文件（--file，工作目录内的相对路径）二选一。target：发到这个群而不是默认群。"""
        c = self.cfg
        identity = c.get("identity", "user")
        content = ["--file", file] if file else ["--markdown", body or ""]
        cmd = [self.dws, "chat", "+messages-send", "--as", identity, *content, "--yes", "-f", "json"]
        if identity == "bot":
            if not c.get("robot_code"):
                raise RuntimeError("notifier.dingtalk.identity=bot 时必须配置 robot_code")
            cmd += ["--robot-code", c["robot_code"]]
        if target or c.get("chat_id"):
            cmd += ["--chat-id", target or c["chat_id"]]
        elif c.get("user"):
            cmd += ["--user", c["user"]]
        elif c.get("open_dingtalk_id"):
            cmd += ["--open-dingtalk-id", c["open_dingtalk_id"]]
        else:
            raise RuntimeError(f"notifier.dingtalk 未配置 chat_id / user / open_dingtalk_id（写在 {paths.ops_data() / 'local.yaml'}）")
        return cmd

    MAX_FILE_MB = 500

    def send_file(self, path, *, title=None, priority=2, ref=None, target=None):
        """以 --file 发到群里（dws 要求工作目录内的相对路径，所以在文件所在目录执行）。"""
        path = Path(path)
        if not path.is_file():
            return {"ok": False, "error": f"文件不存在：{path}"}
        if path.stat().st_size > self.MAX_FILE_MB * 1024 * 1024:
            return {"ok": False, "error": f"文件超过 {self.MAX_FILE_MB} MB：{path}"}
        return self._run(self.build_cmd(file=path.name, target=target), cwd=path.parent)

    def _run(self, cmd: list[str], cwd: Path | None = None) -> dict:
        for attempt in range(3):  # DNS / 超时这类瞬时网络错误：间隔几秒重试
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(cwd) if cwd else None)
            text = proc.stderr + proc.stdout
            if proc.returncode == 0 or not _TRANSIENT.search(text) or attempt == 2:
                break
            time.sleep((3, 8)[attempt])
        if proc.returncode != 0:
            return {"ok": False, "error": text[-800:], "attempts": attempt + 1}
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            data = {"raw": proc.stdout[-400:]}
        if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
            return {"ok": False, "error": json.dumps(data, ensure_ascii=False)[-800:]}
        return {"ok": True, "message_id": _find_message_id(data)}

    def send(self, kind, title, body, *, priority=2, ref=None, target=None):
        if kind == "alert" and self.cfg.get("at_on_alert"):
            body = body + "\n\n" + " ".join(f"@{x}" for x in self.cfg["at_on_alert"])
        return self._run(self.build_cmd(body, target=target))


class LarkNotifier(Notifier):
    """飞书适配器（保留切换能力）：lark-cli im +messages-send。"""

    name = "lark"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def send(self, kind, title, body, *, priority=2, ref=None):
        chat = self.cfg.get("chat_id")
        if not chat:
            return {"ok": False, "error": "notifier.lark.chat_id 未配置"}
        cmd = ["lark-cli", "im", "+messages-send", "--chat-id", chat, "--markdown", body]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout)[-800:]}
        return {"ok": True, "message_id": None}


def build_notifier(cfg: dict) -> Notifier:
    n = cfg.get("notifier") or {}
    adapter = n.get("adapter", "console")
    if adapter == "dingtalk":
        return DingTalkNotifier(n.get("dingtalk") or {})
    if adapter == "lark":
        return LarkNotifier(n.get("lark") or {})
    if adapter == "file":
        return FileNotifier()
    return ConsoleNotifier()
