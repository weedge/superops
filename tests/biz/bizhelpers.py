"""业务模块测试共用：冻结时间、记录调用的假客户端、一个最小的假宿主 HTTP 服务。"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from superops.infra import timeutil


def at(s: str) -> datetime:
    """冻结到某个上海时间，例如 at('2026-11-10 10:00')。"""
    dt = datetime.fromisoformat(s).replace(tzinfo=timeutil.TZ)
    timeutil.set_fake_now(dt)
    return dt


class FakeHub:
    """进程内的假客户端（HubClient 的鸭子类型）：只把调用记下来。"""

    def __init__(self):
        self.events, self.posts, self.notices = [], [], []

    def emit(self, e):
        self.events.append(e)
        return {"ok": True}

    def post(self, path, body=None, retries=0):
        self.posts.append((path, body))
        return {"ok": True}

    def notify(self, *a, **k):
        self.notices.append((a, k))
        return {"ok": True}


class FakeHost:
    """假宿主：实现确定性脚本会调的那几个 /v1/* 接口，收到的东西都记在属性里。

    events / notices：POST 进来的事件、通知（按到达顺序）；同一个 dedup_key 的事件只记一次。
    counters：{名字: 当日累计}；paused：{key: 原因}；digest：GET /v1/digest 返回的内容（可在用例里改）。
    token 不为空时校验 X-Ops-Token。event_reply 可以换成函数，定制 POST /v1/events 的返回。
    """

    def __init__(self, token: str | None = None):
        self.token = token
        self.events: list[dict] = []
        self.notices: list[dict] = []
        self.counters: dict[str, float] = {}
        self.paused: dict[str, str] = {}
        self.digest: dict = {"tasks_by_rule": {}, "cards": [], "unread_feedback": 0}
        self.digest_calls: list[dict] = []
        self.event_reply = lambda evt: {"ok": True}
        self._closed = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()

    def _get(self, path: str, qs: dict):
        if path == "/v1/health":
            return 200, {"ok": True}
        if path == "/v1/counter":
            return 200, {"value": self.counters.get(qs["name"], 0)}
        if path == "/v1/digest":
            self.digest_calls.append(qs)
            return 200, {"since": qs["since"], "until": qs.get("until"), **self.digest}
        return 404, {"error": "not found"}

    def _post(self, path: str, b: dict):
        if path == "/v1/events":
            key = b.get("dedup_key")
            if key and any(e.get("dedup_key") == key for e in self.events):
                return 200, {"ok": True, "duplicate": True}
            self.events.append(b)
            return 200, self.event_reply(b)
        if path == "/v1/notify":
            self.notices.append(b)
            return 200, {"ok": True, "id": len(self.notices)}
        if path == "/v1/counter":
            self.counters[b["name"]] = self.counters.get(b["name"], 0) + float(b.get("value", 1))
            return 200, {"ok": True, "value": self.counters[b["name"]]}
        if path == "/v1/pause":
            self.paused[b["key"]] = b.get("reason", "")
            return 200, {"ok": True}
        if path == "/v1/resume":
            self.paused.pop(b["key"], None)
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    def _handler(self):
        host = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 安静
                pass

            def _send(self, code: int, obj: dict):
                data = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                if host.token and self.headers.get("X-Ops-Token") != host.token:
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if self._authed():
                    u = urlparse(self.path)
                    self._send(*host._get(u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}))

            def do_POST(self):  # noqa: N802
                if self._authed():
                    n = int(self.headers.get("Content-Length") or 0)
                    self._send(*host._post(urlparse(self.path).path, json.loads(self.rfile.read(n) or b"{}")))

        return H
