"""代码托管在哪：每个项目是 GitLab 还是 GitHub，用哪个客户端、往哪个分支合。

local.yaml 的 ci.projects 里：
    - popto/frontend_popto                     GitLab（ci.gitlab_url）
    - github:weedge/superops                   GitHub（ci.github_host，默认 github.com）
    - https://github.com/weedge/superops       同上；地址里是 github_host 的都算 GitHub
两种可以同时接。项目在系统里一律用规范名（owner/repo、group/sub/proj）：事件的 data.project、主题 mr:<项目>!<号>、
ci.repos 的键都用它；事件 data 里另带 forge（gitlab / github）。事件类型沿用 gitlab.*（历史命名），GitHub 的 PR 走同一套。
"""
from __future__ import annotations

from .github import GitHub
from .gitlab import GitLab

KINDS = ("gitlab", "github")
TITLES = {"gitlab": "GitLab", "github": "GitHub"}
WORDS = {"gitlab": "MR", "github": "PR"}


def github_host(c: dict) -> str:
    return str(c.get("github_host") or "github.com")


def parse(c: dict, entry) -> tuple[str, str]:
    """ci.projects 的一项 → (gitlab / github, 规范名)。"""
    s = str(entry).strip()
    host = github_host(c)
    if s.lower().startswith("github:") or host in s or s.startswith("git@github.com"):
        return "github", GitHub.normalize(s)
    if s.lower().startswith("gitlab:"):
        s = s[7:]
    return "gitlab", GitLab.normalize(s)


def projects(c: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in c.get("projects") or []:
        item = parse(c, entry)
        if item not in out:
            out.append(item)
    return out


def kind_of(c: dict, project: str, data: dict | None = None) -> str:
    """项目在哪：事件里写了 forge 就用它；否则按 ci.projects / ci.default_project 里的写法；都没写按 GitLab。"""
    if data and data.get("forge") in KINDS:
        return data["forge"]
    kind, name = parse(c, project)
    if kind == "github":
        return kind
    for entry in [*(c.get("projects") or []), *([c["default_project"]] if c.get("default_project") else [])]:
        k, n = parse(c, entry)
        if n == name:
            return k
    return "gitlab"


def normalize(c: dict, project) -> str:
    return parse(c, project)[1]


def client(c: dict, kind: str):
    if kind == "github":
        return GitHub(github_host(c), merge_method=str(c.get("github_merge_method") or "merge"))
    return GitLab(c.get("gitlab_url", "https://gitlab.example.com"))


def client_for(c: dict, project: str, data: dict | None = None):
    return client(c, kind_of(c, project, data))


def base_branch(c: dict, project: str, kind: str, cl=None) -> str:
    """往哪个分支合：ci.base_branch 写成 {项目: 分支} 时按项目；GitHub 项目没单独写就用仓库的默认分支；
    其余用 ci.base_branch（字符串）或 master。"""
    spec = c.get("base_branch")
    if isinstance(spec, dict):
        for k, v in spec.items():
            if normalize(c, k) == project and v:
                return str(v)
    if kind == "github":
        try:
            found = (cl or client(c, kind)).default_branch(project)
        except Exception:  # noqa: BLE001  查不到就退回配置
            found = None
        if found:
            return found
    if isinstance(spec, str) and spec:
        return spec
    return "master"
