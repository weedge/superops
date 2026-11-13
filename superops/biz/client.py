"""宿主 HTTP 客户端：确定性脚本（采集器 / 执行器 / CI）通过它回调拉起自己的宿主进程。

不走系统代理（宿主在本机或内网）。宿主兼容实现这些接口：
  POST /v1/events   发事件（emit）          POST /v1/notify   发通知
  GET|POST /v1/counter  读 / 累加当日计数     GET /v1/digest    一段时间内的运行摘要
  GET /v1/health    探活                    POST /v1/pause、/v1/resume  暂停 / 恢复
类名沿用 HubClient（脚本契约不变）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HubClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 70.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Ops-Token", self.token)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error")
            except Exception:  # noqa: BLE001
                msg = str(e)
            raise RuntimeError(f"宿主返回 {e.code}: {msg}") from None

    def get(self, path: str, **params) -> dict:
        q = {k: v for k, v in params.items() if v is not None}
        return self._req("GET", path + (("?" + urllib.parse.urlencode(q)) if q else ""))

    def post(self, path: str, body: dict | None = None, retries: int = 0) -> dict:
        for i in range(retries + 1):
            try:
                return self._req("POST", path, body or {})
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if i >= retries:
                    raise
                time.sleep(2 * (i + 1))
        return {}

    # 便捷方法
    def health(self) -> bool:
        try:
            return bool(self.get("/v1/health").get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def emit(self, evt: dict) -> dict:
        return self.post("/v1/events", evt, retries=2)

    def notify(self, kind: str, title: str, body: str, priority: int = 2, ref: str | None = None, dedup: bool = False) -> dict:
        return self.post("/v1/notify", {"kind": kind, "title": title, "body": body, "priority": priority,
                                        "ref": ref, "dedup": dedup}, retries=2)
