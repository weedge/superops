"""GitHub 最小客户端：和 gitlab.GitLab 同一套方法、同一种返回形状，CI 中台和开发 worktree 不用分两套写。

走本机已登录的 gh（`gh api`），令牌由 gh 自己保管（钥匙串），宿主和 Agent 都不碰。要先：
    gh auth login --hostname <host> --git-protocol https --web     # 本人或机器人账号，要有仓库写权限（scope 含 repo）
    gh auth setup-git                                             # git push 走 gh 的凭据
GitHub 的 PR 在这里也叫 MR：iid 是 PR 号，source_branch / target_branch 是 head / base，web_url 是 html_url。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse

FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


class GitHubError(RuntimeError):
    pass


def run_gh(args: list[str], stdin: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=timeout)


class GitHub:
    kind = "github"

    def __init__(self, host: str = "github.com", *, merge_method: str = "merge", runner=None):
        self.host = host or "github.com"
        self.merge_method = merge_method if merge_method in ("merge", "squash", "rebase") else "merge"
        self.runner = runner or run_gh
        self._configured: bool | None = None

    # ---- 基础

    @property
    def configured(self) -> bool:
        """gh 装了且在这个 host 上登录着（结果在这个对象里缓存）。"""
        if self._configured is None:
            if self.runner is run_gh and not shutil.which("gh"):
                self._configured = False
            else:
                try:
                    self._configured = self.runner(["auth", "status", "--hostname", self.host]).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    self._configured = False
        return self._configured

    @staticmethod
    def normalize(project) -> str:
        """接受 "owner/repo"、"github:owner/repo"、https / ssh 地址（带不带 .git）。"""
        p = str(project).strip()
        if p.lower().startswith("github:"):
            p = p[7:]
        if "://" in p:
            p = urllib.parse.urlparse(p).path
        elif p.startswith("git@") and ":" in p:
            p = p.split(":", 1)[1]
        p = p.strip("/")
        if p.endswith(".git"):
            p = p[:-4]
        return "/".join(p.split("/")[:2])

    def api(self, method: str, path: str, body: dict | None = None, *, paginate: bool = False):
        args = ["api", "--hostname", self.host, "-X", method, path.lstrip("/"), "-H", "Accept: application/vnd.github+json"]
        if paginate:
            args += ["--paginate", "--slurp"]
        if body is not None:
            args += ["--input", "-"]
        try:
            p = self.runner(args, json.dumps(body) if body is not None else None)
        except FileNotFoundError as error:
            raise GitHubError("本机没有 gh（brew install gh）") from error
        except subprocess.TimeoutExpired as error:
            raise GitHubError(f"gh api 超时：{method} {path}") from error
        if p.returncode != 0:
            raise GitHubError(f"gh api {method} {path} 失败：{(p.stderr or p.stdout or '').strip()[-300:]}")
        data = json.loads(p.stdout or "null")
        if paginate and isinstance(data, list) and all(isinstance(x, list) for x in data):
            data = [item for page in data for item in page]
        return data

    def _repo(self, project: str) -> str:
        return f"repos/{self.normalize(project)}"

    @staticmethod
    def _pr(d: dict) -> dict:
        """PR → 和 GitLab MR 一样的字段。"""
        d = d or {}
        merged_by = d.get("merged_by") or None
        return {"iid": d.get("number"), "title": d.get("title") or "", "description": d.get("body") or "",
                "source_branch": (d.get("head") or {}).get("ref"), "target_branch": (d.get("base") or {}).get("ref"),
                "sha": (d.get("head") or {}).get("sha"), "labels": [l.get("name") for l in d.get("labels") or [] if l.get("name")],
                "web_url": d.get("html_url"), "state": d.get("state"), "merged_at": d.get("merged_at"),
                "merge_commit_sha": d.get("merge_commit_sha") if d.get("merged_at") else None,
                "merged_by": {"username": merged_by.get("login")} if isinstance(merged_by, dict) else None}

    # ---- 和 GitLab 同名的方法

    def open_mrs(self, project: str) -> list[dict]:
        return [self._pr(d) for d in self.api("GET", f"{self._repo(project)}/pulls?state=open&per_page=50") or []]

    def merged_mrs(self, project: str, per_page: int = 20) -> list[dict]:
        rows = self.api("GET", f"{self._repo(project)}/pulls?state=closed&sort=updated&direction=desc&per_page={per_page}") or []
        return [self._pr(d) for d in rows if d.get("merged_at")]

    def mr(self, project: str, iid: int) -> dict:
        pr = self._pr(self.api("GET", f"{self._repo(project)}/pulls/{int(iid)}"))
        pr["head_pipeline"] = {"status": self.checks_status(project, pr["sha"])} if pr.get("sha") else None
        return pr

    def checks_status(self, project: str, sha: str) -> str | None:
        """这个提交的检查合起来是什么状态：success / failed / running；一个检查都没有返回 None（等于没有流水线）。"""
        runs = (self.api("GET", f"{self._repo(project)}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []
        status = self.api("GET", f"{self._repo(project)}/commits/{sha}/status") or {}
        statuses = status.get("statuses") or []
        if not runs and not statuses:
            return None
        if any(r.get("conclusion") in FAILED for r in runs) or any(s.get("state") in ("failure", "error") for s in statuses):
            return "failed"
        if any(r.get("status") != "completed" for r in runs) or any(s.get("state") == "pending" for s in statuses):
            return "running"
        return "success"

    def changes(self, project: str, iid: int) -> list[dict]:
        """改动的文件，形状同 GitLab 的 /changes：new_path、old_path、diff（GitHub 的 patch，二进制或太大时为空）。"""
        files = self.api("GET", f"{self._repo(project)}/pulls/{int(iid)}/files?per_page=100", paginate=True) or []
        return [{"new_path": f.get("filename"), "old_path": f.get("previous_filename") or f.get("filename"),
                 "diff": f.get("patch") or "", "deleted_file": f.get("status") == "removed",
                 "new_file": f.get("status") == "added", "renamed_file": f.get("status") == "renamed"} for f in files]

    def note(self, project: str, iid: int, body: str) -> dict:
        return self.api("POST", f"{self._repo(project)}/issues/{int(iid)}/comments", {"body": body})

    def add_labels(self, project: str, iid: int, labels: list[str]) -> list:
        """加标签（不存在的标签 GitHub 会自动建）。"""
        return self.api("POST", f"{self._repo(project)}/issues/{int(iid)}/labels", {"labels": list(labels)})

    def create_mr(self, project: str, *, source_branch: str, target_branch: str, title: str, description: str) -> dict:
        return self._pr(self.api("POST", f"{self._repo(project)}/pulls",
                                 {"title": title, "head": source_branch, "base": target_branch, "body": description}))

    def merge_base(self, project: str, a: str, b: str) -> str | None:
        data = self.api("GET", f"{self._repo(project)}/compare/{a}...{b}") or {}
        return (data.get("merge_base_commit") or {}).get("sha")

    def merge(self, project: str, iid: int, sha: str | None = None) -> dict:
        """合并（方式取 ci.github_merge_method）；合并后删掉源分支（同 GitLab 的 should_remove_source_branch），删不掉不算失败。"""
        pr = self._pr(self.api("GET", f"{self._repo(project)}/pulls/{int(iid)}"))
        body = {"merge_method": self.merge_method, **({"sha": sha} if sha else {})}
        res = self.api("PUT", f"{self._repo(project)}/pulls/{int(iid)}/merge", body) or {}
        if not res.get("merged"):
            raise GitHubError(f"PR #{iid} 没合并：{res.get('message') or res}")
        if pr.get("source_branch"):
            try:
                self.api("DELETE", f"{self._repo(project)}/git/refs/heads/{urllib.parse.quote(pr['source_branch'], safe='/')}")
            except GitHubError:
                pass
        return {"merge_commit_sha": res.get("sha"), "web_url": pr.get("web_url"), "title": pr.get("title")}

    # ---- GitHub 才有的

    def repo(self, project: str) -> dict:
        """仓库信息：default_branch、permissions（push / admin）、html_url。"""
        return self.api("GET", self._repo(project)) or {}

    def default_branch(self, project: str) -> str | None:
        return self.repo(project).get("default_branch")
