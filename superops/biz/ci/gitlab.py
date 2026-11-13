"""内网 GitLab API v4 最小客户端（实测服务端 11.11.1，只用 11.x 已有的接口和参数）。
Token 放钥匙串 popto-ops/gitlab。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..scriptkit import keychain


class GitLab:
    def __init__(self, base_url: str, token: str | None = None):
        self.base = base_url.rstrip("/") + "/api/v4"
        self.token = token or keychain("gitlab")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _req(self, method: str, path: str, body: dict | None = None):
        data = urllib.parse.urlencode(body, doseq=True).encode() if body else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("PRIVATE-TOKEN", self.token or "")
        with self.opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode() or "null")

    @staticmethod
    def normalize(project) -> str:
        """接受 "front/x/popto"、"/front/x/popto"、"…/popto.git"、完整 URL 或数字 ID。"""
        p = str(project).strip()
        if "://" in p:
            p = urllib.parse.urlparse(p).path
        p = p.strip("/")
        if p.endswith(".git"):
            p = p[:-4]
        return p

    @classmethod
    def pid(cls, project) -> str:
        return urllib.parse.quote(cls.normalize(project), safe="")

    def open_mrs(self, project: str) -> list[dict]:
        return self._req("GET", f"/projects/{self.pid(project)}/merge_requests?state=opened&per_page=50")

    def merged_mrs(self, project: str, per_page: int = 20) -> list[dict]:
        return self._req("GET", f"/projects/{self.pid(project)}/merge_requests?state=merged&order_by=updated_at&per_page={per_page}")

    def mr(self, project: str, iid: int) -> dict:
        return self._req("GET", f"/projects/{self.pid(project)}/merge_requests/{iid}")

    def changes(self, project: str, iid: int) -> list[dict]:
        return (self._req("GET", f"/projects/{self.pid(project)}/merge_requests/{iid}/changes") or {}).get("changes", [])

    def note(self, project: str, iid: int, body: str) -> dict:
        return self._req("POST", f"/projects/{self.pid(project)}/merge_requests/{iid}/notes", {"body": body})

    def add_labels(self, project: str, iid: int, labels: list[str]) -> dict:
        """公司 GitLab 是 11.11，不支持 add_labels（12.x 才有）：读出现有标签，合并后整体写回 labels。"""
        cur = list((self.mr(project, iid) or {}).get("labels") or [])
        merged = cur + [l for l in labels if l not in cur]
        return self._req("PUT", f"/projects/{self.pid(project)}/merge_requests/{iid}", {"labels": ",".join(merged)})

    def create_mr(self, project: str, *, source_branch: str, target_branch: str, title: str, description: str) -> dict:
        return self._req("POST", f"/projects/{self.pid(project)}/merge_requests",
                         {"source_branch": source_branch, "target_branch": target_branch, "title": title,
                          "description": description, "remove_source_branch": "true"})

    def merge_base(self, project: str, a: str, b: str) -> str | None:
        """两个提交的共同祖先（11.4 起有）：a 是 b 的祖先时返回 a。"""
        q = urllib.parse.urlencode([("refs[]", a), ("refs[]", b)])
        return ((self._req("GET", f"/projects/{self.pid(project)}/repository/merge_base?{q}") or {}).get("id"))

    def merge(self, project: str, iid: int, sha: str | None = None) -> dict:
        body = {"should_remove_source_branch": "true"}
        if sha:
            body["sha"] = sha
        return self._req("PUT", f"/projects/{self.pid(project)}/merge_requests/{iid}/merge", body)
